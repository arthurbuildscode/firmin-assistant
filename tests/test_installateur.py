"""Lot 6 : questionnaire d'installation d'un foyer (install/configurer.py) et script d'installation guidée."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bot.config import parse_config

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("configurer", RACINE / "install" / "configurer.py")
assert spec and spec.loader
configurer = importlib.util.module_from_spec(spec)
sys.modules["configurer"] = configurer
spec.loader.exec_module(configurer)

REPONSES = {
    "assistant": "Zébulon",
    "commune": "Villeneuve",
    "adultes": [
        {"prenom": "Alex", "telegram_id": 111111111},
        {"prenom": "Élodie Marie", "telegram_id": "222222222", "role": "membre", "claude": "cle_api",
         "ical": "https://agenda.exemple.fr/prive.ics"},
    ],
    "enfants": [{"prenom": "Nina", "age": "CE2"}],
    "animaux": [{"nom": "Rex", "espece": "chien"}],
    "telegram_token": "123456789:AAEfauxjetonpourlestestsXXXXXXXXXXXXX",
    "groupe_telegram_id": -100123456789,
    "cle_api": "sk-ant-api03-fausse-cle-de-test-XXXXXXXX",
    "brief_jour": "6h45",
    "brief_hebdo": "sam 18h",
    "bus": True,  # anciennes réponses : réseau Cartreize
}


def test_validations() -> None:
    assert configurer.slug("Élodie Marie") == "elodie-marie"
    assert configurer.slug("2pac") == "p-2pac"
    assert configurer.identifiant_valide("alex") is None
    assert configurer.identifiant_valide("root") and configurer.identifiant_valide("Alex")
    assert configurer.heure("7h") == "07:00" and configurer.heure("19:30") == "19:30"
    assert configurer.jour_heure("dimanche 19h") == "Sun 19:00"
    assert configurer.telegram_id("-100123456789", groupe=True) == -100123456789
    for mauvais, appel in (
        ("25h", configurer.heure),
        ("abc", configurer.telegram_id),
        ("123:court", configurer.token_telegram),
        ("cle", configurer.cle_api),
        ("http://pas-https", configurer.url_ical),
        ("peut-être", configurer.oui_non),
        ("Firmin $(rm -rf /)", configurer.prenom_assistant),
        ("tskey'", configurer.cle_tailscale),
    ):
        with pytest.raises(ValueError):
            appel(mauvais)
    with pytest.raises(ValueError):
        configurer.telegram_id("123456", groupe=True)  # un groupe est négatif


def test_reponses_vers_fichiers(tmp_path: Path) -> None:
    r = configurer.depuis_dict(REPONSES)
    configurer.ecrire(r, tmp_path / "sortie")
    sortie = tmp_path / "sortie"
    assert {f.name for f in sortie.iterdir()} == {"config.yaml", "secrets.env", "identite.md", "choix.env"}
    assert all((f.stat().st_mode & 0o077) == 0 for f in sortie.iterdir())

    brut = yaml.safe_load((sortie / "config.yaml").read_text(encoding="utf-8"))
    cfg = parse_config(brut, tmp_path, RACINE)
    assert cfg.assistant_nom == "Zébulon" and cfg.groupe_telegram_id == -100123456789
    assert [(p.prenom, p.linux_user, p.role, p.auth) for p in cfg.personnes] == [
        ("Alex", "alex", "proprietaire", "abonnement"),
        ("Élodie Marie", "elodie-marie", "membre", "cle_api"),
    ]
    assert brut["crons"] == {"brief_jour": "06:45", "brief_hebdo": "Sat 18:00"}
    assert brut["agendas"] == [{"nom": "Élodie Marie", "ical_url_env": "ICAL_ELODIE_MARIE"}]
    assert brut["meteo"] == {"commune": "Villeneuve"}
    assert "kitchen_daily" not in brut  # non abonné : pas de section

    secrets = (sortie / "secrets.env").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=123456789:AAE" in secrets and "ICAL_ELODIE_MARIE=https://agenda.exemple.fr/prive.ics" in secrets
    assert "sk-ant" not in (sortie / "config.yaml").read_text(encoding="utf-8")

    identite = (sortie / "identite.md").read_text(encoding="utf-8")
    assert "Nina (CE2)" in identite and "Rex (chien)" in identite and "Commune : Villeneuve" in identite
    choix = (sortie / "choix.env").read_text(encoding="utf-8")
    assert "TRANSPORTS=oui" in choix and "RESEAUX='cartreize'" in choix and "KITCHEN_DAILY=non" in choix and "HOTE_TAILSCALE=zebulon" in choix
    assert "PERSONNES='alex:abonnement elodie-marie:cle_api'" in choix


def test_reponses_incoherentes() -> None:
    for modif, message in (
        ({"adultes": []}, "au moins un adulte"),
        ({"adultes": [{"prenom": "Root", "identifiant": "root", "telegram_id": 111}]}, "réservé"),
        ({"adultes": [{"prenom": "A", "telegram_id": 111, "role": "membre"}]}, "propriétaire"),
        ({"vaultwarden": True, "tailscale": False}, "Tailscale"),
    ):
        with pytest.raises(ValueError, match=message):
            configurer.depuis_dict({**REPONSES, **modif})


def test_questionnaire_interactif(tmp_path: Path) -> None:
    """Le parcours au clavier, réponses tapées une à une (getpass lit l'entrée standard sans terminal)."""
    saisies = [
        "",  # prénom de l'assistant : défaut
        "oui",  # tutoiement
        "Villeneuve",
        "1",  # un adulte
        "Camille",
        "",  # identifiant proposé
        "",  # rôle : propriétaire
        "333333333",
        "abonnement",
        "oui", "Sam", "5 ans", "non",  # un enfant
        "non",  # pas d'animal
        "", "",  # Telegram plus tard
        "",  # clé API plus tard
        "", "",  # horaires par défaut
        "",  # pas d'agenda commun
        "",  # pas d'agenda personnel
        "non", "non",  # capacités
        "non",  # pas de Tailscale
        "non",  # pas de copie hors site
        "oui",  # confirmation
    ]
    sortie = tmp_path / "sortie"
    res = subprocess.run(
        [sys.executable, str(RACINE / "install" / "configurer.py"), "--sortie", str(sortie)],
        input="\n".join(saisies) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
    brut = yaml.safe_load((sortie / "config.yaml").read_text(encoding="utf-8"))
    assert brut["assistant"]["nom"] == configurer.NOM_ASSISTANT_DEFAUT
    assert brut["personnes"][0] == {"telegram_id": 333333333, "prenom": "Camille", "linux_user": "camille",
                                    "role": "proprietaire", "auth": "abonnement"}
    assert brut["groupe_telegram_id"] is None and brut["agendas"] == []
    assert "Sam (5 ans)" in (sortie / "identite.md").read_text(encoding="utf-8")
    assert "TELEGRAM=non" in (sortie / "choix.env").read_text(encoding="utf-8")


def test_abandon_n_ecrit_rien(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(RACINE / "install" / "configurer.py"), "--sortie", str(tmp_path / "s")],
        input="", capture_output=True, text=True, timeout=30, check=False,
    )
    assert res.returncode == 1 and not (tmp_path / "s").exists()


def test_scripts_shell() -> None:
    for script in ("install/installer-un-foyer.sh", "install/tailscale.sh", "scripts/empaqueter.sh"):
        assert subprocess.run(["bash", "-n", str(RACINE / script)], capture_output=True, check=False).returncode == 0, script
    aide = subprocess.run(["bash", str(RACINE / "install" / "installer-un-foyer.sh"), "--help"],
                          capture_output=True, text=True, timeout=10, check=False)
    assert aide.returncode == 0 and "--reponses" in aide.stdout


def test_exemple_de_reponses_documente() -> None:
    """L'exemple de docs/installation.md est accepté tel quel par le questionnaire."""
    texte = (RACINE / "docs" / "installation.md").read_text(encoding="utf-8")
    bloc = texte.split("```json", 1)[1].split("```", 1)[0]
    r = configurer.depuis_dict(json.loads(bloc))
    assert r.adultes and r.assistant
