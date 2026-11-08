"""État des modules d'onboarding : `data/onboarding/<module>.json`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.common import content_dir, data_dir, iso_now

_MODULE_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _dossier() -> Path:
    d = data_dir() / "onboarding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def modules_connus() -> list[str]:
    """Modules déclarés dans content/onboarding/*.md (ex. 00-identite-foyer), triés."""
    dossier = content_dir() / "onboarding"
    if not dossier.exists():
        return []
    return sorted(p.stem for p in dossier.glob("*.md"))


def normaliser_module(module: str) -> str:
    m = module.strip().casefold().replace(" ", "-")
    m = m.removesuffix(".md")
    if not _MODULE_OK.match(m):
        raise ValueError(f"Nom de module invalide : « {module} » (lettres, chiffres, tirets).")
    return m


def etat() -> dict[str, Any]:
    """État de chaque module : fait ou non, date de mise à jour, réponses."""
    modules: dict[str, Any] = {}
    for nom in modules_connus():
        modules[nom] = {"done": False, "updated": None, "answers": {}}
    for fichier in sorted(_dossier().glob("*.json")):
        try:
            donnees = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            donnees = {}
        modules[fichier.stem] = {
            "done": bool(donnees.get("done", False)),
            "updated": donnees.get("updated"),
            "answers": donnees.get("answers") or {},
        }
    restants = [m for m, v in modules.items() if not v["done"]]
    return {"modules": modules, "restants": restants, "tout_fait": not restants and bool(modules)}


def terminer(module: str, reponses: dict[str, Any] | None = None) -> dict[str, Any]:
    """Marque un module comme terminé et enregistre les réponses (fusionnées avec les précédentes)."""
    nom = normaliser_module(module)
    fichier = _dossier() / f"{nom}.json"
    precedent: dict[str, Any] = {}
    if fichier.exists():
        try:
            precedent = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            precedent = {}
    answers = dict(precedent.get("answers") or {})
    answers.update(reponses or {})
    donnees = {"done": True, "answers": answers, "updated": iso_now()}
    fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"module": nom, **donnees}
