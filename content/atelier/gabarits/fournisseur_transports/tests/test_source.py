"""Tests du fournisseur (sans réseau) : manifeste, interface commune, analyse d'une réponse capturée."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from capabilities.transports import fournisseurs, interface

DOSSIER = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _source_module() -> Any:
    spec = importlib.util.spec_from_file_location("source_candidat", DOSSIER / "source.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FauxClient:
    """Client réseau factice : renvoie la réponse capturée, sans réseau."""

    def __init__(self, reponse: Any) -> None:
        self.reponse = reponse
        self.dossier = Path("/nonexistent")
        self.appels: list[str] = []

    def json(self, url: str, params: dict[str, Any] | None = None, **_options: Any) -> Any:
        self.appels.append(url)
        return self.reponse

    def octets(self, url: str, params: dict[str, Any] | None = None, **_options: Any) -> bytes:
        self.appels.append(url)
        return b"ok"


def test_manifeste_valide() -> None:
    f = fournisseurs.lire_manifeste(DOSSIER, "atelier")
    assert f.valide, f.erreurs
    assert f.origine == "assistant"


def test_interface_commune() -> None:
    assert issubclass(_source_module().Source, interface.Source)


def test_prochains_departs(monkeypatch: Any) -> None:
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-09T08:00:00")
    reponse = json.loads((FIXTURES / "departs.json").read_text(encoding="utf-8"))
    source = _source_module().Source(FauxClient(reponse), {"site": "https://exemple.org"}, SimpleNamespace(titre="Démo"))
    res = source.prochains_departs({"origine": "Gare", "destination": "Centre"}, 60)
    assert res["departs"] and all(d["heure_theorique"] for d in res["departs"])
    assert source.verifier()["ok"] is True
