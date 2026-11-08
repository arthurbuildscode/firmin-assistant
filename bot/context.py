"""Objets partagés par les handlers (rangés dans ``application.bot_data["ctx"]``)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from telegram.ext import ContextTypes

from bot.claude_runner import ClaudeRunner
from bot.config import Config, empreinte_fichier, load_config
from bot.db import Db
from bot.sessions import Sessions
from bot.transcribe import Transcripteur
from coeur.conversation import Coeur
from coeur.historique import Historique

log = logging.getLogger(__name__)


@dataclass
class BotContext:
    cfg: Config
    db: Db
    sessions: Sessions
    runner: ClaudeRunner
    transcripteur: Transcripteur
    # Un verrou par personne : pas deux `claude -p` en parallèle sur la même session.
    verrous: dict[int, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))

    # Empreinte d'un config.yaml invalide déjà signalé (pas de nouvel avertissement à chaque message).
    empreinte_refusee: tuple[int, int] | None = None
    # Lot 4a : cœur de la conversation partagé avec l'appli web (créé au premier message).
    _coeur: Coeur | None = None

    def verrou(self, telegram_id: int) -> asyncio.Lock:
        return self.verrous[telegram_id]

    @property
    def coeur(self) -> Coeur:
        """Cœur de la conversation (``coeur.conversation``) : verrou par personne entre canaux, historique partagé
        avec l'appli web (``<etat>/appli.sqlite``), validation automatique avec envoi des captures au groupe."""
        if self._coeur is None:
            from bot import confirmations

            async def executer_auto(cfg: Config, db: Db, confs: list, who: str) -> tuple[list, list[str]]:
                return await confirmations.executer_automatiquement(cfg, db, confs, who)

            self._coeur = Coeur(self, Historique(self.cfg.etat_dir / "appli.sqlite"), executer_auto=executer_auto)
        return self._coeur

    def rafraichir_config(self) -> bool:
        """Lot 7a : recharge config.yaml s'il a changé (mtime ou taille) ; renvoie True si rechargé.

        Une configuration devenue illisible est ignorée (la précédente reste en service) et signalée une fois."""
        if not self.cfg.config_modifiee():
            return False
        empreinte = empreinte_fichier(self.cfg.config_path)
        if empreinte is not None and empreinte == self.empreinte_refusee:
            return False
        try:
            nouvelle = load_config(self.cfg.data_dir, self.cfg.root_dir)
        except Exception as exc:  # noqa: BLE001 - jamais de bot arrêté par une configuration cassée
            self.empreinte_refusee = empreinte
            log.warning("config.yaml modifié mais illisible (%s) : configuration précédente conservée", exc)
            return False
        self.cfg = nouvelle
        self.runner.cfg = nouvelle
        self.empreinte_refusee = None
        log.info("config.yaml rechargé (%d personne(s))", len(nouvelle.personnes))
        return True


def get_ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    """Contexte du bot ; la configuration est relue si config.yaml a changé (administration par la conversation)."""
    ctx: BotContext = context.application.bot_data["ctx"]
    try:
        ctx.rafraichir_config()
    except Exception:
        log.warning("rechargement de la configuration impossible", exc_info=True)
    return ctx
