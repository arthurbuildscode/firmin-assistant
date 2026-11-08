"""Lot 4a — appli web : passkeys (authentificateur logiciel), step-up, code de secours, SSE complet avec un faux
`claude` (étapes, texte, carte, confirmation, fin avec modèle), validation d'une confirmation, fichiers, coffre,
brief du jour, routines, partage de la conversation avec Telegram, garde-fous (CSRF, tailles, journaux)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from bot.config import Config, parse_config
from bot.db import STATUS_PENDING, Db
from bot.sessions import Sessions
from coeur.claude import ClaudeRunner
from coeur.historique import Historique
from web import config as configweb
from web.app import creer_app
from web.contexte import ContexteWeb
from web.stockage import StockageAuth

ORIGINE = "https://testserver"
CONFIG_RAW: dict[str, Any] = {
    "assistant": {"nom": "Firmin"},
    "personnes": [
        {"telegram_id": 101, "prenom": "Alice", "linux_user": "alice"},
        {"telegram_id": 202, "prenom": "Bruno", "linux_user": "bruno", "role": "membre"},
    ],
}
SECRET_TEST = "valeur-ultra-secrete-42"


def b64(octets: bytes) -> str:
    return base64.urlsafe_b64encode(octets).rstrip(b"=").decode()


def d64(texte: str) -> bytes:
    return base64.urlsafe_b64decode(texte + "=" * (-len(texte) % 4))


class Authentificateur:
    """Authentificateur WebAuthn logiciel (ES256, attestation « none ») pour les tests."""

    def __init__(self, rp_id: str = "testserver", origine: str = ORIGINE) -> None:
        self.rp_id = rp_id
        self.origine = origine
        self.cle = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.compteur = 0
        self.user_handle: str | None = None

    def _cose(self) -> bytes:
        n = self.cle.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client(self, type_: str, challenge: str) -> bytes:
        return json.dumps({"type": type_, "challenge": challenge, "origin": self.origine, "crossOrigin": False}).encode()

    def creer(self, options: dict[str, Any]) -> dict[str, Any]:
        assert options["rp"]["id"] == self.rp_id
        client = self._client("webauthn.create", options["challenge"])
        auth = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + struct.pack(">I", 0) + b"\0" * 16
                + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        self.user_handle = options["user"]["id"]
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation),
                             "transports": ["internal"]}}

    def assertion(self, options: dict[str, Any], uv: bool = True) -> dict[str, Any]:
        self.compteur += 1
        client = self._client("webauthn.get", options["challenge"])
        flags = 0x01 | (0x04 if uv else 0)
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.compteur)
        signature = self.cle.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(signature),
                             "userHandle": self.user_handle}}


# ---------------------------------------------------------------------------
# Faux claude (stream-json) et contexte
# ---------------------------------------------------------------------------
def ecrire_faux_claude(dossier: Path, action_id: int) -> Path:
    """Rejoue un échange : outil agenda_lire (avec une carte CARTE: dans son résultat), texte en deltas, puis un
    résultat final qui demande une confirmation. Écrit ses arguments dans <dossier>/args-<n>."""
    carte = {"type": "agenda_jour", "id": "c-agenda1", "titre": "Samedi", "donnees": {"date": "2026-11-07",
             "evenements": [{"titre": "Piscine", "debut": "2026-11-07T10:00"}]}, "actions": []}
    resultat_outil = "[{\"titre\": \"Piscine\"}]\nCARTE:" + json.dumps(carte, separators=(",", ":"))
    final = f"Samedi : piscine à 10 h. Je l'ajoute ?\nCONFIRMATION_REQUISE:{action_id}:Ajouter « Piscine » samedi 10 h"
    lignes = [
        {"type": "system", "subtype": "init", "session_id": "sess-web-1"},
        {"type": "assistant", "session_id": "sess-web-1", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__homeos__agenda_lire", "input": {"debut": "2026-11-07"}}]}},
        {"type": "user", "session_id": "sess-web-1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": resultat_outil}]}]}},
        {"type": "stream_event", "session_id": "sess-web-1", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "Samedi : piscine "}}},
        {"type": "stream_event", "session_id": "sess-web-1", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "à 10 h."}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": final, "session_id": "sess-web-1",
         "total_cost_usd": 0.0123, "duration_ms": 850, "usage": {"input_tokens": 10, "output_tokens": 20}},
    ]
    corps = "\n".join(json.dumps(x, ensure_ascii=False) for x in lignes).replace("'", "'\\''")
    script = dossier / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(ls '{dossier}' | grep -c '^args-')\n"
        f"printf '%s\\n' \"$@\" > '{dossier}'/args-$n\n"
        f"printf '%s\\n' '{corps}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class Env:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOMEOS_ETAT", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("HOMEOS_NO_SUDO", "1")
        monkeypatch.setenv("HOMEOS_SECRETS", str(tmp / "secrets"))
        monkeypatch.setenv("HOMEOS_FICHIER_WEB", str(tmp / "absent"))
        monkeypatch.setenv("COFFRE_CLE", "c" * 64)
        self.data = tmp / "data"
        self.data.mkdir()
        monkeypatch.setenv("HOMEOS_DATA", str(self.data))
        root = tmp / "repo"
        (root / "content").mkdir(parents=True)
        (root / "tools").mkdir()
        cap = root / "capabilities" / "demo"
        cap.mkdir(parents=True)
        (cap / "capability.yaml").write_text(
            "nom: demo\ntitre: Démo\ndescription: Service de démonstration.\nversion: 1\nstatut: actif\n"
            "origine: humain\nsecrets:\n  - cle: demo/alice\n    champs: [identifiant, mot_de_passe]\n"
            "    par_personne: true\ncrons:\n  - nom: rafraichir\n    quand: \"Mon 04:00\"\n"
            "actions_avec_effet: [demo_reserver]\nconfirmation_toujours: [demo_reserver]\n",
            encoding="utf-8",
        )
        (cap / "tools.py").write_text("def enregistrer(mcp, ctx):\n    pass\n", encoding="utf-8")
        (cap / "smoke.py").write_text("def verifier(ctx):\n    return {'ok': True, 'message': 'ok'}\n", encoding="utf-8")
        (cap / "crons.py").write_text("def executer(nom, ctx):\n    return None\n", encoding="utf-8")
        (cap / "CLAUDE.md").write_text("Démo.\n", encoding="utf-8")
        (self.data / "config.yaml").write_text(json.dumps(CONFIG_RAW), encoding="utf-8")
        monkeypatch.setenv("HOMEOS_ROOT", str(root))
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.cfg: Config = parse_config(json.loads(json.dumps(CONFIG_RAW)), self.data, root)
        self.cfg.empreinte = None
        from bot.config import empreinte_fichier

        self.cfg.empreinte = empreinte_fichier(self.cfg.config_path)
        self.db = Db(self.cfg.db_path)
        self.db.init()
        self.action_id = self.db.log("Alice", "agenda_ajouter", "Ajouter « Piscine »", {"titre": "Piscine"},
                                     STATUS_PENDING)
        ecrire_faux_claude(self.bin, self.action_id)
        monkeypatch.setenv("PATH", f"{self.bin}:/usr/bin:/bin")
        self.telegram: list[tuple[int, str]] = []
        self.executions: list[int] = []
        captures = self.cfg.kd_captures_dir
        captures.mkdir(parents=True)
        (captures / "selection.png").write_bytes(b"\x89PNG\r\n\x1a\nfaux")

        async def executer(cfg: Config, action_id: int) -> tuple[bool, str]:
            self.executions.append(action_id)
            return True, f"Événement ajouté.\nCAPTURE:{captures / 'selection.png'}"

        async def classer(texte: str) -> str:
            return "decision"

        appli = self.cfg.etat_dir / "appli.sqlite"
        self.ctx = ContexteWeb(
            cfg=self.cfg,
            reglages=configweb.ReglagesWeb(origine=ORIGINE),
            db=self.db,
            sessions=Sessions(self.cfg.sessions_path),
            runner=ClaudeRunner(self.cfg),
            historique=Historique(appli),
            auth=StockageAuth(appli),
            executer=executer,
            envoyer_telegram=lambda chat_id, texte: self.telegram.append((chat_id, texte)) or True,
            classifieur=classer,
        )
        self.app = creer_app(self.ctx)

    def client(self, csrf: bool = True) -> TestClient:
        return TestClient(self.app, base_url=ORIGINE, headers={"X-Requested-With": "homeos"} if csrf else {})

    def args(self, n: int) -> list[str]:
        return (self.bin / f"args-{n}").read_text(encoding="utf-8").splitlines()

    def journal(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM actions_log ORDER BY id")]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    yield Env(tmp_path, monkeypatch)


def enroler(env: Env, client: TestClient, linux_user: str = "alice") -> Authentificateur:
    jeton = env.ctx.auth.creer_invitation(linux_user, timedelta(minutes=10))
    r = client.post("/auth/inscription/options", json={"jeton": jeton})
    assert r.status_code == 200, r.text
    cle = Authentificateur()
    r = client.post("/auth/inscription/verification",
                    json={"ceremonie": r.json()["ceremonie"], "credential": cle.creer(r.json()["options"]), "nom": "Test"})
    assert r.status_code == 200, r.text
    return cle


def step_up(client: TestClient, cle: Authentificateur, uv: bool = True) -> Any:
    r = client.post("/auth/step-up/options", json={})
    assert r.status_code == 200, r.text
    assert r.json()["options"]["userVerification"] == "required"
    return client.post("/auth/step-up", json={"ceremonie": r.json()["ceremonie"],
                                               "credential": cle.assertion(r.json()["options"], uv=uv)})


def lire_sse(reponse: Any) -> list[tuple[str, Any]]:
    evenements: list[tuple[str, Any]] = []
    type_, data = "message", ""
    for ligne in reponse.iter_lines():
        if ligne.startswith("event:"):
            type_ = ligne[6:].strip()
        elif ligne.startswith("data:"):
            data += ligne[5:].strip()
        elif ligne == "" and data:
            evenements.append((type_, json.loads(data)))
            type_, data = "message", ""
    return evenements


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------
def test_enrolement_connexion_et_session(env: Env) -> None:
    client = env.client()
    assert client.get("/auth/session").json() == {"connecte": False}
    assert client.get("/api/conversations").status_code == 401
    cle = enroler(env, client)
    s = client.get("/auth/session").json()
    assert s["connecte"] and s["personne"]["prenom"] == "Alice" and s["passkeys"] == 1
    cookie = next(c for c in client.cookies.jar if c.name == "__Host-homeos_session")
    assert cookie.secure and cookie.path == "/"
    # Session stockée par empreinte seulement.
    with env.ctx.auth.connexion() as c:
        stockes = [r[0] for r in c.execute("SELECT empreinte FROM web_sessions")]
    assert cookie.value not in stockes
    # Nouvelle connexion (autre navigateur) avec la passkey découvrable.
    autre = env.client()
    r = autre.post("/auth/connexion/options", json={})
    assert r.json()["options"].get("allowCredentials", []) == []
    r = autre.post("/auth/connexion/verification",
                   json={"ceremonie": r.json()["ceremonie"], "credential": cle.assertion(r.json()["options"], uv=False)})
    assert r.status_code == 200 and r.json()["personne"]["linux_user"] == "alice"
    # Challenge à usage unique : rejouer la même cérémonie échoue.
    r2 = autre.post("/auth/connexion/options", json={})
    assertion = cle.assertion(r2.json()["options"])
    assert autre.post("/auth/connexion/verification", json={"ceremonie": r2.json()["ceremonie"],
                                                            "credential": assertion}).status_code == 200
    assert autre.post("/auth/connexion/verification", json={"ceremonie": r2.json()["ceremonie"],
                                                            "credential": assertion}).status_code == 400
    # Appareils : deux sessions, révocation de l'autre, déconnexion.
    appareils = client.get("/api/appareils").json()
    assert len(appareils["sessions"]) == 3 and len(appareils["passkeys"]) == 1
    autre_id = next(s["id"] for s in appareils["sessions"] if not s["courante"])
    assert client.delete(f"/api/appareils/sessions/{autre_id}").json()["ok"]
    assert client.post("/auth/deconnexion", json={}).json()["ok"]
    assert client.get("/api/conversations").status_code == 401
    kinds = [l["kind"] for l in env.journal()]
    assert "web_connexion" in kinds and "web_passkey" in kinds


def test_invitation_usage_unique_et_expiree(env: Env) -> None:
    client = env.client()
    jeton = env.ctx.auth.creer_invitation("alice", timedelta(minutes=10))
    r = client.post("/auth/inscription/options", json={"jeton": jeton})
    cle = Authentificateur()
    ok = client.post("/auth/inscription/verification",
                     json={"ceremonie": r.json()["ceremonie"], "credential": cle.creer(r.json()["options"])})
    assert ok.status_code == 200
    assert env.client().post("/auth/inscription/options", json={"jeton": jeton}).status_code == 400
    vieux = env.ctx.auth.creer_invitation("alice", timedelta(seconds=-1))
    assert env.client().post("/auth/inscription/options", json={"jeton": vieux}).status_code == 400
    assert env.client().post("/auth/inscription/options", json={"jeton": "inventé"}).status_code == 400
    # Mauvaise origine dans clientDataJSON : refus.
    jeton2 = env.ctx.auth.creer_invitation("bruno", timedelta(minutes=10))
    r = env.client().post("/auth/inscription/options", json={"jeton": jeton2})
    pirate = Authentificateur(origine="https://ailleurs.example")
    c2 = env.client()
    assert c2.post("/auth/inscription/verification", json={"ceremonie": r.json()["ceremonie"],
                                                          "credential": pirate.creer(r.json()["options"])}).status_code == 400


def test_step_up_et_ajout_appareil(env: Env) -> None:
    client = env.client()
    cle = enroler(env, client)
    # Ajouter une passkey depuis la session : step-up exigé (la personne en a déjà une).
    r = client.post("/auth/inscription/options", json={})
    assert r.status_code == 403 and r.json()["step_up"] is True
    # Assertion sans vérification de l'utilisateur : refusée pour un step-up.
    assert step_up(client, cle, uv=False).status_code == 400
    r = step_up(client, cle)
    assert r.status_code == 200
    jeton = r.json()["jeton"]
    entete = {"X-Homeos-Step-Up": jeton}
    r = client.post("/auth/inscription/options", json={}, headers=entete)
    assert r.status_code == 200
    seconde = Authentificateur()
    r = client.post("/auth/inscription/verification",
                    json={"ceremonie": r.json()["ceremonie"], "credential": seconde.creer(r.json()["options"])})
    assert r.status_code == 200
    assert len(env.ctx.auth.passkeys("alice")) == 2
    # Le jeton de step-up est lié à la session : inutilisable depuis une autre session.
    autre = env.client()
    r = autre.post("/auth/connexion/options", json={})
    autre.post("/auth/connexion/verification", json={"ceremonie": r.json()["ceremonie"],
                                                     "credential": seconde.assertion(r.json()["options"])})
    assert autre.delete("/api/appareils/passkeys/1", headers=entete).status_code == 403


def test_code_de_secours_telegram(env: Env) -> None:
    client = env.client()
    r = client.post("/auth/code/demande", json={"personne": "Alice"})
    assert r.status_code == 200 and env.telegram and env.telegram[0][0] == 101
    code = env.telegram[0][1].split(" : ")[1].split()[0]
    assert len(code) == 6
    # Personne inconnue : même réponse, aucun envoi.
    assert client.post("/auth/code/demande", json={"personne": "Inconnu"}).json()["ok"]
    assert len(env.telegram) == 1
    assert client.post("/auth/code/verification", json={"personne": "alice", "code": "000000" if code != "000000"
                                                        else "111111"}).status_code == 400
    assert client.post("/auth/code/verification", json={"personne": "alice", "code": code}).status_code == 200
    assert client.get("/auth/session").json()["connecte"]
    # Le code ne sert qu'une fois, et n'est jamais journalisé.
    assert env.client().post("/auth/code/verification", json={"personne": "alice", "code": code}).status_code == 400
    assert all(code not in json.dumps(l, default=str) for l in env.journal())


def test_csrf_et_limites(env: Env) -> None:
    sans = env.client(csrf=False)
    assert sans.post("/auth/connexion/options", json={}).status_code == 403
    avec = env.client()
    assert avec.post("/auth/connexion/options", json={}, headers={"Origin": "https://ailleurs.example"}).status_code == 403
    assert avec.post("/auth/connexion/options", json={}, headers={"Origin": ORIGINE}).status_code == 200
    gros = "x" * (configweb.LIMITE_JSON + 10)
    assert avec.post("/auth/code/demande", content=gros, headers={"Content-Type": "application/json"}).status_code == 413
    r = avec.get("/api/sante")
    assert r.json() == {"ok": True} and r.headers["x-frame-options"] == "DENY"
    page = avec.get("/demo")
    assert page.status_code == 200 and "'sha256-" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# Conversation : SSE complet avec un faux claude
# ---------------------------------------------------------------------------
def test_sse_complet_et_confirmation(env: Env) -> None:
    client = env.client()
    enroler(env, client)
    conv = client.post("/api/conversations").json()["conversation"]
    with client.stream("POST", f"/api/conversations/{conv['id']}/messages",
                       json={"texte": "Qu'est-ce qu'on fait samedi, raconte-moi tout ?",
                             "contexte": {"ecran": "agenda", "objet": "samedi"}}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        evs = lire_sse(r)
    types = [t for t, _ in evs]
    assert types[0] == "debut" and types[-1] == "fin"
    # Cartes du jour au premier message du jour (bonjour, à noter), puis carte de l'outil.
    cartes = [d for t, d in evs if t == "carte"]
    assert [c["type"] for c in cartes][:1] == ["bonjour"]
    assert any(c["type"] == "a_noter" for c in cartes)
    agenda = next(c for c in cartes if c["type"] == "agenda_jour")
    assert agenda["id"] == "c-agenda1" and agenda["donnees"]["evenements"][0]["titre"] == "Piscine"
    etape = next(d for t, d in evs if t == "etape")
    assert etape == {**etape, "etape": "Je lis l'agenda…", "outil": "agenda_lire"}
    assert "".join(d["delta"] for t, d in evs if t == "texte") == "Samedi : piscine à 10 h."
    conf = next(d for t, d in evs if t == "confirmation")
    assert conf == {"id": env.action_id, "resume": "Ajouter « Piscine » samedi 10 h", "auto": False, "step_up": False}
    fin = evs[-1][1]
    assert fin["modele"] == "opus" and fin["niveau"] == "decision"  # classifieur de test : « decision »
    assert fin["cout_usd"] == 0.0123 and fin["duree_ms"] == 850
    assert "CONFIRMATION_REQUISE" not in fin["texte"] and "CARTE:" not in fin["texte"]
    assert fin["etapes"][0]["outil"] == "agenda_lire" and "duree_ms" in fin["etapes"][0]
    assert fin["confirmations"] == [{"id": env.action_id, "resume": "Ajouter « Piscine » samedi 10 h"}]
    # Commande claude : canal web, modèle routé, ligne de contexte et écran de l'appli dans le prompt.
    args = env.args(0)
    assert args[args.index("--model") + 1] == "opus"
    prompt = args[args.index("-p") + 1:args.index("--output-format")]
    assert prompt[0].startswith("[contexte: Alice écrit dans l'appli,")
    assert prompt[1] == "[écran de l'appli : agenda ; objet : samedi]"
    # Session Claude partagée (sessions.json, clé telegram_id) et journal avec le modèle.
    assert env.ctx.sessions.get(101) == "sess-web-1"
    sortant = [l for l in env.journal() if l["kind"] == "message_sortant"][-1]
    assert json.loads(sortant["payload"])["modele"] == "opus" and json.loads(sortant["payload"])["canal"] == "web"
    assert sortant["who"] == "Alice"

    # Historique : message de la personne et réponse avec ses cartes.
    msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["personne", "assistant"]
    assert any(c["type"] == "agenda_jour" for c in msgs[1]["cartes"]) and msgs[1]["meta"]["modele"] == "opus"

    # Validation de la confirmation : même chemin que Telegram, capture rendue en carte servie par l'appli.
    r = client.post(f"/api/confirmations/{env.action_id}", json={"decision": "valider"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["texte"].startswith("✅ Validé par Alice.") and env.executions == [env.action_id]
    assert corps["cartes"][0]["type"] == "capture"
    assert client.get(corps["cartes"][0]["donnees"]["url"]).status_code == 200
    assert client.get("/api/captures/..%2Fconfig.yaml").status_code == 404
    # Déjà traitée.
    r = client.post(f"/api/confirmations/{env.action_id}", json={"decision": "valider"})
    assert "déjà traitée" in r.json()["texte"]
    msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert msgs[-1]["role"] == "systeme" and msgs[-1]["meta"]["confirmation"] == env.action_id

    # Deuxième message : session reprise (--resume), règle « confirmation » → haiku.
    with client.stream("POST", f"/api/conversations/{conv['id']}/messages", json={"texte": "ok merci"}) as r:
        evs2 = lire_sse(r)
    assert not any(t == "carte" and d["type"] == "bonjour" for t, d in evs2)  # plus le premier message du jour
    args = env.args(1)
    assert args[args.index("--resume") + 1] == "sess-web-1" and args[args.index("--model") + 1] == "haiku"
    assert evs2[-1][1]["modele"] == "haiku"


def test_confirmation_sensible_exige_step_up(env: Env) -> None:
    client = env.client()
    cle = enroler(env, client)
    aid = env.db.log("Alice", "admin_cron", "Brief du jour à 7h", {"action": "admin_cron"}, STATUS_PENDING)
    r = client.post(f"/api/confirmations/{aid}", json={"decision": "valider"})
    assert r.status_code == 403 and r.json()["step_up"] is True
    jeton = step_up(client, cle).json()["jeton"]
    r = client.post(f"/api/confirmations/{aid}", json={"decision": "valider"}, headers={"X-Homeos-Step-Up": jeton})
    assert r.status_code == 200 and env.executions == [aid]
    # Annuler n'exige pas de step-up ; action « confirmation_toujours » d'une capacité : step-up.
    aid2 = env.db.log("Alice", "demo_reserver", "Réserver", {}, STATUS_PENDING)
    assert client.post(f"/api/confirmations/{aid2}", json={"decision": "valider"}).status_code == 403
    assert client.post(f"/api/confirmations/{aid2}", json={"decision": "annuler"}).json()["texte"].startswith("❌")
    assert client.post("/api/confirmations/99999", json={"decision": "valider"}).status_code == 404


def test_partage_telegram_web(env: Env) -> None:
    """Un message passé par Telegram (même cœur, autre processus) est dans la conversation affichée par l'appli,
    avec la même session Claude ; le flux long-vivant le signale."""
    import asyncio

    from coeur.conversation import Coeur

    client = env.client()
    enroler(env, client)

    class CtxBot:
        cfg = env.cfg
        db = env.db
        sessions = Sessions(env.cfg.sessions_path)
        runner = ClaudeRunner(env.cfg)

    async def classer(texte: str) -> str:
        return "standard"

    bot = Coeur(CtxBot(), Historique(env.cfg.etat_dir / "appli.sqlite"), classifieur=classer)

    async def depuis_telegram() -> None:
        async for _ in bot.echanger(env.cfg.personnes[0], "Qu'y a-t-il samedi, en détail ?", canal="telegram"):
            pass

    asyncio.run(depuis_telegram())
    convs = client.get("/api/conversations").json()
    courante = convs["courante"]
    msgs = client.get(f"/api/conversations/{courante}/messages").json()["messages"]
    assert [(m["role"], m["canal"]) for m in msgs] == [("personne", "telegram"), ("assistant", "telegram")]
    # L'appli reprend la même session Claude.
    with client.stream("POST", f"/api/conversations/{courante}/messages", json={"texte": "ajoute-le"}) as r:
        lire_sse(r)
    args = env.args(1)
    assert args[args.index("--resume") + 1] == "sess-web-1"
    # Le premier message du jour est passé par Telegram : pas de carte « bonjour » dans l'historique.
    assert all(c.get("type") != "bonjour" for m in msgs for c in m["cartes"])


def test_conversation_d_une_autre_personne(env: Env) -> None:
    client = env.client()
    enroler(env, client)
    conv_bruno = env.ctx.historique.creer("bruno")
    assert client.get(f"/api/conversations/{conv_bruno['id']}/messages").status_code == 404
    r = client.post(f"/api/conversations/{conv_bruno['id']}/messages", json={"texte": "salut"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fichiers, coffre, brief, routines, voix
# ---------------------------------------------------------------------------
def test_upload_et_message_avec_piece(env: Env) -> None:
    client = env.client()
    enroler(env, client)
    r = client.post("/api/fichiers", files={"fichier": ("Ordonnance Dr.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 200, r.text
    piece = r.json()
    entrants = list((env.data / "fichiers" / "entrants").iterdir())
    assert len(entrants) == 1 and entrants[0].name.endswith("_alice_ordonnance-dr.pdf")
    assert client.post("/api/fichiers", files={"fichier": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    conv = client.post("/api/conversations").json()["conversation"]
    with client.stream("POST", f"/api/conversations/{conv['id']}/messages",
                       json={"texte": "", "pieces_jointes": [piece["id"], "f-inconnu"]}) as r:
        evs = lire_sse(r)
    assert evs[-1][0] == "fin"
    args = env.args(0)
    prompt = "\n".join(args[args.index("-p") + 1:args.index("--output-format")])
    assert f"[fichier reçu: {entrants[0]}]" in prompt
    assert args[args.index("--model") + 1] == "sonnet"  # règle « fichier » : modèle courant
    msgs = env.ctx.historique.messages(conv["id"])
    assert msgs[0]["texte"] == "📎 Ordonnance Dr.pdf" and msgs[0]["meta"]["pieces"][0]["id"] == piece["id"]


def test_coffre_via_api_sans_valeur_dans_les_journaux(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    client = env.client()
    cle = enroler(env, client)
    d = client.get("/api/coffre/demo").json()
    assert d["champs"] == ["identifiant", "mot_de_passe"] and d["renseignes"] == [] and d["personnel"]
    corps = {"champs": {"identifiant": "alice@example.org", "mot_de_passe": SECRET_TEST}}
    r = client.post("/api/coffre/demo", json=corps)
    assert r.status_code == 403 and r.json()["step_up"]
    entete = {"X-Homeos-Step-Up": step_up(client, cle).json()["jeton"]}
    r = client.post("/api/coffre/demo", json=corps, headers=entete)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "champs": ["identifiant", "mot_de_passe"]}
    from tools import coffre

    assert coffre.lire("demo/alice")["mot_de_passe"] == SECRET_TEST
    assert client.get("/api/coffre/demo").json()["renseignes"] == ["identifiant", "mot_de_passe"]
    # Step-up à usage unique pour le coffre : le jeton a servi à l'écriture, il faut une nouvelle empreinte.
    r = client.post("/api/coffre/demo", json=corps, headers=entete)
    assert r.status_code == 403 and r.json()["step_up"]
    entete = {"X-Homeos-Step-Up": step_up(client, cle).json()["jeton"]}
    assert client.post("/api/coffre/demo", json={"champs": {"autre": "x"}}, headers=entete).status_code == 400
    assert client.post("/api/coffre/inconnue", json=corps, headers=entete).status_code == 404
    # Jamais de valeur dans actions_log, les journaux, ni la base de l'appli.
    assert all(SECRET_TEST not in json.dumps(l, default=str) for l in env.journal())
    assert SECRET_TEST not in caplog.text
    assert SECRET_TEST.encode() not in (env.cfg.etat_dir / "appli.sqlite").read_bytes()
    assert any(l["kind"] == "coffre" and "mot_de_passe" in l["summary"] for l in env.journal())


def test_brief_du_jour_et_feuilletage(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from coeur import brief

    monkeypatch.setattr(brief, "meteo_jour", lambda cfg, jour, telecharger=None: {"min": 8, "max": 17,
                                                                                    "libelle": "Ciel dégagé"})
    client = env.client()
    enroler(env, client)
    r = client.get("/api/brief/jour")
    assert r.status_code == 200
    cartes = r.json()["cartes"]
    assert cartes[0]["type"] == "bonjour" and cartes[0]["donnees"]["prenom"] == "Alice"
    assert cartes[0]["donnees"]["meteo"]["max"] == 17
    from datetime import datetime

    demain = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    r = client.get(f"/api/brief/jour?date={demain}")
    assert r.json()["date"] == demain and r.json()["cartes"][0]["donnees"]["decalage_jours"] == 1
    assert client.get("/api/brief/jour?date=2001-01-01").status_code == 400
    assert client.get("/api/brief/jour?date=demain").status_code == 400


def test_routines_et_etat(env: Env) -> None:
    client = env.client()
    enroler(env, client)
    r = client.get("/api/routines")
    assert r.status_code == 200, r.text
    routines = r.json()["routines"]
    assert any(x["capacite"] == "demo" and x["nom"] == "rafraichir" for x in routines)
    assert any(x["capacite"] == "foyer" and x["nom"] == "brief_jour" for x in routines)
    r = client.patch("/api/routines/demo/rafraichir", json={"quand": "mardi 5h"})
    assert r.status_code == 200, r.text
    conf = r.json()["confirmation"]
    action = env.db.action(conf["id"])
    assert action["kind"] == "admin_cron" and action["status"] == STATUS_PENDING and action["who"] == "Alice"
    assert client.patch("/api/routines/demo/rafraichir", json={"quand": "n'importe quand"}).status_code == 400
    assert client.get("/api/etat").status_code == 200
    # Membre (non propriétaire) : ni état ni modification d'horaire.
    membre = env.client()
    enroler(env, membre, "bruno")
    assert membre.get("/api/etat").status_code == 403
    assert membre.patch("/api/routines/demo/rafraichir", json={"quand": "mardi 5h"}).status_code == 403


def test_voix(env: Env) -> None:
    client = env.client()
    enroler(env, client)

    class FauxTranscripteur:
        async def transcrire(self, chemin: Path) -> str:
            assert chemin.read_bytes() == b"audio"
            return "ajoute du lait"

    env.ctx.transcripteur = FauxTranscripteur()  # type: ignore[assignment]
    r = client.post("/api/voix/transcrire", files={"audio": ("vocal.webm", b"audio", "audio/webm")})
    assert r.json() == {"texte": "ajoute du lait"}
    from coeur.synthese import Synthese

    env.ctx.synthese = Synthese(env.cfg.etat_dir / "voix", piper=Path("/nulle/part/piper"))
    r = client.post("/api/voix/synthese", json={"texte": "Bonjour"})
    assert r.status_code == 503 and r.json()["repli"] == "navigateur"


def test_flux_long_vivant(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le flux d'une conversation commence par « pret », signale les messages arrivés par un autre canal et les
    événements publiés sur le bus, puis se termine par « reconnexion » (durée de vie bornée)."""
    from web import api
    from web.flux import EvenementBus

    monkeypatch.setattr(api, "DUREE_MAX_FLUX_S", 0.6)
    monkeypatch.setattr(api, "INTERVALLE_FLUX_S", 0.1)
    client = env.client()
    enroler(env, client)
    conv = client.post("/api/conversations").json()["conversation"]
    env.ctx.historique.ajouter_message(conv["id"], "assistant", "telegram", "Réponse donnée sur Telegram")
    with client.stream("GET", f"/api/conversations/{conv['id']}/flux?apres=0") as r:
        assert r.status_code == 200
        env.ctx.bus.publier("alice", EvenementBus("carte", {"type": "routine_resultat"}, None))
        env.ctx.bus.publier("alice", EvenementBus("carte", {"type": "autre_conversation"}, conv["id"] + 99))
        recus = lire_sse(r)
    types = [t for t, _ in recus]
    assert types[0] == "pret" and types[-1] == "reconnexion"
    assert ("message", "Réponse donnée sur Telegram") in [(t, d.get("texte")) for t, d in recus]
    cartes = [d["type"] for t, d in recus if t == "carte"]
    assert cartes == ["routine_resultat"]  # l'événement d'une autre conversation n'est pas diffusé ici
    assert recus[-1][1]["apres"] >= 1
    assert env.client().get(f"/api/conversations/{conv['id']}/flux").status_code == 401


def test_invitation_cli(env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from web import invitation

    monkeypatch.setenv("HOMEOS_WEB_ORIGINE", ORIGINE)
    assert invitation.main(["Alice"]) == 0
    url = capsys.readouterr().out.strip().splitlines()[-1]
    assert url.startswith(f"{ORIGINE}/inscription#")
    jeton = url.split("#", 1)[1]
    assert env.ctx.auth.invitation_valide(jeton) == "alice"
    assert invitation.main(["inconnu"]) == 2
    client = env.client()
    enroler(env, client)
    assert invitation.main(["alice", "--revoquer"]) == 0
    assert client.get("/api/conversations").status_code == 401


def test_avis_sur_une_reponse(env: Env) -> None:
    """Barre d'outils sous une réponse : pouce haut / bas → table feedback (sujet « réponse #id »), sans le modèle ;
    seulement sur une réponse d'une conversation de la personne."""
    import sqlite3

    client = env.client()
    enroler(env, client)
    conv = client.post("/api/conversations").json()["conversation"]
    qid = env.ctx.historique.ajouter_message(conv["id"], "personne", "web", "Salut", auteur="Alice")
    rid = env.ctx.historique.ajouter_message(conv["id"], "assistant", "web", "Bonjour !")
    r = client.post(f"/api/messages/{rid}/avis", json={"note": -1, "commentaire": "Trop long"})
    assert r.status_code == 200, r.text
    with sqlite3.connect(env.data / "homeos.sqlite") as conn:
        ligne = conn.execute("SELECT who, sujet, note, commentaire FROM feedback").fetchone()
    assert ligne == ("Alice", f"réponse #{rid}", 1, "Trop long")
    assert client.post(f"/api/messages/{rid}/avis", json={"note": 1}).status_code == 200
    assert client.post(f"/api/messages/{rid}/avis", json={"note": 3}).status_code == 400
    assert client.post(f"/api/messages/{qid}/avis", json={"note": 1}).status_code == 404  # message de la personne
    conv_bruno = env.ctx.historique.creer("bruno")
    aid = env.ctx.historique.ajouter_message(conv_bruno["id"], "assistant", "web", "Coucou")
    assert client.post(f"/api/messages/{aid}/avis", json={"note": 1}).status_code == 404
    assert any(l["kind"] == "feedback" and l["who"] == "Alice" for l in env.journal())
