"""Un échange de conversation, indépendant du canal (lot 4a) : message → événements.

Telegram (``bot/handlers.py``) et l'appli web (``web/``) appellent :meth:`Coeur.echanger`, qui enchaîne exactement
ce que faisait le bot jusqu'au lot 7a, plus ce qu'il faut à l'appli :

0. **filtre de données sensibles** (lot 5c, ``coeur.filtre_sensible``) : carte, IBAN, NIR, mot de passe, clé →
   jeton neutre (``[carte bancaire ••1234]``) dans le texte, le prompt, le journal et l'historique ; valeur gardée
   10 min en quarantaine dans le coffre (``quarantaine/<id>``), ligne ``[donnée sensible détectée : …]`` ajoutée au
   prompt, événement ``masquage`` (texte exactement transmis au modèle, types détectés) juste après ``debut`` ;
1. journal ``message_entrant`` (qui = prénom, texte masqué) ;
2. verrou de la personne (un seul ``claude -p`` à la fois, entre processus) → événement ``attente`` s'il faut
   attendre ;
3. conversation courante et session Claude partagées entre canaux (``sessions.json`` + ``coeur.historique``),
   message de la personne enregistré → ``debut`` ; **renouvellement** : nouvelle session Claude
   après ``ia.session_max_reponses`` réponses (20) ou ``ia.session_ttl_heures`` d'inactivité (2 h) — l'historique
   reste en base et affiché, mais n'est pas renvoyé au modèle ; au premier message d'une nouvelle session d'une
   conversation déjà commencée, un bloc ``[contexte récent]`` (6 lignes au plus, texte brut tronqué, sans appel
   au modèle) précède le message ;
   **routeur de modèle** (``coeur.routeur``) : règles puis classification Haiku → ``--model`` ;
4. appli, premier message du jour : cartes ``bonjour`` / ``brief`` / ``a_noter`` → ``carte`` ;
5. ``claude -p`` (wrapper sous la personne, ou clé API) → ``texte`` (deltas), ``etape`` (appel d'outil : « Je lis
   l'agenda… »), ``carte`` (lignes ``CARTE:`` des résultats d'outils ; secret manquant → ``identifiants``) ;
6. résultat : session enregistrée, ``CONFIRMATION_REQUISE`` extrait, validation automatique des capacités qui
   l'ont choisie → ``confirmation`` (``auto`` vrai ou faux), réponse enregistrée, journal ``message_sortant``
   (coût, jetons, durée, modèle) → ``fin`` (avec ``modele``) ; ou ``erreur`` (message pour la personne, session réinitialisée si la reprise
   a échoué).

Chaque événement est un :class:`Evenement` (``type``, ``donnees`` sérialisables en JSON), diffusé tel quel en SSE
par l'appli, et rendu en messages Telegram par le bot.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from bot import db as dbmod
from bot.config import Config, Personne
from coeur import cartes as cartesmod
from coeur import etapes as etapesmod
from coeur import filtre_sensible
from coeur import routeur as routeurmod
from coeur.claude import ClaudeError, ClaudeRunner
from coeur.confirmations import Confirmation, extraire_confirmations
from coeur.historique import Historique
from coeur.verrous import VerrouPersonne

log = logging.getLogger(__name__)

MESSAGE_ATTENTE = "Je termine ta demande précédente, un instant…"
MESSAGE_INATTENDU = "Une erreur inattendue s'est produite. Réessaie dans un instant."
PAS_DE_REPONSE = "(pas de réponse)"
LIGNES_CONTEXTE_RECENT = 6
PURGE_QUARANTAINE_S = 60  # au plus une purge des quarantaines expirées par minute et par processus
LONGUEUR_LIGNE_CONTEXTE = 200

ExecuterAuto = Callable[[Config, dbmod.Db, list[Confirmation], str], Awaitable[tuple[list[Confirmation], list[str]]]]
CartesDuJour = Callable[[Config, Personne, date], list[dict[str, Any]]]


class Contexte(Protocol):
    """Ce dont le cœur a besoin (``bot.context.BotContext``, ``web.app.ContexteWeb``)."""

    cfg: Config
    db: dbmod.Db
    sessions: Any
    runner: ClaudeRunner


@dataclass
class Evenement:
    type: str  # attente | debut | masquage | carte | etape | texte | confirmation | fin | erreur
    donnees: dict[str, Any] = field(default_factory=dict)


def ligne_contexte(prenom: str, groupe: bool = False, quand: datetime | None = None, canal: str = "telegram") -> str:
    """``[contexte: <prénom> écrit en privé|dans le groupe|dans l'appli, <date heure>]`` (fiable : écrit par le
    canal, jamais par la personne)."""
    quand = quand or datetime.now().astimezone()
    if canal == "web":
        lieu = "dans l'appli"
    else:
        lieu = "dans le groupe" if groupe else "en privé"
    return f"[contexte: {prenom} écrit {lieu}, {quand.strftime('%d/%m/%Y %H:%M')}]"


def _ms() -> int:
    return int(time.time() * 1000)


def _une_ligne(texte: str, longueur: int = LONGUEUR_LIGNE_CONTEXTE) -> str:
    t = " ".join(str(texte or "").split())
    return t if len(t) <= longueur else t[: longueur - 1].rstrip() + "…"


def bloc_contexte_recent(messages: list[dict[str, Any]], nom_assistant: str) -> str:
    """``[contexte récent]`` : les derniers échanges de la conversation (6 lignes au plus, une ligne par message,
    texte brut tronqué), pour une session Claude qui vient d'être renouvelée. Vide s'il n'y a rien à rappeler."""
    lignes: list[str] = []
    for m in messages:
        if m.get("role") not in ("personne", "assistant") or not str(m.get("texte") or "").strip():
            continue
        auteur = nom_assistant if m.get("role") == "assistant" else (m.get("auteur") or "La personne")
        # Messages stockés avant le filtre (lot 5c) : masqués ici aussi, jamais renvoyés en clair au modèle.
        lignes.append(f"{auteur} : {_une_ligne(filtre_sensible.masquer(str(m['texte'])))}")
    lignes = lignes[-LIGNES_CONTEXTE_RECENT:]
    if not lignes:
        return ""
    return (
        "[contexte récent — derniers échanges de cette conversation, pour mémoire seulement]\n"
        + "\n".join(lignes)
        + "\n[fin du contexte récent]"
    )


def ligne_detection(d: filtre_sensible.Detection, quarantaine: str | None, canal: str) -> str:
    """``[donnée sensible détectée : carte bancaire ••1234, quarantaine q7f3]`` (écrite par le cœur, fiable)."""
    morceaux = [d.libelle + (f" {d.apercu}" if d.apercu else "")]
    morceaux.append(f"quarantaine {quarantaine}" if quarantaine else "non conservée")
    if canal == "telegram":
        morceaux.append("vue par Telegram")
    return f"[donnée sensible détectée : {', '.join(morceaux)}]"


class Coeur:
    def __init__(
        self,
        ctx: Contexte,
        historique: Historique | None = None,
        *,
        executer_auto: ExecuterAuto | None = None,
        cartes_du_jour: CartesDuJour | None = None,
        classifieur: routeurmod.Classifieur | None = None,
    ) -> None:
        self.ctx = ctx
        self.historique = historique
        self.executer_auto = executer_auto
        self.cartes_du_jour = cartes_du_jour
        # Classification des messages hors règles (``coeur.routeur``) ; défaut : ``claude --bare -p`` Haiku.
        self.classifieur = classifieur
        self._verrous: dict[str, VerrouPersonne] = {}
        self._derniere_purge = 0.0

    def _classifieur(self) -> routeurmod.Classifieur:
        if self.classifieur is None:
            from tools.securite import reglages_json

            cfg = self.ctx.cfg
            reglages = reglages_json(cfg.data_dir, cfg.etat_dir, cfg.secrets_dir)
            return routeurmod.classifieur_claude(cfg.ia.modele_simple, reglages, cfg.dossier_travail("homeos"))
        return self.classifieur

    async def router(self, texte: str) -> routeurmod.Route:
        """Modèle de ce message (règles, puis classification ; repli ``ia.modele``)."""
        ia = self.ctx.cfg.ia
        route = await routeurmod.choisir(texte, ia, self._classifieur() if ia.routeur else None)
        log.info("routeur : niveau %s (%s) → %s", route.niveau, route.raison, route.modele)
        return route

    # --- verrous et conversations -----------------------------------------------------------------------------

    def verrou(self, personne: Personne) -> VerrouPersonne:
        if personne.linux_user not in self._verrous:
            dossier: Path = self.ctx.cfg.etat_dir / "verrous"
            self._verrous[personne.linux_user] = VerrouPersonne(dossier / f"{personne.linux_user}.lock")
        return self._verrous[personne.linux_user]

    def _hist(self, nom: str, *args: Any, **kwargs: Any) -> Any:
        """Appel à l'historique qui ne fait jamais échouer la conversation (journalisé)."""
        if self.historique is None:
            return None
        try:
            return getattr(self.historique, nom)(*args, **kwargs)
        except Exception:
            log.warning("historique des conversations indisponible (%s)", nom, exc_info=True)
            return None

    def nouvelle_conversation(self, personne: Personne) -> dict[str, Any] | None:
        """/nouveau, ``POST /api/conversations`` : session Claude vide et nouvelle conversation (tous canaux)."""
        self.ctx.sessions.reset(personne.telegram_id)
        return self._hist("creer", personne.linux_user)

    def _conversation_et_session(
        self, personne: Personne, conversation_id: int | None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Conversation cible et session Claude à reprendre.

        La conversation courante partage la session de ``sessions.json`` (Telegram et appli) ; une conversation
        plus ancienne choisie dans l'appli reprend sa propre session et redevient la courante."""
        sessions = self.ctx.sessions
        ia = self.ctx.cfg.ia
        ttl = timedelta(hours=ia.session_ttl_heures)

        def session_courante() -> str | None:
            return sessions.get(personne.telegram_id, ttl=ttl, max_reponses=ia.session_max_reponses)

        courante = self._hist("courante", personne.linux_user)
        cible = None
        if conversation_id is not None:
            cible = self._hist("conversation_de", personne.linux_user, conversation_id)
        if cible is None:
            cible = courante or self._hist("creer", personne.linux_user)
            if courante is None:
                # Nouvelle conversation après 12 h : la session (plus courte) est expirée elle aussi.
                return cible, session_courante()
        if courante is not None and cible is not None and cible["id"] == courante["id"]:
            return cible, session_courante()
        return cible, self._session_conversation(cible, ttl, ia.session_max_reponses)

    @staticmethod
    def _session_conversation(conv: dict[str, Any] | None, ttl: timedelta, max_reponses: int) -> str | None:
        """Session propre à une conversation plus ancienne choisie dans l'appli, si elle n'est pas à renouveler."""
        if not conv or not conv.get("session_id"):
            return None
        try:
            maj = datetime.fromisoformat(str(conv.get("maj")))
        except ValueError:
            return None
        if datetime.now().astimezone() - maj > ttl or int(conv.get("session_reponses") or 0) >= max_reponses:
            return None
        return str(conv["session_id"])

    # --- filtre de données sensibles (lot 5c) ----------------------------------------------------------------------

    async def _purger_quarantaines(self) -> None:
        if time.monotonic() - self._derniere_purge < PURGE_QUARANTAINE_S:
            return
        self._derniere_purge = time.monotonic()
        try:
            from tools import coffre

            await asyncio.to_thread(coffre.purger_quarantaine)
        except Exception:
            log.debug("purge des quarantaines impossible", exc_info=True)

    @staticmethod
    def _mettre_en_quarantaine(personne: Personne, d: filtre_sensible.Detection) -> str | None:
        """Valeur gardée chiffrée dans le coffre 10 minutes (`quarantaine/<id>`), pour la personne seulement."""
        try:
            from tools import coffre

            return coffre.mettre_en_quarantaine(d.valeur_normalisee, personne.linux_user, d.nature, d.apercu)
        except Exception as exc:  # noqa: BLE001 - coffre indisponible : la donnée est seulement masquée
            log.warning("quarantaine impossible (%s)", exc.__class__.__name__)
            return None

    async def filtrer(
        self, personne: Personne, texte: str, prompt: str, canal: str
    ) -> tuple[str, str, Evenement | None]:
        """Masque les données sensibles du message et du prompt AVANT tout appel au modèle.

        Renvoie (texte masqué, prompt masqué + lignes de détection, événement ``masquage`` ou None). Le texte de
        l'événement est exactement celui qui figure dans le prompt envoyé au modèle."""
        (texte_m, prompt_m), detections = filtre_sensible.filtrer_plusieurs(texte, prompt)
        if not detections:
            return texte, prompt, None
        lignes: list[str] = []
        details: list[dict[str, Any]] = []
        for d in detections:
            ident = await asyncio.to_thread(self._mettre_en_quarantaine, personne, d)
            lignes.append(ligne_detection(d, ident, canal))
            details.append({"type": d.nature, "libelle": d.libelle, "apercu": d.apercu or None, "quarantaine": ident})
        natures = [d["type"] for d in details]
        await dbmod.run(
            self.ctx.db.log, personne.prenom, "donnee_sensible",
            "données sensibles masquées : " + ", ".join(dict.fromkeys(d["libelle"] for d in details)),
            {"types": natures, "quarantaines": [d["quarantaine"] for d in details if d["quarantaine"]],
             "canal": canal},
        )
        prompt_m = prompt_m.rstrip("\n") + "\n" + "\n".join(lignes)
        return texte_m, prompt_m, Evenement("masquage", {"texte": texte_m, "types": list(dict.fromkeys(natures)),
                                                         "detections": details})

    # --- confirmations ------------------------------------------------------------------------------------------

    async def _confirmations_de(self, personne: Personne, confs: list[Confirmation]) -> list[Confirmation]:
        """Écarte les ``CONFIRMATION_REQUISE:<id>`` qui ne désignent pas une action en attente de cette personne :
        id inventé ou déjà traité, ou action créée par une autre personne du foyer (contenu injecté par un résultat
        d'outil, revue de sécurité du 8 novembre 2026). Rien de tout cela ne reçoit de bouton."""
        if not confs:
            return confs
        autres = {p.prenom for p in self.ctx.cfg.personnes if p.linux_user != personne.linux_user}
        gardees: list[Confirmation] = []
        for conf in confs:
            action = await dbmod.run(self.ctx.db.action, conf.action_id)
            if action is None or action["status"] != dbmod.STATUS_PENDING:
                log.warning("confirmation #%s ignorée : action absente ou déjà traitée", conf.action_id)
                continue
            if action["who"] in autres:
                log.warning("confirmation #%s ignorée : action d'une autre personne", conf.action_id)
                await dbmod.run(
                    self.ctx.db.log, personne.prenom, "confirmation",
                    f"confirmation #{conf.action_id} d'une autre personne ignorée", None, "ignored",
                )
                continue
            gardees.append(conf)
        return gardees

    # --- échange ------------------------------------------------------------------------------------------------

    async def echanger(
        self,
        personne: Personne,
        texte: str,
        *,
        canal: str = "telegram",
        groupe: bool = False,
        prompt: str | None = None,
        conversation_id: int | None = None,
        meta_journal: dict[str, Any] | None = None,
        meta_message: dict[str, Any] | None = None,
    ) -> AsyncIterator[Evenement]:
        ctx = self.ctx
        who = personne.prenom
        prompt = prompt if prompt is not None else f"{ligne_contexte(personne.prenom, groupe, canal=canal)}\n{texte}"
        # Lot 5c : aucune donnée sensible n'atteint le modèle, le journal ni l'historique (version masquée partout).
        await self._purger_quarantaines()
        texte, prompt, masquage = await self.filtrer(personne, texte, prompt, canal)
        await dbmod.run(ctx.db.log, who, "message_entrant", texte[:500], dict(meta_journal or {}))

        verrou = self.verrou(personne)
        if verrou.occupe():
            yield Evenement("attente", {"message": MESSAGE_ATTENTE})
        async with verrou:
            conv, session_id = self._conversation_et_session(personne, conversation_id)
            conv_id = conv["id"] if conv else None
            premier_du_jour = bool(
                canal == "web" and self.cartes_du_jour is not None
                and self._hist("premier_message_du_jour", personne.linux_user)
            )
            message_id = None
            if conv_id is not None:
                message_id = self._hist(
                    "ajouter_message", conv_id, "personne", canal, texte, auteur=who, meta=meta_message or None
                )
            yield Evenement("debut", {"conversation_id": conv_id, "message_id": message_id})
            if masquage is not None:
                # Preuve visible, indépendante du modèle : ce qui lui est transmis (avant tout appel à claude).
                masquage.donnees["message_id"] = message_id
                yield masquage

            if session_id is None and conv_id is not None and message_id is not None:
                # Session neuve dans une conversation déjà commencée : rappel des derniers échanges, sans modèle.
                precedents = self._hist("messages_avant", conv_id, message_id, LIGNES_CONTEXTE_RECENT * 2) or []
                bloc = bloc_contexte_recent(precedents, ctx.cfg.assistant_nom)
                if bloc:
                    prompt = f"{bloc}\n{prompt}"
            # Fichier reçu (lecture d'un document) : la règle « fichier » du routeur s'applique au prompt.
            route = await self.router(prompt if "[fichier reçu:" in prompt else texte)

            cartes_emises: list[dict[str, Any]] = []
            if premier_du_jour and self.cartes_du_jour is not None:
                try:
                    du_jour = await asyncio.to_thread(
                        self.cartes_du_jour, ctx.cfg, personne, datetime.now().astimezone().date()
                    )
                except Exception:
                    log.warning("cartes du jour indisponibles", exc_info=True)
                    du_jour = []
                if du_jour and self.historique is not None:
                    # Le brief du matin a déjà été remis dans le fil (message spontané, coeur.spontane) : pas de copie.
                    from coeur.spontane import brief_publie_aujourdhui

                    if brief_publie_aujourdhui(self.historique, personne.linux_user):
                        du_jour = [c for c in du_jour if c.get("type") != "brief"]
                for c in du_jour:
                    cartes_emises.append(c)
                    yield Evenement("carte", c)

            debut_ms = _ms()
            run = ctx.runner.run(personne.linux_user, personne.prenom, prompt, session_id,
                                 cle_api=personne.cle_api, canal=canal, modele=route.modele)
            accum: list[str] = []
            etapes: list[dict[str, Any]] = []
            par_outil: dict[str, dict[str, Any]] = {}
            ids_cartes: set[str] = set()
            try:
                async for ev in run.evenements():
                    if ev.kind == "texte":
                        accum.append(ev.texte)
                        yield Evenement("texte", {"delta": ev.texte})
                    elif ev.kind == "outil":
                        etape = {"etape": etapesmod.libelle(ev.outil), "outil": etapesmod.nom_court(ev.outil),
                                 "debut": _ms()}
                        etapes.append(etape)
                        if ev.outil_id:
                            par_outil[ev.outil_id] = etape
                        yield Evenement("etape", dict(etape))
                    elif ev.kind == "resultat_outil":
                        etape = par_outil.get(ev.outil_id or "")
                        if etape is not None:
                            etape["fin"] = _ms()
                            etape["duree_ms"] = etape["fin"] - etape["debut"]
                            if ev.erreur:
                                etape["erreur"] = True
                        _, trouvees = cartesmod.extraire_cartes(ev.texte)
                        if canal == "web" and ev.erreur:
                            identifiants = cartesmod.carte_identifiants(ev.texte)
                            if identifiants is not None:
                                trouvees.append(identifiants)
                        for c in trouvees:
                            if c["id"] in ids_cartes:
                                continue
                            ids_cartes.add(c["id"])
                            cartes_emises.append(c)
                            yield Evenement("carte", c)
            except ClaudeError as exc:
                log.warning("Erreur Claude pour %s : %s | %s", who, exc.message, exc.detail)
                await dbmod.run(
                    ctx.db.log, who, "erreur_claude", exc.message[:500], None, dbmod.STATUS_ERROR, exc.detail[:500]
                )
                # Session introuvable côté Claude (--resume refusé) : on repart d'une session vide.
                if session_id and re.search(r"no conversation found|session", exc.detail or "", re.IGNORECASE):
                    ctx.sessions.reset(personne.telegram_id)
                    log.info("Session de %s réinitialisée après erreur de reprise", who)
                if conv_id is not None:
                    self._hist("ajouter_message", conv_id, "systeme", canal, f"⚠️ {exc.message}")
                yield Evenement("erreur", {"message": exc.message})
                return
            except Exception:
                log.exception("Erreur inattendue pendant l'appel Claude")
                await dbmod.run(ctx.db.log, who, "erreur_claude", "exception inattendue", None, dbmod.STATUS_ERROR)
                if conv_id is not None:
                    self._hist("ajouter_message", conv_id, "systeme", canal, f"⚠️ {MESSAGE_INATTENDU}")
                yield Evenement("erreur", {"message": MESSAGE_INATTENDU})
                return

            resultat = run.result
            assert resultat is not None
            if resultat.session_id:
                ctx.sessions.set(personne.telegram_id, personne.linux_user, resultat.session_id)
                if conv_id is not None:
                    self._hist("maj_session", conv_id, resultat.session_id)

            texte_final, cartes_texte = cartesmod.extraire_cartes(resultat.text or "".join(accum))
            for c in cartes_texte:
                if c["id"] not in ids_cartes:
                    ids_cartes.add(c["id"])
                    cartes_emises.append(c)
                    yield Evenement("carte", c)
            texte_final, confs = extraire_confirmations(texte_final)
            confs = await self._confirmations_de(personne, confs)
            automatiques: list[tuple[Confirmation, str]] = []
            if confs and self.executer_auto is not None:
                # Capacités en validation automatique (config.yaml) : exécution immédiate, sans bouton.
                restantes, resultats = await self.executer_auto(ctx.cfg, ctx.db, confs, who)
                ids_restants = {c.action_id for c in restantes}
                automatiques = list(zip([c for c in confs if c.action_id not in ids_restants], resultats, strict=False))
                confs = restantes
                if resultats:
                    texte_final = (texte_final.rstrip() + "\n\n" if texte_final else "") + "\n\n".join(resultats)
            if not texte_final and not confs:
                texte_final = PAS_DE_REPONSE

            for conf, texte_auto in automatiques:
                yield Evenement("confirmation", {"id": conf.action_id, "resume": conf.resume, "auto": True,
                                                 "resultat": texte_auto})
            retenues: list[Confirmation] = []
            for conf in confs:
                if conv_id is not None and self._hist("associer_action", conf.action_id, conv_id) is False:
                    # Déjà rattachée à une autre conversation (la première association gagne) : pas de bouton ici.
                    log.warning("confirmation #%s déjà rattachée à une autre conversation : ignorée", conf.action_id)
                    continue
                retenues.append(conf)
                yield Evenement("confirmation", {"id": conf.action_id, "resume": conf.resume, "auto": False})
            confs = retenues

            duree_ms = resultat.duree_ms if resultat.duree_ms is not None else _ms() - debut_ms
            reponse_id = None
            if conv_id is not None:
                reponse_id = self._hist(
                    "ajouter_message", conv_id, "assistant", canal, texte_final,
                    auteur="assistant", cartes=cartes_emises or None,
                    meta={"confirmations": [{"id": c.action_id, "resume": c.resume} for c in confs],
                          "etapes": etapes, "modele": route.modele, **resultat.mesures()},
                )
            payload: dict[str, Any] = {
                "outils": resultat.tool_calls,
                "confirmations": [c.action_id for c in confs],
                "groupe": groupe,
                "modele": route.modele,
                "niveau": route.niveau,
                "nouvelle_session": session_id is None,
                **resultat.mesures(),  # coût, jetons, durée (revue P2 : /statut)
            }
            if canal != "telegram":
                payload["canal"] = canal
            await dbmod.run(ctx.db.log, who, "message_sortant", texte_final[:500], payload)
            yield Evenement(
                "fin",
                {
                    "texte": texte_final,
                    "duree_ms": duree_ms,
                    "cout_usd": resultat.cout_usd,
                    "modele": route.modele,
                    "niveau": route.niveau,
                    "etapes": etapes,
                    "conversation_id": conv_id,
                    "message_id": reponse_id,
                    "confirmations": [{"id": c.action_id, "resume": c.resume} for c in confs],
                },
            )
