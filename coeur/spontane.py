"""Messages spontanés (routines, briefs, rappels) : la seule porte d'entrée des crons vers l'appli.

    from coeur import spontane
    spontane.publier("alex", "Ton brief…", carte=spontane.carte_brief(texte),
                     notification={"titre": "Brief du matin", "corps": "Ton brief du jour est prêt."}, motif="brief")
    spontane.publier_foyer(texte, carte=…, notification=…, motif="routine", capacite="kitchen_daily")

Ce que fait :func:`publier` :

1. écrit le message (rôle ``assistant``, canal ``routine``) dans la conversation courante de la personne, ou dans
   une nouvelle s'il n'y en a pas (``<etat>/appli.sqlite``, ``coeur.historique``), avec ses cartes génératives
   (validées par ``coeur.cartes``) et ``meta = {spontane, motif, notification, notifier, seuil?, capacite?}`` ;
2. l'appli (``web.push.surveiller_spontanes``, toutes les 20 s) le publie sur le bus de la personne : les onglets
   ouverts l'affichent en direct ; sans onglet ouvert, ``web.push`` envoie une notification construite **seulement**
   à partir de ``meta.notification`` — jamais à partir du texte ni des cartes du message ;
3. règle « zéro bruit » (skill ``ux-capacites`` §2) : **une notification par personne et par jour**. Au-delà, le
   message est écrit en silence (``notifier = false`` : visible à la prochaine ouverture, pas de notification), sauf
   s'il franchit un **seuil déclaré** par sa capacité (manifeste ``seuils:``, ex. ``retard`` des transports,
   ``date_limite`` de Kitchen Daily). Un seuil non déclaré est ignoré (journalisé) : le message compte dans le quota.

Notification : ``{titre, corps}`` courts (40 et 120 caractères), une ligne, **sans donnée sensible** :
:func:`notification_sure` remplace par un texte générique tout titre ou corps qui contient un montant, une suite de
chiffres (code, numéro), une donnée détectée par ``coeur.filtre_sensible`` ou du balisage. Les crons écrivent des
textes fixes (« Ton brief du jour est prêt. ») : jamais un titre d'événement, jamais le contenu d'une carte.

Destinataires : :func:`destinataires` (adultes de ``config.yaml`` : ``proprietaire`` et ``membre`` ; pour une
capacité réservée, les personnes de son manifeste). Les briefs sont rédigés sous ``homeos``, qui ne lit jamais un
agenda privé (CONTRACT §6) : le même texte peut être remis à chaque adulte sans fuite entre personnes.

Ce module ne dépend ni du bot ni de l'appli : il tourne dans les crons (``tools.runner``, ``tools.capacites``),
sous ``homeos``, avec ``HOMEOS_DATA`` et ``HOMEOS_ETAT``. Aucune erreur de publication n'arrête un cron.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from coeur import cartes as cartesmod
from coeur import filtre_sensible
from coeur.historique import Historique, chemin_par_defaut

log = logging.getLogger(__name__)

CANAL = "routine"
QUOTA_NOTIFICATIONS_PAR_JOUR = 1
LONGUEUR_TITRE = 40
LONGUEUR_CORPS = 120
LONGUEUR_TEXTE = 4000
CORPS_GENERIQUE = "Nouveau message de ta routine."
MOTIFS = ("brief", "rappel", "routine", "alerte", "resultat")

# Montant (« 12,50 € », « € 12 », « 30 euros », « 12 EUR »), suite de 5 chiffres ou plus (code, numéro, IBAN
# partiel), balisage Markdown ou HTML : refusés dans une notification (écran verrouillé, montre, service de push).
_RE_MONTANT = re.compile(r"\d[\d\s  .,]*\s?(?:€|eur\b|euros?\b)|[€$£]\s?\d", re.IGNORECASE)
_RE_CHIFFRES = re.compile(r"\d[\d\s  .-]{4,}\d")
_RE_BALISAGE = re.compile(r"[<>*_`\[\]]")


@dataclass
class Publication:
    personne: str
    conversation_id: int
    message_id: int
    notifier: bool
    notification: dict[str, str]
    cartes: list[dict[str, Any]] = field(default_factory=list)


# --- notification ------------------------------------------------------------------------------------------------


def _une_ligne(texte: Any, longueur: int) -> str:
    t = " ".join(str(texte or "").split())
    return t if len(t) <= longueur else t[: longueur - 1].rstrip() + "…"


def texte_sur(texte: str) -> bool:
    """Vrai si ``texte`` peut s'afficher sur un écran verrouillé : ni montant, ni suite de chiffres, ni donnée
    sensible détectée, ni balisage."""
    if not texte:
        return True
    if _RE_MONTANT.search(texte) or _RE_CHIFFRES.search(texte) or _RE_BALISAGE.search(texte):
        return False
    return not filtre_sensible.detecter(texte)


def notification_sure(notification: dict[str, Any] | None, titre_defaut: str) -> dict[str, str]:
    """``{titre, corps}`` affichable sans risque ; ce qui ne l'est pas est remplacé par un texte générique."""
    brut = notification if isinstance(notification, dict) else {}
    titre = _une_ligne(brut.get("titre"), LONGUEUR_TITRE)
    corps = _une_ligne(brut.get("corps") or brut.get("texte"), LONGUEUR_CORPS)
    if not titre or not texte_sur(titre):
        titre = _une_ligne(titre_defaut, LONGUEUR_TITRE)
    if not corps or not texte_sur(corps):
        corps = CORPS_GENERIQUE
    return {"titre": titre, "corps": corps}


# --- destinataires, seuils -----------------------------------------------------------------------------------------


def _config() -> dict[str, Any]:
    from tools.common import load_config

    return load_config()


def destinataires(config: dict[str, Any] | None = None, capacite: str | None = None) -> list[str]:
    """``linux_user`` des adultes du foyer (rôles ``proprietaire`` et ``membre``) ; pour une capacité réservée
    (``personnes:`` de son manifeste), ceux qu'elle autorise."""
    from tools.common import ROLES_ADULTES

    cfg = _config() if config is None else config
    sortie: list[str] = []
    for p in cfg.get("personnes") or []:
        if not isinstance(p, dict) or not p.get("linux_user"):
            continue
        role = str(p.get("role") or "proprietaire").strip().lower().replace("é", "e")
        if role not in ROLES_ADULTES:
            continue
        sortie.append(str(p["linux_user"]))
    if capacite:
        manifeste = _manifeste(capacite)
        if manifeste is not None and manifeste.personnes:
            from tools import capacites

            sortie = [u for u in sortie if capacites.personne_autorisee(manifeste, u, cfg)]
    return sortie


def _manifeste(capacite: str) -> Any:
    try:
        from tools import capacites

        return capacites.trouver(capacite).manifeste
    except Exception:  # noqa: BLE001 - capacité absente ou invalide : pas de seuil, pas de filtre
        return None


def seuil_declare(capacite: str | None, seuil: str | None) -> bool:
    """Vrai si ``seuil`` figure dans ``seuils:`` du manifeste de ``capacite``."""
    if not capacite or not seuil:
        return False
    manifeste = _manifeste(capacite)
    return manifeste is not None and any(s.nom == seuil for s in getattr(manifeste, "seuils", []) or [])


# --- publication ----------------------------------------------------------------------------------------------------


def historique_par_defaut() -> Historique:
    from tools.common import etat_dir

    return Historique(chemin_par_defaut(etat_dir()))


def _cartes(carte: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if carte is None:
        return []
    return cartesmod.valides(list(carte) if isinstance(carte, list) else [carte])


def _nom_assistant(config: dict[str, Any]) -> str:
    from tools.common import nom_assistant

    return nom_assistant(config)


def publier(
    personne: Any,
    texte: str,
    carte: dict[str, Any] | list[dict[str, Any]] | None = None,
    canal: str = CANAL,
    notification: dict[str, Any] | None = None,
    *,
    motif: str = "routine",
    seuil: str | None = None,
    capacite: str | None = None,
    historique: Historique | None = None,
    config: dict[str, Any] | None = None,
    jour: date | None = None,
) -> Publication:
    """Écrit un message spontané pour ``personne`` (``linux_user`` ou objet avec ``linux_user``) et décide s'il sera
    notifié (quota d'une notification par jour, sauf seuil déclaré). Voir la docstring du module."""
    linux_user = str(getattr(personne, "linux_user", personne) or "").strip()
    if not linux_user:
        raise ValueError("personne destinataire inconnue")
    cfg = _config() if config is None else config
    hist = historique or historique_par_defaut()
    texte = str(texte or "").strip()[:LONGUEUR_TEXTE]
    cartes = _cartes(carte)
    if not texte and not cartes:
        raise ValueError("message spontané vide")
    notif = notification_sure(notification, _nom_assistant(cfg))
    seuil_ok = seuil_declare(capacite, seuil)
    if seuil and not seuil_ok:
        log.warning("seuil « %s » non déclaré par la capacité %s : message compté dans le quota", seuil, capacite)
    deja = sum(1 for m in hist.spontanes_du_jour(linux_user, jour) if (m.get("meta") or {}).get("notifier"))
    notifier = seuil_ok or deja < QUOTA_NOTIFICATIONS_PAR_JOUR
    meta: dict[str, Any] = {"spontane": True, "motif": motif if motif in MOTIFS else "routine",
                            "notification": notif, "notifier": notifier}
    if seuil_ok:
        meta["seuil"] = seuil
    if capacite:
        meta["capacite"] = capacite
    conv = hist.courante(linux_user) or hist.creer(linux_user, notif["titre"])
    message_id = hist.ajouter_message(int(conv["id"]), "assistant", canal, texte, auteur=_nom_assistant(cfg),
                                      cartes=cartes or None, meta=meta)
    return Publication(linux_user, int(conv["id"]), message_id, notifier, notif, cartes)


def publier_tous(
    personnes: Iterable[str],
    texte: str,
    carte: dict[str, Any] | list[dict[str, Any]] | None = None,
    notification: dict[str, Any] | None = None,
    **options: Any,
) -> list[Publication]:
    """:func:`publier` pour chaque personne ; une erreur pour l'une n'empêche pas les autres (journalisée sans
    contenu). Les cartes sont copiées par personne (identifiants distincts)."""
    sortie: list[Publication] = []
    for linux_user in personnes:
        copie = None
        if carte is not None:
            copie = [dict(c, id=cartesmod.nouvel_id()) for c in (carte if isinstance(carte, list) else [carte])]
        try:
            sortie.append(publier(linux_user, texte, copie, notification=notification, **options))
        except Exception as exc:  # noqa: BLE001 - un cron ne s'arrête jamais sur la publication dans l'appli
            log.warning("message spontané non publié pour %s (%s)", linux_user, exc.__class__.__name__)
    return sortie


def publier_foyer(
    texte: str,
    carte: dict[str, Any] | list[dict[str, Any]] | None = None,
    notification: dict[str, Any] | None = None,
    *,
    capacite: str | None = None,
    config: dict[str, Any] | None = None,
    **options: Any,
) -> list[Publication]:
    """Message de routine pour tout le foyer (adultes ; ou personnes autorisées par la capacité)."""
    cfg = _config() if config is None else config
    return publier_tous(destinataires(cfg, capacite), texte, carte, notification, capacite=capacite, config=cfg,
                        **options)


# --- cartes usuelles -------------------------------------------------------------------------------------------------


def carte_brief(texte: str, jour: date | None = None, titre: str = "Brief du jour", periode: str = "jour") -> dict[str, Any]:
    """Carte ``brief`` ; ``periode`` : ``jour`` (brief du matin) ou ``semaine`` (brief hebdomadaire)."""
    jour = jour or datetime.now().astimezone().date()
    return cartesmod.carte("brief", {"texte": texte, "date": jour.isoformat(), "periode": periode}, titre=titre,
                           actions=[{"libelle": "Écouter", "action": "ecouter", "args": {"texte": texte}}])


def carte_routine(texte: str, titre: str, capacite: str | None = None, cron: str | None = None) -> dict[str, Any]:
    donnees: dict[str, Any] = {"texte": texte, "quand": datetime.now().astimezone().isoformat(timespec="minutes")}
    if capacite:
        donnees["capacite"] = capacite
    if cron:
        donnees["cron"] = cron
    return cartesmod.carte("routine_resultat", donnees, titre=titre,
                           actions=[{"libelle": "Écouter", "action": "ecouter", "args": {"texte": texte}}])


def brief_publie_aujourdhui(hist: Historique, linux_user: str, jour: date | None = None) -> bool:
    """Vrai si le brief du matin a déjà été remis à la personne aujourd'hui (le cœur n'en remet pas une copie)."""
    try:
        return any(
            (m.get("meta") or {}).get("motif") == "brief"
            and any(c.get("type") == "brief" and (c.get("donnees") or {}).get("periode") == "jour"
                    for c in m.get("cartes") or [])
            for m in hist.spontanes_du_jour(linux_user, jour)
        )
    except Exception:  # noqa: BLE001
        return False
