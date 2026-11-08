"""Diffusion des événements de l'appli (lot 4a) : un bus en mémoire par personne.

- Un échange (``POST /api/conversations/{id}/messages``) tourne dans une tâche de fond : ses événements partent sur
  la réponse SSE de la requête ET sur le bus de la personne ; si le navigateur se déconnecte, l'échange continue
  (``claude -p`` n'est pas tué) et son résultat arrive par le flux long-vivant ou dans l'historique.
- ``GET /api/conversations/{id}/flux`` s'abonne au bus (événements de cette conversation et événements sans
  conversation : résultats de confirmations, routines, messages spontanés) et, toutes les quelques secondes,
  signale les messages arrivés par un autre canal (Telegram, autre processus) : événement ``message``.

Un abonné lent perd les événements au-delà de :data:`TAILLE_FILE` (jamais de mémoire illimitée).

Observateurs (lot 4b) : chaque événement publié leur est aussi signalé avec le nombre d'abonnés de la personne
(``web.push.Notifieur`` notifie les événements spontanés quand aucun onglet n'est ouvert). Un observateur en erreur
n'empêche jamais la diffusion.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TAILLE_FILE = 500


@dataclass
class EvenementBus:
    type: str
    donnees: dict[str, Any] = field(default_factory=dict)
    conversation_id: int | None = None


Observateur = Callable[[str, "EvenementBus", int], None]


class Bus:
    def __init__(self) -> None:
        self._abonnes: dict[str, set[asyncio.Queue[EvenementBus]]] = defaultdict(set)
        self.observateurs: list[Observateur] = []

    def abonner(self, linux_user: str) -> asyncio.Queue[EvenementBus]:
        file: asyncio.Queue[EvenementBus] = asyncio.Queue(maxsize=TAILLE_FILE)
        self._abonnes[linux_user].add(file)
        return file

    def desabonner(self, linux_user: str, file: asyncio.Queue[EvenementBus]) -> None:
        self._abonnes[linux_user].discard(file)

    def publier(self, linux_user: str, evenement: EvenementBus) -> None:
        for file in list(self._abonnes.get(linux_user, ())):
            try:
                file.put_nowait(evenement)
            except asyncio.QueueFull:
                log.info("abonné du flux trop lent : événement %s perdu", evenement.type)
        for observateur in list(self.observateurs):
            try:
                observateur(linux_user, evenement, self.nb_abonnes(linux_user))
            except Exception:  # un observateur ne bloque jamais la diffusion
                log.warning("observateur du bus en erreur", exc_info=True)

    def nb_abonnes(self, linux_user: str) -> int:
        return len(self._abonnes.get(linux_user, ()))
