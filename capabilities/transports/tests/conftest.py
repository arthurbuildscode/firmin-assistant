"""Fixtures des tests de la capacité transports (sans réseau) : faux service Cartreize, serveur HTTP local générique,
contexte minimal. Marqueur `reseau` : tests d'intégration avec les vraies sources (HOMEOS_TESTS_RESEAU=1)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PASSAGES = "/api/passages/7/grandport/pont-du-moulin-villeneuve"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "reseau: lance le vrai service Node ou interroge les vraies sources ; activé par HOMEOS_TESTS_RESEAU=1"
    )


def fixture_json(nom: str) -> Any:
    return json.loads((FIXTURES / nom).read_text(encoding="utf-8"))


class Serveur:
    """Serveur HTTP local : `routes[chemin] = (code, octets, type)` ; `appels` garde les chemins demandés."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.appels: list[str] = []
        self.repondre_fn: Any = None
        serveur = self

        class Gestionnaire(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # nom imposé par http.server
                serveur.appels.append(self.path)
                if serveur.repondre_fn is not None:
                    code, corps, type_ = serveur.repondre_fn(self.path)
                else:
                    code, corps, type_ = serveur.routes.get(self.path.split("?")[0], (404, b"{}", "application/json"))
                self.send_response(code)
                self.send_header("Content-Type", type_)
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)

        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), Gestionnaire)
        self.port = self.serveur.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.serveur.serve_forever, daemon=True).start()

    def arreter(self) -> None:
        self.serveur.shutdown()
        self.serveur.server_close()


class FauxCartreize:
    """Imite les routes /api/... de app.mjs à partir des réponses capturées sur le vrai service (fixtures/)."""

    def __init__(self, serveur: Serveur) -> None:
        self.s = serveur
        self.mode = "temps_reel"  # ou "theorique", "erreur500"
        serveur.repondre_fn = self.repondre

    @property
    def url(self) -> str:
        return self.s.url

    @property
    def appels(self) -> list[str]:
        return self.s.appels

    def repondre(self, chemin: str) -> tuple[int, bytes, str]:
        def j(code: int, obj: Any) -> tuple[int, bytes, str]:
            return code, json.dumps(obj).encode(), "application/json; charset=utf-8"

        if self.mode == "erreur500":
            return j(500, {"erreur": "boum"})
        if chemin == "/api/lignes":
            return j(200, fixture_json("lignes.json"))
        if chemin == "/api/diagnostic":
            return j(200, fixture_json("diagnostic.json"))
        if chemin == "/api/ligne/7":
            return j(200, fixture_json("ligne_7.json"))
        if chemin.startswith("/api/ligne/"):
            return j(404, {"erreur": "ligne inconnue"})
        if chemin == PASSAGES:
            return j(200, fixture_json(f"passages_7_{self.mode}.json"))
        return j(404, {"erreur": "arrêt inconnu"})


@pytest.fixture()
def serveur() -> Iterator[Serveur]:
    s = Serveur()
    yield s
    s.arreter()


@pytest.fixture(scope="module")
def _serveur_cartreize() -> Iterator[Serveur]:
    s = Serveur()
    yield s
    s.arreter()


@pytest.fixture()
def cartreize(_serveur_cartreize: Serveur) -> FauxCartreize:
    _serveur_cartreize.appels.clear()
    f = FauxCartreize(_serveur_cartreize)
    return f


def ecrire_parametres(donnees: Path, valeurs: dict[str, Any]) -> None:
    d = donnees / "capacites" / "transports"
    d.mkdir(parents=True, exist_ok=True)
    (d / "parametres.yaml").write_text(yaml.safe_dump(valeurs), encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path: Path, cartreize: FauxCartreize, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Contexte minimal (duck-typing) : foyer qui utilise Cartreize ; 7 nov. 2026 (samedi) 16:10, comme les captures."""
    for env in ("HOMEOS_BUS_URL", "HOMEOS_TRANSPORTS_CARTREIZE_URL"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T16:10:00")
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(Path(__file__).resolve().parents[2]))
    donnees = tmp_path / "data"
    donnees.mkdir()
    ecrire_parametres(donnees, {"fournisseurs": "cartreize"})
    return SimpleNamespace(donnees=donnees, personne="alice",
                           config={"transports": {"cartreize": {"url": cartreize.url}}})
