"""Tests de la capacité « Salon démo » (sans réseau ni navigateur)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import capacites

DOSSIER = Path(__file__).resolve().parent.parent

HTML_CRENEAUX = """<ul id="liste"><li class="creneau" data-id="c1"><span class="quand">mardi 10h</span> —
<span class="prestation">Coupe</span></li><li class="creneau" data-id="c3"><span class="quand">jeudi 9h</span> —
<span class="prestation">Couleur</span></li><li class="creneau"><span class="quand">sans id</span></li></ul>"""


def test_manifeste_valide() -> None:
    cap = capacites.lire_capacite(DOSSIER)
    assert cap.valide, cap.erreurs
    assert cap.manifeste is not None
    assert cap.manifeste.origine == "assistant" and cap.manifeste.statut == "essai"
    assert cap.manifeste.confirmation_toujours == cap.manifeste.actions_avec_effet


def test_parser_creneaux_et_filtre() -> None:
    connecteur = capacites.importer_module(capacites.lire_capacite(DOSSIER), "connecteur")
    assert connecteur is not None
    creneaux = connecteur.parser_creneaux(HTML_CRENEAUX)
    assert [c["id"] for c in creneaux] == ["c1", "c3"]
    assert connecteur.filtrer(creneaux, "couleur") == [creneaux[1]]
    assert connecteur.lire_reference("Référence : RDV-0007") == "RDV-0007"
    assert connecteur.lire_reference("rien") is None


def test_outils_et_action_avec_effet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path))
    cap = capacites.lire_capacite(DOSSIER)
    mcp = capacites.MCPFactice()
    outils = capacites.charger_capacite(cap, mcp, donnees=tmp_path)
    assert outils == ["salon_demo_creneaux", "salon_demo_reserver"]
    # En essai, le cadre prépare la validation sans appeler l'outil : aucun effet possible.
    texte = mcp.outils["salon_demo_reserver"](creneau_id="c1")
    assert texte.startswith("CONFIRMATION_REQUISE:")
