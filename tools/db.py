"""Accès SQLite (`data/homeos.sqlite`) et journal des actions (`actions_log`)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tools.common import data_dir, get_logger, iso_now, who

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article TEXT NOT NULL,
    rayon TEXT NOT NULL DEFAULT 'autre',
    coche INTEGER NOT NULL DEFAULT 0,
    ajoute_par TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS echeances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titre TEXT NOT NULL,
    date TEXT NOT NULL,
    rappels_jours TEXT NOT NULL DEFAULT '[30, 7, 1]',
    recurrence TEXT,
    notes TEXT,
    cree_par TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anniversaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prenom TEXT NOT NULL,
    date TEXT NOT NULL,
    lieu TEXT,
    notes TEXT,
    delai_cadeau_jours INTEGER NOT NULL DEFAULT 21,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    who TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    result TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    who TEXT NOT NULL,
    sujet TEXT NOT NULL,
    note INTEGER NOT NULL,
    commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions_log(status);
"""


def db_path() -> Path:
    return data_dir() / "homeos.sqlite"


def connect() -> sqlite3.Connection:
    """Ouvre la base et crée les tables si nécessaire (idempotent)."""
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    """Connexion transactionnelle : commit en sortie, rollback sur exception."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# --- actions_log -----------------------------------------------------------


def log_action(
    kind: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    status: str = "done",
    result: str | None = None,
    acteur: str | None = None,
) -> int:
    """Insère une ligne dans `actions_log` et renvoie son identifiant.

    `payload` ne doit jamais contenir de secret (URL iCal, token…).
    """
    with cursor() as conn:
        cur = conn.execute(
            "INSERT INTO actions_log (ts, who, kind, summary, payload, status, result) VALUES (?,?,?,?,?,?,?)",
            (
                iso_now(),
                acteur or who(),
                kind,
                summary[:500],
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                status,
                result,
            ),
        )
        action_id = int(cur.lastrowid or 0)
    log.debug("actions_log #%s %s %s [%s]", action_id, kind, summary, status)
    return action_id


def get_action(action_id: int) -> dict[str, Any] | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM actions_log WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return None
    action = dict(row)
    try:
        action["payload"] = json.loads(action.get("payload") or "{}")
    except json.JSONDecodeError:
        action["payload"] = {}
    return action


def update_action(action_id: int, status: str, result: str | None = None) -> None:
    with cursor() as conn:
        conn.execute(
            "UPDATE actions_log SET status = ?, result = ? WHERE id = ?",
            (status, result, action_id),
        )


def recent_actions(limit: int = 20) -> list[dict[str, Any]]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT id, ts, who, kind, summary, status FROM actions_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)
