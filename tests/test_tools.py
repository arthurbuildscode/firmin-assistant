"""Tests des outils (`tools/`) : SQLite temporaire, iCal local, sans réseau."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dossier data/ isolé + config.yaml pointant sur la fixture iCal (URL via variable d'env)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.delenv("HOMEOS_NOW", raising=False)
    (data / "config.yaml").write_text(
        "assistant:\n  nom: Testbot\nagendas:\n  - nom: Foyer\n    ical_url_env: ICAL_FOYER\n    ecriture: false\n"
        "  - nom: Alex\n    ical_url_env: ICAL_ALEX\n",
        encoding="utf-8",
    )
    (data / "secrets.env").write_text(
        f"ICAL_FOYER=file://{FIXTURES / 'foyer.ics'}\nTELEGRAM_BOT_TOKEN=fake-token-ne-doit-pas-sortir\n",
        encoding="utf-8",
    )
    return data


# --- base & actions_log -----------------------------------------------------


def test_db_tables_idempotentes(data_tmp: Path) -> None:
    from tools import db

    db.connect().close()
    conn = db.connect()
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"courses", "echeances", "anniversaires", "actions_log", "feedback"} <= tables
    aid = db.log_action("test", "bonjour", {"a": 1}, "done", "ok")
    action = db.get_action(aid)
    assert action is not None and action["who"] == "test" and action["payload"] == {"a": 1}


# --- agenda -----------------------------------------------------------------


def test_agenda_lire_fixture(data_tmp: Path) -> None:
    from tools import agenda

    evs = agenda.agenda_lire(date(2026, 11, 9), date(2026, 11, 15))
    titres = [e["titre"] for e in evs]
    assert "Rendez-vous (exemple)" in titres
    assert "Atelier (hebdo)" in titres  # occurrence récurrente du mercredi 11/11
    assert "Armistice (journée entière)" in titres
    ferie = next(e for e in evs if e["journee_entiere"])
    assert ferie["debut"] == "2026-11-11" and ferie["fin"] == "2026-11-11"
    rdv = next(e for e in evs if e["titre"].startswith("Rendez-vous"))
    assert rdv["debut"].startswith("2026-11-10T14:30") and rdv["lieu"] == "Cabinet du village"
    assert all(e["agenda"] == "Foyer" for e in evs)
    # tri chronologique
    assert [e["debut"][:10] for e in evs] == sorted(e["debut"][:10] for e in evs)
    # aucune fuite d'URL ni de secret
    dump = json.dumps(evs)
    assert "file://" not in dump and "fake-token" not in dump
    # cache écrit
    assert list((data_tmp / ".cache").glob("*.ics"))


def test_agenda_recurrence_plusieurs_semaines(data_tmp: Path) -> None:
    from tools import agenda

    evs = agenda.agenda_lire(date(2026, 11, 1), date(2026, 11, 30), agendas=["Foyer"])
    ateliers = [e for e in evs if e["titre"] == "Atelier (hebdo)"]
    assert len(ateliers) == 4  # 4, 11, 18, 25 novembre 2026 (mercredis)


def test_agenda_filtre_et_url_absente(data_tmp: Path) -> None:
    from tools import agenda

    assert agenda.agenda_lire(date(2026, 11, 9), date(2026, 11, 15), agendas=["Alex"]) == []


def test_agenda_ajouter_sans_google(data_tmp: Path) -> None:
    from tools import agenda, db

    texte = agenda.agenda_ajouter("Alex", "Réunion", "2026-11-12T18:30", "2026-11-12T19:30", lieu="Visio")
    assert "CONFIRMATION_REQUISE" not in texte
    assert "Titre : Réunion" in texte and "Visio" in texte
    assert db.recent_actions(1)[0]["kind"] == "agenda_ajouter"


def test_agenda_ajouter_google_configure(data_tmp: Path) -> None:
    from tools import agenda, db

    (data_tmp / "google-oauth.json").write_text("{}", encoding="utf-8")
    (data_tmp / "google-token.json").write_text("{}", encoding="utf-8")
    texte = agenda.agenda_ajouter("Alex", "Réunion", "2026-11-12T18:30", "2026-11-12T19:30")
    assert texte.startswith("CONFIRMATION_REQUISE:")
    aid = int(texte.split(":")[1])
    action = db.get_action(aid)
    assert action is not None and action["status"] == "pending" and action["payload"]["titre"] == "Réunion"
    # agenda en lecture seule → texte manuel même avec Google
    assert "lecture seule" in agenda.agenda_ajouter("Foyer", "X", "2026-11-12T18:30", "2026-11-12T19:30")


# --- courses ----------------------------------------------------------------


def test_courses_cycle(data_tmp: Path) -> None:
    from tools import courses

    ajout = courses.ajouter(["Lait", "pommes", "lessive", "truc bizarre"])
    rayons = {a["article"]: a["rayon"] for a in ajout}
    assert rayons["Lait"] == "laitier"
    assert rayons["pommes"] == "fruits-légumes"
    assert rayons["lessive"] == "maison"
    assert rayons["truc bizarre"] == "autre"
    assert courses.ajouter(["lait"])[0]["statut"] == "déjà présent"
    assert courses.ajouter(["eau gazeuse"], rayon="boissons")[0]["rayon"] == "boissons"

    res = courses.cocher(["LAIT", "pomme", "inexistant"])
    assert res["coches"] == ["Lait", "pommes"] and res["introuvables"] == ["inexistant"]
    liste = courses.lister()
    assert {a["article"]: a["coche"] for a in liste}["Lait"] is True
    assert len(courses.lister(inclure_coches=False)) == 3
    assert courses.vider_coches() == 2
    assert len(courses.lister()) == 3
    # réactivation d'un article coché
    courses.cocher(["lessive"])
    assert courses.ajouter(["lessive"])[0]["statut"] == "réactivé"


# --- échéances & anniversaires ---------------------------------------------


def test_echeances_rappels(data_tmp: Path) -> None:
    from tools import echeances

    ref = date(2026, 11, 7)
    echeances.ajouter("Contrôle technique", "2026-12-07", rappels_jours=[30, 7, 1])  # J-30
    echeances.ajouter("Vaccin chien", "2026-11-07", recurrence="annuelle")  # J-0
    echeances.ajouter("Loyer", "2026-01-15", recurrence="mensuelle")  # prochaine 15/11, pas de rappel
    echeances.ajouter("Impôts", "2026-10-15")  # en retard
    echeances.ajouter("Assurance", "2027-03-01", recurrence="trimestrielle")
    echeances.ajouter("Poubelles", "2026-11-09", recurrence="hebdomadaire", rappels_jours=[1])
    with pytest.raises(ValueError):
        echeances.ajouter("Mauvaise", "2026-11-09", recurrence="lunaire")

    liste = echeances.lister(60, ref)
    par_titre = {e["titre"]: e for e in liste}
    assert par_titre["Loyer"]["prochaine"] == "2026-11-15"
    assert par_titre["Impôts"]["en_retard"] is True
    assert "Assurance" not in par_titre

    rappels = echeances.rappels_du_jour(ref)
    motifs = {r["titre"]: r["motif"] for r in rappels}
    assert motifs["Contrôle technique"] == "J-30"
    assert motifs["Vaccin chien"] == "aujourd'hui"
    assert motifs["Impôts"].startswith("en retard")
    assert "Loyer" not in motifs and "Poubelles" not in motifs
    assert echeances.supprimer(par_titre["Impôts"]["id"]) is True
    assert echeances.supprimer(9999) is False


def test_anniversaires_rappels(data_tmp: Path) -> None:
    from tools import anniversaires

    ref = date(2026, 11, 7)
    anniversaires.ajouter("Papi", "1950-11-28", delai_cadeau_jours=21)  # J-21 → cadeau
    anniversaires.ajouter("Copine", "14/11")  # J-7, année inconnue
    anniversaires.ajouter("Jumeau", "2018-11-07")  # J-0
    anniversaires.ajouter("Loin", "1990-03-02")

    liste = anniversaires.lister(60, ref)
    assert [a["prenom"] for a in liste] == ["Jumeau", "Copine", "Papi"]
    assert next(a for a in liste if a["prenom"] == "Papi")["age"] == 76
    assert next(a for a in liste if a["prenom"] == "Copine")["age"] is None

    rappels = anniversaires.rappels_du_jour(ref)
    motifs = {r["prenom"]: r["motif"] for r in rappels}
    assert motifs == {"Papi": "cadeau à prévoir", "Copine": "J-7", "Jumeau": "c'est aujourd'hui"}
    assert next(r for r in rappels if r["prenom"] == "Jumeau")["age"] == 8


def test_rappels_du_jour_agregat(data_tmp: Path) -> None:
    from tools import anniversaires, echeances
    from tools.server import rappels_du_jour_data

    ref = date(2026, 11, 7)
    echeances.ajouter("CT", "2026-11-08", rappels_jours=[1])
    anniversaires.ajouter("Mamie", "1945-11-14")
    agg = rappels_du_jour_data(ref)
    assert agg["date"] == "2026-11-07"
    assert [r["titre"] for r in agg["echeances"]] == ["CT"]
    assert [r["prenom"] for r in agg["anniversaires"]] == ["Mamie"]


# --- mémoire & journal ------------------------------------------------------


def test_memoire_restrictions(data_tmp: Path) -> None:
    from tools import memoire

    for mauvais in ("../secrets.env", "foyer/../secrets.env", "/etc/passwd", "secrets.env", "homeos.sqlite",
                    "foyer", "foyer/notes.txt", "onboarding/x.md", "~/x.md"):
        with pytest.raises(memoire.CheminInterdit):
            memoire.resoudre(mauvais)
    assert memoire.resoudre("foyer/identite.md") == (data_tmp / "foyer" / "identite.md").resolve()
    assert memoire.resoudre("data/personnes/alex.md").name == "alex.md"


def test_memoire_cycle_et_git(data_tmp: Path) -> None:
    from tools import memoire

    contenu = memoire.lire("foyer/identite.md")
    assert contenu.startswith("# Identité du foyer")
    memoire.ajouter("personnes/alex.md", "- Aime le café")
    assert "- Aime le café" in memoire.lire("personnes/alex.md")
    memoire.ecrire("alimentation/foyer.md", "# Alimentation\n- Sans arachide")
    assert memoire.lire("alimentation/foyer.md") == "# Alimentation\n- Sans arachide\n"
    with pytest.raises(ValueError):
        memoire.ajouter("foyer/contacts.md", "   ")
    if (data_tmp / ".git").exists():
        log_git = subprocess.run(
            ["git", "-C", str(data_tmp), "log", "--oneline"], capture_output=True, text=True, check=False
        ).stdout
        assert "homeos: memoire_ecrire alimentation/foyer.md" in log_git
        assert "secrets.env" in (data_tmp / ".gitignore").read_text(encoding="utf-8")


def test_journal(data_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import journal

    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T09:15:00")
    journal.ajouter("Première entrée")
    journal.ajouter("Deuxième entrée")
    fichier = data_tmp / "foyer" / "journal" / "2026-11.md"
    assert fichier.exists()
    entrees = journal.dernieres_entrees(5)
    assert [e["texte"] for e in entrees] == ["Deuxième entrée", "Première entrée"]
    assert entrees[0]["quand"] == "2026-11-07 09:15" and entrees[0]["qui"] == "test"


# --- onboarding & feedback --------------------------------------------------


def test_onboarding_feedback(data_tmp: Path) -> None:
    from tools import feedback, onboarding

    assert onboarding.etat()["modules"] == {}
    res = onboarding.terminer("00-identite-foyer", {"prenoms": "A et B"})
    assert res["done"] is True
    etat = onboarding.etat()
    assert etat["modules"]["00-identite-foyer"]["answers"] == {"prenoms": "A et B"}
    assert etat["tout_fait"] is True
    with pytest.raises(ValueError):
        onboarding.terminer("../x")

    fb = feedback.noter("écran courses", 4, "clair")
    assert fb["id"] and feedback.lister()[0]["note"] == 4
    with pytest.raises(ValueError):
        feedback.noter("x", 9)


# --- serveur MCP ------------------------------------------------------------


def test_server_outils_exposes(data_tmp: Path) -> None:
    import asyncio

    from tools import db, server

    attendus = {
        "agenda_lire", "agenda_ajouter", "courses_lister", "courses_ajouter", "courses_cocher",
        "courses_vider_coches", "echeances_lister", "echeances_ajouter", "echeances_supprimer",
        "anniversaires_lister", "anniversaires_ajouter", "memoire_lire", "memoire_ecrire", "memoire_ajouter",
        "journal_ajouter", "onboarding_etat", "onboarding_terminer", "feedback_noter", "rappels_du_jour",
    }
    attendus |= {"meteo_lire"}  # retour d'usage (tests/test_solutions.py)
    attendus |= {"fichiers_ranger", "fichiers_lister", "fichiers_entrants", "photos_journal_ajouter"}  # lot 3
    attendus |= {"kitchen_daily_livraisons", "kitchen_daily_menu", "kitchen_daily_preferences", "kitchen_daily_proposer", "kitchen_daily_historique_ajouter"}  # lot 2, capacité depuis le lot 5a
    attendus |= {"capacites_lister", "capacites_statut", "capacites_parametrer"}  # lot 5a
    attendus |= {"identifiants_lister", "identifiants_preparer", "identifiants_renommer", "identifiants_attribuer",
                 "identifiants_supprimer", "identifiants_ranger_quarantaine"}  # lot 5c
    outils = asyncio.run(server.mcp.list_tools())
    noms = {t.name for t in outils}
    assert attendus <= noms
    # Les autres outils viennent des capacités chargées (préfixés par le nom de leur capacité, ou par le
    # `prefixe` déclaré : administration → admin_*, lot 7a).
    from tools import capacites

    prefixes = {capacites.trouver(c).prefixe for c in server.CAPACITES_CHARGEES}
    assert all(any(n.startswith(f"{p}_") for p in prefixes) for n in noms - attendus)
    assert all(t.description for t in outils)
    # un appel via le wrapper journalise dans actions_log
    server.courses_ajouter(["pain"])
    assert db.recent_actions(1)[0]["kind"] == "courses_ajouter"
    with pytest.raises(ValueError):
        server.memoire_lire("../secrets.env")
    assert db.recent_actions(1)[0]["status"] == "error"


# --- runner -----------------------------------------------------------------


def _run_runner(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.runner", *args],
        cwd=str(RACINE),
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_runner_brief_dry_run(data_tmp: Path) -> None:
    from tools import courses, echeances, journal

    courses.ajouter(["beurre"])
    echeances.ajouter("CT", "2026-11-14", rappels_jours=[7])
    journal.ajouter("Visite chez le vétérinaire")
    env = {k: v for k, v in os.environ.items() if k.startswith("HOMEOS_")}

    proc = _run_runner("brief", "jour", "--dry-run", "--date", "2026-11-07", env=env)
    assert proc.returncode == 0, proc.stderr
    prompt = proc.stdout
    assert "<donnees>" in prompt and "</donnees>" in prompt
    assert "Testbot" in prompt and "{{assistant.nom}}" not in prompt
    donnees = json.loads(prompt.split("<donnees>", 1)[1].split("</donnees>", 1)[0])
    assert donnees["aujourdhui"] == "2026-11-07" and donnees["jour_semaine"] == "samedi"
    assert [c["article"] for c in donnees["courses"]] == ["beurre"]
    assert donnees["rappels_du_jour"]["echeances"][0]["titre"] == "CT"
    assert donnees["journal"][0]["texte"] == "Visite chez le vétérinaire"
    assert len(donnees["agenda"]) == 0  # rien le 7/11 dans la fixture
    assert "fake-token" not in prompt and "file://" not in prompt

    proc = _run_runner("brief", "hebdo", "--dry-run", "--date", "2026-11-09", env=env)
    assert proc.returncode == 0, proc.stderr
    donnees = json.loads(proc.stdout.split("<donnees>", 1)[1].split("</donnees>", 1)[0])
    assert {e["titre"] for e in donnees["agenda"]} == {"Rendez-vous (exemple)", "Atelier (hebdo)", "Armistice (journée entière)"}


def test_runner_prompt_personnalise_et_kitchen(data_tmp: Path, tmp_path: Path) -> None:
    prompts = tmp_path / "content" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "rappel-kitchen-daily.md").write_text(
        "Salut, je suis {{assistant.nom}}.\n<donnees>\n</donnees>\nFin.", encoding="utf-8"
    )
    (data_tmp / "alimentation").mkdir()
    (data_tmp / "alimentation" / "foyer.md").write_text("# Alim\n- Pas de coriandre", encoding="utf-8")
    (data_tmp / "config.yaml").write_text(
        (data_tmp / "config.yaml").read_text(encoding="utf-8") + "kitchen_daily:\n  date_limite: 2026-12-31\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k.startswith("HOMEOS_")}
    proc = _run_runner("rappel", "kitchen-daily", "--dry-run", "--date", "2026-11-10", env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Salut, je suis Testbot.") and proc.stdout.rstrip().endswith("Fin.")
    donnees = json.loads(proc.stdout.split("<donnees>", 1)[1].split("</donnees>", 1)[0])
    assert "Pas de coriandre" in donnees["alimentation"]
    assert donnees["date_limite"] == "2026-12-31"
    assert donnees["jour_semaine"] == "mardi"


def test_runner_execute(data_tmp: Path) -> None:
    from tools import db, runner

    aid = db.log_action("inconnu", "action bidon", {}, "pending")
    assert runner.main(["execute", str(aid)]) == 5
    assert db.get_action(aid)["status"] == "error"
    assert runner.main(["execute", "99999"]) == 2
    aid2 = db.log_action("agenda_ajouter", "ajout", {"agenda": "Alex", "titre": "T", "debut": "x", "fin": "y"}, "pending")
    code = runner.main(["execute", str(aid2)])
    assert code == 1  # bibliothèques Google absentes ou jeton absent → échec propre
    assert db.get_action(aid2)["status"] == "error"
