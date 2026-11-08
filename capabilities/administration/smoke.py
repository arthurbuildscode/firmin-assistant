"""Smoke test de l'administration : config.yaml valide et journal d'administration accessible en écriture."""

from __future__ import annotations

import os
from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    from tools import config_edition

    try:
        config_edition.valider(config_edition.lire_brut(ctx.donnees), ctx.donnees)
    except config_edition.ConfigInvalide as exc:
        return {"ok": False, "message": f"configuration invalide : {exc}"}
    dossier = config_edition.dossier_administration(ctx.donnees)
    if not os.access(dossier, os.W_OK):
        return {"ok": False, "message": "journal d'administration non accessible en écriture"}
    return {"ok": True, "message": "configuration valide, journal accessible"}
