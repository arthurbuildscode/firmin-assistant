"""Smoke test de « courses & promos » (healthcheck) : la base locale s'ouvre et répond.

Aucun site n'est contacté dans ce lot. Le message dit si le mode de collecte des prix reste à choisir.
"""

from __future__ import annotations

from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    from . import stockage

    try:
        with stockage.ouvrir(ctx.dossier) as conn:
            n = conn.execute("SELECT count(*) AS n FROM tickets").fetchone()["n"]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Base des courses illisible ({exc.__class__.__name__})"}
    try:
        mode = ctx.parametre("mode", "a_choisir")
    except Exception:  # noqa: BLE001
        mode = "a_choisir"
    suite = "mode de collecte à choisir" if mode == "a_choisir" else f"mode {mode}"
    return {"ok": True, "message": f"Base des courses OK : {n} ticket(s) lu(s) ; {suite}"}
