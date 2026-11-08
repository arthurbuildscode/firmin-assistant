"""Lot 4b — service de l'appli par FastAPI (build statique, CSP à nonce, PWA), notifications Web Push (abonnements,
envoi des événements spontanés du bus), saisie de la carte identifiants du lot 5c (POST /api/coffre, step-up)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_web import SECRET_TEST, Env, enroler, step_up
from web import app as appmod
from web import push
from web.flux import EvenementBus


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def appli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build factice de l'appli (le vrai est dans web/static/app) : page, ressource, service worker, manifeste."""
    dossier = tmp_path / "app"
    (dossier / "assets").mkdir(parents=True)
    (dossier / "icones").mkdir()
    (dossier / "index.html").write_text(
        '<!doctype html><meta name="csp-nonce" content="__CSP_NONCE__"><title>__ASSISTANT__</title>'
        '<script type="module" src="/assets/index-abc.js"></script>', encoding="utf-8")
    (dossier / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    (dossier / "sw.js").write_text("self.addEventListener('fetch', () => {})", encoding="utf-8")
    (dossier / "manifest.webmanifest").write_text(json.dumps({"name": "x", "short_name": "x", "start_url": "/"}),
                                                  encoding="utf-8")
    (dossier / "icones" / "icone-192.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(appmod, "APPLI", dossier)
    return dossier


# ---------------------------------------------------------------------------
# Service de l'appli
# ---------------------------------------------------------------------------
def test_appli_servie_avec_nonce_par_requete(env: Env, appli: Path) -> None:
    env.cfg.assistant_nom = "Fir<min>"
    client = env.client(csrf=False)
    pages = [client.get(chemin) for chemin in ("/", "/", "/courses", "/inscription")]
    nonces = []
    for r in pages:
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)  # type: ignore[union-attr]
        nonces.append(nonce)
        assert f'content="{nonce}"' in r.text and "__CSP_NONCE__" not in r.text
        assert "unsafe-inline" not in csp and "script-src 'self';" in csp and "worker-src 'self'" in csp
        assert "<title>Fir&lt;min&gt;</title>" in r.text
        assert r.headers["cache-control"] == "no-cache"
    assert len(set(nonces)) == len(nonces)
    assert client.get("/inconnu").status_code == 404
    assert client.get("/api").status_code == 404


def test_ressources_pwa(env: Env, appli: Path) -> None:
    client = env.client(csrf=False)
    r = client.get("/assets/index-abc.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert client.get("/assets/..%2Findex.html").status_code == 404
    assert client.get("/assets/absent.js").status_code == 404
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/" and "javascript" in sw.headers["content-type"]
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["name"] == m.json()["short_name"] == "Firmin"
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert client.get("/icones/icone-192.png").status_code == 200


def test_demo_toujours_accessible(env: Env, appli: Path) -> None:
    page = env.client(csrf=False).get("/demo")
    assert page.status_code == 200 and "démonstration" in page.text
    assert "'sha256-" in page.headers["content-security-policy"]


def test_sans_build_la_demo_tient_lieu_d_appli(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appmod, "APPLI", tmp_path / "absent")
    from fastapi.testclient import TestClient

    from web.app import creer_app

    client = TestClient(creer_app(env.ctx, surveiller=False), base_url="https://testserver")
    assert "démonstration" in client.get("/").text


def test_build_commite_reference_ses_ressources() -> None:
    """Le build commité (web/static/app) est complet : chaque ressource citée par la page existe."""
    racine = Path(__file__).resolve().parents[1] / "web" / "static" / "app"
    if not (racine / "index.html").exists():
        pytest.skip("build absent")
    page = (racine / "index.html").read_text(encoding="utf-8")
    assert "__CSP_NONCE__" in page and "__ASSISTANT__" in page
    for ressource in re.findall(r'(?:src|href)="/(assets/[^"]+)"', page):
        assert (racine / ressource).is_file(), ressource
    for f in ("sw.js", "manifest.webmanifest", "icones/icone-192.png", "icones/icone-512.png", "icones/apple-touch-icon.png"):
        assert (racine / f).is_file(), f
    assert not list(racine.rglob("*.map"))


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------
ABONNEMENT = {"endpoint": "https://push.example.org/abc123", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}


def test_cles_vapid(tmp_path: Path) -> None:
    privee, publique = push.generer_cles()
    assert len(privee) == 43 and len(publique) == 87
    assert push.charger_cles(tmp_path) is None
    (tmp_path / "push.env").write_text(f"VAPID_CLE_PRIVEE={privee}\nVAPID_CLE_PUBLIQUE={publique}\n", encoding="utf-8")
    cles = push.charger_cles(tmp_path)
    assert cles is not None and cles.publique == publique and cles.contact.startswith("mailto:")


def test_abonnement_push(env: Env) -> None:
    client = env.client()
    assert client.get("/api/push/cle").status_code == 401
    enroler(env, client)
    assert client.get("/api/push/cle").status_code == 503  # pas de clés sur ce serveur
    env.app.state.push_cles = lambda: push.ClesVapid("p", "PUBLIQUE", "mailto:x@example.org")
    assert client.get("/api/push/cle").json() == {"cle": "PUBLIQUE", "abonnements": 0}
    assert env.client(csrf=False).post("/api/push/abonnement", json=ABONNEMENT).status_code == 403
    assert client.post("/api/push/abonnement", json={**ABONNEMENT, "endpoint": "http://push.example.org/x"}).status_code == 400
    assert client.post("/api/push/abonnement", json={"endpoint": ABONNEMENT["endpoint"]}).status_code == 400
    assert client.post("/api/push/abonnement", json=ABONNEMENT).json() == {"ok": True}
    assert client.post("/api/push/abonnement", json=ABONNEMENT).json() == {"ok": True}  # même navigateur : remplacé
    assert client.get("/api/push/cle").json()["abonnements"] == 1
    assert client.request("DELETE", "/api/push/abonnement", json={"endpoint": ABONNEMENT["endpoint"]}).json() == {"ok": True}
    assert env.app.state.push_abonnements.nombre("alice") == 0
    assert any(l["kind"] == "web_push" for l in env.journal())


def test_notification_des_evenements_spontanes(env: Env) -> None:
    envois: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    codes = {"https://push.example.org/abc123": 201, "https://push.example.org/perime": 410}

    def envoyer(abonnement: dict[str, Any], donnees: str, cle: str, revendications: dict[str, Any]) -> int:
        envois.append((abonnement["endpoint"], json.loads(donnees), revendications))
        return codes[abonnement["endpoint"]]

    abonnements = push.Abonnements(env.ctx.historique.chemin)
    abonnements.ajouter("alice", ABONNEMENT["endpoint"], "B" * 87, "a" * 22, "Chrome — Android")
    abonnements.ajouter("alice", "https://push.example.org/perime", "B" * 87, "a" * 22, "Firefox — Linux")
    notifieur = push.Notifieur(abonnements, lambda: push.ClesVapid("p", "q", "mailto:x@example.org"), lambda: "Firmin",
                               envoyer=envoyer)

    async def scenario() -> None:
        bus = env.ctx.bus
        bus.observateurs.append(notifieur)
        # Onglet ouvert (abonné au flux) : rien n'est notifié, la conversation l'affiche déjà.
        file = bus.abonner("alice")
        bus.publier("alice", EvenementBus("spontane", {"texte": "Brief prêt"}))
        await asyncio.sleep(0.05)
        assert envois == []
        bus.desabonner("alice", file)
        # Événement d'un échange : jamais notifié.
        bus.publier("alice", EvenementBus("texte", {"delta": "x"}, 3))
        # Message spontané (routine) sans onglet ouvert : notifié à chaque appareil, avec sa notification courte
        # (coeur.spontane), jamais son texte.
        bus.publier("alice", EvenementBus("message", {"role": "assistant", "canal": "routine",
                                                      "texte": "**Bus** : +5 min sur ton trajet, 12,40 € de taxi",
                                                      "meta": {"notification": {"titre": "Bus 7",
                                                                                "corps": "Retard 5 min."},
                                                               "notifier": True}}, 3))
        # Au-delà du quota du jour (notifier faux) : rien.
        bus.publier("alice", EvenementBus("message", {"role": "assistant", "canal": "routine", "texte": "x",
                                                      "meta": {"notifier": False}}, 3))
        await asyncio.sleep(0.1)
        await asyncio.gather(*list(notifieur.taches))

    asyncio.run(scenario())
    assert [e[0] for e in envois] == [ABONNEMENT["endpoint"], "https://push.example.org/perime"]
    assert envois[0][1] == {"titre": "Bus 7", "texte": "Retard 5 min.", "url": "/"}
    assert len(envois) == 2
    assert envois[0][2] == {"sub": "mailto:x@example.org", "aud": "https://push.example.org"}
    assert abonnements.nombre("alice") == 1  # 410 : abonnement périmé retiré


def test_messages_spontanes_surveilles(env: Env) -> None:
    hist = env.ctx.historique
    conv = hist.creer("alice")
    hist.ajouter_message(conv["id"], "assistant", "web", "réponse normale")
    avant = push._dernier_id(hist.chemin)
    hist.ajouter_message(conv["id"], "assistant", "routine", "Brief du jour prêt")
    nouveaux = push._messages_spontanes(hist.chemin, avant)
    assert [(m["texte"], m["linux_user"], m["canal"]) for m in nouveaux] == [("Brief du jour prêt", "alice", "routine")]
    # Message de routine sans notification déclarée : texte générique, jamais le contenu du message.
    assert push.resume_evenement(EvenementBus("message", nouveaux[0])) == {
        "titre": None, "texte": "Nouveau message de ta routine.", "url": "/"}
    assert push._messages_spontanes(hist.chemin, push._dernier_id(hist.chemin)) == []


# ---------------------------------------------------------------------------
# Carte identifiants du lot 5c : POST /api/coffre
# ---------------------------------------------------------------------------
def test_saisie_carte_identifiants(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from tools import coffre

    caplog.set_level(logging.DEBUG)
    coffre.preparer_entree("libre/cantine", {"type": "identifiants", "libelle": "Cantine", "proprietaire": "alice"})
    coffre.preparer_entree("libre/carte", {"type": "carte_bancaire", "libelle": "Carte", "proprietaire": "alice",
                                           "virtuelle": True})
    client = env.client()
    cle = enroler(env, client)
    corps = {"cle": "libre/cantine", "champs": {"identifiant": "parent", "mot_de_passe": SECRET_TEST}}
    r = client.post("/api/coffre", json=corps)
    assert r.status_code == 403 and r.json()["step_up"]
    entete = {"X-Homeos-Step-Up": step_up(client, cle).json()["jeton"]}
    r = client.post("/api/coffre", json=corps, headers=entete)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "champs": ["identifiant", "mot_de_passe"]}
    assert coffre.lire("libre/cantine")["mot_de_passe"] == SECRET_TEST
    # Jeton consommé par l'écriture (usage unique) : nouvelle preuve ; une saisie refusée sans rien écrire le rend.
    assert client.post("/api/coffre", json=corps, headers=entete).json()["step_up"]
    entete = {"X-Homeos-Step-Up": step_up(client, cle).json()["jeton"]}
    # Validation par type : le message cite le champ, jamais la valeur.
    mauvais = client.post("/api/coffre", json={"cle": "libre/carte", "champs": {"numero": "4970101234567890"}}, headers=entete)
    assert mauvais.status_code == 400 and "4970101234567890" not in mauvais.text
    assert client.post("/api/coffre", json={"cle": "libre/inconnue", "champs": {"code": "1"}}, headers=entete).status_code == 403
    assert client.post("/api/coffre", json={"cle": "libre/cantine", "champs": {"code": 12}}, headers=entete).status_code == 400
    assert client.post("/api/coffre", json={"champs": {"code": "1"}}, headers=entete).status_code == 400
    # Une autre personne ne peut pas écrire dans l'entrée d'Alice.
    autre = env.client()
    cle_bruno = enroler(env, autre, "bruno")
    entete_bruno = {"X-Homeos-Step-Up": step_up(autre, cle_bruno).json()["jeton"]}
    assert autre.post("/api/coffre", json=corps, headers=entete_bruno).status_code == 403
    assert all(SECRET_TEST not in json.dumps(l, default=str) for l in env.journal())
    assert SECRET_TEST not in caplog.text and "4970101234567890" not in caplog.text


def test_carte_identifiants_du_lot_5c_acceptee_par_le_coeur() -> None:
    from coeur import cartes

    carte = {"type": "identifiants", "id": "c-1", "titre": "Cantine",
             "donnees": {"cle": "libre/cantine", "type": "identifiants", "champs": ["identifiant", "mot_de_passe"]},
             "actions": [{"libelle": "Enregistrer", "action": "coffre_ecrire", "args": {"cle": "libre/cantine"},
                          "confirmation": "step-up"}]}
    assert cartes.erreurs_carte(carte) == []
    assert cartes.erreurs_carte({**carte, "donnees": {"type": "identifiants"}})
    assert cartes.erreurs_carte({**carte, "actions": [{**carte["actions"][0], "confirmation": "autre"}]})
    assert cartes.erreurs_carte({**carte, "actions": [{**carte["actions"][0], "confirmation": True}]})
