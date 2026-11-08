"""Lot 5c — identifiants et moyens de paiement gérés en conversation, filtre de données sensibles.

Principe vérifié partout : aucune sortie d'outil, aucun journal, aucun message d'erreur, aucun prompt ne contient une
valeur secrète. Sans réseau, sans `claude`."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

CLE_COFFRE = "cle-de-test-identifiants-0123456789abcdef"
MDP = "Hunter2-Mot-De-Passe!"
IDENT = "alice.test@example.org"
CARTE = "4970101234567893"  # numéro de test valide (Luhn), réseau Visa
CARTE_ESPACES = "4970 1012 3456 7893"
CVC = "4821"
IBAN = "FR7630006000011234567890189"


def _nir() -> str:
    corps = "1850578006084"
    return corps + f"{97 - int(corps) % 97:02d}"


TOOLS_CANTINE = '''
def enregistrer(mcp, ctx):
    @mcp.tool()
    def cantine_lire_carte() -> str:
        return "carte " + ctx.secret("cantine")["numero"]

    @mcp.tool()
    def cantine_payer(montant: str) -> str:
        return ctx.demander_confirmation("cantine_payer", f"Payer {montant} € à la cantine", {"montant": montant})


def executer(action, payload, ctx):
    carte = ctx.secret("cantine")
    return f"payé {payload['montant']} € avec la carte {carte['numero']} ({carte['cvc']})"
'''

TOOLS_PLANITY = '''
def enregistrer(mcp, ctx):
    @mcp.tool()
    def planity_connexion() -> str:
        s = ctx.secret("planity")
        return "connecté avec " + s["identifiant"] + " / " + s["mot_de_passe"]
'''


def _capacite(racine: Path, nom: str, tools: str, **manifeste: Any) -> Path:
    d = racine / nom
    d.mkdir(parents=True)
    brut = {"nom": nom, "titre": nom.capitalize(), "description": "Test.", "version": 1, "statut": "actif",
            "origine": "humain", **manifeste}
    (d / "capability.yaml").write_text(yaml.safe_dump(brut, allow_unicode=True), encoding="utf-8")
    (d / "tools.py").write_text(tools, encoding="utf-8")
    (d / "CLAUDE.md").write_text("Test.\n", encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from tools import capacites

    data = tmp_path / "data"
    data.mkdir()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump({
        "assistant": {"nom": "Firmin"},
        "personnes": [
            {"telegram_id": 101, "prenom": "Alice", "linux_user": "alice", "role": "proprietaire"},
            {"telegram_id": 202, "prenom": "Bruno", "linux_user": "bruno", "role": "membre"},
        ],
    }, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(RACINE))
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(caps))
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T12:00:00")
    monkeypatch.setenv("COFFRE_CLE", CLE_COFFRE)
    monkeypatch.setenv("HOMEOS_PERSONNE", "alice")
    monkeypatch.delenv("HOMEOS_WHO", raising=False)
    monkeypatch.delenv("HOMEOS_CANAL", raising=False)
    monkeypatch.setitem(capacites._CACHE_COFFRE, "quand", -1e9)
    monkeypatch.setattr(capacites, "DUREE_CACHE_COFFRE_S", 0.0)
    _capacite(caps, "cantine", TOOLS_CANTINE, secrets=[{"cle": "cantine", "type": "carte_bancaire"}],
              actions_avec_effet=["cantine_payer"], confirmation_toujours=["cantine_payer"])
    _capacite(caps, "planity", TOOLS_PLANITY, secrets=[
        {"cle": "planity/alice", "type": "identifiants", "champs": ["identifiant", "mot_de_passe"],
         "par_personne": True}])
    return {"data": data, "caps": caps, "tmp": tmp_path}


def _tout_le_journal(data: Path) -> str:
    with sqlite3.connect(data / "homeos.sqlite") as c:
        return json.dumps([list(r) for r in c.execute("SELECT * FROM actions_log")], ensure_ascii=False)


def _id(texte: str) -> int:
    assert texte.startswith("CONFIRMATION_REQUISE:"), texte
    return int(texte.split(":")[1])


def _executer(aid: int, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    from tools import runner

    code = runner.main(["execute", str(aid)])
    sortie = capsys.readouterr()
    return code, sortie.out + sortie.err


VALEURS = (MDP, IDENT, CARTE, CARTE_ESPACES, CVC)


def _sans_valeur(*textes: Any) -> None:
    for t in textes:
        brut = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False, default=str)
        for v in VALEURS:
            assert v not in brut, "valeur secrète trouvée"


# =====================================================================================================
# Coffre enrichi
# =====================================================================================================


def test_luhn_et_types() -> None:
    from tools import coffre

    assert coffre.luhn_valide(CARTE) and coffre.luhn_valide("4111111111111111")
    assert not coffre.luhn_valide("4970101234567890") and not coffre.luhn_valide("49701012x4567893")
    assert coffre.normaliser_type("Carte bancaire") == "carte_bancaire"
    assert coffre.normaliser_type("CB") == "carte_bancaire" and coffre.normaliser_type("clé d'API") == "cle_api"
    with pytest.raises(coffre.ErreurCoffre):
        coffre.normaliser_type("bitcoin")


def test_carte_validation_stricte_sans_jamais_citer_la_valeur(env: dict[str, Path]) -> None:
    from tools import coffre

    meta = {"type": "carte_bancaire", "libelle": "Carte cantine", "proprietaire": "foyer", "virtuelle": True}
    cas = [
        ({"numero": "4970101234567890"}, "Luhn"),
        ({"numero": "1234"}, "Luhn"),
        ({"expiration": "13/28"}, "MM/AA"),
        ({"expiration": "01/25"}, "expirée"),
        ({"cvc": "12"}, "Cryptogramme"),
        ({"titulaire": "A1"}, "Titulaire"),
        ({"pin": "1234"}, "inattendu"),
    ]
    for champs, motif in cas:
        with pytest.raises(coffre.ErreurCoffre, match=motif) as exc:
            coffre.ecrire("libre/carte", champs, meta=meta)
        for v in champs.values():
            assert v not in str(exc.value)
    assert coffre.lister() == []
    # Carte physique (virtuelle absente) : refusée.
    with pytest.raises(coffre.ErreurCoffre, match="virtuelle"):
        coffre.ecrire("libre/carte", {"numero": CARTE}, meta={**meta, "virtuelle": None})
    coffre.ecrire("libre/carte", {"titulaire": "Alice Test", "numero": CARTE_ESPACES, "expiration": "7/2028",
                                  "cvc": CVC}, meta=meta)
    infos = coffre.meta("libre/carte")
    assert infos["fin"] == "7893" and infos["virtuelle"] is True and infos["type"] == "carte_bancaire"
    assert infos["champs"] == ["cvc", "expiration", "numero", "titulaire"]
    assert coffre.lire("libre/carte")["numero"] == CARTE and coffre.lire("libre/carte")["expiration"] == "07/28"
    _sans_valeur(coffre.decrire(), infos)
    # Le numéro est masqué aussi dans sa forme d'affichage (groupes de 4).
    assert coffre.masquer(f"payé avec {CARTE_ESPACES}") == "payé avec •••"


def test_migration_entree_sans_metadonnees(env: dict[str, Path]) -> None:
    from tools import coffre

    contenu = {"secrets": {
        "planity/alice": {"champs": {"identifiant": IDENT, "mot_de_passe": MDP}, "maj": "2026-10-01T10:00:00+02:00",
                          "par": "admin"},
        "cantine": {"champs": {"mot_de_passe": MDP}, "maj": "2026-10-01T10:00:00+02:00", "par": "admin"},
    }}
    (env["data"] / "coffre.json").write_text(json.dumps(coffre.chiffrer(contenu, CLE_COFFRE)), encoding="utf-8")
    lignes = {m["cle"]: m for m in coffre.decrire()}
    assert lignes["planity/alice"]["type"] == "identifiants" and lignes["planity/alice"]["proprietaire"] == "alice"
    assert lignes["cantine"]["proprietaire"] == "foyer" and lignes["cantine"]["utilise_par"] == ["cantine"]
    assert lignes["cantine"]["libelle"] == "Cantine"
    # Réécrit au format enrichi à la prochaine écriture, valeurs intactes.
    coffre.preparer_entree("libre/netflix", {"libelle": "Netflix", "proprietaire": "alice"})
    brut = coffre.dechiffrer(json.loads((env["data"] / "coffre.json").read_text()), CLE_COFFRE)
    assert brut["secrets"]["planity/alice"]["type"] == "identifiants"
    assert brut["secrets"]["planity/alice"]["champs"]["mot_de_passe"] == MDP
    assert brut["secrets"]["libre/netflix"]["champs"] == {}


def test_preparer_deplacer_et_changement_de_type(env: dict[str, Path]) -> None:
    from tools import coffre

    infos = coffre.preparer_entree("libre/ecole", {"type": "code", "libelle": "Portail école", "proprietaire": "foyer"})
    assert infos["champs"] == [] and infos["type"] == "code"
    with pytest.raises(coffre.SecretManquant):
        coffre.lire("libre/ecole")
    coffre.ecrire("libre/ecole", {"code": "1234"})
    assert coffre.meta("libre/ecole")["type"] == "code"
    with pytest.raises(coffre.ErreurCoffre, match="supprime-la"):
        coffre.preparer_entree("libre/ecole", {"type": "cle_api"})
    deplacee = coffre.deplacer("libre/ecole", "ecole", {"utilise_par": ["ecole"]})
    assert deplacee["cle"] == "ecole" and deplacee["utilise_par"] == ["ecole"]
    assert coffre.lire("ecole") == {"code": "1234"} and coffre.meta("libre/ecole") is None
    coffre.preparer_entree("libre/autre", {"libelle": "Autre"})
    coffre.ecrire("libre/autre", {"identifiant": "x@y.fr", "mot_de_passe": "abcd1234"})
    with pytest.raises(coffre.ErreurCoffre, match="existe déjà"):
        coffre.deplacer("libre/autre", "ecole")


def test_cli_set_typee_ls_purger(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    from tools import coffre

    saisies = iter(["Alice Test", "Alice Test", CARTE, CARTE, "07/28", "07/28", CVC, CVC])
    invites: list[str] = []

    def faux(invite: str = "") -> str:
        invites.append(invite)
        return next(saisies)

    monkeypatch.setattr(coffre, "_lire_valeur", faux)
    # Sans --virtuelle : refus, rien n'est demandé.
    assert coffre.main(["set", "libre/carte-cantine", "--type", "carte"]) == 1
    assert invites == [] and "virtuelle" in capsys.readouterr().err
    assert coffre.main(["set", "libre/carte-cantine", "--type", "carte", "--virtuelle", "--libelle",
                        "Carte de la cantine", "--proprietaire", "foyer"]) == 0
    assert [i.split(" ")[0] for i in invites[::2]] == ["titulaire", "numero", "expiration", "cvc"]
    assert coffre.main(["ls"]) == 0
    assert coffre.main(["purger"]) == 0
    sortie = capsys.readouterr()
    _sans_valeur(sortie.out, sortie.err)
    assert "carte bancaire, « Carte de la cantine », à foyer, ••• 7893 (virtuelle)" in sortie.out
    assert "Quarantaines expirées effacées : 0" in sortie.out
    # Une entrée préparée en conversation : la CLI demande les champs de son type (url facultative).
    coffre.preparer_entree("libre/netflix", {"type": "identifiants", "libelle": "Netflix"})
    saisies2 = iter([IDENT, IDENT, MDP, MDP, ""])
    monkeypatch.setattr(coffre, "_lire_valeur", lambda invite="": next(saisies2))
    assert coffre.main(["set", "libre/netflix"]) == 0
    assert coffre.lire("libre/netflix") == {"identifiant": IDENT, "mot_de_passe": MDP}
    _sans_valeur(capsys.readouterr().out)


def test_quarantaine_ttl_et_proprietaire(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import coffre

    ident = coffre.mettre_en_quarantaine(CARTE, "alice", "carte_bancaire", "••7893")
    assert coffre.RE_ID_QUARANTAINE.match(ident)
    assert coffre.quarantaine(ident)["personne"] == "alice"
    assert CARTE in coffre.valeurs_connues()  # masquée dans les sorties d'outils pendant sa vie
    assert all(not m["cle"].startswith("quarantaine/") for m in coffre.decrire())
    coffre.preparer_entree("libre/carte", {"type": "carte_bancaire", "libelle": "Carte", "virtuelle": True})
    with pytest.raises(coffre.ErreurCoffre, match="autre personne"):
        coffre.ranger_quarantaine(ident, "libre/carte", "numero", "bruno")
    infos = coffre.ranger_quarantaine(ident, "libre/carte", "numero", "alice")
    assert infos["fin"] == "7893" and coffre.lire("libre/carte")["numero"] == CARTE
    with pytest.raises(coffre.ErreurCoffre, match="plus en quarantaine"):
        coffre.quarantaine(ident)
    # Expiration : 10 minutes, puis purge.
    ident2 = coffre.mettre_en_quarantaine(MDP, "alice", "mot_de_passe")
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T12:11:00")
    with pytest.raises(coffre.ErreurCoffre, match="plus en quarantaine"):
        coffre.quarantaine(ident2)
    assert coffre.purger_quarantaine() == 1
    assert not [c for c in coffre.lister() if c.startswith("quarantaine/")]
    assert MDP not in coffre.valeurs_connues()


# =====================================================================================================
# Capacités : secrets typés, carte bancaire réservée aux actions validées
# =====================================================================================================


def test_manifeste_secret_type(env: dict[str, Path]) -> None:
    from tools import capacites

    cap = capacites.lire_capacite(env["caps"] / "cantine")
    assert cap.valide and cap.manifeste.secrets[0].type == "carte_bancaire"
    assert cap.manifeste.secrets[0].champs == ("titulaire", "numero", "expiration", "cvc")
    m, erreurs, _ = capacites.valider_manifeste({
        "nom": "x", "titre": "X", "description": "x", "statut": "actif", "origine": "humain",
        "secrets": [{"cle": "x", "type": "carte_bancaire"}], "actions_avec_effet": ["x_payer"]})
    assert m is None and any("confirmation_toujours" in e for e in erreurs)
    m, erreurs, _ = capacites.valider_manifeste({
        "nom": "x", "titre": "X", "description": "x", "statut": "actif", "origine": "humain",
        "secrets": [{"cle": "x", "type": "carte_bancaire", "champs": ["numero", "pin"]}],
        "actions_avec_effet": ["x_payer"], "confirmation_toujours": ["x_payer"]})
    assert m is None and any("pin" in e for e in erreurs)
    _m, _e, avert = capacites.valider_manifeste({
        "nom": "x", "titre": "X", "description": "x", "statut": "actif", "origine": "humain",
        "secrets": [{"cle": "x", "champs": ["identifiant"]}]})
    assert any("type" in a for a in avert)
    assert capacites.statut_detaille("cantine")["secrets"][0]["type"] == "carte_bancaire"


def test_carte_lue_seulement_pendant_une_action_validee(env: dict[str, Path],
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    from tools import capacites, coffre

    coffre.ecrire("cantine", {"titulaire": "Alice Test", "numero": CARTE, "expiration": "07/28", "cvc": CVC},
                  meta={"type": "carte_bancaire", "libelle": "Carte cantine", "proprietaire": "foyer",
                        "virtuelle": True})
    mcp = capacites.MCPFactice()
    capacites.charger_capacite(capacites.lire_capacite(env["caps"] / "cantine"), mcp, env["data"])
    # Appelé par le modèle (aucune action validée) : refus, aucune valeur.
    with pytest.raises(ValueError, match="paiement validé") as exc:
        mcp.outils["cantine_lire_carte"]()
    _sans_valeur(str(exc.value))
    texte = mcp.outils["cantine_payer"](montant="12")
    code, sortie = _executer(_id(texte), capsys)
    assert code == 0 and "payé 12 € avec la carte •••" in sortie
    _sans_valeur(sortie, _tout_le_journal(env["data"]))
    # Carte non marquée virtuelle (coffre écrit à la main) : refusée même pendant l'action.
    brut = coffre.dechiffrer(json.loads((env["data"] / "coffre.json").read_text()), CLE_COFFRE)
    brut["secrets"]["cantine"]["virtuelle"] = False
    (env["data"] / "coffre.json").write_text(json.dumps(coffre.chiffrer(brut, CLE_COFFRE)), encoding="utf-8")
    code, sortie = _executer(_id(mcp.outils["cantine_payer"](montant="5")), capsys)
    assert code == 1 and "virtuelle" in sortie
    _sans_valeur(sortie)


# =====================================================================================================
# Outils identifiants_*
# =====================================================================================================


def test_preparer_procedure_et_carte_web(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import coffre, server

    texte = server.identifiants_preparer(libelle="Cantine de l'école", type="identifiants", proprietaire="foyer")
    assert "sudo -u homeos homeos-coffre set libre/cantine-de-l-ecole" in texte and "CARTE:" not in texte
    assert coffre.meta("libre/cantine-de-l-ecole")["proprietaire"] == "foyer"
    # Carte : la question « virtuelle à plafond ? » d'abord, rien n'est créé.
    question = server.identifiants_preparer(libelle="Carte cantine", type="carte bancaire")
    assert "virtuelle à plafond" in question and coffre.meta("libre/carte-cantine") is None
    # Canal web : ligne CARTE: conforme au contrat (formulaire soumis à /api/coffre sous step-up).
    monkeypatch.setenv("HOMEOS_CANAL", "web")
    texte = server.identifiants_preparer(libelle="Carte cantine", type="carte_bancaire", virtuelle=True)
    ligne = next(lg for lg in texte.splitlines() if lg.startswith("CARTE:"))
    carte = json.loads(ligne.removeprefix("CARTE:"))
    assert carte["type"] == "identifiants" and carte["titre"] == "Carte cantine"
    assert carte["donnees"] == {"cle": "libre/carte-cantine", "type": "carte_bancaire",
                                "champs": ["titulaire", "numero", "expiration", "cvc"], "facultatifs": [],
                                "libelle": "Carte cantine", "virtuelle": True}
    assert carte["actions"] == [{"libelle": "Enregistrer", "action": "coffre_ecrire",
                                 "args": {"cle": "libre/carte-cantine"}, "confirmation": "step-up"}]
    # Pour une capacité : clé déclarée (personnelle ici).
    texte = server.identifiants_preparer(libelle="Planity", type="identifiants", capacite="planity")
    assert "homeos-coffre set planity/alice" in texte
    assert coffre.meta("planity/alice")["utilise_par"] == ["planity"]


@pytest.mark.parametrize("argument", [
    MDP, "Soleil2024", CARTE_ESPACES, "mot de passe : Azerty123!", "sk-ant-api03-abcdefghijklmnopqrstuv",
])
def test_valeur_collee_refusee_jamais_journalisee(env: dict[str, Path], argument: str) -> None:
    from tools import server

    with pytest.raises(ValueError) as exc:
        server.identifiants_preparer(libelle=argument, type="identifiants")
    message = str(exc.value)
    assert "Je ne l'enregistre pas" in message and "change-le" in message and argument not in message
    journal = _tout_le_journal(env["data"])
    assert argument not in journal and "valeur refusée, non journalisée" in journal
    with pytest.raises(ValueError) as exc:
        server.identifiants_renommer(cle="libre/x", libelle=argument)
    assert argument not in str(exc.value) and argument not in _tout_le_journal(env["data"])


def test_valeur_deja_dans_le_coffre_refusee(env: dict[str, Path]) -> None:
    from tools import coffre, server

    coffre.ecrire("planity/alice", {"identifiant": IDENT, "mot_de_passe": "tresSECRET"})
    with pytest.raises(ValueError, match="Je ne l'enregistre pas") as exc:
        server.identifiants_preparer(libelle="tresSECRET", type="code")
    assert "tresSECRET" not in str(exc.value) and "tresSECRET" not in _tout_le_journal(env["data"])
    with pytest.raises(ValueError, match="ni identifiant ni adresse"):
        server.identifiants_preparer(libelle="Compte bob@example.org", type="identifiants")
    assert "bob@example.org" not in _tout_le_journal(env["data"])


def test_lister_droits_et_aucune_valeur(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import coffre, server

    coffre.ecrire("planity/alice", {"identifiant": IDENT, "mot_de_passe": MDP})
    coffre.ecrire("libre/perso-bruno", {"code": "98765"},
                  meta={"type": "code", "libelle": "Casier", "proprietaire": "bruno"})
    coffre.ecrire("libre/carte", {"numero": CARTE}, meta={"type": "carte_bancaire", "libelle": "Carte cantine",
                                                          "proprietaire": "foyer", "virtuelle": True})
    vue = server.identifiants_lister()
    _sans_valeur(vue, _tout_le_journal(env["data"]))
    par_cle = {e["cle"]: e for e in vue["identifiants"]}
    assert set(par_cle) == {"planity/alice", "libre/perso-bruno", "libre/carte"}  # propriétaire du foyer : tout
    assert par_cle["libre/carte"]["carte"] == "••• 7893"
    assert par_cle["libre/carte"]["champs_manquants"] == ["titulaire", "expiration", "cvc"]
    assert par_cle["planity/alice"]["proprietaire"] == "moi"
    assert par_cle["planity/alice"]["utilise_par"] == ["Planity"]
    # Capacité active dont le secret manque : la carte de la cantine.
    manquants = {a["cle"]: a for a in vue["a_renseigner"]}
    assert manquants["cantine"]["champs_manquants"] == ["titulaire", "numero", "expiration", "cvc"]
    assert "planity/alice" not in manquants
    # Bruno (membre) : les siens et ceux du foyer seulement.
    monkeypatch.setenv("HOMEOS_PERSONNE", "bruno")
    vue_b = server.identifiants_lister()
    assert {e["cle"] for e in vue_b["identifiants"]} == {"libre/perso-bruno", "libre/carte"}
    assert {a["cle"] for a in vue_b["a_renseigner"]} == {"planity/bruno", "cantine"}
    # Personne inconnue (jeton homeos, crons) : refus.
    monkeypatch.setenv("HOMEOS_PERSONNE", "homeos")
    with pytest.raises(ValueError, match="qui me parle"):
        server.identifiants_lister()


def test_renommer_attribuer_supprimer_par_bouton(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    from tools import coffre, server

    coffre.ecrire("libre/carte", {"titulaire": "Alice Test", "numero": CARTE, "expiration": "07/28", "cvc": CVC},
                  meta={"type": "carte_bancaire", "libelle": "Ma carte", "proprietaire": "alice", "virtuelle": True})
    # Renommer : confirmation, puis exécution par le runner.
    code, sortie = _executer(_id(server.identifiants_renommer(cle="libre/carte", libelle="Carte cantine")), capsys)
    assert code == 0 and "« Carte cantine »" in sortie and coffre.meta("libre/carte")["libelle"] == "Carte cantine"
    # Attribuer : le type doit correspondre à un secret déclaré.
    with pytest.raises(ValueError, match="attend"):
        server.identifiants_attribuer(cle="libre/carte", capacite="planity")
    code, sortie = _executer(_id(server.identifiants_attribuer(cle="libre/carte", capacite="cantine")), capsys)
    assert code == 0 and "sert maintenant à « Cantine »" in sortie
    assert coffre.meta("cantine")["utilise_par"] == ["cantine"] and coffre.meta("libre/carte") is None
    # Droits : Bruno (membre) ne peut ni renommer ni supprimer une entrée d'Alice.
    monkeypatch.setenv("HOMEOS_PERSONNE", "bruno")
    with pytest.raises(ValueError, match="Je ne trouve pas|Seul le propriétaire"):
        server.identifiants_supprimer(cle="cantine")  # entrée d'Alice : invisible pour Bruno
    monkeypatch.setenv("HOMEOS_PERSONNE", "alice")
    texte = server.identifiants_supprimer(cle="cantine")
    assert "••• 7893" in texte
    code, sortie = _executer(_id(texte), capsys)
    assert code == 0 and coffre.meta("cantine") is None
    _sans_valeur(sortie, _tout_le_journal(env["data"]))
    with sqlite3.connect(env["data"] / "homeos.sqlite") as c:
        qui = {r[0] for r in c.execute("SELECT who FROM actions_log WHERE kind LIKE 'identifiants_%' AND status != 'error'")}
    assert "Alice" in qui


def test_attribuer_controle_les_champs(env: dict[str, Path]) -> None:
    from tools import coffre, server

    coffre.preparer_entree("libre/planity", {"type": "identifiants", "libelle": "Planity", "proprietaire": "alice"})
    coffre.ecrire("libre/planity", {"identifiant": IDENT}, fusion=True)
    with pytest.raises(ValueError, match="il manque mot_de_passe"):
        server.identifiants_attribuer(cle="libre/planity", capacite="planity")


def test_enregistrer_saisie_web(env: dict[str, Path]) -> None:
    from tools import coffre, identifiants

    coffre.preparer_entree("libre/carte", {"type": "carte_bancaire", "libelle": "Carte", "proprietaire": "alice",
                                           "virtuelle": True})
    with pytest.raises(identifiants.ErreurIdentifiants, match="Seul le propriétaire"):
        identifiants.enregistrer_saisie("libre/carte", {"numero": CARTE}, "bruno")
    with pytest.raises(coffre.ErreurCoffre, match="Luhn"):
        identifiants.enregistrer_saisie("libre/carte", {"numero": "4970101234567890"}, "alice")
    assert identifiants.enregistrer_saisie("libre/carte", {"numero": CARTE, "cvc": CVC}, "alice") == {
        "ok": True, "champs": ["cvc", "numero"]}
    _sans_valeur(_tout_le_journal(env["data"]))


# =====================================================================================================
# Filtre de données sensibles
# =====================================================================================================


@pytest.mark.parametrize(("texte", "jeton"), [
    (f"voilà la carte de la cantine {CARTE_ESPACES}", "[carte bancaire ••7893]"),
    ("4111-1111-1111-1111", "[carte bancaire ••1111]"),
    (f"mon IBAN {IBAN}", "[IBAN ••0189]"),
    ("IBAN FR76 3000 6000 0112 3456 7890 189", "[IBAN ••0189]"),
    (f"sécu {_nir()}", "[numéro de sécurité sociale]"),
    ("mot de passe : Soleil2024!", "[mot de passe masqué]"),
    ("mon mdp c'est azerty123", "[mot de passe masqué]"),
    ("password=correcthorse", "[mot de passe masqué]"),
    ("le code du portail est 1234", "[mot de passe masqué]"),
    ("code pin : 1234", "[mot de passe masqué]"),
    (f"identifiant {IDENT} / Bonjour2024", "[mot de passe masqué]"),
    ("ma clé sk-ant-api03-abcdefghijklmnopqrstuv", "[clé masquée]"),
    ("AKIAIOSFODNN7EXAMPLE", "[clé masquée]"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[clé masquée]"),
    ("jeton Xy7#kLm9_Pq2Rs8tVw3Za1", "[clé masquée]"),
])
def test_filtre_detecte(texte: str, jeton: str) -> None:
    from coeur import filtre_sensible

    resultat = filtre_sensible.filtrer(texte)
    assert jeton in resultat.texte and resultat.detections
    for d in resultat.detections:
        assert d.valeur not in resultat.texte and d.valeur not in repr(d)


@pytest.mark.parametrize("texte", [
    "appelle-moi au 06 12 34 56 78", "+33 6 12 34 56 78", "rendez-vous le 12/03/2026 à 14h30",
    "ça coûte 1 234,56 €", "virement de 12 500 €", "code postal 47300", "47300 Villeneuve", "commande n° 123456789",
    "le code promo est NOEL2024", "commit 3f2a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a",
    "https://exemple.fr/chemin/Abc123Def456Ghi789Jkl", "mon mot de passe est oublié",
    "j'ai changé le mot de passe hier", "uuid 123e4567-e89b-12d3-a456-426614174000",
    "numéro de suivi 1Z999AA10123456784", "siret 73282932000074", "le code est simple",
    "4970101234567890", "réf 2026-11-12-ABCD", "Super !!! Vraiment-très-bien-merci-beaucoup",
    "[fichier reçu: /srv/home-os/data/fichiers/entrants/2026-11-07-facture.pdf]",
    "[contexte: Alice écrit en privé, 07/11/2026 18:32]", "le vol AF1234 part à 7h", "ISBN 978-2-07-036822-8",
])
def test_filtre_faux_positifs(texte: str) -> None:
    from coeur import filtre_sensible

    assert filtre_sensible.filtrer(texte).texte == texte


def test_filtre_plusieurs_et_idempotence() -> None:
    from coeur import filtre_sensible

    (t, p), dets = filtre_sensible.filtrer_plusieurs(f"carte {CARTE}, mdp : {MDP}",
                                                     f"[contexte: x]\ncarte {CARTE}, mdp : {MDP}")
    assert len(dets) == 2 and CARTE not in t + p and MDP not in t + p and t in p
    assert filtre_sensible.masquer(t) == t and filtre_sensible.masquer(p) == p  # idempotent


# =====================================================================================================
# Cœur : masquage avant le modèle, quarantaine, historique, journal
# =====================================================================================================


class _FauxRun:
    def __init__(self) -> None:
        self.result: Any = None

    async def evenements(self) -> Any:
        from coeur.claude import ClaudeResult, EvenementClaude

        yield EvenementClaude("texte", texte="Je n'ai pas pu la lire : elle a été masquée.")
        self.result = ClaudeResult(text="Je n'ai pas pu la lire : elle a été masquée.", session_id="s1",
                                   cout_usd=0.0, duree_ms=1)


class _FauxRunner:
    def __init__(self, journal: list[str]) -> None:
        self.appels: list[dict[str, Any]] = []
        self.journal = journal

    def run(self, linux_user: str, prenom: str, prompt: str, session_id: str | None, **kw: Any) -> _FauxRun:
        self.journal.append("claude")
        self.appels.append({"prompt": prompt, **kw})
        return _FauxRun()


@pytest.fixture()
def coeur_env(env: dict[str, Path]) -> Any:
    from bot.config import parse_config
    from bot.db import Db
    from bot.sessions import Sessions
    from coeur.conversation import Coeur
    from coeur.historique import Historique

    cfg = parse_config(yaml.safe_load((env["data"] / "config.yaml").read_text()), env["data"], RACINE)
    ordre: list[str] = []

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cfg = cfg
    ctx.db = Db(cfg.db_path)
    ctx.db.init()
    ctx.sessions = Sessions(cfg.sessions_path)
    ctx.runner = _FauxRunner(ordre)

    async def classer(texte: str) -> str:
        ordre.append("classifieur")
        assert CARTE not in texte and CARTE_ESPACES not in texte
        return "standard"

    coeur = Coeur(ctx, Historique(cfg.etat_dir / "appli.sqlite"), classifieur=classer)
    return {"coeur": coeur, "ctx": ctx, "cfg": cfg, "ordre": ordre, **env}


def _echanger(coeur: Any, personne: Any, texte: str, ordre: list[str], **kw: Any) -> list[Any]:
    async def go() -> list[Any]:
        evs = []
        async for ev in coeur.echanger(personne, texte, **kw):
            ordre.append(ev.type)
            evs.append(ev)
        return evs

    return asyncio.run(go())


def test_coeur_masque_avant_le_modele(coeur_env: dict[str, Any]) -> None:
    from coeur.claude import construire_commande

    alice = coeur_env["cfg"].personnes[0]
    message = f"voilà la carte de la cantine {CARTE_ESPACES}, et le mdp : {MDP}"
    evs = _echanger(coeur_env["coeur"], alice, message, coeur_env["ordre"], canal="web")
    ordre = coeur_env["ordre"]
    assert ordre.index("masquage") < ordre.index("claude") and ordre.index("masquage") < ordre.index("classifieur")
    masquage = next(e for e in evs if e.type == "masquage")
    attendu = "voilà la carte de la cantine [carte bancaire ••7893], et le mdp : [mot de passe masqué]"
    assert masquage.donnees["texte"] == attendu
    assert masquage.donnees["types"] == ["carte_bancaire", "mot_de_passe"]
    ids = [d["quarantaine"] for d in masquage.donnees["detections"]]
    assert all(ids)
    prompt = coeur_env["ctx"].runner.appels[0]["prompt"]
    assert attendu in prompt and f"[donnée sensible détectée : carte bancaire ••7893, quarantaine {ids[0]}]" in prompt
    assert "vue par Telegram" not in prompt
    # La commande `claude -p` reçoit exactement cette chaîne, sans aucune valeur.
    cmd = construire_commande(coeur_env["cfg"], "alice", prompt, None)
    assert cmd[cmd.index("-p") + 1] == prompt
    _sans_valeur(prompt, [e.donnees for e in evs], _tout_le_journal(coeur_env["data"]))
    # Historique : version masquée (ce que la personne relit = ce que l'assistant a reçu).
    hist = coeur_env["coeur"].historique
    messages = hist.messages(hist.courante("alice")["id"])
    assert messages[0]["texte"] == attendu
    with sqlite3.connect(coeur_env["cfg"].etat_dir / "appli.sqlite") as c:
        _sans_valeur(json.dumps([list(r) for r in c.execute("SELECT * FROM messages")]))
        _sans_valeur(json.dumps([list(r) for r in c.execute("SELECT * FROM conversations")]))
    journal = _tout_le_journal(coeur_env["data"])
    assert "donnee_sensible" in journal and "carte_bancaire" in journal


def test_coeur_quarantaine_puis_rangement(coeur_env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    from tools import coffre, server

    alice = coeur_env["cfg"].personnes[0]
    evs = _echanger(coeur_env["coeur"], alice, f"carte cantine {CARTE}", coeur_env["ordre"], canal="telegram")
    masquage = next(e for e in evs if e.type == "masquage")
    ident = masquage.donnees["detections"][0]["quarantaine"]
    assert "vue par Telegram" in coeur_env["ctx"].runner.appels[0]["prompt"]
    # L'assistant prépare la carte (virtuelle confirmée) puis demande le rangement : bouton, valeur jamais vue.
    server.identifiants_preparer(libelle="Carte cantine", type="carte_bancaire", proprietaire="foyer", virtuelle=True)
    texte = server.identifiants_ranger_quarantaine(id=ident, cle="libre/carte-cantine")
    assert "carte bancaire ••7893" in texte and "champ numero" in texte
    code, sortie = _executer(_id(texte), capsys)
    assert code == 0 and "••• 7893" in sortie
    assert coffre.lire("libre/carte-cantine")["numero"] == CARTE
    _sans_valeur(sortie, _tout_le_journal(coeur_env["data"]))
    # Une autre personne ne peut pas ranger la quarantaine d'Alice.
    evs = _echanger(coeur_env["coeur"], alice, f"mot de passe : {MDP}", coeur_env["ordre"], canal="web")
    ident2 = next(e for e in evs if e.type == "masquage").donnees["detections"][0]["quarantaine"]
    import os

    os.environ["HOMEOS_PERSONNE"] = "bruno"
    try:
        with pytest.raises(ValueError, match="autre personne"):
            server.identifiants_ranger_quarantaine(id=ident2, cle="libre/carte-cantine")
    finally:
        os.environ["HOMEOS_PERSONNE"] = "alice"


def test_coeur_sans_coffre_masque_quand_meme(coeur_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COFFRE_CLE")
    alice = coeur_env["cfg"].personnes[0]
    evs = _echanger(coeur_env["coeur"], alice, f"IBAN {IBAN}", coeur_env["ordre"], canal="web")
    masquage = next(e for e in evs if e.type == "masquage")
    assert masquage.donnees["texte"] == "IBAN [IBAN ••0189]" and masquage.donnees["detections"][0]["quarantaine"] is None
    assert "non conservée" in coeur_env["ctx"].runner.appels[0]["prompt"]
    assert IBAN not in coeur_env["ctx"].runner.appels[0]["prompt"]


def test_contexte_recent_masque_les_anciens_messages() -> None:
    from coeur.conversation import bloc_contexte_recent

    bloc = bloc_contexte_recent([{"role": "personne", "auteur": "Alice", "texte": f"ma carte {CARTE}"}], "Firmin")
    assert CARTE not in bloc and "[carte bancaire ••7893]" in bloc


def test_confirmations_identifiants(env: dict[str, Path]) -> None:
    from bot.config import parse_config
    from coeur import confirmations

    cfg = parse_config(yaml.safe_load((env["data"] / "config.yaml").read_text()), env["data"], RACINE)
    alice, bruno = cfg.personnes
    action = {"kind": "identifiants_supprimer", "payload": json.dumps({"demandeur": "bruno"})}
    assert confirmations.kind_sensible(cfg, "identifiants_supprimer")
    assert confirmations.peut_valider_identifiants(action, bruno)
    assert confirmations.peut_valider_identifiants(action, alice)  # propriétaire du foyer
    action_a = {"kind": "identifiants_supprimer", "payload": json.dumps({"demandeur": "alice"})}
    assert not confirmations.peut_valider_identifiants(action_a, bruno)
    quarantaine = {"kind": "identifiants_ranger_quarantaine", "payload": json.dumps({"demandeur": "bruno"})}
    assert not confirmations.peut_valider_identifiants(quarantaine, alice)  # la personne qui l'a envoyée seule


def test_bot_ligne_transmis() -> None:
    from bot.handlers import texte_masquage

    assert texte_masquage("Firmin", "voilà [carte bancaire ••1234]") == "🔒 Transmis à Firmin : voilà [carte bancaire ••1234]"
