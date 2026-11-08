"""Sessions Claude par personne (``<etat>/sessions.json``), partagées par le bot Telegram et l'appli web.

Format : ``{"<telegram_user_id>": {"linux_user": "alex", "session_id": "...", "updated": iso, "reponses": n}}``.

- Une session expire après ``ttl`` d'inactivité (``SESSION_TTL`` par défaut ; le cœur applique depuis le lot 4a
  ``ia.session_ttl_heures``, 2 h par défaut) ou après ``ia.session_max_reponses`` réponses (``reponses``, compté
  par :meth:`Sessions.set` tant que l'identifiant de session ne change pas) : voir ``coeur.conversation``.
- Deux processus (bot, appli web) lisent et écrivent ce fichier : il est relu quand il a changé sur le disque, et
  chaque écriture se fait sous verrou (``sessions.json.lock``) après relecture, pour ne jamais écraser l'entrée
  écrite par l'autre processus.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SESSION_TTL = timedelta(hours=12)


def _now() -> datetime:
    return datetime.now().astimezone()


class Sessions:
    def __init__(self, path: Path, ttl: timedelta = SESSION_TTL) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self._data: dict[str, dict[str, Any]] = {}
        self._empreinte: tuple[int, int] | None = None
        self._load()

    # --- disque ------------------------------------------------------------------------------------------------

    def _empreinte_disque(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self) -> None:
        self._empreinte = self._empreinte_disque()
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("sessions.json illisible (%s), repart de zéro", exc)
            self._data = {}

    def _rafraichir(self) -> None:
        """Relit le fichier s'il a été modifié (par l'autre canal) depuis la dernière lecture."""
        if self._empreinte_disque() != self._empreinte:
            self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sessions-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._empreinte = self._empreinte_disque()

    @contextmanager
    def _verrou(self) -> Iterator[None]:
        """Verrou d'écriture entre processus ; sans dossier accessible, on écrit quand même (verrou local)."""
        fd: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                os.close(fd)
            fd = None
        try:
            yield
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _modifier(self, fn: Callable[[dict[str, dict[str, Any]]], bool]) -> None:
        with self._verrou():
            self._load()
            if fn(self._data):
                self._save()

    # --- lecture -----------------------------------------------------------------------------------------------

    def entree(self, telegram_id: int) -> dict[str, Any] | None:
        """Entrée brute (copie) de cette personne, relue du disque si besoin ; ``None`` si absente."""
        self._rafraichir()
        entree = self._data.get(str(telegram_id))
        return dict(entree) if isinstance(entree, dict) else None

    def get(
        self,
        telegram_id: int,
        now: datetime | None = None,
        *,
        ttl: timedelta | None = None,
        max_reponses: int | None = None,
    ) -> str | None:
        """Renvoie le ``session_id`` valide, ou ``None`` (absent, expiré, ou trop de réponses)."""
        entry = self.entree(telegram_id)
        if not entry or not entry.get("session_id"):
            return None
        now = now or _now()
        try:
            updated = datetime.fromisoformat(entry.get("updated", ""))
            if updated.tzinfo is None:
                updated = updated.astimezone()
        except ValueError:
            return None
        if now - updated > (ttl or self.ttl):
            log.info("Session expirée pour %s (dernière activité %s)", telegram_id, entry.get("updated"))
            return None
        if max_reponses is not None and self.reponses(telegram_id) >= max_reponses:
            log.info("Session renouvelée pour %s (%s réponses)", telegram_id, entry.get("reponses"))
            return None
        return str(entry["session_id"])

    def reponses(self, telegram_id: int) -> int:
        """Nombre de réponses déjà données dans la session courante (0 si inconnu)."""
        entry = self.entree(telegram_id) or {}
        try:
            return int(entry.get("reponses") or 0)
        except (TypeError, ValueError):
            return 0

    # --- écriture ----------------------------------------------------------------------------------------------

    def set(self, telegram_id: int, linux_user: str, session_id: str) -> None:
        """Enregistre la session après une réponse ; ``reponses`` + 1 si c'est la même session, sinon 1."""

        def maj(data: dict[str, dict[str, Any]]) -> bool:
            avant = data.get(str(telegram_id)) or {}
            try:
                deja = int(avant.get("reponses") or 0) if avant.get("session_id") == session_id else 0
            except (TypeError, ValueError):
                deja = 0
            data[str(telegram_id)] = {
                "linux_user": linux_user,
                "session_id": session_id,
                "updated": _now().isoformat(timespec="seconds"),
                "reponses": deja + 1,
            }
            return True

        self._modifier(maj)

    def touch(self, telegram_id: int) -> None:
        def maj(data: dict[str, dict[str, Any]]) -> bool:
            entry = data.get(str(telegram_id))
            if not entry:
                return False
            entry["updated"] = _now().isoformat(timespec="seconds")
            return True

        self._modifier(maj)

    def reset(self, telegram_id: int) -> None:
        def maj(data: dict[str, dict[str, Any]]) -> bool:
            return data.pop(str(telegram_id), None) is not None

        self._modifier(maj)
