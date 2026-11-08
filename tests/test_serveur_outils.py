"""Tests du serveur d'outils persistant (lot 7) : jetons, identité de l'appelant, transport HTTP, rechargement,
génération des jetons, wrapper côté bot, navigateur distant. Sans réseau externe : le serveur HTTP est lancé sur
127.0.0.1 (port libre) et interrogé avec le client du SDK mcp."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASH = "/bin/bash" if Path("/bin/bash").exists() else None

JETON_A = "a" * 24 + "1" * 40
JETON_B = "b" * 24 + "2" * 40
JETON_H = "c" * 24 + "3" * 40
JETON_C = "d" * 24 + "4" * 40

TOOLS_DEMO = '''\
from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    @mcp.tool()
    def demo_qui() -> dict:
        """Dit qui parle."""
        return {"personne": ctx.personne}

    @mcp.tool()
    def demo_secret() -> str:
        """Identifiant personnel."""
        return ctx.secret("demo")["identifiant"]

    @ctx.carte_pour("demo_qui")
    def carte_qui(resultat: Any, arguments: dict) -> dict:
        return {"type": "bonjour", "donnees": {"prenom": resultat["personne"]}}
'''


def _config() -> dict[str, Any]:
    return {
        "assistant": {"nom": "Testbot"},
        "personnes": [
            {"telegram_id": 1, "prenom": "Alice", "linux_user": "alice"},
            {"telegram_id": 2, "prenom": "Bob", "linux_user": "bob"},
        ],
        "agendas": [
            {"nom": "Foyer", "ical_url_env": "ICAL_FOYER", "ecriture": False},
            {"nom": "Agenda de Bob", "ical_url_env": "ICAL_BOB", "prive": "bob"},
            {"nom": "Piège", "ical_url_env": "TELEGRAM_BOT_TOKEN"},
        ],
    }


def _capacite(caps: Path, nom: str = "demo") -> Path:
    d = caps / nom
    d.mkdir(parents=True)
    manifeste = {
        "nom": nom,
        "titre": "Démo",
        "description": "Capacité de test.",
        "version": 1,
        "statut": "actif",
        "origine": "humain",
        "secrets": [{"cle": "demo/alice", "champs": ["identifiant"], "par_personne": True}],
    }
    (d / "capability.yaml").write_text(yaml.safe_dump(manifeste, allow_unicode=True), encoding="utf-8")
    (d / "tools.py").write_text(TOOLS_DEMO, encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data = tmp_path / "data"
    data.mkdir()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump(_config(), allow_unicode=True), encoding="utf-8")
    ics = FIXTURES / "foyer.ics"
    (secrets / "ical.env").write_text(f"ICAL_FOYER=file://{ics}\n", encoding="utf-8")
    (secrets / "ical-bob.env").write_text(f"ICAL_BOB=file://{ics}\n", encoding="utf-8")
    (secrets / "telegram.env").write_text("TELEGRAM_BOT_TOKEN=123:ne-doit-jamais-sortir\n", encoding="utf-8")
    (secrets / "outils-jetons.env").write_text(
        f"# test\nJETON_alice={JETON_A}\nJETON_bob={JETON_B}\nJETON_homeos={JETON_H}\n", encoding="utf-8"
    )
    _capacite(caps)
    for cle, valeur in {
        "HOMEOS_DATA": data,
        "HOMEOS_ROOT": tmp_path,
        "HOMEOS_CAPABILITIES": caps,
        "HOMEOS_SECRETS": secrets,
        "COFFRE_CLE": "cle-de-test-serveur-outils-0123456789abcdef",
    }.items():
        monkeypatch.setenv(cle, str(valeur))
    for cle in ("HOMEOS_WHO", "HOMEOS_PERSONNE", "HOMEOS_NOW"):
        monkeypatch.delenv(cle, raising=False)
    return {"data": data, "caps": caps, "secrets": secrets, "tmp": tmp_path}


# --- jetons ---------------------------------------------------------------------------------------------------


def _jetons() -> Any:
    from tools import serveur_http

    j = serveur_http.Jetons(Path("/inexistant"))
    j.ajouter("alice", JETON_A)
    j.ajouter("bob", JETON_B)
    j.ajouter("homeos", JETON_H)
    return j


def test_resolution_jeton_vers_personne() -> None:
    j = _jetons()
    assert j.resoudre(JETON_A) == "alice"
    assert j.resoudre(JETON_B) == "bob"
    assert j.resoudre(JETON_H) == "homeos"
    for mauvais in (None, "", JETON_A[:-1], JETON_A + "0", JETON_C, JETON_A.upper()):
        assert j.resoudre(mauvais) is None


def test_resolution_temps_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toutes les entrées sont comparées, sur des empreintes de même longueur, avec hmac.compare_digest :
    ni sortie anticipée ni dépendance à la longueur ou au préfixe du jeton présenté."""
    from tools import serveur_http

    appels: list[tuple[int, int]] = []
    vrai = serveur_http.hmac.compare_digest

    def compter(a: bytes, b: bytes) -> bool:
        appels.append((len(a), len(b)))
        return vrai(a, b)

    monkeypatch.setattr(serveur_http.hmac, "compare_digest", compter)
    j = _jetons()
    for presente in (JETON_A, JETON_H, "x", "y" * 500):
        appels.clear()
        j.resoudre(presente)
        assert len(appels) == 3  # premier, dernier ou inconnu : toujours toutes les entrées
        assert set(appels) == {(32, 32)}  # empreintes SHA-256, longueur fixe


def test_chargement_fichier_jetons(tmp_path: Path) -> None:
    from tools import serveur_http

    f = tmp_path / "outils-jetons.env"
    f.write_text(
        f"# commentaire\nJETON_alice={JETON_A}\nJETON_Mauvais={JETON_B}\nJETON_court=abc\nAUTRE={JETON_C}\n",
        encoding="utf-8",
    )
    j = serveur_http.Jetons(f)
    assert j.charger() == 1 and j.utilisateurs() == ["alice"]
    f.chmod(0)
    if os.geteuid() != 0:  # fichier devenu illisible : la table précédente reste en service
        assert j.charger() == 1 and j.resoudre(JETON_A) == "alice"
    f.unlink()
    assert j.charger() == 1


def test_jeton_bearer() -> None:
    from tools.serveur_http import jeton_bearer

    def scope(*entetes: tuple[bytes, bytes]) -> dict[str, Any]:
        return {"headers": list(entetes)}

    assert jeton_bearer(scope((b"authorization", b"Bearer abc"))) == "abc"
    assert jeton_bearer(scope((b"Authorization", b"bearer   abc  "))) == "abc"
    assert jeton_bearer(scope((b"authorization", b"Basic abc"))) is None
    assert jeton_bearer(scope((b"authorization", b"Bearer"))) is None
    assert jeton_bearer(scope()) is None


# --- intergiciel ASGI ---------------------------------------------------------------------------------------------


def _appeler(app: Any, chemin: str = "/mcp", jeton: str | None = None, methode: str = "POST") -> tuple[int, dict, bytes]:
    entetes = [(b"content-type", b"application/json")]
    if jeton is not None:
        entetes.append((b"authorization", f"Bearer {jeton}".encode()))
    scope = {"type": "http", "method": methode, "path": chemin, "headers": entetes, "client": ("127.0.0.1", 5)}
    recu: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        recu.append(message)

    asyncio.run(app(scope, receive, send))
    debut = next(m for m in recu if m["type"] == "http.response.start")
    corps = b"".join(m.get("body", b"") for m in recu if m["type"] == "http.response.body")
    return debut["status"], {k.decode(): v.decode() for k, v in debut["headers"]}, corps


def test_401_sans_jeton_ou_jeton_inconnu() -> None:
    from tools import identite
    from tools.serveur_http import AuthentificationJeton

    vus: list[str | None] = []

    async def interne(scope: Any, receive: Any, send: Any) -> None:
        vus.append(identite.utilisateur_requete())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = AuthentificationJeton(interne, _jetons())
    for jeton in (None, "", JETON_C):
        statut, entetes, corps = _appeler(app, jeton=jeton)
        assert statut == 401 and "Bearer" in entetes["www-authenticate"]
        assert json.loads(corps) == {"erreur": "jeton absent ou inconnu"}
    assert vus == []  # la requête n'a jamais atteint le SDK
    assert _appeler(app, "/sante", methode="GET")[0] == 200  # santé sans jeton, sans rien révéler
    assert vus == []
    assert _appeler(app, jeton=JETON_B)[0] == 200
    assert _appeler(app, "/sante", jeton=JETON_A, methode="POST")[0] == 200  # /sante en POST : authentifié
    assert vus == ["bob", "alice"]
    assert identite.utilisateur_requete() is None  # remis à zéro après la requête


# --- identité : journal, secrets, agendas ---------------------------------------------------------------------------


def test_contextvar_personne_dans_actions_log(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import db, identite

    db.log_action("essai", "hors requête")
    with identite.requete("bob"):
        db.log_action("essai", "bob")
        assert identite.utilisateur_courant() == "bob"
    with identite.requete("homeos"):
        db.log_action("essai", "homeos")
    with identite.requete("inconnu"):
        db.log_action("essai", "inconnu")
    monkeypatch.setenv("HOMEOS_WHO", "cli")
    with identite.requete("alice"):
        db.log_action("essai", "who explicite")
    qui = [a["who"] for a in reversed(db.recent_actions(5))]
    assert qui == ["assistant", "Bob", "assistant", "assistant", "cli"]
    with pytest.raises(ValueError), identite.requete("../etc"):
        pass


def test_contextvar_personne_dans_ctx_secret(env: dict[str, Path]) -> None:
    from tools import capacites, coffre, identite

    capacites._CACHE_COFFRE["quand"] = -1e9
    coffre.ecrire("demo/alice", {"identifiant": "id-alice"})
    coffre.ecrire("demo/bob", {"identifiant": "id-bob"})
    cap = capacites.lire_capacite(env["caps"] / "demo")
    ctx = capacites.Contexte(env["data"], None, None, cap)  # chargé une fois par le serveur, sous homeos
    with identite.requete("alice"):
        assert ctx.personne == "alice" and ctx.acteur == "Alice"
        assert ctx.secret("demo") == {"identifiant": "id-alice"}
        with pytest.raises(capacites.ActionRefusee):
            ctx.secret("demo/bob")
    with identite.requete("bob"):
        assert ctx.secret("demo") == {"identifiant": "id-bob"}
    with identite.requete("homeos"):  # bot et crons : aucune personne, pas d'identifiant personnel
        assert ctx.personne is None
        with pytest.raises(capacites.SecretManquant):
            ctx.secret("demo")
    assert ctx.personne is None  # hors requête : personne fixée au chargement


def test_outil_en_thread_garde_identite_et_schema(env: dict[str, Path]) -> None:
    from tools import identite, server

    vu: list[str | None] = []

    def outil(debut: str, n: int = 2) -> str:
        """Doc."""
        vu.append(identite.utilisateur_requete())
        return f"{debut}-{n}"

    asynchrone = identite.en_thread(outil)

    async def appel() -> str:
        with identite.requete("bob"):
            return await asynchrone("x", n=3)  # type: ignore[no-any-return]

    assert asyncio.run(appel()) == "x-3" and vu == ["bob"]
    outils = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = getattr(outils["agenda_lire"], "inputSchema", None) or outils["agenda_lire"].input_schema
    assert schema["properties"]["debut"].get("format") == "date"  # annotations évaluées dans tools.server
    assert outils["agenda_lire"].description.startswith("Lit les événements")


def test_agenda_prive_selon_le_jeton(env: dict[str, Path]) -> None:
    from datetime import date

    from tools import agenda, identite

    def agendas(qui: str) -> set[str]:
        with identite.requete(qui):
            return {e["agenda"] for e in agenda.agenda_lire(date(2026, 11, 1), date(2026, 11, 30))}

    assert agendas("bob") == {"Foyer", "Agenda de Bob"}
    assert agendas("alice") == {"Foyer"}
    assert agendas("homeos") == {"Foyer"}
    # Une variable qui n'est pas ICAL_* n'est jamais traitée comme une URL (secrets lisibles sous homeos).
    assert agenda._url_pour({"ical_url_env": "TELEGRAM_BOT_TOKEN"}, {"TELEGRAM_BOT_TOKEN": "x"}) is None


def test_jetons_jamais_dans_load_secrets(env: dict[str, Path]) -> None:
    from tools.common import load_secrets

    s = load_secrets()
    assert "ICAL_FOYER" in s and not any(k.startswith("JETON_") for k in s)


# --- serveur HTTP réel ------------------------------------------------------------------------------------------------


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def serveur(env: dict[str, Path]) -> Iterator[dict[str, Any]]:
    port = _port_libre()
    journal = env["tmp"] / "serveur.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.server", "--transport", "http", "--port", str(port)],
        cwd=str(RACINE),
        env={**os.environ, "PYTHONPATH": str(RACINE), "HOMEOS_LOG": "INFO"},
        stdout=subprocess.DEVNULL,
        stderr=journal.open("w"),
    )
    url = f"http://127.0.0.1:{port}/mcp"
    limite = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/sante", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if proc.poll() is not None or time.monotonic() > limite:
            proc.kill()
            pytest.fail("le serveur HTTP n'a pas démarré :\n" + journal.read_text(encoding="utf-8")[-2000:])
        time.sleep(0.2)
    try:
        yield {"url": url, "proc": proc, "journal": journal, **env}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _appel_outil(url: str, jeton: str, nom: str, arguments: dict[str, Any] | None = None) -> tuple[bool, str]:
    from tools import serveur_http

    async def go() -> tuple[bool, str]:
        async with serveur_http.client(url, jeton) as session:
            r = await session.call_tool(nom, arguments or {})
            return serveur_http.est_erreur(r), serveur_http.texte_resultat(r)

    return asyncio.run(go())


def _noms_outils(url: str, jeton: str) -> set[str]:
    from tools import serveur_http

    async def go() -> set[str]:
        async with serveur_http.client(url, jeton) as session:
            return {t.name for t in (await session.list_tools()).tools}

    return asyncio.run(go())


def test_http_integration_jetons_et_identite(serveur: dict[str, Any]) -> None:
    """Le vrai serveur HTTP, appelé avec le client du SDK mcp : 401 sans jeton, outils sous l'identité du jeton,
    deux personnes en parallèle, agenda privé, erreurs transmises."""
    from tools import db, serveur_http

    url = serveur["url"]
    assert serveur_http.code_sans_jeton(url) == 401
    requete = urllib.request.Request(url, data=b"{}", method="POST", headers={"Authorization": f"Bearer {JETON_C}"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(requete, timeout=5)
    assert exc.value.code == 401

    noms = _noms_outils(url, JETON_H)
    assert {"courses_lister", "agenda_lire", "capacites_lister", "demo_qui"} <= noms

    async def deux_personnes() -> list[str]:
        async def appel(jeton: str) -> str:
            async with serveur_http.client(url, jeton) as session:
                return serveur_http.texte_resultat(await session.call_tool("demo_qui", {}))

        return list(await asyncio.gather(appel(JETON_A), appel(JETON_B), appel(JETON_H)))

    reponses = [json.loads(r) for r in asyncio.run(deux_personnes())]
    assert [r["personne"] for r in reponses] == ["alice", "bob", None]

    assert _appel_outil(url, JETON_A, "courses_ajouter", {"articles": ["pain"]})[0] is False
    assert _appel_outil(url, JETON_B, "courses_ajouter", {"articles": ["lait"]})[0] is False
    qui = {a["summary"]: a["who"] for a in db.recent_actions(20) if a["kind"] == "courses_ajouter"}
    assert qui == {"ajout pain": "Alice", "ajout lait": "Bob"}

    erreur, texte = _appel_outil(url, JETON_B, "agenda_lire", {"debut": "2026-11-01", "fin": "2026-11-30"})
    assert not erreur and "Agenda de Bob" in texte and "ne-doit-jamais-sortir" not in texte
    erreur, texte = _appel_outil(url, JETON_A, "agenda_lire", {"debut": "2026-11-01", "fin": "2026-11-30"})
    assert not erreur and "Agenda de Bob" not in texte and "Foyer" in texte

    erreur, texte = _appel_outil(url, JETON_A, "memoire_lire", {"chemin": "../secrets.env"})
    assert erreur and texte, texte
    assert JETON_A not in serveur["journal"].read_text(encoding="utf-8")


def test_http_rechargement_sighup(serveur: dict[str, Any]) -> None:
    """SIGHUP : jetons relus (révocation, ajout) et capacités rechargées (désactivée → outils retirés)."""
    from tools import capacites

    url = serveur["url"]
    assert "demo_qui" in _noms_outils(url, JETON_A)
    (serveur["secrets"] / "outils-jetons.env").write_text(
        f"JETON_alice={JETON_A}\nJETON_homeos={JETON_H}\nJETON_carol={JETON_C}\n", encoding="utf-8"
    )
    capacites.desactiver("demo", donnees=serveur["data"])
    serveur["proc"].send_signal(signal.SIGHUP)
    from tools import serveur_http

    limite = time.monotonic() + 15
    while time.monotonic() < limite:
        with contextlib.suppress(BaseException):  # 401 tant que le jeton n'est pas rechargé
            if "demo_qui" not in _noms_outils(url, JETON_C):
                break
        time.sleep(0.3)
    else:
        pytest.fail("rechargement non pris en compte :\n" + serveur["journal"].read_text(encoding="utf-8")[-2000:])
    assert serveur["proc"].poll() is None  # le service n'a pas été coupé
    assert "courses_lister" in _noms_outils(url, JETON_A)
    with pytest.raises(BaseException):  # noqa: B017 - 401 : jeton révoqué (erreur du transport du SDK)
        _noms_outils(url, JETON_B)
    assert serveur_http.code_sans_jeton(url) == 401
    capacites.activer("demo", donnees=serveur["data"])
    serveur["proc"].send_signal(signal.SIGHUP)
    limite = time.monotonic() + 15
    while "demo_qui" not in _noms_outils(url, JETON_C):
        assert time.monotonic() < limite, "capacité réactivée non rechargée"
        time.sleep(0.3)


def test_sonde_healthcheck(serveur: dict[str, Any]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "tools.serveur_http", "sonde", "--url", serveur["url"], "--appel", "capacites_lister"],
        cwd=str(RACINE),
        env={**os.environ, "PYTHONPATH": str(RACINE)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    rapport = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rapport["ok"] and rapport["sans_jeton"] == 401 and rapport["outils"] > 10 and not rapport["appel_erreur"]
    assert JETON_H not in proc.stdout + proc.stderr


def test_ecoute_locale_seulement() -> None:
    from tools import serveur_http

    with pytest.raises(SystemExit):
        asyncio.run(serveur_http.servir(object(), _jetons(), "0.0.0.0", _port_libre()))


# --- génération des jetons (install.sh → scripts/jetons-outils.sh) ----------------------------------------------------


@pytest.mark.skipif(BASH is None, reason="bash absent")
def test_generation_jetons_idempotente(tmp_path: Path) -> None:
    secrets, maisons = tmp_path / "secrets", tmp_path / "home"
    secrets.mkdir()
    for u in ("alice", "bob"):
        (maisons / u).mkdir(parents=True)
    env = {**os.environ, "HOMEOS_SECRETS": str(secrets), "HOMEOS_MAISONS": str(maisons), "HOMEOS_SANS_CHOWN": "1"}

    def lancer(*utilisateurs: str) -> str:
        proc = subprocess.run(
            [BASH, str(RACINE / "scripts" / "jetons-outils.sh"), *utilisateurs],
            env=env, capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def serveur_() -> dict[str, str]:
        lignes = (secrets / "outils-jetons.env").read_text(encoding="utf-8").splitlines()
        return dict(l.split("=", 1) for l in lignes if l.startswith("JETON_"))

    def perso(u: str) -> Path:
        return maisons / u / ".config" / "homeos" / "jeton"

    assert "changements=3" in lancer("homeos", "alice", "bob")
    jetons = serveur_()
    assert set(jetons) == {"JETON_homeos", "JETON_alice", "JETON_bob"}
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in jetons.values())
    assert len(set(jetons.values())) == 3
    for u in ("alice", "bob"):
        assert perso(u).read_text().strip() == jetons[f"JETON_{u}"]
        assert stat.S_IMODE(perso(u).stat().st_mode) == 0o600
        assert stat.S_IMODE(perso(u).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((secrets / "outils-jetons.env").stat().st_mode) == 0o640
    assert not (maisons / "homeos").exists()  # homeos lit le fichier serveur, pas de copie

    # Idempotent : rien ne change au second passage.
    assert "changements=0" in lancer("homeos", "alice", "bob")
    assert serveur_() == jetons
    # Copie de la personne altérée → jeton régénéré des deux côtés ; les autres sont conservés.
    perso("bob").write_text("autre\n")
    assert "changements=1" in lancer("homeos", "alice", "bob")
    nouveaux = serveur_()
    assert nouveaux["JETON_bob"] != jetons["JETON_bob"] and perso("bob").read_text().strip() == nouveaux["JETON_bob"]
    assert nouveaux["JETON_alice"] == jetons["JETON_alice"] and nouveaux["JETON_homeos"] == jetons["JETON_homeos"]
    # Personne retirée de config.yaml → jeton révoqué côté serveur.
    assert "changements=1" in lancer("homeos", "alice")
    assert set(serveur_()) == {"JETON_homeos", "JETON_alice"}
    # Le serveur accepte le fichier généré.
    from tools import serveur_http

    j = serveur_http.Jetons(secrets / "outils-jetons.env")
    assert j.charger() == 2 and j.resoudre(perso("alice").read_text().strip()) == "alice"


@pytest.mark.skipif(BASH is None, reason="bash absent")
def test_generation_jetons_refuse_un_nom_invalide(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    proc = subprocess.run(
        [BASH, str(RACINE / "scripts" / "jetons-outils.sh"), "homeos", "../root"],
        env={**os.environ, "HOMEOS_SECRETS": str(tmp_path / "secrets"), "HOMEOS_MAISONS": str(tmp_path),
             "HOMEOS_SANS_CHOWN": "1"},
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0 and "invalide" in proc.stderr
    assert not (tmp_path / "secrets" / "outils-jetons.env").exists()


# --- bot : personne en clé API avec le serveur d'outils HTTP ----------------------------------------------------


def test_bot_cle_api_config_mcp_http(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.claude_runner import ClaudeError, ClaudeRunner, config_mcp_http, construire_commande
    from bot.config import parse_config

    etat = env["tmp"] / "etat"
    monkeypatch.setenv("HOMEOS_ETAT", str(etat))
    monkeypatch.delenv("HOMEOS_NO_SUDO", raising=False)
    (env["tmp"] / "outils").write_text("HOMEOS_OUTILS=http\nHOMEOS_OUTILS_URL=http://127.0.0.1:8099/mcp\n")
    monkeypatch.setenv("HOMEOS_FICHIER_OUTILS", str(env["tmp"] / "outils"))
    cfg = parse_config(_config() | {"personnes": [{"telegram_id": 1, "prenom": "Alice", "linux_user": "alice",
                                                   "auth": "cle_api"}]}, env["data"], env["tmp"])
    assert cfg.mode_outils == "http"
    chemin = config_mcp_http(cfg, "alice")
    assert chemin == etat / "outils" / "mcp-alice.json"
    assert stat.S_IMODE(chemin.stat().st_mode) == 0o600
    conf = json.loads(chemin.read_text())["mcpServers"]["homeos"]
    assert conf == {"type": "http", "url": "http://127.0.0.1:8099/mcp", "headers": {"Authorization": f"Bearer {JETON_A}"}}
    with pytest.raises(ClaudeError):
        config_mcp_http(cfg, "carol")
    cmd = construire_commande(cfg, "alice", "bonjour", None, cle_api=True, mcp_config=chemin)
    assert cmd[cmd.index("--mcp-config") + 1] == str(chemin) and "--strict-mcp-config" in cmd
    assert JETON_A not in " ".join(cmd)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    run = ClaudeRunner(cfg).run("alice", "Alice", "x", None, cle_api=True)
    assert str(chemin) in run.cmd
    # Retour arrière en stdio : configuration du dépôt, sans --strict-mcp-config ajouté par le bot.
    (env["tmp"] / "outils").write_text("HOMEOS_OUTILS=stdio\n")
    run = ClaudeRunner(cfg).run("alice", "Alice", "x", None, cle_api=True)
    assert str(cfg.mcp_config_path) in run.cmd and "--strict-mcp-config" not in run.cmd


def test_mode_outils(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tools.common import mode_outils, reglages_outils

    assert mode_outils() == "stdio"  # fichier absent : repli
    f = tmp_path / "outils"
    f.write_text("HOMEOS_OUTILS=http\nHOMEOS_OUTILS_URL=http://127.0.0.1:9000/mcp\n")
    monkeypatch.setenv("HOMEOS_FICHIER_OUTILS", str(f))
    assert reglages_outils() == {"mode": "http", "url": "http://127.0.0.1:9000/mcp"}
    monkeypatch.setenv("HOMEOS_OUTILS", "nimporte")
    assert mode_outils() == "stdio"


def test_reglages_refusent_le_jeton() -> None:
    from tools import securite

    refus = securite.regles_refus(Path("/srv/home-os/data"), Path("/var/lib/homeos"), Path("/etc/homeos"))
    assert "Read(//home/*/.config/homeos/**)" in refus
    assert securite.dossier_travail(Path("/var/lib/homeos"), Path("/srv/home-os/data"), "alice") == Path(
        "/var/lib/homeos-cwd/alice"
    )


# --- navigateur distant (homeos-web) ------------------------------------------------------------------------------


def test_navigateur_distant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tools import kitchen_daily as kd

    assert kd.navigateur_distant() is None  # conftest : HOMEOS_NAVIGATEUR=local
    monkeypatch.delenv("HOMEOS_NAVIGATEUR")
    monkeypatch.setenv("HOMEOS_SECRETS", str(tmp_path))
    assert kd.navigateur_distant() is None  # rien de configuré : Chromium local
    (tmp_path / "navigateur.env").write_text("HOMEOS_NAVIGATEUR_WS=ws://127.0.0.1:8091/abcdef0123456789abcdef0123456789\n")
    assert kd.navigateur_distant() == "ws://127.0.0.1:8091/abcdef0123456789abcdef0123456789"
    monkeypatch.setenv("HOMEOS_NAVIGATEUR_WS", "ws://exemple.org:8091/x")
    assert kd.navigateur_distant() is None  # jamais un navigateur hors de la machine
    monkeypatch.setenv("HOMEOS_NAVIGATEUR", "local")
    assert kd.navigateur_distant() is None


def test_navigateur_distant_contexte_copie(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_hors_boucle_asyncio` (Playwright sync dans un fil) garde l'identité de la requête."""
    from tools import identite
    from tools import kitchen_daily as kd

    @kd._hors_boucle_asyncio
    def qui() -> str | None:
        return identite.utilisateur_requete()

    async def dans_la_boucle() -> str | None:
        with identite.requete("bob"):
            return qui()

    assert asyncio.run(dans_la_boucle()) == "bob"


def test_navigateur_distant_reel(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """`playwright run-server` (comme homeos-web) + `kitchen_daily.navigateur()` : session passée en mémoire, capture et
    session rafraîchie écrites par le client. Ignoré si Chromium n'est pas installé (développement)."""
    pytest.importorskip("playwright")
    from tools import kitchen_daily as kd

    port = _port_libre()
    chemin = "0123456789abcdef0123456789abcdef"
    journal = env["tmp"] / "pw.log"
    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "playwright"), "run-server", "--host", "127.0.0.1", "--port", str(port),
         "--path", f"/{chemin}", "--max-clients", "2"],
        stdout=journal.open("w"), stderr=subprocess.STDOUT,
    )
    try:
        limite = time.monotonic() + 20
        while "Listening" not in journal.read_text(encoding="utf-8"):
            if proc.poll() is not None or time.monotonic() > limite:
                pytest.skip("serveur Playwright indisponible : " + journal.read_text(encoding="utf-8")[-300:])
            time.sleep(0.2)
        monkeypatch.delenv("HOMEOS_NAVIGATEUR")
        monkeypatch.setenv("HOMEOS_NAVIGATEUR_WS", f"ws://127.0.0.1:{port}/{chemin}")
        kd.state_path().write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        try:
            with kd.navigateur() as page:
                page.set_content("<h1>Bonjour</h1>")
                capture = kd.captures_dir() / "essai.png"
                page.screenshot(path=str(capture))
                assert page.inner_text("h1") == "Bonjour"
        except kd.ErreurKitchenDaily as exc:
            if "Executable doesn't exist" in str(exc.__cause__) or "install" in str(exc.__cause__):
                pytest.skip("Chromium non installé")
            raise
        assert capture.stat().st_size > 1000
        assert json.loads(kd.state_path().read_text(encoding="utf-8"))["cookies"] == []
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --- lot 4a : cartes génératives en canal web ------------------------------------------------------------------


def test_http_cartes_canal_web(serveur: dict[str, Any]) -> None:
    """Avec l'en-tête X-Homeos-Canal: web, le résultat d'un outil porte en plus un bloc `CARTE:<json>` (outil de
    base et capacité) ; sans l'en-tête (Telegram, crons), le résultat est inchangé."""
    from tools import serveur_http

    url = serveur["url"]

    async def appel(nom: str, arguments: dict[str, Any], canal: str | None) -> tuple[list[str], Any]:
        async with serveur_http.client(url, JETON_A, canal=canal) as session:
            r = await session.call_tool(nom, arguments)
            textes = [b.text for b in r.content if getattr(b, "text", None)]
            return textes, getattr(r, "structured_content", None) or getattr(r, "structuredContent", None)

    assert _appel_outil(url, JETON_A, "courses_ajouter", {"articles": ["pain", "lait"]})[0] is False
    sans, structure_sans = asyncio.run(appel("courses_lister", {}, None))
    assert not any(t.startswith("CARTE:") for t in sans)
    avec, structure_avec = asyncio.run(appel("courses_lister", {}, "web"))
    cartes = [json.loads(t[len("CARTE:"):]) for t in avec if t.startswith("CARTE:")]
    assert len(cartes) == 1 and cartes[0]["type"] == "courses" and cartes[0]["donnees"]["restants"] == 2
    assert [t for t in avec if not t.startswith("CARTE:")] == sans  # résultat de l'outil intact
    # Sans sortie structurée : Claude Code transmet alors les blocs texte (dont CARTE:) au lieu du seul JSON structuré.
    assert structure_avec is None and structure_sans is None

    textes, _ = asyncio.run(appel("demo_qui", {}, "web"))
    carte = json.loads(next(t for t in textes if t.startswith("CARTE:"))[len("CARTE:"):])
    assert carte["type"] == "bonjour" and carte["donnees"] == {"prenom": "alice"} and carte["id"].startswith("c-")
    textes, _ = asyncio.run(appel("demo_qui", {}, "telegram"))
    assert not any(t.startswith("CARTE:") for t in textes)


def test_stdio_cartes_resultat_non_textuel(monkeypatch: pytest.MonkeyPatch) -> None:
    """En stdio (HOMEOS_CANAL=web), la carte d'un outil qui renvoie une liste ou un objet voyage comme bloc texte de
    plus (régression du 8 nov. 2026 : `courses_lister` renvoyait une liste, sa carte était perdue)."""
    from tools import cartes

    monkeypatch.setenv("HOMEOS_CANAL", "web")
    liste = cartes.avec_cartes(lambda: [{"id": 1, "article": "Lait", "rayon": "laitier", "coche": False}], "courses_lister")()
    assert liste[0]["article"] == "Lait" and liste[-1].startswith("CARTE:")
    assert json.loads(liste[-1][len("CARTE:"):])["type"] == "courses"
    objet = cartes.avec_cartes(lambda: {"coches": ["Lait"], "introuvables": []}, "courses_cocher")
    monkeypatch.setattr(cartes, "_courses_actuelles", list)
    sortie = objet()
    assert sortie[0] == {"coches": ["Lait"], "introuvables": []} and sortie[1].startswith("CARTE:")
    assert cartes.sans_sortie_structuree({}) == {"structured_output": False}
    monkeypatch.setenv("HOMEOS_CANAL", "telegram")
    assert cartes.avec_cartes(lambda: [1], "courses_lister")() == [1]
