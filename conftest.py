"""Isolation commune à toute la suite (tests/ et capabilities/*/tests/).

Sur le VPS (scripts/verifier.sh), les tests ne doivent jamais lire les vrais secrets (/etc/homeos), l'état
(/var/lib/homeos), le proxy (/etc/default/homeos-proxy) ni le mode des outils (/etc/default/homeos-outils, lot 7) :
ces emplacements sont redirigés vers des chemins absents, chaque test qui en a besoin les fixe lui-même.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolation_homeos(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    vide: Path = tmp_path_factory.getbasetemp() / "absent-homeos"
    monkeypatch.setenv("HOMEOS_SECRETS", str(vide / "secrets"))
    monkeypatch.setenv("HOMEOS_FICHIER_PROXY", str(vide / "homeos-proxy"))
    monkeypatch.setenv("HOMEOS_FICHIER_OUTILS", str(vide / "homeos-outils"))
    for variable in ("HOMEOS_ETAT", "HOMEOS_OUTILS", "HOMEOS_OUTILS_URL", "HOMEOS_JETONS", "HOMEOS_NAVIGATEUR_WS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOMEOS_NAVIGATEUR", "local")
