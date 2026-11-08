"""Migration douce bus → transports : activation, réglages et trajets d'un foyer déjà installé repris automatiquement."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from capabilities.transports import modele
from capabilities.transports import tools as outils
from tools import capacites

RACINE = Path(__file__).resolve().parents[2]  # capabilities/

ANCIENS_TRAJETS = """\
# Trajets habituels en bus, par personne (slug du prénom).
alice:
  - ligne: "7"
    sens: grandport
    sens_libelle: Vers Grandport
    arret: pont-du-moulin-villeneuve
    arret_nom: Pont du Moulin
    commune: Villeneuve
    heure: "16:25"
    jours: [sam, dim]
    seuil_alerte_min: 8
bruno:
  ligne: 7
  sens: villeneuve
  arret: pont-du-moulin-villeneuve
  heure: 07:40
  jours: lun-ven
"""


def _ancien_foyer(donnees: Path, statut: str | None = "actif") -> None:
    bus = donnees / "capacites" / "bus"
    (bus / "gtfs").mkdir(parents=True)
    (bus / "trajets.yaml").write_text(ANCIENS_TRAJETS, encoding="utf-8")
    (bus / "gtfs" / "reseau.json").write_text('{"genere": "2026-11-02T04:00:00", "lignes": [1]}', encoding="utf-8")
    (bus / "alertes.json").write_text('{"jour": "2026-11-07", "remises": []}', encoding="utf-8")
    if statut:
        (donnees / "capacites" / "etat.json").write_text(json.dumps({
            "bus": {"statut": statut, "smoke": {"ok": True}}, "kitchen_daily": {"statut": "actif"}}), encoding="utf-8")


def test_trajets_grille_et_reseau_repris(tmp_path: Path) -> None:
    donnees = tmp_path / "data"
    _ancien_foyer(donnees)
    trajets = modele.lire(donnees)  # la migration se fait au premier accès
    (a,) = trajets["alice"]
    assert {k: a[k] for k in ("mode", "fournisseur", "origine", "destination", "ligne", "heure", "jours",
                               "seuil_alerte_min")} == {
        "mode": "bus", "fournisseur": "cartreize", "origine": "Pont du Moulin (Villeneuve)", "destination": "Grandport",
        "ligne": "7", "heure": "16:25", "jours": ["sam", "dim"], "seuil_alerte_min": 8}
    assert a["details"]["sens"] == "grandport" and a["details"]["arret"] == "pont-du-moulin-villeneuve"
    (b,) = trajets["bruno"]  # édité à la main : heure non quotée, un seul trajet en dictionnaire
    assert (b["heure"], b["jours"], b["seuil_alerte_min"], b["destination"]) == ("07:40", ["lun", "mar", "mer", "jeu", "ven"], 5, "villeneuve")
    nouveau = donnees / "capacites" / "transports"
    assert (nouveau / "cartreize" / "gtfs" / "reseau.json").is_file()  # grille déplacée, pas re-téléchargée
    assert yaml.safe_load((nouveau / "parametres.yaml").read_text(encoding="utf-8"))["fournisseurs"] == "cartreize"
    assert (donnees / "capacites" / "bus" / "trajets.yaml").is_file()  # l'ancien dossier est conservé
    assert json.loads((nouveau / modele.MARQUEUR_MIGRATION).read_text(encoding="utf-8"))["trajets"] == 2


def test_migration_idempotente_et_sans_ecraser(tmp_path: Path) -> None:
    donnees = tmp_path / "data"
    _ancien_foyer(donnees)
    assert modele.migrer_bus(donnees) is not None
    ids = [t["id"] for t in modele.lire(donnees)["alice"]]
    modele.ecrire(donnees, {"alice": []})  # la personne a tout supprimé depuis
    assert modele.migrer_bus(donnees) is None
    assert modele.lire(donnees) == {} and ids


def test_rien_a_migrer(tmp_path: Path) -> None:
    assert modele.migrer_bus(tmp_path) is None
    assert modele.lire(tmp_path) == {}


def test_parametre_existant_complete(tmp_path: Path) -> None:
    donnees = tmp_path / "data"
    _ancien_foyer(donnees)
    d = donnees / "capacites" / "transports"
    d.mkdir(parents=True)
    (d / "parametres.yaml").write_text("fournisseurs: tcl_lyon\nseuil_retard_min: 7\n", encoding="utf-8")
    modele.migrer_bus(donnees)
    valeurs = yaml.safe_load((d / "parametres.yaml").read_text(encoding="utf-8"))
    assert valeurs == {"fournisseurs": "tcl_lyon, cartreize", "seuil_retard_min": 7}


# --- cadre des capacités : `remplace: [bus]` -------------------------------------------------------------------------------


@pytest.fixture()
def cap() -> capacites.Capacite:
    c = capacites.trouver("transports", RACINE)
    assert c.valide and c.manifeste is not None and c.manifeste.remplace == ["bus"]
    return c


@pytest.mark.parametrize(("statut_bus", "attendu"), [("actif", "actif"), ("inactif", "inactif"), (None, "actif")])
def test_statut_repris_de_bus(tmp_path: Path, cap: capacites.Capacite, statut_bus: str | None, attendu: str) -> None:
    _ancien_foyer(tmp_path, statut_bus)
    assert capacites.statut_effectif(cap, capacites.lire_etat(tmp_path)) == attendu


def test_migrer_remplacees(tmp_path: Path, cap: capacites.Capacite) -> None:
    _ancien_foyer(tmp_path, "inactif")
    assert capacites.migrer_remplacees(RACINE, tmp_path) == ["bus → transports"]
    etat = capacites.lire_etat(tmp_path)
    assert "bus" not in etat and etat["transports"] == {"statut": "inactif", "smoke": {"ok": True}}
    assert etat["kitchen_daily"] == {"statut": "actif"}
    assert capacites.migrer_remplacees(RACINE, tmp_path) == []  # idempotent
    assert capacites.statut_effectif(cap, etat) == "inactif"


def test_ancien_nom_reconnu(cap: capacites.Capacite) -> None:
    assert capacites.trouver("bus", RACINE).nom == "transports"
    from tools import administration

    assert administration.trouver_capacite("bus", RACINE).nom == "transports"


def test_reglages_config_repris(cap: capacites.Capacite) -> None:
    alertes = next(c for c in cap.manifeste.crons if c.nom == "alertes_retard")  # type: ignore[union-attr]
    config: dict[str, Any] = {"capacites": {"bus": {"crons": {"alertes_retard": "07:00..09:00/10"}}}}
    assert capacites.quand_cron(cap, alertes, config) == "07:00..09:00/10"
    config["capacites"]["transports"] = {"crons": {}}
    assert capacites.quand_cron(cap, alertes, config) == alertes.quand


def test_remplace_invalide() -> None:
    brut = yaml.safe_load((RACINE / "transports" / "capability.yaml").read_text(encoding="utf-8"))
    _m, erreurs, _a = capacites.valider_manifeste({**brut, "remplace": ["transports"]}, "transports")
    assert any("elle-même" in e for e in erreurs)
    _m, erreurs, _a = capacites.valider_manifeste({**brut, "remplace": "bus"}, "transports")
    assert any("`remplace`" in e for e in erreurs)


def test_alertes_apres_migration(tmp_path: Path, cartreize: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le trajet repris de l'ancienne capacité bus alerte comme avant (fournisseur Cartreize inchangé)."""
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T16:10:00")
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(RACINE))
    donnees = tmp_path / "data"
    _ancien_foyer(donnees)
    ctx = SimpleNamespace(donnees=donnees, personne="alice", config={"bus": {"url": cartreize.url}})  # ancien réglage
    (t,) = outils.prochains(ctx)["trajets"]
    assert t["alerte"] is None  # seuil de 8 min repris : retard de 7 min, pas d'alerte
    trajets = modele.lire(donnees)
    trajets["alice"][0]["seuil_alerte_min"] = 5
    modele.ecrire(donnees, trajets)
    (t,) = outils.prochains(ctx)["trajets"]
    assert t["alerte"] == "retard 7 min sur le départ de 16:25"
