"""Un seul ``claude -p`` à la fois par personne, tous canaux confondus (lot 4a).

Le bot Telegram et l'appli web sont deux processus : un verrou asyncio ne suffit plus. :class:`VerrouPersonne`
combine un ``asyncio.Lock`` (dans le processus) et un verrou de fichier ``flock`` sur
``<etat>/verrous/<personne>.lock`` (entre processus), attendu sans bloquer la boucle asyncio.

:data:`VerrouFichier` (même classe) sert aussi aux validations : un seul exécuteur par action
(``<etat>/verrous/action-<id>.lock``), deux instances d'un même processus s'excluant par le ``flock`` lui-même.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

INTERVALLE_S = 0.25


class VerrouPersonne:
    def __init__(self, chemin: Path) -> None:
        self.chemin = Path(chemin)
        self._local = asyncio.Lock()
        self._fd: int | None = None

    def _essayer_fichier(self) -> bool:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.chemin, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def occupe(self) -> bool:
        """Vrai si une demande de cette personne est en cours (ici ou dans l'autre canal)."""
        if self._local.locked():
            return True
        try:
            self.chemin.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.chemin, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)  # libère aussi le verrou pris pour le test
        return False

    async def __aenter__(self) -> Self:
        await self._local.acquire()
        try:
            while True:
                try:
                    if self._essayer_fichier():
                        return self
                except OSError:
                    return self  # dossier d'état inaccessible : le verrou local reste la garantie minimale
                await asyncio.sleep(INTERVALLE_S)
        except BaseException:
            self._local.release()
            raise

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._local.release()


VerrouFichier = VerrouPersonne
