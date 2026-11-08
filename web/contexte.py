"""Contexte de l'appli web (lot 4a) : configuration, base, sessions Claude, cœur de la conversation, bus.

Même cœur que le bot Telegram (``coeur.conversation.Coeur``) avec le canal ``web`` : cartes du jour au premier message
du jour, captures des actions validées en cartes ``capture`` diffusées sur le bus de la personne.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot import db as dbmod
from bot.config import Config, empreinte_fichier, load_config
from bot.sessions import Sessions
from coeur import cartes as cartesmod
from coeur import confirmations as coeur_confirmations
from coeur.brief import cartes_du_jour
from coeur.claude import ClaudeRunner
from coeur.conversation import Coeur
from coeur.historique import Historique
from coeur.routeur import Classifieur
from coeur.synthese import Synthese
from coeur.transcription import Transcripteur
from web import config as configweb
from web.flux import Bus, EvenementBus
from web.stockage import StockageAuth

log = logging.getLogger(__name__)

Executer = Callable[[Config, int], Awaitable[tuple[bool, str]]]
EnvoyerTelegram = Callable[[int, str], bool]


@dataclass
class ContexteWeb:
    cfg: Config
    reglages: configweb.ReglagesWeb
    db: dbmod.Db
    sessions: Sessions
    runner: ClaudeRunner
    historique: Historique
    auth: StockageAuth
    bus: Bus = field(default_factory=Bus)
    transcripteur: Transcripteur = field(default_factory=Transcripteur)
    synthese: Synthese | None = None
    executer: Executer | None = None
    envoyer_telegram: EnvoyerTelegram | None = None
    classifieur: Classifieur | None = None
    empreinte_refusee: tuple[int, int] | None = None
    taches: set[asyncio.Task[Any]] = field(default_factory=set)
    # Échanges en cours ou en attente du verrou, par personne (plafond : ``web.limites.MAX_ECHANGES_PAR_PERSONNE``).
    en_cours: dict[str, int] = field(default_factory=dict)
    # Signalements déjà journalisés par ce processus (accès local sans en-tête Tailscale, refus répétés).
    signalements: set[str] = field(default_factory=set)
    _coeur: Coeur | None = None

    # --- plafonds -----------------------------------------------------------------------------------------------

    def echange_debute(self, linux_user: str) -> int:
        self.en_cours[linux_user] = self.en_cours.get(linux_user, 0) + 1
        return self.en_cours[linux_user]

    def echange_termine(self, linux_user: str) -> None:
        restant = self.en_cours.get(linux_user, 0) - 1
        if restant <= 0:
            self.en_cours.pop(linux_user, None)
        else:
            self.en_cours[linux_user] = restant

    # --- cœur -------------------------------------------------------------------------------------------------

    @property
    def coeur(self) -> Coeur:
        if self._coeur is None:
            self._coeur = Coeur(
                self,
                self.historique,
                executer_auto=self._executer_auto,
                cartes_du_jour=cartes_du_jour,
                classifieur=self.classifieur,
            )
        return self._coeur

    async def executer_action(self, cfg: Config, action_id: int) -> tuple[bool, str]:
        return await (self.executer or coeur_confirmations.executer_action)(cfg, action_id)

    def cartes_captures(self, chemins: list[Path], legende: str) -> list[dict[str, Any]]:
        """Captures autorisées (déjà filtrées) → cartes ``capture`` servies par ``GET /api/captures/{nom}``."""
        return [
            cartesmod.carte("capture", {"url": f"/api/captures/{c.name}", "legende": legende}, titre="Capture d'écran")
            for c in chemins
        ]

    async def decider(
        self, verbe: str, action_id: int, who: str, personne: Any = None, cartes: list[dict[str, Any]] | None = None
    ) -> str:
        """Chemin du bouton Valider/Annuler (``coeur.confirmations.decider``) ; captures → ``cartes``."""

        async def captures(chemins: list[Path], legende: str) -> int:
            if cartes is not None:
                cartes.extend(self.cartes_captures(chemins, legende))
            return len(chemins)

        return await coeur_confirmations.decider(
            self.cfg, self.db, verbe, action_id, who, personne=personne, executer=self.executer_action,
            envoyer_captures=captures,
        )

    async def _executer_auto(
        self, cfg: Config, db: dbmod.Db, confs: list[coeur_confirmations.Confirmation], who: str
    ) -> tuple[list[coeur_confirmations.Confirmation], list[str]]:
        """Validation automatique (capacités qui l'ont choisie) : captures publiées sur le bus de la personne."""
        personne = next((p for p in cfg.personnes if p.prenom == who), None)

        async def traiter(data: str, qui: str) -> str:
            analyse = coeur_confirmations.parser_callback(data)
            if analyse is None:
                return "Bouton inconnu."
            cartes: list[dict[str, Any]] = []
            texte = await self.decider(analyse[0], analyse[1], qui, cartes=cartes)
            if personne is not None:
                for c in cartes:
                    self.bus.publier(personne.linux_user, EvenementBus("carte", c))
            return texte

        return await coeur_confirmations.executer_automatiquement(cfg, db, confs, who, traiter)

    # --- configuration ------------------------------------------------------------------------------------------

    def rafraichir_config(self) -> bool:
        """config.yaml relu s'il a changé (comme le bot, lot 7a) ; une configuration invalide est ignorée."""
        if not self.cfg.config_modifiee():
            return False
        empreinte = empreinte_fichier(self.cfg.config_path)
        if empreinte is not None and empreinte == self.empreinte_refusee:
            return False
        try:
            nouvelle = load_config(self.cfg.data_dir, self.cfg.root_dir)
        except Exception as exc:  # noqa: BLE001 - jamais d'appli arrêtée par une configuration cassée
            self.empreinte_refusee = empreinte
            log.warning("config.yaml modifié mais illisible (%s) : configuration précédente conservée", exc)
            return False
        self.cfg = nouvelle
        self.runner.cfg = nouvelle
        self.empreinte_refusee = None
        return True

    def lancer(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Tâche de fond gardée en référence jusqu'à sa fin (échange qui survit à la déconnexion du navigateur)."""
        tache = asyncio.ensure_future(coro)
        self.taches.add(tache)
        tache.add_done_callback(self.taches.discard)
        return tache


def contexte_par_defaut() -> ContexteWeb:
    """Contexte du service ``homeos-web-app`` : ``HOMEOS_DATA``, ``HOMEOS_ETAT``, ``/etc/default/homeos-web-app``."""
    from web.telegram import envoyer_message

    cfg = load_config()
    db = dbmod.Db(cfg.db_path)
    db.init()
    appli = cfg.etat_dir / "appli.sqlite"
    ctx = ContexteWeb(
        cfg=cfg,
        reglages=configweb.charger(),
        db=db,
        sessions=Sessions(cfg.sessions_path),
        runner=ClaudeRunner(cfg),
        historique=Historique(appli),
        auth=StockageAuth(appli),
        synthese=Synthese(cfg.etat_dir / "web" / "voix"),
    )
    ctx.envoyer_telegram = lambda chat_id, texte: envoyer_message(ctx.cfg.telegram_token, chat_id, texte)
    return ctx
