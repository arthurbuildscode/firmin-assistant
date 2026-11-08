"""Accès SQLite (data/homeos.sqlite) — table ``actions_log`` partagée avec les outils.

Schéma (identique à celui du chantier outils) :

    actions_log(id INTEGER PRIMARY KEY, ts TEXT, who TEXT, kind TEXT, summary TEXT,
                payload TEXT (JSON), status TEXT, result TEXT)

Les fonctions sont synchrones (sqlite3 stdlib) et courtes ; les appelants asyncio
les enveloppent dans ``asyncio.to_thread`` via :func:`run`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

SCHEMA_ACTIONS_LOG = """
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    who TEXT,
    kind TEXT NOT NULL,
    summary TEXT,
    payload TEXT,
    status TEXT,
    result TEXT
);
"""

STATUS_PENDING = "pending"
STATUS_CANCELLED = "cancelled"
STATUS_DONE = "done"
STATUS_OK = "ok"
STATUS_ERROR = "error"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Db:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_ACTIONS_LOG)

    # --- Journalisation ---------------------------------------------------
    def log(
        self,
        who: str | None,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        status: str = STATUS_OK,
        result: str | None = None,
    ) -> int:
        """Ajoute une ligne. ``summary`` est tronqué à 500 caractères, jamais de secret."""
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO actions_log (ts, who, kind, summary, payload, status, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), who, kind, (summary or "")[:500], payload_json, status, result),
            )
            return int(cur.lastrowid or 0)

    def dernieres_actions(self, n: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, ts, who, kind, summary, status, result FROM actions_log "
                    "ORDER BY id DESC LIMIT ?",
                    (n,),
                )
            )

    def action(self, action_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM actions_log WHERE id = ?", (action_id,)).fetchone()

    def maj_statut(self, action_id: int, status: str, result: str | None = None) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE actions_log SET status = ?, result = COALESCE(?, result) WHERE id = ?",
                (status, result, action_id),
            )
            return cur.rowcount > 0

    def annuler_action(self, action_id: int, who: str) -> bool:
        """Passe une action ``pending`` à ``cancelled``. Renvoie False si elle n'était plus en attente."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE actions_log SET status = ?, result = ? WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, f"annulée par {who}", action_id, STATUS_PENDING),
            )
            return cur.rowcount > 0

    # --- Courses (table créée par les outils ; lecture seule ici) -----------
    def table_existe(self, nom: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (nom,)
            ).fetchone()
            return row is not None

    def courses(self) -> list[sqlite3.Row] | None:
        """Articles non cochés, triés par rayon. ``None`` si la table n'existe pas."""
        if not self.table_existe("courses"):
            return None
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, article, rayon, coche, ajoute_par, ts FROM courses "
                    "WHERE COALESCE(coche, 0) = 0 ORDER BY COALESCE(rayon, ''), id"
                )
            )

    def dernier_brief(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT ts, kind, status FROM actions_log WHERE kind LIKE 'brief%' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def mesures_mois(self, debut_iso: str) -> dict[str, Any]:
        """Dépense estimée et latence médiane des réponses (``message_sortant``) et briefs depuis ``debut_iso``.

        Les coûts viennent de ``total_cost_usd`` (estimation de Claude Code, sessions abonnement comprises)."""
        cout = 0.0
        durees: list[int] = []
        n = 0
        with self.connect() as conn:
            lignes = conn.execute(
                "SELECT kind, payload FROM actions_log WHERE ts >= ? AND payload IS NOT NULL "
                "AND (kind = 'message_sortant' OR kind LIKE 'brief%')",
                (debut_iso,),
            ).fetchall()
        for ligne in lignes:
            try:
                payload = json.loads(ligne["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("cout_usd"), (int, float)):
                cout += float(payload["cout_usd"])
            if ligne["kind"] == "message_sortant":
                n += 1
                if isinstance(payload.get("duree_ms"), int):
                    durees.append(payload["duree_ms"])
        durees.sort()
        mediane = None
        if durees:
            milieu = len(durees) // 2
            mediane = durees[milieu] if len(durees) % 2 else (durees[milieu - 1] + durees[milieu]) // 2
        return {"cout_usd": round(cout, 2), "reponses": n, "latence_mediane_ms": mediane}


async def run(fn: Callable[..., T], *args: Any) -> T:
    """Exécute un appel SQLite bloquant hors de la boucle asyncio."""
    return await asyncio.to_thread(fn, *args)
