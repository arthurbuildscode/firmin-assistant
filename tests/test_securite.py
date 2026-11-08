"""Lot 5d — sécurité : wrapper homeos-claude, réglages des sessions Claude, secrets et état hors de data/."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bot.claude_runner import construire_commande
from bot.config import charger_secrets, parse_config
from tools import agenda, capacites, common, securite

RACINE = Path(__file__).resolve().parent.parent
WRAPPER = RACINE / "install" / "bin" / "homeos-claude"
BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash absent")
def test_wrapper_tests_bash() -> None:
    """Suite bash du wrapper (tests/wrapper/test_homeos_claude.sh)."""
    proc = subprocess.run(
        [BASH, str(RACINE / "tests" / "wrapper" / "test_homeos_claude.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash absent")
def test_commande_du_bot_acceptee_par_le_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La commande construite par le bot passe telle quelle le contrôle du wrapper (mêmes chemins)."""
    racine, donnees, etat = tmp_path / "racine", tmp_path / "racine" / "data", tmp_path / "etat"
    moi = getpass.getuser()
    for d in (donnees / "foyer", donnees / "personnes", donnees / "fichiers", racine / "content",
              racine / "capabilities", racine / "tools", tmp_path / "etat-cwd" / moi):
        d.mkdir(parents=True)
    faux = tmp_path / "claude"
    faux.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'sortie'}\n", encoding="utf-8")
    faux.chmod(0o755)
    monkeypatch.setenv("HOMEOS_ETAT", str(etat))
    monkeypatch.setenv("HOMEOS_SECRETS", str(tmp_path / "secrets"))
    monkeypatch.delenv("HOMEOS_NO_SUDO", raising=False)
    cfg = parse_config(
        {"personnes": [{"telegram_id": 1, "prenom": "A", "linux_user": moi}, {"telegram_id": 2, "linux_user": "autre"}]},
        donnees,
        racine,
    )
    cmd = construire_commande(cfg, moi, "[contexte: test] bonjour", "0b6f1c2e-1234-4abc-9def-000000000000")
    assert cmd[:6] == ["sudo", "-n", "-H", "-u", moi, str(cfg.wrapper_path)]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOMEOS_WRAPPER_TEST": "1",
        "HOMEOS_RACINE": str(racine),
        "HOMEOS_DONNEES": str(donnees),
        "HOMEOS_ETAT": str(etat),
        "HOMEOS_SECRETS": str(tmp_path / "secrets"),
        "HOMEOS_FICHIER_PROXY": str(tmp_path / "absent"),
        "HOMEOS_FICHIER_OUTILS": str(tmp_path / "absent-outils"),  # mode stdio, indépendant du serveur réel
        "HOMEOS_CLAUDE_BIN": str(faux),
    }
    proc = subprocess.run([BASH, str(WRAPPER), *cmd[6:]], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    transmis = (tmp_path / "sortie").read_text(encoding="utf-8").splitlines()
    assert transmis[:2] == ["-p", "[contexte: test] bonjour"]
    assert "dontAsk" in transmis and "--settings" in transmis


def test_reglages_session() -> None:
    donnees, etat, secrets = Path("/srv/home-os/data"), Path("/var/lib/homeos"), Path("/etc/homeos")
    r = securite.reglages_session(donnees, etat, secrets, "alex", ["alex", "camille"])
    assert r["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert r["cleanupPeriodDays"] == 30
    refus = r["permissions"]["deny"]
    for attendu in (
        "Read(//etc/homeos/**)",
        "Read(//var/lib/homeos/**)",
        "Read(//srv/home-os/data/**/secrets*)",
        "Read(//srv/home-os/data/*.sqlite*)",
        "Read(//home/**/.claude/**)",
        "Read(//srv/home-os/data/personnes/camille/prive/**)",
    ):
        assert attendu in refus
    assert "Read(//srv/home-os/data/personnes/alex/prive/**)" not in refus
    # Sans état séparé (développement), pas de règle sur l'état (c'est data/).
    r2 = securite.reglages_session(donnees, donnees, secrets)
    assert not any(x.startswith("Read(//srv/home-os/data/**)") for x in r2["permissions"]["deny"])


def test_dossiers_lisibles(tmp_path: Path) -> None:
    donnees, racine = tmp_path / "data", tmp_path
    (donnees / "foyer").mkdir(parents=True)
    (donnees / "onboarding").mkdir()
    (racine / "content").mkdir()
    assert securite.dossiers_lisibles(donnees, racine) == [donnees / "foyer", racine / "content"]
    tous = securite.dossiers_lisibles(donnees, racine, existants_seulement=False)
    assert donnees not in tous and donnees / "onboarding" not in tous and racine / "capabilities" in tous


def test_secrets_dossier_puis_repli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data, secrets = tmp_path / "data", tmp_path / "etc"
    data.mkdir()
    secrets.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_SECRETS", str(secrets))
    (secrets / "ical.env").write_text("ICAL_FOYER=https://exemple/neuf.ics\n", encoding="utf-8")
    (secrets / "coffre.env").write_text("export COFFRE_CLE='abc'\n", encoding="utf-8")
    (secrets / "notes.txt").write_text("PAS_LU=1\n", encoding="utf-8")
    (data / "secrets.env").write_text("ICAL_FOYER=https://exemple/ancien.ics\nICAL_ALEX=https://a\n", encoding="utf-8")
    s = common.load_secrets()
    assert s["ICAL_FOYER"] == "https://exemple/neuf.ics"  # /etc/homeos prime sur l'ancien fichier
    assert s["ICAL_ALEX"] == "https://a"  # repli pendant la migration
    assert s["COFFRE_CLE"] == "abc" and "PAS_LU" not in s
    assert charger_secrets(data, secrets)["ICAL_FOYER"] == "https://exemple/neuf.ics"


@pytest.mark.skipif(os.geteuid() == 0, reason="root lit tout : test des droits impossible")
def test_secret_illisible_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = tmp_path / "etc"
    secrets.mkdir()
    (secrets / "restic.env").write_text("RESTIC_PASSWORD=x\n", encoding="utf-8")
    (secrets / "restic.env").chmod(0)
    monkeypatch.setenv("HOMEOS_SECRETS", str(secrets))
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path / "data"))
    assert "RESTIC_PASSWORD" not in common.load_secrets()


def test_etat_hors_donnees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path / "data"))
    assert common.etat_dir() == tmp_path / "data" and not common.etat_separe()
    assert common.chemin_kd() == tmp_path / "data" / ".kd"
    monkeypatch.setenv("HOMEOS_ETAT", str(tmp_path / "etat"))
    assert common.etat_separe() and common.chemin_kd() == tmp_path / "etat" / "kd"
    from tools import coffre

    assert coffre.BackendFichier().chemin == tmp_path / "etat" / "coffre.json"


def test_proxy_sortant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in common.VARIABLES_PROXY:
        monkeypatch.delenv(var, raising=False)
    assert common.proxy_sortant() == {}
    fichier = tmp_path / "proxy"
    fichier.write_text("HTTPS_PROXY=http://127.0.0.1:3128\nNO_PROXY=127.0.0.1\nAUTRE=x\n", encoding="utf-8")
    monkeypatch.setenv("HOMEOS_FICHIER_PROXY", str(fichier))
    assert common.proxy_sortant() == {"HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": "127.0.0.1"}
    from tools import kitchen_daily

    opts = kitchen_daily.options_lancement()
    assert opts["proxy"] == {"server": "http://127.0.0.1:3128", "bypass": "127.0.0.1"} and opts["headless"]


def test_agenda_prive() -> None:
    commun = {"nom": "Foyer", "prive": None}
    prive = {"nom": "Perso", "prive": "alex"}
    assert agenda.agenda_visible(commun, "camille") and agenda.agenda_visible(commun, "homeos")
    assert agenda.agenda_visible(prive, "alex")
    assert not agenda.agenda_visible(prive, "camille") and not agenda.agenda_visible(prive, "homeos")


def test_agenda_prive_filtre_lecture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(
        "agendas:\n  - {nom: Foyer, ical_url_env: ICAL_FOYER}\n  - {nom: Perso, ical_url_env: ICAL_PERSO, prive: alex}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("ICAL_FOYER", "https://exemple/foyer.ics")
    monkeypatch.setenv("ICAL_PERSO", "https://exemple/perso.ics")
    lus: list[str] = []
    monkeypatch.setattr(agenda, "_lire_ics", lambda url, nom: lus.append(nom))
    from datetime import date

    monkeypatch.setenv("HOMEOS_PERSONNE", "camille")
    agenda.agenda_lire(date(2026, 11, 7), date(2026, 11, 8))
    assert lus == ["Foyer"]
    lus.clear()
    monkeypatch.setenv("HOMEOS_PERSONNE", "alex")
    agenda.agenda_lire(date(2026, 11, 7), date(2026, 11, 8))
    assert lus == ["Foyer", "Perso"]


def test_capacites_reseau(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("HOMEOS_CAPABILITIES", raising=False)
    hotes = capacites.reseau(racine=RACINE / "capabilities", donnees=tmp_path / "data")
    assert "www.kitchen-daily.fr" in hotes and hotes == sorted(set(hotes))
    assert capacites.RE_HOTE.match("*.exemple.fr") and not capacites.RE_HOTE.match("https://x.fr/a")
    assert not capacites.RE_HOTE.match("x.fr:443") and not capacites.RE_HOTE.match("a.*.fr")


def test_mcp_json_sans_secret() -> None:
    conf = json.loads((RACINE / "tools" / "mcp.json").read_text(encoding="utf-8"))
    env = conf["mcpServers"]["homeos"]["env"]
    assert env["HOMEOS_ETAT"] == "/var/lib/homeos" and env["DISABLE_AUTOUPDATER"] == "1"
    assert not any("KEY" in k or "TOKEN" in k or "PASSWORD" in k for k in env)


def test_mesures_du_mois_et_restauration(tmp_path: Path) -> None:
    """Revue P2 / R8 : dépense et latence médiane du mois, dernier test de restauration dans /statut."""
    from bot import commands
    from bot.db import Db

    db = Db(tmp_path / "homeos.sqlite")
    db.init()
    db.log("Alex", "message_sortant", "a", {"cout_usd": 0.10, "duree_ms": 3000})
    db.log("Alex", "message_sortant", "b", {"cout_usd": 0.05, "duree_ms": 5000})
    db.log("Alex", "message_sortant", "c", {"outils": 0})
    db.log(None, "brief_jour", "brief", {"cout_usd": 0.02})
    m = db.mesures_mois("2000-01-01")
    assert m == {"cout_usd": 0.17, "reponses": 3, "latence_mediane_ms": 4000}

    cfg = parse_config({}, tmp_path, tmp_path)
    assert commands._derniere_restauration(cfg) == "jamais testée"
    (tmp_path / ".derniere-restauration").write_text(
        json.dumps({"date": "2026-11-03T05:10:00+01:00", "ok": True, "depot": "distant", "fichiers": 412}),
        encoding="utf-8",
    )
    assert commands._derniere_restauration(cfg) == "✅ 2026-11-03 05:10 (distant, 412 fichiers)"
    (tmp_path / ".derniere-restauration").write_text(json.dumps({"ok": False, "message": "base corrompue"}), encoding="utf-8")
    assert "base corrompue" in commands._derniere_restauration(cfg)
    assert commands._copie_hors_site(cfg) == "non configurée"
