"""Smoke test de l'atelier : prompts présents, dossiers de travail accessibles, aucun travail bloqué."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    from tools import atelier
    from tools.common import content_dir, now

    for fichier in ("SYSTEM.md", "RELECTURE.md"):
        if not (content_dir() / "atelier" / fichier).exists():
            return {"ok": False, "message": f"prompt de l'atelier absent : content/atelier/{fichier}"}
    bloques = []
    for e in atelier.etat_public():  # type: ignore[union-attr]
        if e.get("statut") == "en_cours" and e.get("maj"):
            try:
                if now() - datetime.fromisoformat(e["maj"]) > timedelta(hours=2):
                    bloques.append(e.get("titre") or e.get("nom"))
            except ValueError:
                continue
    if bloques:
        return {"ok": False, "message": f"travail d'atelier bloqué depuis plus de 2 h : {', '.join(bloques)}"}
    return {"ok": True, "message": "atelier prêt"}
