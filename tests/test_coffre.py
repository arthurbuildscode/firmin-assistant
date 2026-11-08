"""Tests du coffre (`tools/coffre.py`) : chiffrement, mauvaise clé, masquage, CLI sans fuite."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))
CLE_TEST = "cle-de-test-0123456789abcdef0123456789abcdef"
SECRET = "Mdp-Tres-Secret-42!"
IDENTIFIANT = "alex.test@example.org"


@pytest.fixture()
def data_coffre(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.delenv("COFFRE_CLE", raising=False)
    (data / "secrets.env").write_text(f"TELEGRAM_BOT_TOKEN=x\nCOFFRE_CLE={CLE_TEST}\n", encoding="utf-8")
    return data


# --- chiffrement ------------------------------------------------------------------------


def test_chiffrer_dechiffrer_aller_retour() -> None:
    from tools import coffre

    contenu = {"secrets": {"planity/alex": {"champs": {"mot_de_passe": SECRET}}}}
    env1 = coffre.chiffrer(contenu, CLE_TEST)
    env2 = coffre.chiffrer(contenu, CLE_TEST)
    assert SECRET not in json.dumps(env1)
    assert env1["nonce"] != env2["nonce"] and env1["kdf"]["sel"] != env2["kdf"]["sel"]
    assert coffre.dechiffrer(env1, CLE_TEST) == contenu


def test_dechiffrer_mauvaise_cle_et_alteration() -> None:
    from tools import coffre

    env = coffre.chiffrer({"secrets": {"a": {"champs": {"x": SECRET}}}}, CLE_TEST)
    with pytest.raises(coffre.CoffreIllisible) as exc:
        coffre.dechiffrer(env, "une-autre-cle-tout-aussi-longue")
    assert SECRET not in str(exc.value) and "COFFRE_CLE incorrecte" in str(exc.value)
    # Données associées authentifiées : modifier les paramètres de dérivation casse le tag.
    altere = {**env, "kdf": {**env["kdf"], "n": env["kdf"]["n"] * 2}}
    with pytest.raises(coffre.CoffreIllisible):
        coffre.dechiffrer(altere, CLE_TEST)
    with pytest.raises(coffre.CoffreIllisible):
        coffre.dechiffrer({**env, "format": "autre"}, CLE_TEST)


def test_fichier_sur_disque_chiffre_et_mode(data_coffre: Path) -> None:
    from tools import coffre

    coffre.ecrire("planity/alex", {"identifiant": IDENTIFIANT, "mot_de_passe": SECRET})
    fichier = data_coffre / "coffre.json"
    brut = fichier.read_text(encoding="utf-8")
    assert SECRET not in brut and IDENTIFIANT not in brut and "planity" not in brut
    assert stat.S_IMODE(fichier.stat().st_mode) == 0o640
    assert not list(data_coffre.glob(".coffre.json.*.tmp"))
    assert coffre.lire("secret://planity/alex") == {"identifiant": IDENTIFIANT, "mot_de_passe": SECRET}


def test_mauvaise_cle_sur_disque(data_coffre: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import coffre

    coffre.ecrire("cantine", {"mot_de_passe": SECRET})
    monkeypatch.setenv("COFFRE_CLE", "mauvaise-cle-mais-assez-longue")
    with pytest.raises(coffre.CoffreIllisible):
        coffre.lire("cantine")
    with pytest.raises(coffre.CoffreIllisible):
        coffre.ecrire("cantine", {"mot_de_passe": "autre"})  # n'écrase jamais un coffre illisible
    assert coffre.valeurs_connues() == set()  # ne lève pas
    monkeypatch.delenv("COFFRE_CLE")
    assert coffre.lire("cantine") == {"mot_de_passe": SECRET}


def test_cle_absente(data_coffre: Path) -> None:
    from tools import coffre

    (data_coffre / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    assert coffre.lister() == []  # coffre pas encore créé : pas besoin de clé
    with pytest.raises(coffre.CleCoffreAbsente):
        coffre.ecrire("planity/alex", {"mot_de_passe": SECRET})
    (data_coffre / "secrets.env").write_text("COFFRE_CLE=court\n", encoding="utf-8")
    with pytest.raises(coffre.CleCoffreAbsente):
        coffre.ecrire("planity/alex", {"mot_de_passe": SECRET})


# --- API ------------------------------------------------------------------------------------


def test_api_lister_fusion_supprimer(data_coffre: Path) -> None:
    from tools import coffre

    assert coffre.lister() == []
    coffre.ecrire("planity/alex", {"identifiant": IDENTIFIANT, "mot_de_passe": SECRET})
    coffre.ecrire("planity/camille", {"identifiant": "b@example.org", "mot_de_passe": "autre-secret"})
    assert coffre.lister() == ["planity/alex", "planity/camille"]
    coffre.ecrire("planity/alex", {"mot_de_passe": "nouveau-secret"}, fusion=True)
    assert coffre.lire("planity/alex") == {"identifiant": IDENTIFIANT, "mot_de_passe": "nouveau-secret"}
    coffre.ecrire("planity/alex", {"mot_de_passe": "remplace"})
    assert coffre.lire("planity/alex") == {"mot_de_passe": "remplace"}
    meta = coffre.decrire()
    assert meta[0]["cle"] == "planity/alex" and meta[0]["champs"] == ["mot_de_passe"] and meta[0]["par"] == "test"
    assert "remplace" not in json.dumps(meta)
    assert coffre.supprimer("planity/alex") is True
    assert coffre.supprimer("planity/alex") is False
    assert coffre.lister() == ["planity/camille"]


def test_secret_manquant_message(data_coffre: Path) -> None:
    from tools import coffre

    with pytest.raises(coffre.SecretManquant) as exc:
        coffre.lire("planity/alex")
    assert str(exc.value) == "Identifiants Planity à renseigner : /identifiants planity"
    assert exc.value.capacite == "planity"
    coffre.ecrire("planity/alex", {"identifiant": IDENTIFIANT})
    with pytest.raises(coffre.SecretManquant) as exc:
        coffre.lire("planity/alex", champs=["identifiant", "mot_de_passe"])
    assert exc.value.champs_manquants == ["mot_de_passe"] and IDENTIFIANT not in str(exc.value)


@pytest.mark.parametrize("cle", ["", "../secrets", "planity/../x", "plan ity", "a" * 200, "/", "é/t"])
def test_cles_invalides(cle: str) -> None:
    from tools import coffre

    with pytest.raises(coffre.ErreurCoffre):
        coffre.normaliser_cle(cle)


def test_champs_invalides(data_coffre: Path) -> None:
    from tools import coffre

    with pytest.raises(coffre.ErreurCoffre):
        coffre.ecrire("planity/alex", {})
    with pytest.raises(coffre.ErreurCoffre):
        coffre.ecrire("planity/alex", {"Mot de passe": SECRET})
    with pytest.raises(coffre.ErreurCoffre) as exc:
        coffre.ecrire("planity/alex", {"mot_de_passe": ""})
    assert coffre.lister() == []
    assert SECRET not in str(exc.value)


def test_backend_vaultwarden_non_implemente(data_coffre: Path) -> None:
    from tools import coffre

    (data_coffre / "config.yaml").write_text("coffre:\n  backend: vaultwarden\n", encoding="utf-8")
    with pytest.raises(NotImplementedError, match="docs/coffre.md"):
        coffre.lire("planity/alex")
    assert coffre.valeurs_connues() == set()
    (data_coffre / "config.yaml").write_text("coffre:\n  backend: inconnu\n", encoding="utf-8")
    with pytest.raises(coffre.ErreurCoffre, match="inconnu"):
        coffre.lister()


# --- masquage ---------------------------------------------------------------------------------


def test_valeurs_connues_et_masquer(data_coffre: Path) -> None:
    from tools import coffre

    assert coffre.masquer(f"rien {SECRET}") == f"rien {SECRET}"  # coffre vide
    coffre.ecrire("planity/alex", {"identifiant": IDENTIFIANT, "mot_de_passe": SECRET, "pin": "12"})
    assert coffre.valeurs_connues() == {IDENTIFIANT, SECRET}  # « 12 » trop court pour être masqué
    sortie = coffre.masquer(f"Connexion de {IDENTIFIANT} avec {SECRET} réussie")
    assert sortie == "Connexion de ••• avec ••• réussie"
    # Les valeurs les plus longues d'abord (une valeur contenue dans une autre ne laisse pas de reste).
    assert coffre.masquer("abcdefgh", {"abcd", "abcdefgh"}) == "•••"


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_set_get_ls_rm_sans_fuite(data_coffre: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import coffre

    saisies = iter([IDENTIFIANT, IDENTIFIANT, SECRET, SECRET])
    invites: list[str] = []

    def faux_getpass(invite: str = "") -> str:
        invites.append(invite)
        return next(saisies)

    monkeypatch.setattr(coffre, "_lire_valeur", faux_getpass)
    assert coffre.main(["set", "planity/alex"]) == 0
    assert invites == [
        "identifiant (saisie masquée) : ",
        "identifiant (confirmation) : ",
        "mot_de_passe (saisie masquée) : ",
        "mot_de_passe (confirmation) : ",
    ]
    assert coffre.main(["get", "planity/alex", "--champs"]) == 0
    assert coffre.main(["ls"]) == 0
    assert coffre.main(["statut"]) == 0
    sortie = capsys.readouterr()
    assert SECRET not in sortie.out + sortie.err and IDENTIFIANT not in sortie.out + sortie.err
    assert "planity/alex : identifiant, mot_de_passe" in sortie.out
    assert coffre.lire("planity/alex")["mot_de_passe"] == SECRET

    assert coffre.main(["get", "planity/alex", "--revele"]) == 0
    revele = capsys.readouterr()
    assert f"mot_de_passe = {SECRET}" in revele.out and "Attention" in revele.err

    assert coffre.main(["rm", "planity/alex", "--oui"]) == 0
    assert coffre.main(["get", "planity/alex"]) == 1
    assert coffre.lister() == []


def test_cli_set_confirmation_differente(data_coffre: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import coffre

    saisies = iter([SECRET, SECRET + "x"])
    monkeypatch.setattr(coffre, "_lire_valeur", lambda invite="": next(saisies))
    assert coffre.main(["set", "cantine", "--champs", "mot_de_passe"]) == 1
    sortie = capsys.readouterr()
    assert "diffèrent" in sortie.err and SECRET not in sortie.out + sortie.err
    assert coffre.lister() == []


def test_cli_sans_argument_de_valeur(capsys: pytest.CaptureFixture[str]) -> None:
    """Aucune sous-commande n'accepte de valeur : un argument en trop est refusé, sans être réaffiché."""
    from tools import coffre

    for argv in (["set", "planity/alex", SECRET], ["set", "planity/alex", "--valeur", SECRET], [SECRET]):
        with pytest.raises(SystemExit):
            coffre.main(argv)
        sortie = capsys.readouterr()
        assert SECRET not in sortie.out + sortie.err
        assert "jamais en argument" in sortie.err


def test_cli_sous_processus_stdin_sans_fuite(data_coffre: Path) -> None:
    """`python -m tools.coffre` réel (sans terminal, lecture de stdin) : aucune valeur sur stdout/stderr."""
    env = {**os.environ, "PYTHONPATH": str(RACINE)}
    entree = f"{IDENTIFIANT}\n{IDENTIFIANT}\n{SECRET}\n{SECRET}\n"
    r = subprocess.run(
        [sys.executable, "-m", "tools.coffre", "set", "planity/camille"],
        input=entree, capture_output=True, text=True, cwd=RACINE, env=env, timeout=60, check=False,
    )
    assert r.returncode == 0, r.stderr
    for args in (["get", "planity/camille"], ["ls"], ["statut"]):
        s = subprocess.run(
            [sys.executable, "-m", "tools.coffre", *args],
            capture_output=True, text=True, cwd=RACINE, env=env, timeout=60, check=False,
        )
        assert s.returncode == 0, s.stderr
        assert SECRET not in s.stdout + s.stderr and IDENTIFIANT not in s.stdout + s.stderr
    assert SECRET not in r.stdout + r.stderr and IDENTIFIANT not in r.stdout + r.stderr
    assert "Enregistré : planity/camille" in r.stdout
