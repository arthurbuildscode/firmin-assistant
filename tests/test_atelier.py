"""Tests du lot 7b — atelier : l'assistant fabrique une capacité à la demande du foyer.

Le sous-agent Claude Code est remplacé par `tests/fixtures/atelier/faux_claude.py` (aucun modèle appelé) ; tout le
reste est réel : file, exploration Playwright, contrôles statiques, ruff, tests et smoke test de la capacité dans des
sous-processus isolés, relecture, installation en essai, carte d'identifiants, première action validée à la main par
un propriétaire, retrait par l'administration. Le site est un faux site local (tests/fixtures/faux_rdv) : le vrai
Planity n'est jamais touché.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

RACINE = Path(__file__).resolve().parent.parent
FIXTURES = RACINE / "tests" / "fixtures"
MODELE = FIXTURES / "atelier" / "salon_demo"
FAUX_CLAUDE = FIXTURES / "atelier" / "faux_claude.py"
CAP_ATELIER = RACINE / "capabilities" / "atelier"
CAP_ADMIN = RACINE / "capabilities" / "administration"
CLE_COFFRE = "cle-de-test-atelier-0123456789abcdef"

sys.path.insert(0, str(FIXTURES / "faux_rdv"))
import serveur as faux_site

CONFIG = """\
assistant:
  nom: "Testbot"
groupe_telegram_id: -100
personnes:
  - telegram_id: 111
    prenom: Alex
    linux_user: alex
  - telegram_id: 222
    prenom: Camille
    linux_user: camille
    role: membre
"""

DEMANDE = {
    "demande": "ajoute le salon démo pour prendre les rendez-vous coiffeur",
    "compte": "moi",
    "action": "voir les créneaux libres et réserver une coupe",
    "titre": "Salon démo",
}


@pytest.fixture(scope="session")
def chromium() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium indisponible : {exc.__class__.__name__}")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from tools import atelier, capacites

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(RACINE))
    for v in ("HOMEOS_CAPABILITIES", "HOMEOS_CAPABILITIES_ATELIER", "HOMEOS_PERSONNE", "HOMEOS_WHO", "HOMEOS_ETAT",
              "FAUX_CLAUDE_DEFAUT", "FAUX_CLAUDE_CORRIGE", "FAUX_CLAUDE_VERDICT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOMEOS_ACTION_ID", "")
    monkeypatch.setenv("HOMEOS_ATELIER_CLAUDE", str(FAUX_CLAUDE))
    monkeypatch.setenv("HOMEOS_ATELIER_LANCEMENT", "synchrone")
    monkeypatch.setenv("FAUX_CLAUDE_JOURNAL", str(tmp_path / "faux_claude.jsonl"))
    monkeypatch.setenv("COFFRE_CLE", CLE_COFFRE)
    monkeypatch.setitem(capacites._CACHE_COFFRE, "quand", -1e9)
    monkeypatch.setattr(capacites, "DUREE_CACHE_COFFRE_S", 0.0)
    messages: list[tuple[str, str | None]] = []
    monkeypatch.setattr(atelier, "envoyer_telegram", lambda texte, chat: messages.append((texte, chat)) or True)
    return {"data": data, "messages": messages, "journal": tmp_path / "faux_claude.jsonl", "tmp": tmp_path}


@pytest.fixture()
def site() -> Any:
    serveur, url, etat = faux_site.demarrer()
    yield url, etat
    serveur.shutdown()


def charger(dossier: Path, data: Path, personne: str | None) -> Any:
    from tools import capacites

    mcp = capacites.MCPFactice()
    capacites.charger_capacite(capacites.lire_capacite(dossier), mcp, donnees=data, personne=personne)
    return mcp


def id_confirmation(texte: str) -> int:
    assert texte.startswith("CONFIRMATION_REQUISE:"), texte
    return int(texte.split(":")[1])


def valider(action_id: int) -> str:
    from tools import db, runner

    code = runner.executer(action_id)
    action = db.get_action(action_id)
    assert action is not None
    assert code == 0, action
    return str(action["result"])


def sans_navigateur(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipeline sans Chromium : exploration et smoke simulés (la logique de l'atelier reste réelle)."""
    from tools import atelier

    monkeypatch.setattr(atelier, "explorer_site", lambda d, travail: {"titre": "Salon", "hotes_bloques": []})
    monkeypatch.setattr(atelier, "lancer_smoke", lambda dossier, d: (True, "ok"))


def lancer_demande(env: dict[str, Any], url: str) -> str:
    mcp = charger(CAP_ATELIER, env["data"], "alex")
    texte = mcp.outils["atelier_creer"](site=url, **DEMANDE)
    return valider(id_confirmation(texte))


def bot_contexte(data: Path) -> Any:
    from bot.config import load_config
    from bot.db import Db

    cfg = load_config(data, RACINE)
    db = Db(cfg.db_path)
    db.init()
    return cfg, db


async def _executer_runner(cfg: Any, action_id: int) -> tuple[bool, str]:
    from tools import db, runner

    code = await asyncio.to_thread(runner.executer, action_id)
    action = db.get_action(action_id)
    return code == 0, str(action["result"] if action else "")


# --- demande et découverte ----------------------------------------------------------------------


def test_normaliser_site() -> None:
    from tools import atelier

    assert atelier.normaliser_site("planity.com") == ("https://planity.com", "planity.com")
    assert atelier.normaliser_site("http://www.planity.com/paris/") == ("https://www.planity.com/paris", "www.planity.com")
    assert atelier.normaliser_site("http://127.0.0.1:8765") == ("http://127.0.0.1:8765", "127.0.0.1")
    with pytest.raises(atelier.ErreurAtelier):
        atelier.normaliser_site("pas une adresse")


def test_atelier_reserve_aux_proprietaires(env: dict[str, Any]) -> None:
    from tools import capacites

    mcp = charger(CAP_ATELIER, env["data"], "camille")
    with pytest.raises(capacites.erreur_outil("x").__class__, match="propriétaires"):
        mcp.outils["atelier_creer"](site="planity.com", **DEMANDE)


@pytest.mark.parametrize("demande", [
    "branche-toi sur mes mails du travail pour trier",
    "ajoute l'intranet de mon employeur",
    "gère mon espace pro sur le site des impôts",
])
def test_atelier_refuse_l_espace_pro(env: dict[str, Any], demande: str) -> None:
    mcp = charger(CAP_ATELIER, env["data"], "alex")
    with pytest.raises(Exception, match="espace professionnel"):
        mcp.outils["atelier_creer"](demande=demande, site="exemple.fr", compte="moi", action="lire")


def test_reformulation_et_site_approuve(env: dict[str, Any]) -> None:
    mcp = charger(CAP_ATELIER, env["data"], "alex")
    texte = mcp.outils["atelier_creer"](demande="ajoute Planity pour prendre les rendez-vous coiffeur",
                                        site="www.planity.com", compte="moi", action="réserver une coupe", titre="Planity")
    assert "Planity parlera à www.planity.com" in texte and "essai" in texte
    from tools import db

    action = db.get_action(id_confirmation(texte))
    assert action is not None and action["kind"] == "atelier_creer" and action["status"] == "pending"
    assert action["payload"]["nom"] == "planity" and action["payload"]["demandeur"] == "alex"
    # Une demande de paiement est acceptée mais rappelle le bouton Valider.
    texte = mcp.outils["atelier_creer"](demande="commander les courses au drive", site="drive.exemple.fr",
                                        compte="foyer", action="payer la commande", titre="Drive")
    assert "paiement attendra toujours ton bouton Valider" in texte


def test_questions_de_decouverte(env: dict[str, Any]) -> None:
    mcp = charger(CAP_ATELIER, env["data"], "alex")
    with pytest.raises(Exception, match="compte commun"):
        mcp.outils["atelier_creer"](demande="ajoute Planity", site="planity.com", compte="", action="réserver")
    with pytest.raises(Exception, match="attends exactement"):
        mcp.outils["atelier_creer"](demande="ajoute Planity", site="planity.com", compte="moi", action="")
    with pytest.raises(Exception, match="déjà"):
        mcp.outils["atelier_creer"](demande="refais le bus", site="exemple.fr", compte="foyer", action="horaires",
                                    titre="Bus")


# --- contrôles statiques ------------------------------------------------------------------------------------


def _candidat(tmp: Path, defaut: str = "") -> Path:
    import os
    import subprocess

    dossier = tmp / "salon_demo"
    dossier.mkdir()
    env = {**os.environ, "HOMEOS_ATELIER_MODE": "generer", "FAUX_CLAUDE_DEFAUT": defaut}
    subprocess.run([sys.executable, str(FAUX_CLAUDE)], cwd=dossier, env=env, check=True, capture_output=True)
    return dossier


def _demande(**kw: Any) -> Any:
    from tools import atelier

    valeurs = {"nom": "salon_demo", "titre": "Salon démo", "demande": "x", "site": "http://127.0.0.1:1",
               "hote": "127.0.0.1", "compte": "moi", "action": "réserver", "demandeur": "alex", "prenom": "Alex"}
    return atelier.Demande(**{**valeurs, **kw})


def test_modele_passe_les_controles(tmp_path: Path) -> None:
    from tools import atelier

    manifeste, erreurs, en_plus = atelier.verifier_candidat(_candidat(tmp_path), _demande())
    assert erreurs == [] and manifeste is not None and en_plus == []
    assert atelier.lancer_ruff(tmp_path / "salon_demo") == []


@pytest.mark.parametrize(("defaut", "attendu"), [
    ("secret_en_dur", "secret écrite en dur"),
    ("sans_confirmation", "absente de confirmation_toujours"),
    ("paiement", "paiement sans confirmation_toujours"),
    ("import_reseau", "import interdit « urllib.request »"),
])
def test_controles_refusent_les_defauts(tmp_path: Path, defaut: str, attendu: str) -> None:
    from tools import atelier

    _m, erreurs, _ = atelier.verifier_candidat(_candidat(tmp_path, defaut), _demande())
    assert any(attendu in e for e in erreurs), erreurs


def test_controle_du_code() -> None:
    from tools import atelier

    erreurs = atelier.controler_code("connecteur.py", (
        "import os\nimport subprocess\nfrom tools import coffre\n"
        "def f():\n    os.system('id')\n    open('/etc/homeos/coffre.env')\n    eval('1')\n"
        "    return 'contact@exemple.fr'\n"
    ))
    texte = "\n".join(erreurs)
    for motif in ("« os »", "« subprocess »", "« tools.coffre »", "os.system", "« eval »", "fichier ouvert",
                  "chemin système", "adresse e-mail"):
        assert motif in texte, (motif, erreurs)
    assert atelier.controler_code("tests/test_x.py", "import pytest\nfrom pathlib import Path\n") == []
    assert atelier.controler_code("tests/test_x.py", "import subprocess\n")


def test_demande_pro_dans_le_manifeste_refusee(tmp_path: Path) -> None:
    from tools import atelier

    dossier = _candidat(tmp_path)
    manifeste = dossier / "capability.yaml"
    manifeste.write_text(manifeste.read_text(encoding="utf-8").replace(
        "description: Rendez-vous coiffeur", "description: Messagerie du travail et rendez-vous coiffeur"),
        encoding="utf-8")
    _m, erreurs, _ = atelier.verifier_candidat(dossier, _demande())
    assert any("espace professionnel" in e for e in erreurs)


def test_commande_sous_agent_par_le_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import atelier

    wrapper = tmp_path / "homeos-claude"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(atelier, "WRAPPER", str(wrapper))
    monkeypatch.delenv("HOMEOS_ATELIER_CLAUDE", raising=False)
    cmd = atelier.commande_sous_agent("generer", "salon_demo", "fabrique", tmp_path)
    assert cmd[:5] == [str(wrapper), "--homeos-atelier", "salon_demo", "--homeos-atelier-mode", "generer"]
    assert "--allowedTools" not in cmd  # imposés par le wrapper
    monkeypatch.setenv("HOMEOS_ATELIER_CLAUDE", "/bin/claude-dev")
    cmd = atelier.commande_sous_agent("relire", "salon_demo", "relis", tmp_path)
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep"
    assert cmd[cmd.index("--append-system-prompt-file") + 1].endswith("content/atelier/RELECTURE.md")


def test_wrapper_impose_les_memes_outils() -> None:
    """Les chaînes d'outils du wrapper root et de tools.atelier ne doivent jamais diverger."""
    from tools import atelier

    texte = (RACINE / "install" / "bin" / "homeos-claude").read_text(encoding="utf-8")
    assert f'OUTILS_ATELIER_GENERER="{atelier.OUTILS_GENERER}"' in texte
    assert f'OUTILS_ATELIER_RELIRE="{atelier.OUTILS_RELIRE}"' in texte


# --- pipeline (sans navigateur) ---------------------------------------------------------------------------


def test_echec_dit_simplement_et_rien_n_est_installe(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import atelier, capacites

    sans_navigateur(monkeypatch)
    monkeypatch.setenv("FAUX_CLAUDE_DEFAUT", "secret_en_dur")
    assert "C'est parti" in lancer_demande(env, "http://127.0.0.1:9")
    suivi = atelier.lire_suivi("salon_demo")
    assert suivi["statut"] == "echec"
    texte, chat = env["messages"][-1]
    assert texte.startswith("Ça n'a pas marché pour « Salon démo »") and "je garde ce que j'ai appris" in texte.casefold()
    assert chat == "111"  # message privé au demandeur
    assert "secret" in (atelier.dossier_suivi("salon_demo") / "apprentissages.md").read_text(encoding="utf-8")
    with pytest.raises(capacites.ErreurCapacite):
        capacites.trouver("salon_demo")
    assert "salon_demo" not in capacites.lire_etat()  # le site sort de la liste blanche
    lignes = [json.loads(x) for x in env["journal"].read_text(encoding="utf-8").splitlines()]
    assert [x["mode"] for x in lignes] == ["generer", "generer"]  # un tour de correction, pas plus


def test_tour_de_correction(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import atelier

    sans_navigateur(monkeypatch)
    monkeypatch.setenv("FAUX_CLAUDE_DEFAUT", "sans_confirmation")
    monkeypatch.setenv("FAUX_CLAUDE_CORRIGE", "1")
    lancer_demande(env, "http://127.0.0.1:9")
    assert atelier.lire_suivi("salon_demo")["statut"] == "pret"
    etapes = [h["etape"] for h in atelier.lire_suivi("salon_demo")["historique"]]
    assert "correction" in etapes and etapes[-1] == "pret"


def test_relecteur_peut_refuser(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import atelier, capacites

    sans_navigateur(monkeypatch)
    monkeypatch.setenv("FAUX_CLAUDE_VERDICT", "refus")
    lancer_demande(env, "http://127.0.0.1:9")
    assert atelier.lire_suivi("salon_demo")["statut"] == "echec"
    assert "relecture de sécurité a refusé" in env["messages"][-1][0]
    with pytest.raises(capacites.ErreurCapacite):
        capacites.trouver("salon_demo")


def test_hotes_supplementaires_a_approuver(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tools import capacites

    sans_navigateur(monkeypatch)
    modele = tmp_path / "modele"
    shutil.copytree(MODELE, modele)
    m = modele / "capability.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace('reseau: ["127.0.0.1"]', 'reseau: ["127.0.0.1", "cdn.exemple.fr"]'),
                 encoding="utf-8")
    monkeypatch.setenv("FAUX_CLAUDE_MODELE", str(modele))
    lancer_demande(env, "http://127.0.0.1:9")
    assert "voudrait aussi parler à cdn.exemple.fr" in env["messages"][-1][0]
    assert "127.0.0.1" in capacites.reseau() and "cdn.exemple.fr" not in capacites.reseau()  # pas encore approuvé
    cap = capacites.trouver("salon_demo")
    assert capacites.hotes_autorises(cap) == ["127.0.0.1"]
    mcp = charger(CAP_ATELIER, env["data"], "alex")
    texte = mcp.outils["atelier_approuver_reseau"](nom="salon démo")
    assert "veut aussi parler à cdn.exemple.fr" in texte
    assert "peut maintenant parler à cdn.exemple.fr" in valider(id_confirmation(texte))
    assert "cdn.exemple.fr" in capacites.reseau()


def test_confirmation_toujours_des_capacites_de_l_atelier(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    sans_navigateur(monkeypatch)
    lancer_demande(env, "http://127.0.0.1:9")
    cfg, _db = bot_contexte(env["data"])
    assert "salon_demo_reserver" in cfg.actions_confirmation_toujours()


def test_essai_d_une_capacite_humaine_reste_en_lecture_seule(env: dict[str, Any], tmp_path: Path) -> None:
    """Seules les capacités de l'assistant passent par la première validation : une capacité humaine en essai
    (archive installée) refuse toujours ses actions avec effet."""

    racine = tmp_path / "caps"
    shutil.copytree(MODELE, racine / "salon_demo")
    m = racine / "salon_demo" / "capability.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("origine: assistant", "origine: humain"), encoding="utf-8")
    mcp = charger(racine / "salon_demo", env["data"], "alex")
    with pytest.raises(Exception, match="en essai"):
        mcp.outils["salon_demo_reserver"](creneau_id="c1")


# --- démonstration de bout en bout (faux site local, Chromium) -----------------------------------------------


def test_demonstration_de_bout_en_bout(env: dict[str, Any], site: Any, chromium: None,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from coeur import confirmations as coeur_conf
    from tools import atelier, capacites, coffre, db

    url, etat_site = site
    data = env["data"]

    # 1. « Ajoute le salon démo » → reformulation, bouton Valider → l'atelier tourne (ici en synchrone).
    assert "C'est parti" in lancer_demande(env, url)
    suivi = atelier.lire_suivi("salon_demo")
    assert suivi["statut"] == "pret", (suivi, (atelier.dossier_suivi("salon_demo") / "apprentissages.md").read_text()
                                       if (atelier.dossier_suivi("salon_demo") / "apprentissages.md").exists() else "")
    etapes = [h["etape"] for h in suivi["historique"]]
    assert etapes == ["en_attente", "exploration", "generation", "verification", "relecture", "installation", "pret"]
    texte, chat = env["messages"][-1]
    assert texte.startswith("Capacité « Salon démo » prête à l'essai. Il me faut tes identifiants Salon démo")
    assert chat == "111"
    # L'exploration a respecté robots.txt et relevé les CGU ; le sous-agent a eu ses outils restreints.
    exploration = atelier.dossier_travail("salon_demo") / "exploration"
    assert "Disallow: /admin" in (exploration / "robots.txt").read_text(encoding="utf-8")
    assert any(lien["cgu"] for lien in json.loads((exploration / "liens.json").read_text(encoding="utf-8")))
    assert "assistant personnel" in (exploration / "cgu-1.txt").read_text(encoding="utf-8")
    assert "Conditions d'utilisation lues" in (atelier.dossier_travail("salon_demo") / "DEMANDE.md").read_text()
    appels = [json.loads(x) for x in env["journal"].read_text(encoding="utf-8").splitlines()]
    assert [a["mode"] for a in appels] == ["generer", "relire"]
    assert appels[0]["args"][appels[0]["args"].index("--allowedTools") + 1] == atelier.OUTILS_GENERER
    assert appels[0]["cwd"] == str(atelier.dossier_travail("salon_demo"))

    # 2. Installée en essai, hors du dépôt, site approuvé.
    cap = capacites.trouver("salon_demo")
    assert capacites.de_l_atelier(cap) and cap.dossier.parent == data / "capacites-atelier"
    assert capacites.statut_effectif(cap, capacites.lire_etat()) == "essai"
    assert "127.0.0.1" in capacites.reseau()
    assert not (cap.dossier / "DEMANDE.md").exists() and not (cap.dossier / "exploration").exists()

    # 3. Identifiants : la carte sécurisée (identifiants_preparer) puis la saisie, simulée ici dans le coffre.
    from tools import identifiants

    monkeypatch.setenv("HOMEOS_PERSONNE", "alex")
    preparation = identifiants.preparer("Salon démo", "identifiants", "moi", "salon_demo", None)
    monkeypatch.delenv("HOMEOS_PERSONNE")
    assert "salon_demo/alex" in preparation
    coffre.ecrire("salon_demo/alex", dict(faux_site.IDENTIFIANTS), meta={"type": "identifiants"})

    # 4. Lecture : créneaux du site (connexion, hydratation, API JSON écoutée).
    mcp = charger(cap.dossier, data, "alex")
    creneaux = mcp.outils["salon_demo_creneaux"]()
    assert [c["id"] for c in creneaux["creneaux"]] == ["c1", "c2", "c3"]
    assert faux_site.IDENTIFIANTS["mot_de_passe"] not in json.dumps(creneaux)

    # 5. Réservation en essai : CONFIRMATION_REQUISE préparée par le cadre, rien n'est réservé.
    texte = mcp.outils["salon_demo_reserver"](creneau_id="c1")
    aid = id_confirmation(texte)
    assert "première utilisation" in texte and etat_site.reservations == []

    cfg, base = bot_contexte(data)
    alex, camille = cfg.personnes
    assert coeur_conf.kind_sensible(cfg, "salon_demo_reserver")  # step-up dans l'appli

    # Camille (membre) ne peut pas faire la première validation.
    refus = asyncio.run(coeur_conf.decider(cfg, base, "confirm", aid, "Camille", personne=camille,
                                           executer=_executer_runner))
    assert refus == coeur_conf.MESSAGE_PREMIERE and etat_site.reservations == []
    # Sans autorisation du propriétaire, le runner refuse aussi (bouton contourné).
    from tools import runner

    assert runner.executer(aid) == 1 and etat_site.reservations == []
    db.update_action(aid, "pending", None)

    # Alex (propriétaire) valide : la réservation passe sur le site, la capacité devient active.
    ok = asyncio.run(coeur_conf.decider(cfg, base, "confirm", aid, "Alex", personne=alex, executer=_executer_runner))
    assert ok.startswith("✅ Validé par Alex"), ok
    assert "Rendez-vous réservé : Coupe — mardi 10 novembre à 10h00 (référence RDV-0001)" in ok
    assert "maintenant active" in ok
    assert [r["reference"] for r in etat_site.reservations] == ["RDV-0001"]
    entree = capacites.lire_etat()["salon_demo"]
    assert capacites.statut_effectif(cap, capacites.lire_etat()) == "actif" and entree["validee_par"] == "alex"
    assert "premiere_validation" not in entree

    # 6. Active : l'outil prépare lui-même la validation (toujours par bouton, confirmation_toujours).
    texte = mcp.outils["salon_demo_reserver"](creneau_id="c2")
    assert id_confirmation(texte) and "première" not in texte
    assert not coeur_conf.premiere_validation(cfg, "salon_demo_reserver")

    # 7. Journal : captures et journal de navigation, sans secret.
    prive = data / "capacites-etat" / "salon_demo"
    journal_nav = (prive / "journal.jsonl").read_text(encoding="utf-8")
    assert "reservation" in journal_nav and faux_site.IDENTIFIANTS["mot_de_passe"] not in journal_nav
    assert list((prive / "captures").glob("*apres-reservation.png"))
    assert any(a["kind"] == "capacite_validee_salon_demo" for a in db.recent_actions(50))

    # 8. Retrait par l'administration.
    admin = charger(CAP_ADMIN, data, "alex")
    texte = admin.outils["admin_capacite_retirer"](nom="Salon démo")
    assert "Retirer « Salon démo »" in texte
    assert "est retirée" in valider(id_confirmation(texte))
    with pytest.raises(capacites.ErreurCapacite):
        capacites.trouver("salon_demo")
    with pytest.raises(Exception, match="désactiver, pas la retirer"):
        admin.outils["admin_capacite_retirer"](nom="bus")


# --- navigation (Chromium, faux site) ---------------------------------------------------------------------------


def test_navigation_respecte_robots_antibot_et_hotes(env: dict[str, Any], site: Any, chromium: None) -> None:
    from tools import navigation

    url, _etat = site
    with navigation.Session("demo", ["127.0.0.1"], dossier=env["tmp"] / "nav") as s:
        page = s.ouvrir(url + "/")
        assert "Salon Démo" in page.title()
        with pytest.raises(navigation.RobotsRefuse):
            s.ouvrir(url + "/admin")
        with pytest.raises(navigation.VerificationHumaine, match="je ne contourne pas"):
            s.ouvrir(url + "/bloque")
        with pytest.raises(navigation.HoteRefuse):
            s.ouvrir("https://www.planity.com/")
    assert "challenges.cloudflare.com" in s.bloques  # l'iframe tierce n'est jamais chargée
    journal = (env["tmp"] / "nav" / "journal.jsonl").read_text(encoding="utf-8")
    assert "robots_refus" in journal and "verification_humaine" in journal


def test_url_et_hotes() -> None:
    from tools import navigation

    assert navigation.hote_autorise("cdn.planity.com", ["*.planity.com"])
    assert not navigation.hote_autorise("planity.com.evil.fr", ["*.planity.com"])
    assert not navigation.hote_autorise("evil.fr", ["planity.com"])
    with pytest.raises(navigation.HoteRefuse, match="https"):
        navigation.verifier_url("http://www.planity.com/", ["www.planity.com"])
    assert navigation.url_journal("https://a.fr/x?jeton=abc#f") == "https://a.fr/x"
    assert navigation.est_page_antibot("<title>Just a moment...</title>")
    assert not navigation.est_page_antibot("<form><input name=email></form>")


def test_liste_blanche_du_proxy_suit_les_approbations(env: dict[str, Any], tmp_path: Path) -> None:
    """Le site approuvé à la demande entre dans la liste blanche du proxy (homeos-appliquer-config, étape 7)."""
    import os
    import subprocess

    from tools import capacites

    with capacites._modifier_etat() as etat:
        etat["salon_demo"] = {"atelier": {"en_cours": True}, "reseau_approuve": ["salon.exemple.fr"]}
    filtre = tmp_path / "homeos-filtre"
    filtre.write_text("# ancien\n^ancien\\.fr$\n", encoding="utf-8")
    variables = {**os.environ, "HOMEOS_DATA": str(env["data"]), "HOMEOS_ETAT": str(env["data"]),
                 "HOMEOS_SYSTEMD_DIR": str(tmp_path / "unites"), "HOMEOS_SANS_SYSTEMCTL": "1",
                 "HOMEOS_PYTHON": sys.executable, "HOMEOS_FILTRE_PROXY": str(filtre)}
    res = subprocess.run(["bash", str(RACINE / "scripts" / "appliquer-config.sh"), str(RACINE)], env=variables,
                         capture_output=True, text=True, timeout=120, check=False)
    assert res.returncode == 0, res.stderr
    texte = filtre.read_text(encoding="utf-8")
    assert "^salon\\.exemple\\.fr$" in texte and "^api\\.telegram\\.org$" in texte and "ancien" not in texte
