"""Atelier, second type de fabrication : un fournisseur de la capacité transports (« ajoute le métro de Lyon »).

Le sous-agent est le faux `tests/fixtures/atelier/faux_claude.py` en mode `garder` : il laisse le gabarit
`content/atelier/gabarits/fournisseur_transports/` tel que l'atelier l'a amorcé. Tout le reste est réel : contrôles
statiques, ruff, tests et smoke test du fournisseur en sous-processus isolés (le smoke joint un faux site local),
relecture, installation dans `<etat>/fournisseurs-atelier/`, hôte approuvé pour la capacité transports.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

RACINE = Path(__file__).resolve().parent.parent
FAUX_CLAUDE = RACINE / "tests" / "fixtures" / "atelier" / "faux_claude.py"
CAP_ATELIER = RACINE / "capabilities" / "atelier"
CONFIG = """\
assistant:
  nom: "Testbot"
personnes:
  - telegram_id: 111
    prenom: Alex
    linux_user: alex
  - telegram_id: 222
    prenom: Camille
    linux_user: camille
    role: membre
"""


@pytest.fixture()
def site() -> Iterator[str]:
    class Gestionnaire(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # nom imposé par http.server
            corps = b"<html><title>Metro demo</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

    serveur = ThreadingHTTPServer(("127.0.0.1", 0), Gestionnaire)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{serveur.server_address[1]}"
    serveur.shutdown()
    serveur.server_close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from tools import atelier

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(RACINE))
    for v in ("HOMEOS_CAPABILITIES", "HOMEOS_CAPABILITIES_ATELIER", "HOMEOS_PERSONNE", "HOMEOS_WHO", "HOMEOS_ETAT",
              "HOMEOS_FOURNISSEURS_ATELIER", "FAUX_CLAUDE_DEFAUT", "FAUX_CLAUDE_CORRIGE", "FAUX_CLAUDE_VERDICT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOMEOS_ACTION_ID", "")
    monkeypatch.setenv("HOMEOS_ATELIER_CLAUDE", str(FAUX_CLAUDE))
    monkeypatch.setenv("FAUX_CLAUDE_MODELE", "garder")
    monkeypatch.setenv("HOMEOS_ATELIER_LANCEMENT", "synchrone")
    monkeypatch.setenv("FAUX_CLAUDE_JOURNAL", str(tmp_path / "faux_claude.jsonl"))
    monkeypatch.setattr(atelier, "explorer_site", lambda d, travail: {"titre": "Métro démo", "hotes_bloques": []})
    messages: list[tuple[str, str | None]] = []
    monkeypatch.setattr(atelier, "envoyer_telegram", lambda texte, chat: messages.append((texte, chat)) or True)
    return {"data": data, "messages": messages, "tmp": tmp_path}


def _atelier(data: Path, personne: str) -> Any:
    from tools import capacites

    mcp = capacites.MCPFactice()
    capacites.charger_capacite(capacites.lire_capacite(CAP_ATELIER), mcp, donnees=data, personne=personne)
    return mcp


def _valider(texte: str) -> str:
    from tools import db, runner

    assert texte.startswith("CONFIRMATION_REQUISE:"), texte
    action_id = int(texte.split(":")[1])
    assert runner.executer(action_id) == 0, db.get_action(action_id)
    action = db.get_action(action_id)
    assert action is not None
    return str(action["result"])


def test_demande_reformulee_sans_compte(env: dict[str, Any]) -> None:
    from tools import db

    texte = _atelier(env["data"], "alex").outils["atelier_creer"](
        demande="ajoute le métro de Lyon", site="data.exemple-lyon.fr", titre="Métro de Lyon",
        fabrication="fournisseur_transports", modes="métro, tram", zone="Lyon")
    assert "Ajouter le réseau « Métro de Lyon » (metro, tram) aux transports" in texte
    assert "Métro de Lyon parlera à data.exemple-lyon.fr, en lecture seule" in texte
    action = db.get_action(int(texte.split(":")[1]))
    assert action is not None and action["payload"]["fabrication"] == "fournisseur_transports"
    assert action["payload"]["nom"] == "metro_de_lyon" and action["payload"]["compte"] == "foyer"


@pytest.mark.parametrize(("arguments", "message"), [
    ({"modes": ""}, "quel type de transport"),
    ({"modes": "fusée"}, "Mode « fusée » inconnu"),
    ({"titre": "SNCF"}, "existe déjà"),
    ({"demande": "ajoute l'intranet de mon employeur"}, "espace professionnel"),
])
def test_demande_refusee(env: dict[str, Any], arguments: dict[str, str], message: str) -> None:
    base = {"demande": "ajoute le métro", "site": "data.exemple.fr", "titre": "Métro", "modes": "metro",
            "fabrication": "fournisseur_transports"}
    with pytest.raises(Exception, match=message):
        _atelier(env["data"], "alex").outils["atelier_creer"](**{**base, **arguments})


def test_reserve_aux_proprietaires(env: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="propriétaires"):
        _atelier(env["data"], "camille").outils["atelier_creer"](
            demande="ajoute le métro", site="data.exemple.fr", modes="metro", fabrication="fournisseur_transports")


def test_fabrication_installation_et_usage(env: dict[str, Any], site: str) -> None:
    from capabilities.transports import fournisseurs as fr
    from capabilities.transports import tools as outils
    from tools import atelier, capacites

    texte = _atelier(env["data"], "alex").outils["atelier_creer"](
        demande="ajoute le métro démo", site=site, titre="Métro démo", fabrication="fournisseur_transports",
        modes="metro")
    resultat = _valider(texte)
    assert resultat.startswith("C'est parti : je prépare le réseau « Métro démo »")
    suivi = atelier.lire_suivi("metro_demo")
    assert suivi["statut"] == "pret", (suivi, (atelier.dossier_suivi("metro_demo") / "apprentissages.md").read_text(encoding="utf-8")
                                        if (atelier.dossier_suivi("metro_demo") / "apprentissages.md").exists() else "")
    message, chat = env["messages"][-1]
    assert message == "Réseau « Métro démo » prêt : dis-moi ton trajet (d'où, vers où, à quelle heure) et je le note."
    assert chat == "111"
    # Installé hors du dépôt, découvert comme fournisseur de l'atelier, hôte approuvé pour la capacité transports.
    f = fr.trouver("metro_demo", env["data"])
    assert (f.provenance, f.origine, f.modes) == ("atelier", "assistant", ["metro"])
    assert f.dossier is not None and f.dossier.parent.name == "fournisseurs-atelier"
    etat = capacites.lire_etat(env["data"])["transports"]
    assert etat["reseau_approuve"] == ["127.0.0.1"] and etat["fournisseurs_atelier"]["metro_demo"]["en_cours"] is False
    ctx = type("Ctx", (), {"donnees": env["data"], "personne": "alex", "config": {}})()
    assert "metro_demo" in {x.nom for x in outils.lister_fournisseurs(ctx)}
    assert fr.verifier_source(f, env["data"], ctx)["ok"] is True
    # Les sous-agents : génération puis relecture, sur une copie figée des seuls fichiers d'un fournisseur.
    lignes = [json.loads(x) for x in (env["tmp"] / "faux_claude.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["mode"] for x in lignes] == ["generer", "relire"]
    fige = atelier.dossier_relecture("metro_demo")
    assert sorted(p.name for p in fige.iterdir()) == ["README.md", "fournisseur.yaml", "source.py", "tests"]
    # Retrait : archivé, hôte retiré.
    assert "retiré" in atelier.retirer_fournisseur("metro_demo")
    assert capacites.lire_etat(env["data"])["transports"]["reseau_approuve"] == []
    with pytest.raises(Exception, match="Je ne connais pas le réseau"):
        fr.trouver("metro_demo", env["data"])


def test_controles_d_un_fournisseur(env: dict[str, Any], tmp_path: Path) -> None:
    from tools import atelier

    d = atelier.Demande(nom="metro_x", titre="Métro X", demande="x", site="https://data.exemple.fr", hote="data.exemple.fr",
                        compte="foyer", action="lire", demandeur="alex", prenom="Alex",
                        fabrication="fournisseur_transports", modes=["metro"])
    dossier = tmp_path / "metro_x"
    dossier.mkdir()
    atelier.semer_gabarit(d, dossier)
    _f, erreurs, en_plus = atelier.verifier_fournisseur(dossier, d)
    assert erreurs == [] and en_plus == []
    assert atelier.lancer_ruff(dossier) == []
    source = dossier / "source.py"
    source.write_text("import urllib.request\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    manifeste = dossier / "fournisseur.yaml"
    manifeste.write_text(manifeste.read_text(encoding="utf-8").replace("origine: assistant", "origine: humain")
                         .replace('reseau: ["data.exemple.fr"]', 'reseau: ["data.exemple.fr", "cdn.exemple.fr"]')
                         + "secret: {cle: transports/x, type: cle_api}\n", encoding="utf-8")
    _f, erreurs, en_plus = atelier.verifier_fournisseur(dossier, d)
    texte = "\n".join(erreurs)
    for motif in ("import interdit « urllib.request »", "origine: assistant", "ni identifiant ni clé d'API"):
        assert motif in texte, (motif, erreurs)
    assert en_plus == ["cdn.exemple.fr"]
