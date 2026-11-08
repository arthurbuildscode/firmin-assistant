"""Smoke test de « Salon démo » : le site répond et la page de connexion est reconnue (sans identifiants)."""

from __future__ import annotations

from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    from . import connecteur

    return connecteur.verifier_site(ctx)
