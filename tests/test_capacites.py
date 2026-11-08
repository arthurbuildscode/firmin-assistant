"""Tests du système de capacités (lot 5a) : manifeste, chargement, essai, secrets, paramètres, CLI, crons,
export/install, prompt, bot. Sans réseau ni navigateur."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent

TOOLS_DEMO = '''\
from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    @mcp.tool()
    def demo_lire(texte: str = "") -> dict:
        """Lit quelque chose."""
        return {"texte": texte, "personne": ctx.personne}

    @mcp.tool()
    def demo_agir(quoi: str) -> str:
        """Agit (à confirmer)."""
        return ctx.demander_confirmation("agir", f"faire {quoi}", {"quoi": quoi})

    @mcp.tool()
    def demo_secret() -> str:
        """Utilise un secret."""
        s = ctx.secret("demo")
        return f"connecté avec {s['mot_de_passe']}"

    @mcp.tool()
    def demo_reglage() -> Any:
        """Lit un réglage."""
        return ctx.parametre("arret")


def executer(action: str, payload: dict, ctx: Any) -> str:
    return f"fait : {payload['quoi']}"
'''

SMOKE_DEMO = '''\
import os


def verifier(ctx):
    return {"ok": os.environ.get("DEMO_SMOKE", "ok") == "ok", "message": "message du smoke"}
'''

CRONS_DEMO = '''\
def executer(nom_cron, ctx):
    if nom_cron == "boum":
        raise RuntimeError("cron cassé")
    return f"cron {nom_cron} exécuté"
'''


def creer_capacite(racine: Path, nom: str = "demo", tools: str = TOOLS_DEMO, **surcharges: Any) -> Path:
    manifeste: dict[str, Any] = {
        "nom": nom,
        "titre": "Démo",
        "description": "Capacité de test.",
        "version": 1,
        "statut": "actif",
        "origine": "humain",
        "secrets": [{"cle": "demo", "champs": ["identifiant", "mot_de_passe"]}],
        "actions_avec_effet": [f"{nom}_agir"],
        "commandes_telegram": [{"nom": nom, "aide": "Commande de démo"}],
        "parametres": [{"cle": "arret", "type": "texte", "question": "Quel arrêt ?", "obligatoire": True}],
        **surcharges,
    }
    d = racine / nom
    d.mkdir(parents=True)
    (d / "capability.yaml").write_text(yaml.safe_dump(manifeste, allow_unicode=True), encoding="utf-8")
    (d / "tools.py").write_text(tools.replace("demo_", f"{nom}_"), encoding="utf-8")
    (d / "CLAUDE.md").write_text(f"## Démo\nUtilise `{nom}_lire`.\n", encoding="utf-8")
    (d / "smoke.py").write_text(SMOKE_DEMO, encoding="utf-8")
    if manifeste.get("crons"):
        (d / "crons.py").write_text(CRONS_DEMO, encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data = tmp_path / "data"
    data.mkdir()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(caps))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T12:00:00")
    monkeypatch.delenv("HOMEOS_PERSONNE", raising=False)
    config = {
        "assistant": {"nom": "Testbot"},
        "personnes": [
            {"telegram_id": 1, "prenom": "Alex", "linux_user": "alex"},
            {"telegram_id": 2, "prenom": "Camille", "linux_user": "camille"},
        ],
    }
    (data / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return {"data": data, "caps": caps, "tmp": tmp_path}


def charger(cap_dir: Path, data: Path, personne: str | None = None) -> Any:
    from tools import capacites

    mcp = capacites.MCPFactice()
    capacites.charger_capacite(capacites.lire_capacite(cap_dir), mcp, donnees=data, personne=personne)
    return mcp


# --- manifeste ------------------------------------------------------------------


def test_manifeste_kitchen_daily_valide() -> None:
    from tools import capacites

    cap = capacites.lire_capacite(RACINE / "capabilities" / "kitchen_daily")
    assert cap.valide, cap.erreurs
    m = cap.manifeste
    assert m is not None and m.statut == "actif" and m.origine == "humain"
    assert [c.nom for c in m.crons] == ["rappel"] and m.crons[0].quand == "Wed 18:00"
    assert m.actions_avec_effet == [
        "kitchen_daily_proposer", "kitchen_daily_extras_definir", "kitchen_daily_livraison_definir",
    ]
    # Extras et semaine sautée : toujours un bouton, même si les plats sont en validation automatique.
    assert m.confirmation_toujours == ["kitchen_daily_extras_definir", "kitchen_daily_livraison_definir"]


def test_manifeste_invalide() -> None:
    from tools import capacites

    base = {"nom": "demo", "titre": "Démo", "description": "x", "version": 1, "statut": "actif", "origine": "humain"}
    m, erreurs, avert = capacites.valider_manifeste({**base, "inconnue": 1}, "demo")
    assert m is not None and not erreurs and any("inconnue" in a for a in avert)

    cas = {
        "nom": {**base, "nom": "Démo-1"},
        "dossier": base,  # nom ≠ dossier
        "statut": {**base, "statut": "allumé"},
        "origine": {**base, "origine": "robot"},
        "action": {**base, "actions_avec_effet": ["autre_agir"]},
        "toujours": {**base, "actions_avec_effet": ["demo_agir"], "confirmation_toujours": ["demo_autre"]},
        "cron": {**base, "crons": [{"nom": "x", "quand": "Mercredi 18h"}]},
        "commande": {**base, "commandes_telegram": [{"nom": "start", "aide": "x"}]},
        "secret": {**base, "secrets": [{"cle": "demo", "champs": []}]},
        "parametre": {**base, "parametres": [{"cle": "ligne", "type": "choix", "question": "Ligne ?"}]},
        "type": {**base, "parametres": [{"cle": "x", "type": "date", "question": "?"}]},
        "version": {**base, "version": 0},
    }
    for cle, brut in cas.items():
        m, erreurs, _ = capacites.valider_manifeste(brut, "autre" if cle == "dossier" else "demo")
        assert m is None and erreurs, cle
    m, erreurs, _ = capacites.valider_manifeste([1, 2])
    assert m is None and erreurs


def test_fichiers_obligatoires(env: dict[str, Path]) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"], crons=[{"nom": "tache", "quand": "07:00"}])
    (d / "crons.py").unlink()
    assert not capacites.lire_capacite(d).valide
    (d / "crons.py").write_text(CRONS_DEMO, encoding="utf-8")
    (d / "tools.py").unlink()
    cap = capacites.lire_capacite(d)
    assert not cap.valide and any("tools.py" in e for e in cap.erreurs)
    assert capacites.statut_effectif(cap) == "invalide"


def test_oncalendar() -> None:
    from tools.capacites import oncalendar

    assert oncalendar("Wed 18:00") == "Wed *-*-* 18:00:00"
    assert oncalendar("7:05") == "*-*-* 07:05:00"
    assert oncalendar("mon..fri 07:00") == "Mon..Fri *-*-* 07:00:00"
    assert oncalendar("Mon,Thu 04:30") == "Mon,Thu *-*-* 04:30:00"
    for mauvais in ("25:00", "Mercredi 18:00", "18h", ""):
        with pytest.raises(ValueError):
            oncalendar(mauvais)


# --- masquage, chargement, essai, actions confirmées ------------------------------------


def test_masquer_secrets() -> None:
    from tools.capacites import MASQUE, masquer_secrets

    donnees = {"a": "mdp=Sup3rSecret!", "b": ["x", {"c": "token abcd1234 fin"}], "n": 3, "abcd1234": True}
    masque = masquer_secrets(donnees, ["Sup3rSecret!", "abcd1234", "ab"])
    assert masque == {"a": f"mdp={MASQUE}", "b": ["x", {"c": f"token {MASQUE} fin"}], "n": 3, MASQUE: True}
    assert masquer_secrets("rien", []) == "rien"


def test_chargement_fastmcp_signatures(env: dict[str, Path]) -> None:
    from tools import capacites

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # SDK mcp 2.x
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

    creer_capacite(env["caps"])
    creer_capacite(env["caps"], "inactive", statut="inactif")
    creer_capacite(env["caps"], "cassee", tools="def enregistrer(mcp, ctx):\n    raise RuntimeError('boum')\n")
    mcp = FastMCP("test")
    charges = capacites.charger_actives(mcp)
    assert set(charges) == {"demo"}  # inactive ignorée, cassée journalisée et ignorée
    outils = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert {"demo_lire", "demo_agir", "demo_secret", "demo_reglage"} <= set(outils)
    outil = outils["demo_lire"]
    schema = getattr(outil, "inputSchema", None) or getattr(outil, "input_schema", None)  # SDK mcp 1.x / 2.x
    assert "texte" in schema["properties"]
    assert outils["demo_lire"].description == "Lit quelque chose."


def test_prefixe_obligatoire(env: dict[str, Path]) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"], tools="def enregistrer(mcp, ctx):\n    @mcp.tool()\n    def autre_outil() -> str:\n        return 'x'\n")
    with pytest.raises(capacites.ManifesteInvalide):
        charger(d, env["data"])


def test_essai_bloque_action_avec_effet(env: dict[str, Path]) -> None:
    from tools import capacites, db, runner

    d = creer_capacite(env["caps"], statut="essai")
    mcp = charger(d, env["data"])
    assert mcp.outils["demo_lire"](texte="x")["texte"] == "x"  # lecture libre en essai
    with pytest.raises(ValueError, match="en essai"):
        mcp.outils["demo_agir"](quoi="réserver")
    assert db.recent_actions(1)[0]["status"] == "error"

    capacites.activer("demo")
    reponse = mcp.outils["demo_agir"](quoi="réserver")
    assert reponse.startswith("CONFIRMATION_REQUISE:") and reponse.endswith(":faire réserver")
    aid = int(reponse.split(":")[1])
    action = db.get_action(aid)
    assert action["kind"] == "demo_agir" and action["status"] == "pending"
    assert runner.main(["execute", str(aid)]) == 0
    assert db.get_action(aid)["result"] == "fait : réserver"

    # Désactivée : l'outil et l'exécution d'une action déjà proposée sont refusés.
    aid2 = int(mcp.outils["demo_agir"](quoi="encore").split(":")[1])
    capacites.desactiver("demo")
    with pytest.raises(ValueError, match="désactivée"):
        mcp.outils["demo_lire"]()
    assert runner.main(["execute", str(aid2)]) == 1
    assert db.get_action(aid2)["status"] == "error"


def test_origine_assistant_reste_en_essai(env: dict[str, Path]) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"], origine="assistant")
    cap = capacites.lire_capacite(d)
    assert capacites.statut_effectif(cap, capacites.lire_etat()) == "essai"
    # Lot 7b : l'action avec effet n'est pas exécutée, le cadre prépare la première validation (propriétaire) ;
    # sans l'autorisation donnée au clic, le runner refuse de l'exécuter.
    texte = charger(d, env["data"]).outils["demo_agir"](quoi="x")
    assert texte.startswith("CONFIRMATION_REQUISE:") and "première utilisation" in texte
    from tools import db, runner

    aid = int(texte.split(":")[1])
    assert db.get_action(aid)["payload"] == {"quoi": "x"}
    assert runner.main(["execute", str(aid)]) == 1
    capacites.activer("demo")  # validation humaine
    etat = capacites.lire_etat()
    assert capacites.statut_effectif(cap, etat) == "actif" and etat["demo"]["validee_le"]
    assert (env["data"] / "capacites" / "demo").is_dir()


def test_personnes_restreintes(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"], personnes=["Camille"])
    assert capacites.lire_capacite(d).manifeste.personnes == ["camille"]
    with pytest.raises(ValueError, match="pas ouverte"):
        charger(d, env["data"], personne="alex").outils["demo_lire"]()
    assert charger(d, env["data"], personne="camille").outils["demo_lire"]()["personne"] == "camille"
    monkeypatch.setenv("HOMEOS_PERSONNE", "alex")
    assert capacites.charger_actives(capacites.MCPFactice()) == {}
    monkeypatch.setenv("HOMEOS_PERSONNE", "camille")
    assert "demo" in capacites.charger_actives(capacites.MCPFactice())


# --- secrets ----------------------------------------------------------------------


class _EntreeCoffre(dict):
    """Entrée du coffre de test : modifier un champ l'écrit aussi dans le vrai coffre (fusion)."""

    def __init__(self, cle: str, champs: dict[str, str]) -> None:
        super().__init__(champs)
        self.cle = cle

    def __setitem__(self, champ: str, valeur: str) -> None:
        from tools import capacites, coffre

        super().__setitem__(champ, valeur)
        coffre.ecrire(self.cle, {champ: valeur}, fusion=True)
        capacites._CACHE_COFFRE["quand"] = -1e9


class _CoffreTest(dict):
    """`coffre["cle"] = {...}` écrit dans le vrai coffre (`tools.coffre`, backend fichier, data/ de test)."""

    def __setitem__(self, cle: str, champs: dict[str, str]) -> None:
        from tools import capacites, coffre

        coffre.ecrire(cle, dict(champs))
        capacites._CACHE_COFFRE["quand"] = -1e9
        super().__setitem__(cle, _EntreeCoffre(cle, dict(champs)))


@pytest.fixture()
def coffre(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    """Vrai coffre du lot 5b (data/coffre.json chiffré) dans le data/ de test."""
    from tools import capacites

    monkeypatch.setenv("COFFRE_CLE", "cle-de-test-capacites-0123456789abcdef")
    monkeypatch.setitem(capacites._CACHE_COFFRE, "quand", -1e9)  # pas de valeurs du coffre en cache
    return _CoffreTest()


def test_secret_et_masquage(env: dict[str, Path], coffre: dict[str, dict[str, str]]) -> None:
    from tools import db

    d = creer_capacite(env["caps"])
    mcp = charger(d, env["data"])
    with pytest.raises(ValueError, match=r"Identifiants Démo à renseigner : /identifiants demo"):
        mcp.outils["demo_secret"]()
    coffre["demo"] = {"identifiant": "moi@exemple.fr"}
    with pytest.raises(ValueError, match="mot_de_passe"):
        mcp.outils["demo_secret"]()
    coffre["demo"]["mot_de_passe"] = "Tr3sSecret"
    assert mcp.outils["demo_secret"]() == "connecté avec •••"
    # Une valeur connue du coffre est masquée partout, y compris dans les arguments journalisés.
    assert "Tr3sSecret" not in json.dumps(mcp.outils["demo_lire"](texte="code Tr3sSecret"))
    assert all("Tr3sSecret" not in json.dumps(a) for a in db.recent_actions(10))


def test_secret_par_personne(env: dict[str, Path], coffre: dict[str, dict[str, str]]) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"], "planity", titre="Planity",
                       secrets=[{"cle": "planity/alex", "champs": ["identifiant", "mot_de_passe"], "par_personne": True}])
    cap = capacites.lire_capacite(d)
    coffre["planity/alex"] = {"identifiant": "a", "mot_de_passe": "motdepasseA"}
    ctx = capacites.Contexte(env["data"], None, "alex", cap)
    assert ctx.secret("planity")["mot_de_passe"] == "motdepasseA"
    assert ctx.secret("secret://planity/alex")["identifiant"] == "a"
    with pytest.raises(capacites.ActionRefusee):
        ctx.secret("planity/camille")
    with pytest.raises(capacites.SecretManquant, match="Identifiants Planity à renseigner : /identifiants planity"):
        capacites.Contexte(env["data"], None, "camille", cap).secret("planity")
    with pytest.raises(capacites.ErreurCapacite, match="non déclaré"):
        ctx.secret("autre/chose")


def test_secret_coffre_indisponible(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import capacites, coffre

    monkeypatch.setitem(capacites._CACHE_COFFRE, "quand", -1e9)
    cap = capacites.lire_capacite(creer_capacite(env["caps"]))
    ctx = capacites.Contexte(env["data"], None, None, cap)
    # Coffre vide (mais clé présente) : invitation à /identifiants, avec la clé et les champs pour l'appelant.
    monkeypatch.setenv("COFFRE_CLE", "cle-de-test-capacites-0123456789abcdef")
    with pytest.raises(capacites.SecretManquant, match="/identifiants demo") as exc:
        ctx.secret("demo")
    assert capacites.SecretManquant is coffre.SecretManquant and exc.value.cle == "demo"
    # COFFRE_CLE absente avec un coffre existant : message clair, jamais d'exception brute.
    coffre.ecrire("demo", {"identifiant": "moi@exemple.fr"})
    monkeypatch.delenv("COFFRE_CLE")
    with pytest.raises(capacites.SecretManquant, match="Coffre indisponible"):
        ctx.secret("demo")
    assert capacites.valeurs_secretes() >= set()


# --- paramètres ---------------------------------------------------------------------


def test_parametres(env: dict[str, Path]) -> None:
    from tools import capacites

    parametres = [
        {"cle": "arret", "type": "texte", "question": "Quel arrêt ?", "obligatoire": True},
        {"cle": "seuil", "type": "nombre", "question": "Seuil d'alerte (min) ?", "defaut": 5},
        {"cle": "ligne", "type": "choix", "question": "Quelle ligne ?", "choix": ["7", "N12"]},
        {"cle": "alerte", "type": "booleen", "question": "M'alerter ?"},
    ]
    d = creer_capacite(env["caps"], parametres=parametres)
    cap = capacites.lire_capacite(d)
    mcp = charger(d, env["data"])
    with pytest.raises(ValueError, match="Quel arrêt"):
        mcp.outils["demo_reglage"]()
    assert [p.cle for p in capacites.parametres_manquants(cap, env["data"])] == ["arret"]
    assert "Réglages à compléter" in capacites.fragments_prompt()

    capacites.definir_parametre("demo", "arret", " Les Tilleuls ")
    capacites.definir_parametre("demo", "ligne", "n12")
    capacites.definir_parametre("demo", "alerte", "oui")
    capacites.definir_parametre("demo", "seuil", "7,5")
    assert mcp.outils["demo_reglage"]() == "Les Tilleuls"
    valeurs = yaml.safe_load((env["data"] / "capacites" / "demo" / "parametres.yaml").read_text(encoding="utf-8"))
    assert valeurs == {"arret": "Les Tilleuls", "ligne": "N12", "alerte": True, "seuil": 7.5}
    ctx = capacites.Contexte(env["data"], None, None, cap)
    assert ctx.parametre("seuil") == 7.5 and ctx.parametre("alerte") is True
    for cle, valeur in (("ligne", "52"), ("seuil", "beaucoup"), ("alerte", "peut-être"), ("inconnu", "x")):
        with pytest.raises(capacites.ErreurCapacite):
            capacites.definir_parametre("demo", cle, valeur)
    assert "Réglages à compléter" not in capacites.fragments_prompt()
    params = {p["cle"]: p for p in capacites.statut_detaille("demo")["parametres"]}
    assert params["seuil"]["valeur"] == 7.5 and params["arret"]["renseigne"]


# --- CLI ------------------------------------------------------------------------------


def _cli(*args: str, entree: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.capacites", *args],
        cwd=str(RACINE),
        env={**os.environ, "PYTHONPATH": str(RACINE)},
        input=entree,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_new_produit_un_module_qui_charge(env: dict[str, Path]) -> None:
    from tools import capacites

    proc = _cli("new", "planity", "--titre", "Planity", "--description", "Rendez-vous coiffeur : « créneaux ».")
    assert proc.returncode == 0, proc.stderr
    dossier = env["caps"] / "planity"
    for f in ("capability.yaml", "tools.py", "smoke.py", "CLAUDE.md", "README.md", "tests/test_planity.py"):
        assert (dossier / f).exists(), f
    cap = capacites.lire_capacite(dossier)
    assert cap.valide and cap.manifeste.statut == "essai" and cap.manifeste.description.endswith("« créneaux ».")
    assert charger(dossier, env["data"]).outils["planity_exemple"](texte="ok") == "Planity a bien reçu : ok"
    # Le test généré passe tel quel.
    test = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=importlib",
         "--rootdir", str(dossier), str(dossier / "tests")],
        cwd=str(RACINE), env={**os.environ, "PYTHONPATH": str(RACINE)}, capture_output=True, text=True, check=False,
    )
    assert test.returncode == 0, test.stdout + test.stderr
    assert _cli("new", "planity", "--titre", "X").returncode == 1  # existe déjà
    assert _cli("new", "Pas-Bon", "--titre", "X").returncode == 1


def test_cli_cycle_de_vie(env: dict[str, Path]) -> None:
    creer_capacite(env["caps"], statut="inactif")
    (env["caps"] / "vide").mkdir()  # dossier sans manifeste : listé comme invalide

    infos = {i["nom"]: i for i in json.loads(_cli("list", "--json").stdout)}
    assert infos["demo"]["statut"] == "inactif" and infos["vide"]["statut"] == "invalide"
    assert "demo" in _cli("list").stdout

    assert _cli("enable", "demo").returncode == 0
    assert json.loads(_cli("statut", "demo").stdout)["statut"] == "actif"
    assert _cli("disable", "demo").returncode == 0
    assert json.loads(_cli("statut", "demo").stdout)["statut"] == "inactif"
    assert (env["data"] / "capacites" / "demo").is_dir()  # disable garde les données
    assert _cli("enable", "vide").returncode == 1
    assert _cli("parametre", "demo", "arret", "Gare").returncode == 0
    assert json.loads(_cli("parametre", "demo").stdout)[0]["valeur"] == "Gare"

    assert _cli("remove", "demo", entree="non\n").returncode == 1
    assert (env["caps"] / "demo").exists()
    assert _cli("remove", "demo", "--donnees", "--oui").returncode == 0
    assert not (env["caps"] / "demo").exists() and not (env["data"] / "capacites" / "demo").exists()
    assert _cli("statut", "demo").returncode == 1


def test_cli_list_avec_vraies_capacites(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMEOS_CAPABILITIES")
    proc = _cli("list")
    assert proc.returncode == 0 and "kitchen_daily" in proc.stdout and "actif" in proc.stdout


# --- smoke, crons, timers ------------------------------------------------------------------


def test_smoke(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import capacites

    creer_capacite(env["caps"])
    creer_capacite(env["caps"], "lente", tools=TOOLS_DEMO)
    (env["caps"] / "lente" / "smoke.py").write_text("import time\ndef verifier(ctx):\n    time.sleep(5)\n", encoding="utf-8")
    assert capacites.smoke(capacites.trouver("lente"), delai_s=0.2) == {"ok": False, "message": "pas de réponse en 0 s"}
    (env["caps"] / "lente" / "smoke.py").unlink()

    monkeypatch.setenv("DEMO_SMOKE", "ko")
    proc = _cli("smoke", "--tsv")
    assert proc.returncode == 1
    lignes = dict(line.split("\t", 1) for line in proc.stdout.strip().splitlines())
    assert lignes["demo"] == "0\tDémo\tmessage du smoke" and lignes["lente"].startswith("1\t")
    assert capacites.lire_etat()["demo"]["smoke"]["ok"] is False
    # Cache : le résultat récent est réutilisé même si le service est rétabli.
    monkeypatch.setenv("DEMO_SMOKE", "ok")
    assert capacites.smoke_tous("demo", cache_minutes=30)[0]["cache"] is True
    assert capacites.smoke_tous("demo")[0]["ok"] is True
    assert _cli("smoke", "demo").returncode == 0


def test_executer_cron(env: dict[str, Path]) -> None:
    from tools import capacites, db

    creer_capacite(env["caps"], crons=[{"nom": "tache", "quand": "Mon 04:00"}, {"nom": "boum", "quand": "05:00"}])
    proc = _cli("cron", "demo", "tache")
    assert proc.returncode == 0 and proc.stdout.strip() == "cron tache exécuté"
    assert db.recent_actions(1)[0]["kind"] == "cron_demo_tache"
    with pytest.raises(capacites.ErreurCapacite, match="cron cassé"):
        capacites.executer_cron("demo", "boum")
    with pytest.raises(capacites.ErreurCapacite, match="non déclaré"):
        capacites.executer_cron("demo", "absent")
    (env["data"] / "PAUSE").write_text("pause", encoding="utf-8")
    assert capacites.executer_cron("demo", "tache") is None
    (env["data"] / "PAUSE").unlink()
    capacites.desactiver("demo")
    with pytest.raises(capacites.ActionRefusee):
        capacites.executer_cron("demo", "tache")
    assert _cli("cron", "demo", "tache").returncode == 1


def test_timers(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import capacites

    monkeypatch.delenv("HOMEOS_CAPABILITIES")
    assert ("kitchen_daily", "rappel", "Wed *-*-* 18:00:00") in capacites.timers()
    config = env["data"] / "config.yaml"
    config.write_text("crons:\n  rappel_kitchen_daily: \"Tue 19:30\"\n", encoding="utf-8")  # clé du lot 2
    assert ("kitchen_daily", "rappel", "Tue *-*-* 19:30:00") in capacites.timers()
    config.write_text("capacites:\n  kitchen_daily:\n    crons:\n      rappel: \"Thu 08:00\"\n", encoding="utf-8")
    assert "kitchen_daily\trappel\tThu *-*-* 08:00:00" in _cli("timers").stdout.splitlines()
    capacites.desactiver("kitchen_daily")
    assert not any(t[0] == "kitchen_daily" for t in capacites.timers())


# --- prompt et bot -----------------------------------------------------------------------------


def test_fragments_prompt_et_bot(env: dict[str, Path]) -> None:
    from bot.claude_runner import construire_commande, preparer_prompt_systeme
    from bot.commands import (
        commandes_capacites,
        commandes_telegram,
        formater_capacites,
        texte_aide,
    )
    from bot.config import parse_config
    from tools import capacites

    creer_capacite(env["caps"], statut="essai", personnes=["alex"])
    creer_capacite(env["caps"], "eteinte", statut="inactif")
    fragments = capacites.fragments_prompt()
    assert "# Capacités actives" in fragments and "**Démo**" in fragments and "en essai" in fragments
    assert "`/demo`" in fragments and "## Démo" in fragments and "eteinte" not in fragments

    root = env["tmp"]
    (root / "content").mkdir()
    (root / "content" / "CLAUDE.md").write_text("# {{assistant.nom}}\nBase.", encoding="utf-8")
    cfg = parse_config(yaml.safe_load((env["data"] / "config.yaml").read_text(encoding="utf-8")), env["data"], root)
    texte = preparer_prompt_systeme(cfg).read_text(encoding="utf-8")
    assert texte.startswith("# Testbot\nBase.") and "## Démo" in texte
    cmd = construire_commande(cfg, "alex", "x", None)
    add_dirs = [a for i, a in enumerate(cmd) if cmd[i - 1] == "--add-dir"]
    assert str(env["caps"]) in add_dirs

    assert commandes_capacites(cfg) == [("demo", "Commande de démo", "demo")]
    assert any(c.command == "demo" for c in commandes_telegram(cfg))
    assert "/demo — Commande de démo" in texte_aide("Testbot", cfg)
    liste = formater_capacites(capacites.lister(), cfg.personnes)
    assert "<b>Démo</b>" in liste and "en essai" in liste and "Pour : Alex" in liste and "désactivée" in liste


def test_analyser_commande() -> None:
    from bot.commands import analyser_commande

    assert analyser_commande("/bus 7 Les Tilleuls") == ("bus", None)
    assert analyser_commande("/Bus@MonBot") == ("bus", "MonBot")
    assert analyser_commande("") == ("", None)


# --- export / install ---------------------------------------------------------------------------


def test_export_install(env: dict[str, Path], tmp_path: Path) -> None:
    from tools import capacites

    d = creer_capacite(env["caps"])
    (d / "tests").mkdir()
    (d / "tests" / "test_demo.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "tools.cpython-312.pyc").write_bytes(b"x")
    (d / "data").mkdir()
    (d / "data" / "cache.json").write_text("{}", encoding="utf-8")
    (d / ".env").write_text("CLE=1", encoding="utf-8")
    (d / "secrets.yaml").write_text("x: 1", encoding="utf-8")

    sortie = tmp_path / "export"
    proc = _cli("export", "demo", "--sortie", str(sortie))
    assert proc.returncode == 0, proc.stderr
    archive = sortie / "demo-v1.tar.gz"
    empreinte = sortie / "demo-v1.tar.gz.sha256"
    assert archive.exists() and empreinte.read_text(encoding="utf-8").endswith("  demo-v1.tar.gz\n")
    with tarfile.open(archive) as tar:
        noms = sorted(tar.getnames())
    assert noms == ["demo/CLAUDE.md", "demo/capability.yaml", "demo/smoke.py", "demo/tests/test_demo.py", "demo/tools.py"]

    autre = tmp_path / "autre-foyer"
    with pytest.raises(capacites.ErreurCapacite, match="annulée"):
        capacites.installer(archive, lambda texte: False, racine=autre)
    vu: list[str] = []
    cap = capacites.installer(archive, lambda texte: vu.append(texte) or True, racine=autre)
    assert cap.valide and (autre / "demo" / "tools.py").exists()
    assert "Identifiants demandés (coffre) : demo (identifiant, mot_de_passe)" in vu[0]
    assert "Actions avec effet" in vu[0] and "à venir" in vu[0]
    assert capacites.lire_etat()["demo"]["statut"] == "essai"
    with pytest.raises(capacites.ErreurCapacite, match="déjà installée"):
        capacites.installer(archive, lambda texte: True, racine=autre)

    # Empreinte qui ne correspond plus : refus.
    empreinte.write_text("0" * 64 + "  demo-v1.tar.gz\n", encoding="utf-8")
    with pytest.raises(capacites.ErreurCapacite, match="SHA256"):
        capacites.installer(archive, lambda texte: True, racine=tmp_path / "x")


def test_cli_install(env: dict[str, Path], tmp_path: Path) -> None:
    creer_capacite(env["caps"])
    sortie = tmp_path / "export"
    assert _cli("export", "demo", "--sortie", str(sortie)).returncode == 0
    _cli("remove", "demo", "--oui")
    proc = _cli("install", str(sortie / "demo-v1.tar.gz"), entree="n\n")
    assert proc.returncode == 1 and "Accès réseau" in proc.stdout
    proc = _cli("install", str(sortie / "demo-v1.tar.gz"), entree="o\n")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(_cli("statut", "demo").stdout)["statut"] == "essai"


def test_install_archive_malveillante(env: dict[str, Path], tmp_path: Path) -> None:
    from tools import capacites

    archive = tmp_path / "mauvaise.tar.gz"
    source = tmp_path / "f.txt"
    source.write_text("x", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="demo/f.txt")
        tar.add(source, arcname="autre/f.txt")
    with pytest.raises(capacites.ErreurCapacite, match="un seul dossier"):
        capacites.installer(archive, lambda t: True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="../evasion.txt")
    with pytest.raises(capacites.ErreurCapacite):
        capacites.installer(archive, lambda t: True)
    assert not (tmp_path / "evasion.txt").exists()


# --- Kitchen Daily migré -------------------------------------------------------------------------


def test_kitchen_daily_capacite(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import capacites, kitchen_daily, runner

    monkeypatch.delenv("HOMEOS_CAPABILITIES")
    cap = capacites.trouver("kitchen_daily")
    mcp = charger(cap.dossier, env["data"])
    assert set(mcp.outils) == {
        "kitchen_daily_livraisons", "kitchen_daily_menu", "kitchen_daily_preferences",
        "kitchen_daily_proposer", "kitchen_daily_historique_ajouter",
        "kitchen_daily_extras", "kitchen_daily_extras_definir", "kitchen_daily_livraison_definir",
    }
    assert "Aucune préférence" in mcp.outils["kitchen_daily_preferences"]()
    with pytest.raises(ValueError, match="Session Kitchen Daily"):
        mcp.outils["kitchen_daily_proposer"](livraison_id="x", selection={"a": 18}, justification="j")

    # Smoke : pas de session → échec explicite, sans lancer de navigateur.
    assert capacites.smoke(cap) == {"ok": False, "message": kitchen_daily.MESSAGE_SESSION}

    # Cron « rappel » : même assemblage que `runner rappel kitchen-daily`, texte renvoyé pour le groupe.
    prompts: list[str] = []
    monkeypatch.setattr(
        runner, "appeler_claude", lambda prompt, modele=None: prompts.append(prompt) or "Proposition de la semaine"
    )
    assert capacites.executer_cron("kitchen_daily", "rappel") == "Proposition de la semaine"
    assert "<donnees>" in prompts[0] and "kitchen_daily_erreur" in prompts[0]

    # L'action validée `kitchen_daily_selection` est routée vers la capacité (refusée si elle est désactivée).
    assert capacites.executeur_pour("kitchen_daily_selection") is not None
    assert capacites.executeur_pour("kitchen_daily_extras_definir") is not None
    assert capacites.executeur_pour("agenda_ajouter") is None
    capacites.desactiver("kitchen_daily")
    with pytest.raises(capacites.ActionRefusee):
        capacites.executeur_pour("kitchen_daily_selection")({"livraison_id": "x", "selection": {"a": 1}})


def test_commande_dynamique(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """« /demo … » est transmis tel quel à l'assistant ; commande inconnue, autre bot, personne non autorisée."""
    from types import SimpleNamespace

    from bot import commands
    from bot.config import parse_config
    from bot.db import Db

    creer_capacite(env["caps"], personnes=["camille"])
    root = env["tmp"]
    cfg = parse_config(yaml.safe_load((env["data"] / "config.yaml").read_text(encoding="utf-8")), env["data"], root)
    db = Db(cfg.db_path)
    db.init()
    transmis: list[tuple[str, str]] = []
    envoyes: list[str] = []

    async def faux_claude(update: Any, context: Any, personne: Any, texte: str) -> None:
        transmis.append((personne.prenom, texte))

    async def faux_envoyer(update: Any, texte: str, html: bool = True, reply_markup: Any = None) -> None:
        envoyes.append(texte)

    monkeypatch.setattr(commands, "repondre_via_claude", faux_claude)
    monkeypatch.setattr(commands, "_envoyer", faux_envoyer)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": SimpleNamespace(cfg=cfg, db=db)}),
                              bot=SimpleNamespace(username="FoyerBot"))

    def message(texte: str, telegram_id: int, chat_type: str = "private") -> Any:
        return SimpleNamespace(
            effective_message=SimpleNamespace(text=texte),
            effective_user=SimpleNamespace(id=telegram_id, username="x"),
            effective_chat=SimpleNamespace(id=telegram_id, type=chat_type),
        )

    asyncio.run(commands.cmd_capacite_dynamique(message("/demo@FoyerBot gare", 2), context))
    assert transmis == [("Camille", "/demo gare")]
    asyncio.run(commands.cmd_capacite_dynamique(message("/demo", 1), context))
    assert transmis == [("Camille", "/demo gare")] and envoyes[-1] == "Cette commande n'est pas ouverte pour toi."
    asyncio.run(commands.cmd_capacite_dynamique(message("/demo@AutreBot", 2), context))
    asyncio.run(commands.cmd_capacite_dynamique(message("/inconnue", 2, "group"), context))  # groupe : silence
    assert len(transmis) == 1 and len(envoyes) == 1
    asyncio.run(commands.cmd_capacite_dynamique(message("/inconnue", 2), context))
    assert envoyes[-1].startswith("Commande inconnue")
