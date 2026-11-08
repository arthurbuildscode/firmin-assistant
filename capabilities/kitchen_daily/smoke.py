"""Smoke test Kitchen Daily : la session du site est-elle encore valide ? (lance Chromium quelques secondes)."""

from __future__ import annotations

from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    from tools import kitchen_daily

    if not kitchen_daily.state_path().exists():
        return {"ok": False, "message": kitchen_daily.MESSAGE_SESSION}
    if kitchen_daily.session_valide():
        return {"ok": True, "message": "session Kitchen Daily valide"}
    return {"ok": False, "message": kitchen_daily.MESSAGE_SESSION}
