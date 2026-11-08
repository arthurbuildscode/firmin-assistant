"""Points d'accès directs des écrans (web/ecrans.py) : courses, échéances, journal, fichiers, mémoire, routines —
mêmes fonctions que les outils, droits Famille / personnel / privé, journal actions_log avec who, événements
`maj` sur le bus de la personne et de l'autre adulte, aucune fuite entre personnes."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_web import CONFIG_RAW, Env, enroler


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    # HOMEOS_WHO (CLI) primerait sur l'identité de la requête : certains exécuteurs le posent dans os.environ.
    monkeypatch.delenv("HOMEOS_WHO", raising=False)
    return Env(tmp_path, monkeypatch)


def deux_clients(env: Env) -> tuple[Any, Any]:
    alice, bruno = env.client(), env.client()
    enroler(env, alice, "alice")
    enroler(env, bruno, "bruno")
    return alice, bruno


def ecouter(env: Env, *personnes: str) -> dict[str, asyncio.Queue[Any]]:
    return {p: env.ctx.bus.abonner(p) for p in personnes}


def vider(file: asyncio.Queue[Any]) -> list[Any]:
    sortie = []
    while not file.empty():
        sortie.append(file.get_nowait())
    return sortie


def majs(file: asyncio.Queue[Any]) -> list[dict[str, Any]]:
    return [e.donnees for e in vider(file) if e.type == "maj"]


def jpeg() -> bytes:
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 40)).save(tampon, "JPEG")
    return tampon.getvalue()


# --- courses -----------------------------------------------------------------------------------------------------


def test_courses_cocher_sur_place_et_diffusion(env: Env) -> None:
    alice, bruno = deux_clients(env)
    files = ecouter(env, "alice", "bruno")
    r = alice.post("/api/courses", json={"articles": ["lait", "pommes", "lessive", "lait"]})
    assert r.status_code == 200, r.text
    assert [a["statut"] for a in r.json()["ajoutes"]] == ["ajouté", "ajouté", "ajouté", "déjà présent"]
    ordre = [a["article"] for a in bruno.get("/api/courses").json()["articles"]]
    assert ordre == ["pommes", "lait", "lessive"]  # rayon puis nom ; Bruno voit la liste Famille
    assert all(a["ajoute_par"] == "Alice" for a in bruno.get("/api/courses").json()["articles"])
    lait = next(a for a in r.json()["articles"] if a["article"] == "lait")
    r = bruno.patch(f"/api/courses/{lait['id']}", json={"coche": True})
    assert r.status_code == 200 and r.json()["article"]["coche"] is True
    etat = alice.get("/api/courses").json()
    assert [a["article"] for a in etat["articles"]] == ordre  # rien ne bouge sous le doigt
    assert etat["restants"] == 2
    assert bruno.patch(f"/api/courses/{lait['id']}", json={"coche": False}).json()["article"]["coche"] is False
    assert bruno.patch("/api/courses/9999", json={"coche": True}).status_code == 404
    assert bruno.patch(f"/api/courses/{lait['id']}", json={"coche": "oui"}).status_code == 400
    bruno.patch(f"/api/courses/{lait['id']}", json={"coche": True})
    r = alice.delete("/api/courses/coches")
    assert r.json()["supprimes"] == 1 and [a["article"] for a in r.json()["articles"]] == ["pommes", "lessive"]
    # Chaque écriture : événement `maj` sans contenu pour les deux adultes.
    for p in ("alice", "bruno"):
        evs = majs(files[p])
        assert [e["action"] for e in evs] == ["ajouter", "cocher", "cocher", "cocher", "vider_coches"]
        assert "lait" not in json.dumps(evs)
    journal = [(l["who"], l["kind"]) for l in env.journal() if l["kind"].startswith("courses_")]
    assert ("Alice", "courses_ajouter") in journal and ("Bruno", "courses_cocher") in journal
    assert all(json.loads(l["payload"])["canal"] == "appli" for l in env.journal() if l["kind"] == "courses_ajouter")
    assert alice.post("/api/courses", json={"articles": []}).status_code == 400
    assert env.client(csrf=False).post("/api/courses", json={"articles": ["x"]}).status_code == 403


# --- échéances ---------------------------------------------------------------------------------------------------


def test_echeances(env: Env) -> None:
    alice, bruno = deux_clients(env)
    r = alice.post("/api/echeances", json={"titre": "Contrôle technique", "date": "2027-03-01",
                                           "rappels_jours": [30, 7], "recurrence": "annuelle", "notes": "garage"})
    assert r.status_code == 200, r.text
    e = r.json()["echeance"]
    assert e["cree_par"] == "Alice" and e["rappels_jours"] == [30, 7]
    assert [x["titre"] for x in bruno.get("/api/echeances?horizon=3650").json()["echeances"]] == ["Contrôle technique"]
    assert alice.post("/api/echeances", json={"titre": "X", "date": "pas une date"}).status_code == 400
    assert alice.post("/api/echeances", json={"titre": "X", "date": "2027-01-01", "rappels_jours": [-1]}).status_code == 400
    assert alice.get("/api/echeances?horizon=0").status_code == 400
    assert bruno.delete(f"/api/echeances/{e['id']}").json() == {"ok": True}
    assert bruno.delete(f"/api/echeances/{e['id']}").status_code == 404


# --- journal -------------------------------------------------------------------------------------------------------


def test_journal_ligne_et_photo(env: Env) -> None:
    from datetime import date

    alice, bruno = deux_clients(env)
    files = ecouter(env, "bruno")
    assert alice.post("/api/journal", json={"texte": "Premier bain de la saison"}).status_code == 200
    mois = date.today().strftime("%Y-%m")
    r = bruno.get(f"/api/journal?mois={mois}").json()
    assert r["entrees"][-1]["texte"] == "Premier bain de la saison" and r["entrees"][-1]["qui"] == "Alice"
    assert mois in r["mois_disponibles"]
    assert bruno.get("/api/journal?mois=2026-13").status_code == 400
    # Photo : pièce téléversée par Alice, inutilisable par Bruno.
    piece = alice.post("/api/fichiers", files={"fichier": ("plage.jpg", jpeg(), "image/jpeg")}).json()
    assert bruno.post("/api/journal", json={"texte": "Volée", "piece": piece["id"]}).status_code == 404
    r = alice.post("/api/journal", json={"texte": "La plage", "piece": piece["id"], "date": "2026-08-14"})
    assert r.status_code == 200, r.text
    assert r.json()["mois"] == "2026-08" and r.json()["url"].startswith("/api/photos/2026/08/")
    entree = bruno.get("/api/journal?mois=2026-08").json()["entrees"][-1]
    assert entree["texte"] == "La plage" and entree["url"] == r.json()["url"] and "photo" not in entree
    assert bruno.get(entree["url"]).headers["content-type"] == "image/jpeg"
    assert bruno.get("/api/photos/2026/08/..%2F..%2Fconfig.yaml").status_code == 404
    assert [e["action"] for e in majs(files["bruno"])] == ["ajouter", "photo"]


# --- fichiers ------------------------------------------------------------------------------------------------------


def test_fichiers_ranges_et_entrants_personnels(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import fichiers

    alice, bruno = deux_clients(env)
    entrant = alice.post("/api/fichiers", files={"fichier": ("Facture garage.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert entrant.status_code == 200
    # Entrants : ceux d'Alice pour Alice, rien pour Bruno, jamais de chemin du serveur.
    (e,) = alice.get("/api/fichiers/entrants").json()["entrants"]
    assert "chemin" not in e and e["nom"].endswith("_alice_facture-garage.pdf")
    assert alice.get(e["url"]).content == b"%PDF-1.4 x"
    assert bruno.get("/api/fichiers/entrants").json()["entrants"] == []
    assert bruno.get(e["url"]).status_code == 404
    # Rangé (Famille) : visible des deux, ouvrable par son url.
    source = next((env.data / "fichiers" / "entrants").iterdir())
    fichiers.ranger(str(source), "voiture", "facture-garage", "Vidange")
    (f,) = bruno.get("/api/fichiers?categorie=voiture").json()["fichiers"]
    assert "chemin" not in f and f["url"].startswith("/api/fichiers/voiture/")
    assert bruno.get(f["url"]).content == b"%PDF-1.4 x"
    for url in ("/api/fichiers/voiture/2026/..%2F..%2Fconfig.yaml", "/api/fichiers/inconnue/2026/x.pdf",
                "/api/fichiers/voiture/26/facture-garage.pdf"):
        assert bruno.get(url).status_code == 404, url
    assert bruno.get("/api/fichiers?categorie=nimporte").status_code == 400


# --- mémoire -------------------------------------------------------------------------------------------------------


def preparer_memoire(env: Env) -> None:
    for rel, texte in {
        "foyer/contacts.md": "# Contacts\n\n- Plombier\n",
        "personnes/alice.md": "# Alice\n\n- aime le thé\n",
        "personnes/bruno.md": "# Bruno\n\n- secret de Bruno\n",
        "personnes/enfants.md": "# Les enfants\n",
        "alimentation/bruno.md": "# Alimentation — Bruno\n",
        "personnes/alice/prive/notes.md": "# Notes privées\n",
    }.items():
        chemin = env.data / rel
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(texte, encoding="utf-8")


def test_memoire_famille_personnel_prive(env: Env) -> None:
    preparer_memoire(env)
    alice, bruno = deux_clients(env)
    fiches = {f["chemin"]: f["niveau"] for f in alice.get("/api/memoire").json()["fiches"]}
    assert fiches == {"foyer/contacts.md": "famille", "personnes/alice.md": "personnel",
                      "personnes/enfants.md": "famille"}
    assert "personnes/bruno.md" not in json.dumps(alice.get("/api/memoire").json())
    for chemin in ("personnes/bruno.md", "alimentation/bruno.md", "personnes/alice/prive/notes.md"):
        assert alice.get(f"/api/memoire/fiche?chemin={chemin}").status_code == 403, chemin
    assert bruno.get("/api/memoire/fiche?chemin=personnes/alice.md").status_code == 403
    assert alice.get("/api/memoire/fiche?chemin=../config.yaml").status_code == 400
    assert alice.get("/api/memoire/fiche?chemin=foyer/absent.md").status_code == 404
    # Écriture avec contrôle de version : un écrasement concurrent est refusé.
    files = ecouter(env, "alice", "bruno")
    lu = bruno.get("/api/memoire/fiche?chemin=foyer/contacts.md").json()
    assert lu["niveau"] == "famille" and "Plombier" in lu["contenu"]
    assert alice.post("/api/memoire/fiche/ligne", json={"chemin": "foyer/contacts.md", "ligne": "- Électricien"}).status_code == 200
    r = bruno.put("/api/memoire/fiche", json={"chemin": "foyer/contacts.md", "contenu": "# Contacts\n", "version": lu["version"]})
    assert r.status_code == 409
    relu = bruno.get("/api/memoire/fiche?chemin=foyer/contacts.md").json()
    r = bruno.put("/api/memoire/fiche", json={"chemin": "foyer/contacts.md", "contenu": relu["contenu"] + "- Vitrier\n",
                                              "version": relu["version"]})
    assert r.status_code == 200 and r.json()["version"] != relu["version"]
    assert "Vitrier" in (env.data / "foyer" / "contacts.md").read_text(encoding="utf-8")
    # Fiche personnelle : l'événement ne va qu'à sa propriétaire.
    assert alice.put("/api/memoire/fiche", json={"chemin": "personnes/alice.md", "contenu": "# Alice\n- café\n"}).status_code == 200
    assert bruno.put("/api/memoire/fiche", json={"chemin": "personnes/alice.md", "contenu": "piraté"}).status_code == 403
    assert "café" in (env.data / "personnes" / "alice.md").read_text(encoding="utf-8")
    assert [e["action"] for e in majs(files["bruno"])] == ["ajouter", "ecrire"]
    assert [e["action"] for e in majs(files["alice"])] == ["ajouter", "ecrire", "ecrire"]
    kinds = {(l["who"], l["kind"]) for l in env.journal()}
    assert {("Alice", "memoire_ajouter"), ("Bruno", "memoire_ecrire"), ("Alice", "memoire_ecrire")} <= kinds


def test_outil_memoire_refuse_le_prive_de_l_autre(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import memoire

    preparer_memoire(env)
    with pytest.raises(memoire.CheminInterdit):
        memoire.verifier_prive("personnes/alice/prive/notes.md", "bruno")
    memoire.verifier_prive("personnes/alice/prive/notes.md", "alice")
    memoire.verifier_prive("foyer/contacts.md", "bruno")


def test_invite_lit_mais_n_ecrit_pas(env: Env) -> None:
    raw = json.loads(json.dumps(CONFIG_RAW))
    raw["personnes"].append({"telegram_id": 303, "prenom": "Claire", "linux_user": "claire", "role": "invite"})
    (env.data / "config.yaml").write_text(json.dumps(raw), encoding="utf-8")
    invite = env.client()
    enroler(env, invite, "claire")
    assert invite.get("/api/courses").status_code == 200
    assert invite.post("/api/courses", json={"articles": ["chips"]}).status_code == 403
    assert invite.post("/api/journal", json={"texte": "x"}).status_code == 403
    assert invite.get("/api/memoire").json()["modifiable"] is False


def test_routines_dernier_passage(env: Env) -> None:
    alice, _ = deux_clients(env)
    env.db.log("cron", "cron_demo_rafraichir", "Démo : rafraichir", None, "done", "texte produit")
    routines = alice.get("/api/routines").json()["routines"]
    demo = next(r for r in routines if r["capacite"] == "demo")
    assert demo["dernier"]["statut"] == "done" and "texte" not in json.dumps(demo["dernier"])


def test_session_de_secours_lit_mais_n_ecrit_pas(env: Env) -> None:
    from tests.test_web_securite import _connexion_code

    client = env.client()
    assert _connexion_code(env, client).json()["niveau"] == "secours"
    assert client.get("/api/courses").status_code == 200
    assert client.get("/api/memoire").json()["modifiable"] is False
    for r in (client.post("/api/courses", json={"articles": ["pain"]}),
              client.post("/api/journal", json={"texte": "x"}),
              client.post("/api/echeances", json={"titre": "x", "date": "2027-01-01"}),
              client.get("/api/sensibles")):
        assert r.status_code == 403 and r.json()["secours"] is True, r.text
