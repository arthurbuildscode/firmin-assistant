"""Retours d'usage (interviews, maquette) : table `feedback`."""

from __future__ import annotations

from typing import Any

from tools import db
from tools.common import iso_now, who


def noter(sujet: str, note: int, commentaire: str | None = None) -> dict[str, Any]:
    """Enregistre une note de 1 à 5 sur un écran ou un sujet, avec commentaire libre."""
    sujet = " ".join(sujet.strip().split())
    if not sujet:
        raise ValueError("Le sujet est vide.")
    note = int(note)
    if not 1 <= note <= 5:
        raise ValueError("La note doit être comprise entre 1 et 5.")
    with db.cursor() as conn:
        cur = conn.execute(
            "INSERT INTO feedback (ts, who, sujet, note, commentaire) VALUES (?,?,?,?,?)",
            (iso_now(), who(), sujet, note, (commentaire or "").strip() or None),
        )
        fid = cur.lastrowid
    return {"id": fid, "sujet": sujet, "note": note, "commentaire": commentaire}


def lister(limit: int = 50) -> list[dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return db.rows_to_dicts(rows)
