"""Tests de la capacité « Atelier » (la logique est testée dans tests/test_atelier.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import capacites

DOSSIER = Path(__file__).resolve().parent.parent


def test_manifeste_valide() -> None:
    cap = capacites.lire_capacite(DOSSIER)
    assert cap.valide, cap.erreurs
    assert cap.manifeste is not None
    assert set(cap.manifeste.confirmation_toujours) == set(cap.manifeste.actions_avec_effet)


def test_outils_et_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path))
    monkeypatch.delenv("HOMEOS_ETAT", raising=False)
    mcp = capacites.MCPFactice()
    outils = capacites.charger_capacite(capacites.lire_capacite(DOSSIER), mcp, donnees=tmp_path)
    assert outils == ["atelier_creer", "atelier_etat", "atelier_approuver_reseau"]
    assert capacites.smoke(capacites.lire_capacite(DOSSIER), tmp_path)["ok"]
