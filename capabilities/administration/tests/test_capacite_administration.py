"""Tests propres à la capacité « Administration » (le gros des tests : tests/test_administration.py)."""

from __future__ import annotations

from pathlib import Path

from tools import capacites

DOSSIER = Path(__file__).resolve().parent.parent


def test_manifeste_valide_avec_prefixe() -> None:
    cap = capacites.lire_capacite(DOSSIER)
    assert cap.valide, cap.erreurs
    assert cap.prefixe == "admin"
    assert all(a.startswith("admin_") for a in cap.manifeste.actions_avec_effet)
