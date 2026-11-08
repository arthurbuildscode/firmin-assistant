"""Limites de débit et plafonds de l'appli web (revue de sécurité du 8 novembre 2026).

L'appli n'écoute qu'en local derrière ``tailscale serve`` : l'adresse du client est celle que le proxy met dans
``X-Forwarded-For`` (seul un processus local peut la forger, et cela ne lui donne qu'une autre case du compteur).
Deux niveaux : par client et global, sur les routes d'authentification non authentifiées (création de cérémonies,
codes Telegram, invitations), qui sont les seules joignables par un membre du tailnet qui n'est pas du foyer.

Plafonds par personne (membre du foyer malveillant ou front emballé) : échanges ``claude -p`` en file et
abonnements au flux SSE.
"""

from __future__ import annotations

import time
from collections import deque

MAX_AUTH_PAR_MINUTE = 20  # par client (adresse Tailscale)
MAX_AUTH_GLOBAL_PAR_MINUTE = 120  # toutes adresses confondues
MAX_ECHANGES_PAR_PERSONNE = 3  # échanges en cours ou en attente du verrou
MAX_FLUX_PAR_PERSONNE = 10  # flux SSE long-vivants ouverts (onglets, appareils)
CLES_MAX = 10_000  # taille mémoire bornée du limiteur


class Limiteur:
    """Fenêtre glissante : au plus ``maximum`` passages par ``fenetre_s`` secondes et par clé."""

    def __init__(self, maximum: int, fenetre_s: float = 60.0) -> None:
        self.maximum = maximum
        self.fenetre_s = fenetre_s
        self._passages: dict[str, deque[float]] = {}

    def autoriser(self, cle: str, maintenant: float | None = None) -> bool:
        t = time.monotonic() if maintenant is None else maintenant
        file = self._passages.get(cle)
        if file is None:
            if len(self._passages) >= CLES_MAX:
                self._elaguer(t)
            file = self._passages[cle] = deque()
        while file and t - file[0] > self.fenetre_s:
            file.popleft()
        if len(file) >= self.maximum:
            return False
        file.append(t)
        return True

    def _elaguer(self, t: float) -> None:
        for cle, file in list(self._passages.items()):
            while file and t - file[0] > self.fenetre_s:
                file.popleft()
            if not file:
                del self._passages[cle]
        if len(self._passages) >= CLES_MAX:  # tout le monde actif : on repart de zéro plutôt que de grossir
            self._passages.clear()


def cle_client(entetes: dict[str, str], client: tuple[str, int] | None) -> str:
    """Adresse du client : premier maillon de ``X-Forwarded-For`` (posé par tailscale serve), sinon le pair TCP."""
    transmis = entetes.get("x-forwarded-for", "")
    if transmis:
        premier = transmis.split(",")[0].strip()
        if premier:
            return premier[:64]
    return str(client[0] if client else "inconnu")
