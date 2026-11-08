"""Tests du lot 7a — administration par la conversation : liste blanche, validation, sauvegarde et retour
arrière, rôle propriétaire, horaires en français, rechargement à chaud de la configuration, journal, application
des timers. Sans réseau, sans systemd, sans Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent
CAP_ADMIN = RACINE / "capabilities" / "administration"

CONFIG = """\
# Configuration de test (commentaire à conserver)
assistant:
  nom: "Testbot"
personnes:
  - telegram_id: 111
    prenom: Alex
    linux_user: alex          # sans rôle : propriétaire par défaut
  - telegram_id: 222
    prenom: Camille
    linux_user: camille
    role: membre
crons:
  brief_hebdo: "Sun 19:00"
  brief_jour: "07:15"         # commentaire du brief
capacites:
  kitchen_daily:
    validation: humaine
    crons:
      rappel: "Wed 18:00"
"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(RACINE))
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T12:00:00")
    monkeypatch.delenv("HOMEOS_CAPABILITIES", raising=False)
    monkeypatch.delenv("HOMEOS_PERSONNE", raising=False)
    monkeypatch.delenv("HOMEOS_WHO", raising=False)
    return data


def charger_admin(data: Path, personne: str | None) -> Any:
    from tools import capacites

    mcp = capacites.MCPFactice()
    capacites.charger_capacite(capacites.lire_capacite(CAP_ADMIN), mcp, donnees=data, personne=personne)
    return mcp


def id_confirmation(texte: str) -> int:
    assert texte.startswith("CONFIRMATION_REQUISE:"), texte
    return int(texte.split(":")[1])


def valider_action(action_id: int) -> str:
    """Comme le bouton Valider : tools.runner execute <id> ; renvoie le message de l'action."""
    from tools import db, runner

    assert runner.executer(action_id) == 0
    action = db.get_action(action_id)
    assert action is not None and action["status"] == "done", action
    return str(action["result"])


# --- horaires ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texte", "attendu"),
    [
        ("mercredi 12h", "Wed 12:00"),
        ("Mercredi à midi", "Wed 12:00"),
        ("tous les jours 7h15", "07:15"),
        ("tous les jours à 07:15", "07:15"),
        ("Wed 18:00", "Wed 18:00"),
        ("wed 8:00", "Wed 08:00"),
        ("du lundi au vendredi 7h", "Mon..Fri 07:00"),
        ("en semaine à 7h45", "Mon..Fri 07:45"),
        ("lundi et jeudi 8h30", "Mon,Thu 08:30"),
        ("le week-end à 9h", "Sat,Sun 09:00"),
        ("chaque mardi à 18 heures", "Tue 18:00"),
        ("mercredi 6h du soir", "Wed 18:00"),
        ("7h et demie", "07:30"),
    ],
)
def test_horaires_analyse(texte: str, attendu: str) -> None:
    from tools import capacites, horaires

    assert horaires.analyser(texte) == attendu
    capacites.oncalendar(attendu)  # toujours accepté par le cadre des capacités (timers)


@pytest.mark.parametrize("texte", ["mercredi", "25h", "", "jeudi 12h70", "Xyz 10:00"])
def test_horaires_refus(texte: str) -> None:
    from tools import horaires

    with pytest.raises(ValueError):
        horaires.analyser(texte)


def test_horaires_lisible() -> None:
    from tools import horaires

    assert horaires.lisible("Wed 12:00") == "le mercredi à midi"
    assert horaires.lisible("Tue 18:00") == "le mardi à 18h"
    assert horaires.lisible("07:15") == "tous les jours à 7h15"
    assert horaires.lisible("Mon..Fri 07:00") == "en semaine à 7h"
    assert horaires.lisible("Mon,Thu 08:30") == "le lundi et le jeudi à 8h30"


# --- liste blanche et validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "cle",
    ["capacites.kitchen_daily.validation", "capacites.bus.crons.rafraichir_gtfs", "crons.brief_jour",
     "capacites.bus.parametres.seuil"],
)
def test_liste_blanche_accepte(cle: str) -> None:
    from tools import config_edition

    assert config_edition.verifier_cle(cle) == cle.split(".")


@pytest.mark.parametrize(
    "cle",
    ["personnes", "assistant.nom", "groupe_telegram_id", "agendas", "crons.porteur_linux_user",
     "capacites.bus.parametres.token", "capacites.x.parametres.mot_de_passe", "coffre.backend",
     "capacites.kitchen_daily", "plafonds.confirmation_requise_eur", "../config", "crons.brief_jour.x"],
)
def test_liste_blanche_refuse(env: Path, cle: str) -> None:
    from tools import config_edition

    with pytest.raises(config_edition.CleRefusee):
        config_edition.modifier(cle, "x", "Alex")
    assert (env / "config.yaml").read_text(encoding="utf-8") == CONFIG


def test_validation_refusee_rien_n_est_ecrit(env: Path) -> None:
    from tools import config_edition

    for cle, valeur in [("crons.brief_jour", "25:00"), ("capacites.kitchen_daily.crons.rappel", "jamais"),
                        ("capacites.kitchen_daily.validation", "peut-etre")]:
        with pytest.raises(config_edition.ConfigInvalide):
            config_edition.modifier(cle, valeur, "Alex")
    with pytest.raises(config_edition.ConfigInvalide):
        config_edition.modifier("crons.brief_jour", "https://exemple.org", "Alex")
    assert (env / "config.yaml").read_text(encoding="utf-8") == CONFIG
    assert config_edition.sauvegardes(env) == []
    assert config_edition.lire_journal(env) == []


def test_valider_detecte_personne_illisible_et_role_inconnu(env: Path) -> None:
    from tools import config_edition

    brut = yaml.safe_load(CONFIG)
    config_edition.valider(brut)
    casse = yaml.safe_load(CONFIG)
    del casse["personnes"][0]["telegram_id"]
    with pytest.raises(config_edition.ConfigInvalide, match="personne"):
        config_edition.valider(casse)
    casse = yaml.safe_load(CONFIG)
    casse["personnes"][1]["role"] = "chef"
    with pytest.raises(config_edition.ConfigInvalide, match="Rôle"):
        config_edition.valider(casse)


def test_modifier_sauvegarde_journal_et_commentaires(env: Path) -> None:
    from tools import config_edition

    entree = config_edition.modifier("crons.brief_jour", "07:30", "Alex", "brief du matin à 7h30")
    texte = (env / "config.yaml").read_text(encoding="utf-8")
    assert '"07:30"' in texte
    assert "# commentaire du brief" in texte and "commentaire à conserver" in texte  # ruamel.yaml
    assert yaml.safe_load(texte)["crons"]["brief_jour"] == "07:30"
    (sauvegarde,) = config_edition.sauvegardes(env)
    assert sauvegarde.read_text(encoding="utf-8") == CONFIG
    assert entree["avant"] == "07:15" and entree["apres"] == "07:30" and entree["auteur"] == "Alex"
    (ligne,) = config_edition.lire_journal(env)
    assert ligne["cle"] == "crons.brief_jour" and ligne["id"] == 1
    # Nouvelle clé dans un bloc absent, puis suppression : le bloc vide disparaît.
    config_edition.modifier("capacites.bus.validation", "automatique", "Alex")
    assert yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))["capacites"]["bus"] == {
        "validation": "automatique"
    }
    config_edition.modifier("capacites.bus.validation", None, "Alex")
    assert "bus" not in yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))["capacites"]


def test_dix_sauvegardes_conservees(env: Path) -> None:
    from tools import config_edition

    for i in range(12):
        config_edition.modifier("crons.brief_jour", f"07:{10 + i:02d}", "Alex")
    restantes = config_edition.sauvegardes(env)
    assert len(restantes) == config_edition.NB_SAUVEGARDES
    # La plus récente contient l'avant-dernière valeur.
    assert yaml.safe_load(restantes[-1].read_text(encoding="utf-8"))["crons"]["brief_jour"] == "07:20"


def test_repli_pyyaml_sans_ruamel(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import config_edition

    monkeypatch.setattr(config_edition, "_ruamel", lambda: None)
    config_edition.modifier("capacites.kitchen_daily.crons.rappel", "Wed 12:00", "Alex")
    brut = yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))
    assert brut["capacites"]["kitchen_daily"]["crons"]["rappel"] == "Wed 12:00"
    assert brut["personnes"][1]["role"] == "membre"


# --- rôle propriétaire ---------------------------------------------------------------------------


def test_role_proprietaire(env: Path) -> None:
    from bot.config import load_config
    from tools import administration, capacites

    cfg = load_config(env, RACINE)
    assert cfg.personnes[0].role == "proprietaire" and cfg.personnes[0].proprietaire  # défaut
    assert cfg.personnes[1].role == "membre" and not cfg.personnes[1].proprietaire
    assert administration.verifier_proprietaire("alex") == "Alex"
    with pytest.raises(capacites.ActionRefusee, match="réservé aux propriétaires"):
        administration.verifier_proprietaire("camille")
    with pytest.raises(capacites.ActionRefusee, match="réservé aux propriétaires"):
        administration.verifier_proprietaire(None)
    with pytest.raises(capacites.ActionRefusee):
        administration.verifier_proprietaire("inconnu")


def test_outils_refuses_aux_non_proprietaires(env: Path) -> None:
    mcp = charger_admin(env, "camille")
    for nom, kwargs in [("admin_etat", {}), ("admin_journal", {}),
                        ("admin_cron", {"nom": "brief_jour", "cron": "", "quand": "7h30"}),
                        ("admin_capacite_desactiver", {"nom": "bus"}), ("admin_pause", {})]:
        with pytest.raises(ValueError, match="réservé aux propriétaires"):
            mcp.outils[nom](**kwargs)
    assert (env / "config.yaml").read_text(encoding="utf-8") == CONFIG


# --- parcours complets (outil → bouton Valider → runner) -----------------------------------------


def test_cron_kitchen_daily_valide_puis_annule(env: Path) -> None:
    from tools import config_edition, db

    mcp = charger_admin(env, "alex")
    reponse = mcp.outils["admin_cron"](nom="Kitchen Daily", cron="rappel", quand="mercredi 12h")
    aid = id_confirmation(reponse)
    assert "le mercredi à midi" in reponse and "au lieu de le mercredi à 18h" in reponse
    action = db.get_action(aid)
    assert action["kind"] == "admin_cron" and action["who"] == "Alex" and action["status"] == "pending"
    assert (env / "config.yaml").read_text(encoding="utf-8") == CONFIG  # rien avant Valider

    message = valider_action(aid)
    assert message.startswith("Fait. Le rappel Kitchen Daily est maintenant le mercredi à midi")
    assert "(avant : le mercredi à 18h)" in message and "annuler" in message
    brut = config_edition.lire_brut(env)
    assert brut["capacites"]["kitchen_daily"]["crons"]["rappel"] == "Wed 12:00"

    (entree,) = mcp.outils["admin_journal"](n=5)
    assert entree["auteur"] == "Alex" and entree["avant"] == "Wed 18:00" and entree["apres"] == "Wed 12:00"

    annulation = id_confirmation(mcp.outils["admin_annuler"](id=entree["id"]))
    assert "retour à la valeur précédente" in valider_action(annulation)
    assert config_edition.lire_brut(env)["capacites"]["kitchen_daily"]["crons"]["rappel"] == "Wed 18:00"
    journal = mcp.outils["admin_journal"](n=5)
    assert journal[0]["annule"] == entree["id"] and journal[1]["annule_par"] == journal[0]["id"]
    with pytest.raises(ValueError, match="déjà été annulé"):
        mcp.outils["admin_annuler"](id=entree["id"])


def test_annulation_refusee_si_valeur_changee(env: Path) -> None:
    from tools import administration, config_edition

    premiere = config_edition.modifier("crons.brief_jour", "07:30", "Alex")
    config_edition.modifier("crons.brief_jour", "08:00", "Alex")
    with pytest.raises(administration.ErreurAdministration, match="a changé depuis"):
        administration.preparer_annulation(premiere["id"], "Alex", donnees=env)


def test_brief_et_validation(env: Path) -> None:
    from bot.config import load_config
    from tools import config_edition

    mcp = charger_admin(env, "alex")
    valider_action(id_confirmation(mcp.outils["admin_cron"](nom="brief_jour", cron="", quand="tous les jours 7h30")))
    assert config_edition.lire_brut(env)["crons"]["brief_jour"] == "07:30"
    with pytest.raises(ValueError, match="déjà"):
        mcp.outils["admin_cron"](nom="brief_jour", cron="", quand="7h30")
    with pytest.raises(ValueError, match="Il manque l'heure"):
        mcp.outils["admin_cron"](nom="brief_jour", cron="", quand="mercredi")

    message = valider_action(id_confirmation(mcp.outils["admin_validation"](nom="kitchen_daily", mode="auto")))
    assert message.startswith("Fait.")
    cfg = load_config(env, RACINE)
    assert "kitchen_daily" in cfg.capacites_automatiques()
    assert cfg.action_automatique("kitchen_daily_selection")
    with pytest.raises(ValueError, match="Mode inconnu"):
        mcp.outils["admin_validation"](nom="kitchen_daily", mode="parfois")


def test_action_automatique_avec_prefixe(env: Path) -> None:
    from bot.config import load_config
    from tools import config_edition

    config_edition.modifier("capacites.administration.validation", "automatique", "Alex")
    cfg = load_config(env, RACINE)
    assert cfg.action_automatique("admin_cron")
    assert not cfg.action_automatique("kitchen_daily_selection")


def test_activation_desactivation_et_annulation(env: Path) -> None:
    from tools import capacites

    mcp = charger_admin(env, "alex")
    with pytest.raises(ValueError, match="administration elle-même"):
        mcp.outils["admin_capacite_desactiver"](nom="administration")
    with pytest.raises(ValueError, match="déjà active"):
        mcp.outils["admin_capacite_activer"](nom="bus")
    message = valider_action(id_confirmation(mcp.outils["admin_capacite_desactiver"](nom="Transports")))
    assert "désactivée" in message
    assert capacites.statut_effectif(capacites.trouver("bus"), capacites.lire_etat(env)) == "inactif"
    (entree,) = mcp.outils["admin_journal"]()
    valider_action(id_confirmation(mcp.outils["admin_annuler"](id=entree["id"])))
    assert capacites.statut_effectif(capacites.trouver("bus"), capacites.lire_etat(env)) == "actif"
    assert "transports" not in capacites.lire_etat(env)  # état d'origine restauré à l'identique


def test_pause_et_reprise_validable_en_pause(env: Path) -> None:
    mcp = charger_admin(env, "alex")
    valider_action(id_confirmation(mcp.outils["admin_pause"]()))
    assert (env / "PAUSE").exists()
    # Une autre action reste bloquée pendant la pause…
    from tools import db, runner

    bloquee = id_confirmation(mcp.outils["admin_cron"](nom="brief_jour", cron="", quand="8h"))
    assert runner.executer(bloquee) == 4
    assert db.get_action(bloquee)["status"] == "pending"
    # … mais « reprendre » passe.
    valider_action(id_confirmation(mcp.outils["admin_reprendre"]()))
    assert not (env / "PAUSE").exists()


def test_parametre_journalise_et_annulable(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import administration, capacites

    caps = tmp_path / "caps"
    (caps / "demo").mkdir(parents=True)
    (caps / "demo" / "capability.yaml").write_text(
        yaml.safe_dump({
            "nom": "demo", "titre": "Démo", "description": "Test.", "statut": "actif", "origine": "humain",
            "parametres": [{"cle": "arret", "type": "texte", "question": "Quel arrêt ?"}],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    (caps / "demo" / "tools.py").write_text("def enregistrer(mcp, ctx):\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(caps))
    resultat = administration.definir_parametre("demo", "arret", "Pont du Moulin", "Alex", donnees=env)
    assert resultat["message"].startswith("Fait.") and "annuler" in resultat["message"]
    assert capacites.lire_parametres("demo", env) == {"arret": "Pont du Moulin"}
    (entree,) = administration.journal(5, env)
    _resume, payload = administration.preparer_annulation(entree["id"], "Alex", donnees=env)
    administration.executer("admin_annuler", payload, donnees=env)
    assert capacites.lire_parametres("demo", env) == {}


def test_etat_et_texte(env: Path) -> None:
    from tools import administration

    mcp = charger_admin(env, "alex")
    etat = mcp.outils["admin_etat"]()
    noms = {c["nom"] for c in etat["capacites"]}
    assert {"administration", "kitchen_daily", "transports"} <= noms
    kd = next(c for c in etat["capacites"] if c["nom"] == "kitchen_daily")
    assert kd["crons"][0]["lisible"] == "le mercredi à 18h" and kd["validation"] == "humaine"
    assert {c["cron"] for c in etat["crons_foyer"]} == {"brief_jour", "brief_hebdo"}
    assert etat["pause"] is False and etat["depense_mois"]["reponses"] == 0
    texte = administration.etat_texte(etat)
    assert "Capacités" in texte and "Kitchen Daily" in texte and "brief du matin tous les jours à 7h15" in texte
    assert "capacites." not in texte  # aucun nom de clé


def test_reconnexion(env: Path) -> None:
    mcp = charger_admin(env, "alex")
    texte = mcp.outils["admin_reconnexion"](nom="Kitchen Daily")
    assert "tools.kitchen_daily login" in texte and "jamais ici" in texte
    assert "pas de connexion à refaire" in mcp.outils["admin_reconnexion"](nom="bus")
    with pytest.raises(ValueError, match="Je ne connais pas"):
        mcp.outils["admin_reconnexion"](nom="planity")


def test_smoke_administration(env: Path) -> None:
    from tools import capacites

    assert capacites.smoke(capacites.lire_capacite(CAP_ADMIN), env)["ok"]
    (env / "config.yaml").write_text(CONFIG.replace('"07:15"', '"7:99"'), encoding="utf-8")
    assert not capacites.smoke(capacites.lire_capacite(CAP_ADMIN), env)["ok"]


# --- bot : rechargement à chaud, boutons ----------------------------------------------------------


class _RunnerFactice:
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg


def _contexte(env: Path) -> Any:
    from bot.config import load_config
    from bot.context import BotContext
    from bot.db import Db

    cfg = load_config(env, RACINE)
    db = Db(cfg.db_path)
    db.init()
    return BotContext(cfg=cfg, db=db, sessions=None, runner=_RunnerFactice(cfg), transcripteur=None)  # type: ignore[arg-type]


def test_rechargement_a_chaud_de_la_config(env: Path) -> None:
    from tools import config_edition

    ctx = _contexte(env)
    assert ctx.rafraichir_config() is False
    config_edition.modifier("capacites.kitchen_daily.validation", "automatique", "Alex")
    assert ctx.rafraichir_config() is True
    assert "kitchen_daily" in ctx.cfg.capacites_automatiques()
    assert ctx.runner.cfg is ctx.cfg
    # Configuration cassée à la main : la précédente reste en service.
    precedente = ctx.cfg
    (env / "config.yaml").write_text("personnes: [\n", encoding="utf-8")
    assert ctx.rafraichir_config() is False
    assert ctx.cfg is precedente


def test_bouton_admin_reserve_aux_proprietaires_et_hors_pause(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot import confirmations
    from tools import db

    ctx = _contexte(env)
    alex, camille = ctx.cfg.personnes
    executees: list[int] = []

    async def executer_factice(cfg: Any, action_id: int) -> tuple[bool, str]:
        executees.append(action_id)
        db.update_action(action_id, "done", "ok")
        return True, "Fait."

    monkeypatch.setattr(confirmations, "executer_action", executer_factice)
    aid = db.log_action("admin_cron", "brief", {}, status="pending")
    texte = asyncio.run(confirmations.traiter_callback(ctx.cfg, ctx.db, f"confirm:{aid}", "Camille",
                                                       personne=camille))
    assert texte == confirmations.MESSAGE_RESERVE and executees == []
    (env / "PAUSE").write_text("pause", encoding="utf-8")
    texte = asyncio.run(confirmations.traiter_callback(ctx.cfg, ctx.db, f"confirm:{aid}", "Alex", personne=alex))
    assert "pause" in texte and executees == []
    reprise = db.log_action("admin_reprendre", "reprendre", {}, status="pending")
    texte = asyncio.run(confirmations.traiter_callback(ctx.cfg, ctx.db, f"confirm:{reprise}", "Alex",
                                                       personne=alex))
    assert texte.startswith("✅") and executees == [reprise]


def test_commande_admin_reservee() -> None:
    from bot import commands
    from tools import capacites

    assert "admin" in {c for c, _ in commands.COMMANDES}
    assert "admin" in capacites.COMMANDES_RESERVEES


# --- application des timers (scripts/appliquer-config.sh, sans systemd) --------------------------


def _appliquer(env: Path, unites: Path, etat: Path) -> subprocess.CompletedProcess[str]:
    variables = {
        **os.environ,
        "HOMEOS_DATA": str(env),
        "HOMEOS_ETAT": str(etat),
        "HOMEOS_SYSTEMD_DIR": str(unites),
        "HOMEOS_SANS_SYSTEMCTL": "1",
        "HOMEOS_PYTHON": sys.executable,
    }
    return subprocess.run(["bash", str(RACINE / "scripts" / "appliquer-config.sh"), str(RACINE)],
                          env=variables, capture_output=True, text=True, timeout=120, check=False)


def test_appliquer_config_genere_les_timers(env: Path, tmp_path: Path) -> None:
    from tools import config_edition

    unites, etat = tmp_path / "unites", tmp_path / "etat"
    res = _appliquer(env, unites, etat)
    assert res.returncode == 0, res.stderr
    timer = (unites / "homeos-cap-kitchen_daily-rappel.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Wed *-*-* 18:00:00" in timer
    assert "OnCalendar=*-*-* 07:15:00" in (unites / "homeos-brief-jour.timer.d" / "override.conf").read_text()
    assert json.loads((env / "administration" / "application.json").read_text())["ok"] is True

    config_edition.modifier("capacites.kitchen_daily.crons.rappel", "Mon,Thu 08:30", "Alex")
    assert _appliquer(env, unites, etat).returncode == 0
    assert "OnCalendar=Mon,Thu *-*-* 08:30:00" in (unites / "homeos-cap-kitchen_daily-rappel.timer").read_text()

    (env / "config.yaml").write_text(CONFIG.replace('"07:15"', '"7:99"'), encoding="utf-8")
    res = _appliquer(env, unites, etat)
    assert res.returncode == 1
    assert json.loads((env / "administration" / "application.json").read_text())["ok"] is False
    assert "OnCalendar=*-*-* 07:15:00" in (unites / "homeos-brief-jour.timer.d" / "override.conf").read_text()


def test_scripts_bash_syntaxe() -> None:
    for script in [RACINE / "scripts" / "appliquer-config.sh", RACINE / "install" / "install.sh"]:
        assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0, script
