"""Tests Kitchen Daily (lot 2) : parsing pur sur fixtures HTML, proposition, runner, captures du bot.

Aucun accès réseau. Les tests Playwright chargent les fixtures via `page.set_content` et sont ignorés
si Chromium n'est pas disponible dans l'environnement.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UPCOMING = (FIXTURES / "kd_upcoming.html").read_text(encoding="utf-8")
SELECT = (FIXTURES / "kd_select.html").read_text(encoding="utf-8")


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T12:00:00")
    (data / "config.yaml").write_text("assistant:\n  nom: Testbot\n", encoding="utf-8")
    return data


def _ref() -> datetime:
    from tools.common import TZ

    return datetime(2026, 11, 7, 12, 0, tzinfo=TZ)


# --- utilitaires purs ---------------------------------------------------------


def test_normaliser() -> None:
    from tools.kitchen_daily import normaliser

    assert normaliser("  PAVÉ de Saumon,   riz  BASMATI ") == "pave de saumon riz basmati"
    assert normaliser("Bœuf bourguignon") == "boeuf bourguignon"
    assert normaliser("Paëlla au poulet") == normaliser("paella au poulet")
    assert normaliser("Protéines +") == "proteines +"
    assert normaliser("Blanquette de veau à l’ancienne") == normaliser("blanquette de veau a l'ancienne")


def test_resoudre_titres() -> None:
    from tools.kitchen_daily import resoudre_titres

    menu = ["Tagliatelles carbonara", "Poulet basquaise et riz", "Poulet aux épices, légumes et coco", "Moussaka"]
    resolue, introuvables, ambigus = resoudre_titres(
        {"TAGLIATELLES CARBONARA": 2, "moussaka": 1, "poulet": 1, "Pizza": 1, "basquaise": 1}, menu
    )
    assert resolue == {"Tagliatelles carbonara": 2, "Moussaka": 1, "Poulet basquaise et riz": 1}
    assert introuvables == ["Pizza"]
    assert set(ambigus["poulet"]) == {"Poulet basquaise et riz", "Poulet aux épices, légumes et coco"}


def test_valider_quantites() -> None:
    from tools.kitchen_daily import ErreurKitchenDaily, valider_quantites

    assert valider_quantites({"a": 2, "b": "1"}) == {"a": 2, "b": 1}
    for mauvais in ({}, {"a": 0}, {"a": -1}, {"a": 1.5}, {"a": True}, {"a": "x"}):
        with pytest.raises(ErreurKitchenDaily):
            valider_quantites(mauvais)


def test_lire_compteur() -> None:
    from tools.kitchen_daily import lire_compteur

    assert lire_compteur("Remise à zéro 18 / 18 PLATS Je valide") == (18, 18)
    assert lire_compteur("3/18 plats") == (3, 18)
    assert lire_compteur("rien") is None


# --- parsing des fixtures (sans navigateur) -----------------------------------


def test_parser_livraisons_fixture() -> None:
    from tools.kitchen_daily import parser_livraisons, prochaine_modifiable

    livs = parser_livraisons(UPCOMING, _ref())
    assert [x["id"] for x in livs] == ["AbCd12Ef34", "AAAA000002", "AAAA000003"]
    verrouillee, prochaine, suivante = livs
    assert verrouillee["modifiable"] is False and verrouillee["payee"] is True
    assert verrouillee["modifiable_jusqua"] == "2026-11-06T23:59:00+01:00"
    assert prochaine == {
        "id": "AAAA000002",
        "date": "2026-11-19",
        "jour": "Jeudi",
        "creneau": "9h - 12h",
        "numero": "110000002",
        "statut": "Livraison confirmée",
        "montant": 110.0,
        "prelevement_le": "2026-11-15",
        "payee": False,
        "modifiable_jusqua": "2026-11-14T23:59:00+01:00",
        "modifiable": True,
        "livraison_voulue": None,  # lu sur la page de détail par le navigateur
    }
    assert suivante["creneau"] == "9h30 - 12h"
    assert prochaine_modifiable(livs)["id"] == "AAAA000002"
    # Après la date limite, plus rien n'est modifiable pour la livraison du 19/11
    from tools.common import TZ

    tard = parser_livraisons(UPCOMING, datetime(2026, 11, 15, 0, 0, tzinfo=TZ))
    assert [x["modifiable"] for x in tard] == [False, False, True]


def test_parser_livraisons_texte_fragmente() -> None:
    """Texte découpé dans des balises inline, carte entière sans lien : pas d'exception."""
    from tools.kitchen_daily import parser_livraisons

    html = (
        "<div><span>Vendredi</span> <b>20/11/2026</b><p>Livraison <i>confirmée</i></p><p>- 123456</p>"
        "<p>99,00 €</p><a href='/account/shippings/details/AAA111'>Voir le détail</a></div>"
    )
    livs = parser_livraisons(html, _ref())
    assert len(livs) == 1 and livs[0]["id"] == "AAA111" and livs[0]["montant"] == 99.0
    assert livs[0]["modifiable"] is False  # date limite inconnue → prudence


def test_parser_menu_fixture() -> None:
    from tools.kitchen_daily import parser_menu

    menu = parser_menu(SELECT, "AAAA000002")
    assert menu["livraison_id"] == "AAAA000002"
    assert menu["formule_plats"] == 18
    assert menu["compteur"] == {"selectionnes": 18, "formule": 18}
    assert menu["etapes"] == ["PLATS", "DESSERTS", "ENTRÉES, SOUPES & APÉROS", "JUS ET SHOTS DETOX"]
    plats = menu["plats"]
    assert len(plats) == 30
    assert plats[0]["titre"] == "Parmentier de pulled pork aux patates douces"
    assert plats[1]["badges"] == ["protéines +"]
    assert plats[4]["badges"] == ["protéines +", "Contrôle calories"]
    assert plats[3]["badges"] == ["Végétarien"]
    # La quantité affichée vaut 1 partout : c'est la classe d'état qui fait foi
    assert menu["detection_selection"] == "classe"
    assert sum(1 for p in plats if p["selectionne"]) == 18
    assert all(p["selectionne"] is (p["index"] < 18) for p in plats)
    assert plats[0]["quantite"] == 1 and plats[29]["quantite"] == 0
    assert "avertissement" not in menu


def test_parser_menu_indetermine() -> None:
    """Sans aucun signal (classes neutres, quantités identiques) : repli explicite à None."""
    from tools.kitchen_daily import parser_menu

    html = SELECT.replace("ItemProduct-module-scss-module__Xk9a2__isSelected", "")
    menu = parser_menu(html)
    assert menu["detection_selection"] == "indeterminee"
    assert all(p["selectionne"] is None and p["quantite"] is None for p in menu["plats"])
    assert "avertissement" in menu


def test_detecter_selection_variantes() -> None:
    from tools.kitchen_daily import detecter_selection

    def s(aria: bool | None = None, classe: bool = False, qty: int | None = 1, couleur: str | None = "a") -> dict[str, Any]:
        return {"aria": aria, "classe": classe, "qty": qty, "couleur_plus": couleur, "a_plus": True}

    assert detecter_selection([s(), s()], (0, 18))[0] == "compteur_zero"
    m, sel, _ = detecter_selection([s(aria=True), s(aria=False)], (1, 18))
    assert m == "aria" and sel == [True, False]
    m, sel, q = detecter_selection([s(qty=2), s(qty=0), s(qty=1)], (3, 18))
    assert m == "quantite" and sel == [True, False, True] and q == [2, 0, 1]
    m, sel, q = detecter_selection([s(couleur="vert"), s(couleur="blanc"), s(couleur="blanc")], (1, 18))
    assert m == "couleur" and sel == [True, False, False] and q == [1, 0, 0]
    # Toutes les cartes « actives » : signal non discriminant → indéterminé
    assert detecter_selection([s(classe=True), s(classe=True)], (2, 18))[0] == "indeterminee"


# --- proposition, historique, préférences ------------------------------------


def test_enregistrer_proposition(data_tmp: Path) -> None:
    from tools import db
    from tools.kitchen_daily import (
        ErreurKitchenDaily,
        _enregistrer_proposition,
        parser_livraisons,
        parser_menu,
    )

    liv = parser_livraisons(UPCOMING, _ref())[1]
    menu = parser_menu(SELECT, liv["id"])
    titres = [p["titre"] for p in menu["plats"]]

    with pytest.raises(ErreurKitchenDaily, match="17 plats"):
        _enregistrer_proposition(liv, menu, {titres[0]: 17}, "x", db.log_action)
    with pytest.raises(ErreurKitchenDaily, match="Pizza"):
        _enregistrer_proposition(liv, menu, {titres[0]: 17, "Pizza 4 fromages": 1}, "x", db.log_action)

    selection = {"tagliatelles CARBONARA": 2, "dahl de lentilles corail au lait de coco": 2}
    selection |= {t: 1 for t in titres[10:24]}
    sortie = _enregistrer_proposition(liv, menu, selection, "Variété et plats végétariens", db.log_action)
    assert sortie.startswith("CONFIRMATION_REQUISE:")
    aid = int(sortie.split(":")[1])
    assert sortie.endswith("18 plats pour la livraison du 19/11")
    action = db.get_action(aid)
    assert action["status"] == "pending" and action["kind"] == "kitchen_daily_selection"
    assert action["payload"]["livraison_id"] == "AAAA000002"
    assert action["payload"]["selection"]["Tagliatelles carbonara"] == 2
    assert sum(action["payload"]["selection"].values()) == 18


def test_proposer_sans_session(data_tmp: Path) -> None:
    from tools import kitchen_daily

    with pytest.raises(kitchen_daily.SessionExpiree, match="tools.kitchen_daily login"):
        kitchen_daily.proposer("AAAA000002", {"a": 18}, "x")
    assert (data_tmp / ".kd" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")


def test_preferences_et_historique(data_tmp: Path) -> None:
    from tools import kitchen_daily

    assert "Aucune préférence" in kitchen_daily.preferences()
    (data_tmp / "alimentation").mkdir()
    (data_tmp / "alimentation" / "enfants.md").write_text("# Enfants\n- Pas d'épices", encoding="utf-8")
    kitchen_daily.historique_ajouter("2026-11-19", ["Tagliatelles carbonara ×2", "Moussaka"], "carbonara trop salée")
    hist = (data_tmp / "alimentation" / "historique.md").read_text(encoding="utf-8")
    assert "- 19/11/2026 — Kitchen Daily : Tagliatelles carbonara ×2 ; Moussaka — carbonara trop salée" in hist
    prefs = kitchen_daily.preferences()
    assert "Pas d'épices" in prefs and "carbonara trop salée" in prefs
    assert prefs.index("enfants.md") < prefs.index("historique.md")


def test_hors_boucle_asyncio() -> None:
    from tools.kitchen_daily import _hors_boucle_asyncio

    @_hors_boucle_asyncio
    def nom_thread() -> str:
        return threading.current_thread().name

    async def dans_boucle() -> str:
        return nom_thread()

    assert nom_thread() == threading.current_thread().name
    assert asyncio.run(dans_boucle()).startswith("kd")


# --- runner --------------------------------------------------------------------


def test_runner_rappel_sans_session(data_tmp: Path) -> None:
    from tools import runner

    donnees = runner.donnees_rappel_kitchen()
    assert "Session Kitchen Daily expirée" in donnees["kitchen_daily_erreur"]
    assert "preferences" in donnees and "menu" not in donnees


def test_runner_rappel_avec_donnees(data_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import kitchen_daily, runner
    from tools.kitchen_daily import parser_livraisons, parser_menu, prochaine_modifiable

    def faux() -> dict[str, Any]:
        livs = parser_livraisons(UPCOMING, _ref())
        cible = prochaine_modifiable(livs)
        return {"livraisons": livs, "livraison_cible": cible, "menu": parser_menu(SELECT, cible["id"]),
                "date_limite": cible["modifiable_jusqua"]}

    monkeypatch.setattr(kitchen_daily, "donnees_rappel", faux)
    prompt = runner.assembler_prompt("rappel-kitchen-daily", runner.donnees_rappel_kitchen())
    donnees = json.loads(prompt.split("<donnees>", 1)[1].split("</donnees>", 1)[0])
    assert donnees["date_limite"] == "2026-11-14T23:59:00+01:00"
    assert len(donnees["menu"]["plats"]) == 30 and len(donnees["livraisons"]) == 3
    assert "kitchen_daily_erreur" not in donnees


def test_runner_execute_selection(data_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import db, kitchen_daily, runner

    capture = kitchen_daily.captures_dir() / "test-apres.png"
    capture.write_bytes(b"\x89PNG")
    appels: list[tuple[str, dict[str, int], bool]] = []

    def faux_appliquer(livraison_id: str, selection: dict[str, int], dry_run: bool = False) -> dict[str, Any]:
        appels.append((livraison_id, selection, dry_run))
        return {"ok": True, "compteur": "18 / 18", "selection_appliquee": selection,
                "verification_plats": "conforme", "captures": {"avant": "x", "apres": str(capture)}}

    monkeypatch.setattr(kitchen_daily, "appliquer_selection", faux_appliquer)
    payload = {"livraison_id": "AAAA000002", "livraison_date": "2026-11-19",
               "selection": {"Tagliatelles carbonara": 2, "Moussaka": 16}, "justification": "test"}
    aid = db.log_action("kitchen_daily_selection", "18 plats pour la livraison du 19/11", payload, "pending")
    assert runner.main(["execute", str(aid)]) == 0
    sortie = capsys.readouterr().out
    assert appels == [("AAAA000002", payload["selection"], False)]
    assert f"CAPTURE:{capture}" in sortie and "• Tagliatelles carbonara ×2" in sortie
    assert db.get_action(aid)["status"] == "done"
    hist = (data_tmp / "alimentation" / "historique.md").read_text(encoding="utf-8")
    assert "19/11/2026 — Kitchen Daily : Tagliatelles carbonara ×2 ; Moussaka ×16" in hist


# --- bot : captures --------------------------------------------------------------


def test_bot_captures(data_tmp: Path) -> None:
    from bot import confirmations
    from bot.config import Config

    cfg = Config(data_dir=data_tmp, root_dir=data_tmp.parent, groupe_telegram_id=-100)
    bonne = data_tmp / ".kd" / "captures" / "x-apres.png"
    bonne.parent.mkdir(parents=True)
    bonne.write_bytes(b"\x89PNG")
    intrus = data_tmp / "secrets.env"
    intrus.write_text("X=1", encoding="utf-8")

    texte, chemins = confirmations.extraire_captures(f"Sélection enregistrée.\n• a\nCAPTURE:{bonne}\nCAPTURE:{intrus}\n")
    assert texte == "Sélection enregistrée.\n• a" and chemins == [bonne, intrus]
    assert confirmations.extraire_captures("rien") == ("rien", [])
    assert confirmations.capture_autorisee(cfg, bonne) and not confirmations.capture_autorisee(cfg, intrus)

    envoyees: list[Path] = []

    async def envoyer(chemin: Path, legende: str) -> None:
        envoyees.append(chemin)

    n = asyncio.run(confirmations.envoyer_captures(cfg, chemins, envoyer, "légende"))
    assert n == 1 and envoyees == [bonne]


# --- Playwright (fixtures chargées par set_content, sans réseau) ---------------------


@pytest.fixture(scope="module")
def navigateur() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright non installé")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium indisponible ({exc.__class__.__name__}) : `playwright install chromium`")
        yield browser
        browser.close()


@pytest.fixture()
def page_select(navigateur: Any, data_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from tools import kitchen_daily

    monkeypatch.setattr(kitchen_daily, "ATTENTE_VALIDATION_MS", 300)
    page = navigateur.new_page()
    page.set_default_timeout(5000)
    page.set_content(SELECT)
    kitchen_daily.attendre_page_selection(page, "AAAA000002")
    yield page
    page.close()


def _selection_test() -> dict[str, int]:
    from tools.kitchen_daily import parser_menu

    titres = [p["titre"] for p in parser_menu(SELECT)["plats"]]
    selection = {titres[20].upper(): 2, titres[25]: 2}  # majuscules : correspondance normalisée
    selection |= {t: 1 for t in titres[2:16]}
    assert sum(selection.values()) == 18
    return selection


def test_pw_livraisons_et_cookies(navigateur: Any) -> None:
    from tools.kitchen_daily import fermer_cookies, parser_livraisons

    page = navigateur.new_page()
    page.set_content(UPCOMING)
    page.evaluate("document.getElementById('didomi-notice-disagree-button').onclick = () => document.getElementById('didomi-host').remove()")
    fermer_cookies(page)
    assert page.locator("#didomi-host").count() == 0
    assert [x["id"] for x in parser_livraisons(page.content(), _ref())] == ["AbCd12Ef34", "AAAA000002", "AAAA000003"]
    page.close()


def test_pw_lire_menu(page_select: Any) -> None:
    from tools.kitchen_daily import compteur_page, lire_menu_page

    menu = lire_menu_page(page_select, "AAAA000002")
    assert len(menu["plats"]) == 30 and menu["compteur"] == {"selectionnes": 18, "formule": 18}
    assert menu["detection_selection"] == "classe"
    assert [p["selectionne"] for p in menu["plats"]] == [True] * 18 + [False] * 12
    # Lecture seule : rien n'a été cliqué
    assert page_select.evaluate("window.__raz") == 0 and compteur_page(page_select) == (18, 18)


def test_pw_appliquer_dry_run(page_select: Any) -> None:
    from tools.kitchen_daily import appliquer_sur_page, lire_menu_page, normaliser

    selection = _selection_test()
    res = appliquer_sur_page(page_select, "AAAA000002", selection, dry_run=True)
    assert res["ok"] and res["dry_run"] and res["compteur"] == "18 / 18"
    assert Path(res["captures"]["avant"]).is_file() and "apres" not in res["captures"]
    assert page_select.evaluate("[window.__raz, window.__valide, window.__motivation]") == [1, False, False]
    menu = lire_menu_page(page_select)
    coches = {normaliser(p["titre"]) for p in menu["plats"] if p["selectionne"]}
    assert coches == {normaliser(t) for t in selection}


def test_pw_appliquer_valide_barre_du_bas(page_select: Any) -> None:
    from tools.kitchen_daily import appliquer_sur_page

    res = appliquer_sur_page(page_select, "AAAA000002", _selection_test(), dry_run=False, recharger=None)
    # Tout le tunnel parcouru (3 étapes) puis « Je valide ma commande », sans toucher au bandeau motivation.
    assert page_select.evaluate("[window.__valide, window.__commande, window.__etapes, window.__motivation]") == [True, True, 3, False]
    assert res["ok"] and res["verification_plats"] == "conforme"
    assert res["confirmation"] == "message de confirmation"
    assert Path(res["captures"]["apres"]).is_file()


def test_pw_appliquer_refus_avant_clic(page_select: Any) -> None:
    from tools.kitchen_daily import ErreurKitchenDaily, appliquer_sur_page

    with pytest.raises(ErreurKitchenDaily, match="Pizza"):
        appliquer_sur_page(page_select, "x", {"Pizza": 18}, dry_run=True)
    with pytest.raises(ErreurKitchenDaily, match="formule en demande 18"):
        appliquer_sur_page(page_select, "x", {"Moussaka": 3}, dry_run=True)
    assert page_select.evaluate("window.__raz") == 0


# --- lot 2b : extras, interrupteur de livraison (fixtures anonymisées) -------------------------

TUNNEL = (FIXTURES / "kd_tunnel.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "kd_detail.html").read_text(encoding="utf-8")

# Étape DESSERTS telle que rendue par le site (structure du 7 nov. 2026, produits inventés).
_P = "ItemProduct-module-scss-module__Ab1__"


def _carte_statique(titre: str, prix: str, qte: int, offert: bool = False, promo: str = "") -> str:
    li = f"{_P}product {_P}hasPrice" + (f" {_P}isOfferedExtras" if offert else "")
    ent, cents = prix.split(",")
    return (
        f'<li class="{li}"><div class="product-item {_P}productItem"><div class="{_P}vignette">'
        + (f'<span class="{_P}promotion">promo</span>' if promo == "badge" else "")
        + f'</div><div class="{_P}toolsCol"><div class="{_P}name"><h3 class="t_text">{titre}</h3></div>'
        f'<div class="{_P}priceTools"><div class="t_text {_P}price">{ent},<sup>{cents}</sup>€ / unité</div>'
        f'<div class="{_P}tools"><div class="{_P}quantite"><button type="button" class="{_P}moins"><span>-</span></button>'
        f'<input class="input-text qty" readonly type="text" value="{qte}">'
        f'<button type="button" class="{_P}plus"><span>+</span></button></div></div></div>'
        + (f'<div class="{_P}promo"><u>4,20€</u><span>Économisez</span><b>0,90€</b></div>' if promo == "bloc" else "")
        + "</div></div></li>"
    )


ETAPE_DESSERTS = (
    "<html><body><ul>"
    '<li class="TabsMeals-module-scss-module__T1__dot TabsMeals-module-scss-module__T1__active"><strong>Plats</strong></li>'
    '<li class="TabsMeals-module-scss-module__T1__dot TabsMeals-module-scss-module__T1__current"><strong>Desserts</strong></li>'
    '<li class="TabsMeals-module-scss-module__T1__dot"><strong>Entrées, Soupes &amp; Apéros</strong></li></ul>'
    '<span class="t_text">Je choisis mes <strong>desserts</strong></span><ul>'
    + _carte_statique("Compote pomme poire", "2,99", 0)
    + _carte_statique("Pack  3 Desserts du chef", "9,99", 0, promo="badge")
    + _carte_statique("Crème au chocolat", "3,99", 1, offert=True)
    + _carte_statique("Flan pâtissier", "3,99", 0, offert=True)
    + _carte_statique("4 Yaourts nature", "0,83", 0, promo="bloc")
    + '</ul><div class="ProgressBar-module-scss-module__Pb__spot"><span class="t_text">1 Desserts - 0 €</span></div>'
    '<button type="button" data-testid="confirmOrder">Je valide</button></body></html>'
)


def _etapes_statiques() -> dict[str, Any]:
    from tools.kitchen_daily import parser_etape_extras

    jus = parser_etape_extras(
        "<div><span>Je choisis mes <strong>jus et shots detox</strong></span><ul>"
        + _carte_statique("Shot gingembre citron", "2,49", 2)
        + "</ul><span>2 Jus et shots detox - 4,98 €</span></div>"
    )
    return {"desserts": parser_etape_extras(ETAPE_DESSERTS), "jus": jus}


def test_categorie_et_barre_extras() -> None:
    from tools.kitchen_daily import categorie_etape, lire_barre_extras

    assert categorie_etape("Desserts") == "desserts"
    assert categorie_etape("Entrées, Soupes & Apéros") == "entrees" == categorie_etape("entrees")
    assert categorie_etape("JUS ET SHOTS DETOX") == "jus"
    assert categorie_etape("Plats") == "plats" and categorie_etape("Extras") is None
    assert lire_barre_extras("Plats 1 Desserts - 0 € Veuillez choisir 1 produit Je valide") == ("desserts", 1, 0.0)
    assert lire_barre_extras("x | 0 Entrées, Soupes & Apéros - 0 € | Vendus par NaN") == ("entrees", 0, 0.0)
    assert lire_barre_extras("2 Jus et shots detox - 7,98 €") == ("jus", 2, 7.98)
    assert lire_barre_extras("4,20€ Économisez 0,90€ 18 / 18 Plats") is None


def test_parser_etape_extras_statique() -> None:
    from tools.kitchen_daily import detecter_etape, parser_etape_extras

    etape = parser_etape_extras(ETAPE_DESSERTS)
    assert etape["etape"] == "desserts" and etape["nombre"] == 1 and etape["montant"] == 0.0
    produits = {p["titre"]: p for p in etape["produits"]}
    assert list(produits) == [
        "Compote pomme poire", "Pack 3 Desserts du chef", "Crème au chocolat", "Flan pâtissier", "4 Yaourts nature",
    ]
    assert produits["Compote pomme poire"]["prix_unitaire"] == 2.99
    assert produits["Crème au chocolat"]["offert_possible"] and produits["Crème au chocolat"]["quantite"] == 1
    assert not produits["Compote pomme poire"]["offert_possible"]
    assert produits["Pack 3 Desserts du chef"]["promo"]
    assert produits["4 Yaourts nature"]["promo"] and produits["4 Yaourts nature"]["economie"] == 0.9
    assert produits["4 Yaourts nature"]["prix_barre"] == 4.2 and produits["4 Yaourts nature"]["prix_unitaire"] == 0.83
    # Détection de l'étape : onglet `…__current`, sinon titre, sinon barre, sinon compteur de plats.
    sans_onglet = ETAPE_DESSERTS.replace("__current", "__show")
    assert detecter_etape(sans_onglet) == "desserts"
    assert detecter_etape("<p>0 Jus et shots detox - 0 €</p>") == "jus"
    assert detecter_etape(SELECT) == "plats"
    assert detecter_etape("<p>DESSERTS</p>") is None


def test_valider_et_resoudre_extras() -> None:
    from tools.kitchen_daily import ErreurKitchenDaily, resoudre_extras, valider_extras

    assert valider_extras({"Desserts": {"crème": "2"}, "jus": {"shot": 0}}) == {"desserts": {"crème": 2}, "jus": {"shot": 0}}
    for mauvais in ({}, {"vins": {"x": 1}}, {"desserts": {}}, {"desserts": {"x": -1}}, {"desserts": {"x": 1.5}},
                    {"desserts": {"x": True}}, {"desserts": {"x": 99}}, {"desserts": ["x"]}):
        with pytest.raises(ErreurKitchenDaily):
            valider_extras(mauvais)

    etapes = _etapes_statiques()
    cibles, changements = resoudre_extras({"desserts": {"CREME AU CHOCOLAT": 1, "flan": 1}, "jus": {"shot": 0}}, etapes)
    assert cibles == {"desserts": {"Flan pâtissier": 1}, "jus": {"Shot gingembre citron": 0}}
    assert [(c["titre"], c["avant"], c["apres"]) for c in changements] == [
        ("Flan pâtissier", 0, 1), ("Shot gingembre citron", 2, 0),
    ]
    with pytest.raises(ErreurKitchenDaily, match="Tiramisu.*disponibles : Compote pomme poire"):
        resoudre_extras({"desserts": {"Tiramisu": 1}}, etapes)
    with pytest.raises(ErreurKitchenDaily, match="ambigu"):
        resoudre_extras({"desserts": {"p": 1}}, etapes)  # « p » est contenu dans plusieurs titres
    with pytest.raises(ErreurKitchenDaily, match="Entrées.*illisible"):
        resoudre_extras({"entrees": {"x": 1}}, etapes)


def test_estimer_extras_dessert_offert() -> None:
    from tools.kitchen_daily import estimer_extras, resoudre_extras

    etapes = _etapes_statiques()
    # Dessert offert déjà pris (crème au chocolat) : un 2e dessert éligible devient payant.
    _, ch = resoudre_extras({"desserts": {"Flan pâtissier": 1}}, etapes)
    assert estimer_extras(etapes, ch) == {"supplement_estime": 3.99, "retraits": False, "gratuit": False}
    # Sans dessert offert pris : en ajouter un est gratuit.
    etapes["desserts"]["produits"][2]["quantite"] = 0
    _, ch = resoudre_extras({"desserts": {"Flan pâtissier": 1}}, etapes)
    assert estimer_extras(etapes, ch) == {"supplement_estime": 0.0, "retraits": False, "gratuit": True}
    # Deux d'un coup : le second est payant ; un dessert non éligible est payant ; un retrait demande confirmation.
    _, ch = resoudre_extras({"desserts": {"Flan pâtissier": 2}}, etapes)
    assert estimer_extras(etapes, ch)["gratuit"] is False
    _, ch = resoudre_extras({"desserts": {"Compote pomme poire": 1}}, etapes)
    assert estimer_extras(etapes, ch) == {"supplement_estime": 2.99, "retraits": False, "gratuit": False}
    _, ch = resoudre_extras({"jus": {"Shot gingembre citron": 0}}, etapes)
    assert estimer_extras(etapes, ch) == {"supplement_estime": -4.98, "retraits": True, "gratuit": False}


def test_enregistrer_extras_et_livraison(data_tmp: Path) -> None:
    from tools import db
    from tools.kitchen_daily import (
        ErreurKitchenDaily,
        _enregistrer_extras,
        _enregistrer_livraison,
        estimer_extras,
        parser_livraisons,
        resoudre_extras,
    )

    liv = parser_livraisons(UPCOMING, _ref())[1]
    etapes = _etapes_statiques()
    cibles, ch = resoudre_extras({"desserts": {"Crème au chocolat": 3}, "jus": {"shot": 0}}, etapes)
    sortie = _enregistrer_extras(liv, cibles, ch, estimer_extras(etapes, ch), db.log_action)
    assert sortie.startswith("CONFIRMATION_REQUISE:")
    assert sortie.endswith(
        "Extras du 19/11 : +2 Crème au chocolat (→ 3) ; retirer Shot gingembre citron (+3,00 € estimé)"
    )
    action = db.get_action(int(sortie.split(":")[1]))
    assert action["kind"] == "kitchen_daily_extras_definir" and action["status"] == "pending"
    assert action["payload"]["extras"] == {"desserts": {"Crème au chocolat": 3}, "jus": {"Shot gingembre citron": 0}}

    sortie = _enregistrer_livraison(liv, False, True, db.log_action)
    assert sortie.endswith("Pas de livraison le jeudi 19/11 (semaine sautée)")
    action = db.get_action(int(sortie.split(":")[1]))
    assert action["kind"] == "kitchen_daily_livraison_definir"
    assert action["payload"] == {"livraison_id": "AAAA000002", "livraison_date": "2026-11-19", "voulue": False}
    assert _enregistrer_livraison(liv, True, True, db.log_action).startswith("Rien à faire")
    assert "Réactiver la livraison du jeudi 19/11" in _enregistrer_livraison(liv, True, False, db.log_action)
    with pytest.raises(ErreurKitchenDaily, match="lire l'interrupteur"):
        _enregistrer_livraison(liv, False, None, db.log_action)


def test_parser_interrupteur_et_prochaine_modifiable() -> None:
    from tools.kitchen_daily import parser_interrupteur, parser_livraisons, prochaine_modifiable

    assert parser_interrupteur(DETAIL) is False  # attribut `checked` absent du HTML statique (posé par le script)
    coche = DETAIL.replace('id="voulue">', 'id="voulue" checked>')
    assert parser_interrupteur(coche) is True
    switch = '<div><span>Je veux ma livraison</span><button role="switch" aria-checked="false">x</button></div>'
    assert parser_interrupteur(switch) is False
    assert parser_interrupteur('<p data-kd-etat="true">Je veux ma livraison</p>') is True
    assert parser_interrupteur("<p>Rien ici</p>") is None

    livs = parser_livraisons(UPCOMING, _ref())
    assert all(liv["livraison_voulue"] is None for liv in livs)
    livs[1]["livraison_voulue"] = False  # semaine du 19/11 sautée → la cible devient le 26/11
    assert prochaine_modifiable(livs)["id"] == "AAAA000003"
    annulee = UPCOMING.replace("Livraison confirmée - 110000002", "Livraison annulée - 110000002")
    assert parser_livraisons(annulee, _ref())[1]["livraison_voulue"] is False


# --- Playwright : tunnel et interrupteur (set_content, sans réseau) ---------------------------


def _tunnel(etat: dict[str, Any] | None = None, options: dict[str, Any] | None = None, lid: str = "AAAA000002") -> str:
    return (
        TUNNEL.replace('/*LIVRAISON_ID*/"test"', json.dumps(lid))
        .replace("/*ETAT_INITIAL*/null", json.dumps(etat))
        .replace("/*OPTIONS*/{}", json.dumps(options or {}))
    )


class _SiteLocal:
    """Simule la persistance du site pour `page.set_content` : « Je valide ma commande » enregistre l'état."""

    def __init__(self, page: Any, etat: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> None:
        self.page, self.options = page, options or {}
        self.etat = etat or {"plats": {"P1": 2, "P2": 1, "P3": 1, "P4": 1, "P5": 1}, "desserts": {"D3": 1},
                             "entrees": {}, "jus": {"J2": 2}}
        self.commandes = 0

    def ouvrir(self) -> None:
        self.page.set_content(_tunnel(self.etat, self.options))
        from tools.kitchen_daily import attendre_page_selection

        attendre_page_selection(self.page, "AAAA000002")

    def recharger(self) -> None:
        enregistre = self.page.evaluate("window.__enregistre")
        if enregistre:
            self.etat = enregistre
            self.commandes += 1
        self.ouvrir()


@pytest.fixture()
def page_tunnel(navigateur: Any, data_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from tools import kitchen_daily

    monkeypatch.setattr(kitchen_daily, "ATTENTE_VALIDATION_MS", 300)
    page = navigateur.new_page()
    page.set_default_timeout(5000)
    yield page
    page.close()


def test_pw_lire_extras_tunnel(page_tunnel: Any) -> None:
    from tools.kitchen_daily import _lire_extras_tunnel, _synthese_extras

    site = _SiteLocal(page_tunnel)
    site.ouvrir()
    etapes = _lire_extras_tunnel(page_tunnel)
    assert list(etapes) == ["desserts", "entrees", "jus"]
    assert etapes["desserts"]["nombre"] == 1 and etapes["desserts"]["montant"] == 0.0
    assert etapes["jus"]["montant"] == 4.98
    synthese = _synthese_extras(etapes)
    assert synthese["selection"] == {"desserts": {"Crème au chocolat": 1}, "entrees": {}, "jus": {"Shot gingembre citron": 2}}
    assert synthese["dessert_offert"]["deja_choisi"] == "Crème au chocolat"
    assert synthese["montant_estime"] == 4.98
    velout = next(p for p in etapes["entrees"]["produits"] if p["titre"] == "Velouté de potimarron")
    assert velout["stock_limite"] and velout["prix_unitaire"] == 4.49
    # Lecture seule : aucune commande validée, bandeau motivation intact.
    assert page_tunnel.evaluate("[window.__commandes, window.__motivation, window.__etapes]") == [0, False, 3]


def test_pw_extras_dry_run_puis_enregistrement(page_tunnel: Any) -> None:
    from tools.kitchen_daily import appliquer_extras_sur_page

    site = _SiteLocal(page_tunnel)
    site.ouvrir()
    demandes = {"desserts": {"creme au chocolat": 3, "Compote pomme poire": 1}, "jus": {"shot": 0}}
    res = appliquer_extras_sur_page(page_tunnel, "AAAA000002", demandes, dry_run=True)
    assert res["ok"] and res["dry_run"] and "confirmation" not in res
    assert res["reglages"] == {"desserts": {"Crème au chocolat": [1, 3], "Compote pomme poire": [0, 1]},
                               "jus": {"Shot gingembre citron": [2, 0]}}
    assert res["supplement_affiche"] == round(2 * 3.99 + 2.99 - 4.98, 2)
    assert page_tunnel.evaluate("window.__commandes") == 0 and Path(res["captures"]["avant"]).is_file()

    site.ouvrir()
    res = appliquer_extras_sur_page(page_tunnel, "AAAA000002", demandes, dry_run=False, recharger=site.recharger)
    assert res["ok"] and res["verification_extras"] == "conforme" and res["verification_plats"] == "inchangés"
    assert res["confirmation"] == "message de confirmation"
    assert site.commandes == 1
    assert site.etat["desserts"] == {"D3": 3, "D1": 1} and site.etat["jus"] == {"J2": 0}
    assert site.etat["plats"] == {"P1": 2, "P2": 1, "P3": 1, "P4": 1, "P5": 1}  # plats intacts
    assert res["extras_relus"] == {"desserts": {"Compote pomme poire": 1, "Crème au chocolat": 3}, "entrees": {}, "jus": {}}
    assert Path(res["captures"]["apres"]).is_file()


def test_pw_extras_refus(page_tunnel: Any) -> None:
    from tools.kitchen_daily import ErreurKitchenDaily, SupplementNonGratuit, appliquer_extras_sur_page

    site = _SiteLocal(page_tunnel, options={"bloques": ["E2"]})
    site.ouvrir()
    with pytest.raises(ErreurKitchenDaily, match="Tiramisu.*disponibles"):
        appliquer_extras_sur_page(page_tunnel, "x", {"desserts": {"Tiramisu": 1}}, dry_run=False)
    site.ouvrir()
    with pytest.raises(ErreurKitchenDaily, match="refuse de passer « Rillettes de thon »"):
        appliquer_extras_sur_page(page_tunnel, "x", {"entrees": {"Rillettes de thon": 1}}, dry_run=False)
    # « Dessert offert » présumé alors que l'offert est déjà pris : le montant affiché augmente → refus avant validation.
    site.ouvrir()
    with pytest.raises(SupplementNonGratuit, match="3,99 €"):
        appliquer_extras_sur_page(page_tunnel, "x", {"desserts": {"Flan": 1}}, dry_run=False, supplement_max=0.0)
    assert page_tunnel.evaluate("window.__commandes") == 0
    # Plats incomplets : le site reste sur PLATS, rien n'est validé.
    site = _SiteLocal(page_tunnel, etat={"plats": {"P1": 1}, "desserts": {}, "entrees": {}, "jus": {}})
    site.ouvrir()
    with pytest.raises(ErreurKitchenDaily, match="reste sur l'étape « plats »"):
        appliquer_extras_sur_page(page_tunnel, "x", {"desserts": {"Flan": 1}}, dry_run=True)


@pytest.mark.parametrize("raz_efface_extras", [False, True])
def test_pw_plats_conservent_les_extras(page_tunnel: Any, raz_efface_extras: bool) -> None:
    """Enregistrer les plats ne touche pas aux extras, même si « Remise à zéro » les vidait (réapplication)."""
    from tools.kitchen_daily import _lire_extras_tunnel, appliquer_sur_page, quantites_extras

    site = _SiteLocal(page_tunnel, options={"razEffaceExtras": raz_efface_extras})
    site.ouvrir()
    extras_avant = quantites_extras(_lire_extras_tunnel(page_tunnel))
    site.ouvrir()
    selection = {"Curry de pois chiches": 2, "Risotto aux champignons": 2, "Saumon à l'aneth et riz": 2}
    res = appliquer_sur_page(page_tunnel, "AAAA000002", selection, False, site.recharger, extras_attendus=extras_avant)
    assert res["ok"] and res["verification_plats"] == "conforme"
    assert res["verification_extras"] == "conforme"
    assert site.etat["plats"] == {"P6": 2, "P7": 2, "P8": 2}
    assert site.etat["desserts"].get("D3") == 1 and site.etat["jus"].get("J2") == 2
    if raz_efface_extras:
        assert res["extras_reappliques"] == {"desserts": {"Crème au chocolat": 1}, "jus": {"Shot gingembre citron": 2}}
    else:
        assert "extras_reappliques" not in res
    assert page_tunnel.evaluate("window.__motivation") is False


def _detail(voulue: bool, lid: str = "AAAA000002") -> str:
    return DETAIL.replace('/*LIVRAISON_ID*/"test"', json.dumps(lid)).replace("/*VOULUE*/true", json.dumps(voulue))


def test_pw_interrupteur_livraison(page_tunnel: Any) -> None:
    from tools.kitchen_daily import definir_livraison_sur_page, lire_interrupteur_page

    page = page_tunnel
    serveur = {"voulue": True}

    def recharger() -> None:
        serveur["voulue"] = page.evaluate("window.__voulue")
        page.set_content(_detail(serveur["voulue"]))

    page.set_content(_detail(True))
    assert lire_interrupteur_page(page) == {"trouve": True, "mode": "interrupteur", "etat": True}
    res = definir_livraison_sur_page(page, "AAAA000002", False, dry_run=True)
    assert res["avant"] is True and res["apres"] is True and page.evaluate("window.__modales") == 0
    # Désactiver : clic sur le <label> (case masquée), modale « Confirmer », relecture après rechargement.
    res = definir_livraison_sur_page(page, "AAAA000002", False, dry_run=False, recharger=recharger)
    assert res["ok"] and res["modale_confirmee"] and res["apres"] is False and serveur["voulue"] is False
    assert Path(res["captures"]["apres"]).is_file()
    # Déjà dans l'état voulu : aucun clic.
    assert definir_livraison_sur_page(page, "AAAA000002", False, dry_run=False)["deja"] is True
    # Réactiver : pas de modale.
    res = definir_livraison_sur_page(page, "AAAA000002", True, dry_run=False, recharger=recharger)
    assert res["ok"] and not res["modale_confirmee"] and serveur["voulue"] is True


def test_pw_interrupteur_oui_non(page_tunnel: Any) -> None:
    from tools.kitchen_daily import ErreurKitchenDaily, definir_livraison_sur_page, lire_interrupteur_page

    page = page_tunnel
    page.set_content(
        "<div><p>Je veux ma livraison</p><div>"
        "<button type='button' class='Toggle__opt' id='non' onclick=\"this.classList.add('Toggle__active');"
        "document.getElementById('oui').classList.remove('Toggle__active')\">Non</button>"
        "<button type='button' class='Toggle__opt Toggle__active' id='oui' onclick=\"this.classList.add('Toggle__active');"
        "document.getElementById('non').classList.remove('Toggle__active')\">Oui</button></div></div>"
    )
    assert lire_interrupteur_page(page)["mode"] == "oui_non"
    res = definir_livraison_sur_page(page, "x", False, dry_run=False)
    assert res["ok"] and res["apres"] is False
    page.set_content("<p>Je veux ma livraison</p>")
    with pytest.raises(ErreurKitchenDaily, match="introuvable ou illisible"):
        definir_livraison_sur_page(page, "x", False, dry_run=True)


# --- bout en bout : site factice local (HTTP sur 127.0.0.1), fonctions publiques et runner ---------


class _Site:
    def __init__(self) -> None:
        self.etats: dict[str, dict[str, Any]] = {}
        self.voulues: dict[str, bool] = {}
        self.options: dict[str, Any] = {}
        self.posts: list[str] = []


@pytest.fixture()
def site(navigateur: Any, data_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Site]:
    import re
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from tools import kitchen_daily

    etat_site = _Site()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _html(self, corps: str) -> None:
            donnees = corps.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(donnees)))
            self.end_headers()
            self.wfile.write(donnees)

        def do_GET(self) -> None:
            if self.path == "/account/shippings/upcoming":
                return self._html(UPCOMING)
            if m := re.fullmatch(r"/account/shippings/details/(\w+)", self.path):
                return self._html(_detail(etat_site.voulues.get(m.group(1), True), m.group(1)))
            if m := re.fullmatch(r"/account/shippings/edit/select-meal/quotidien/(\w+)", self.path):
                return self._html(_tunnel(etat_site.etats.get(m.group(1)), etat_site.options, m.group(1)))
            self.send_error(404)

        def do_POST(self) -> None:
            corps = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            etat_site.posts.append(self.path)
            if m := re.fullmatch(r"/api-proxy/orders/(\w+)/bulk", self.path):
                etat_site.etats[m.group(1)] = corps
            elif m := re.fullmatch(r"/api-proxy/shippings/(\w+)/want", self.path):
                etat_site.voulues[m.group(1)] = bool(corps.get("voulue"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    serveur = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    (data_tmp / "config.yaml").write_text(
        f"assistant:\n  nom: Testbot\nkitchen_daily:\n  url: http://127.0.0.1:{serveur.server_address[1]}\n",
        encoding="utf-8",
    )
    kitchen_daily.state_path().write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(kitchen_daily, "options_lancement", lambda: {"headless": True})
    monkeypatch.setattr(kitchen_daily, "fermer_cookies", lambda page: None)
    monkeypatch.setattr(kitchen_daily, "ATTENTE_VALIDATION_MS", 500)
    yield etat_site
    serveur.shutdown()
    serveur.server_close()


def test_bout_en_bout_livraisons_et_extras(site: _Site, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import db, kitchen_daily, runner

    site.voulues["AAAA000003"] = False
    livs = kitchen_daily.livraisons()
    assert [(x["id"], x["livraison_voulue"]) for x in livs] == [
        ("AbCd12Ef34", None), ("AAAA000002", True), ("AAAA000003", False),  # verrouillée : non lue
    ]
    lu = kitchen_daily.extras("AAAA000002")
    assert lu["modifiable"] is True and lu["selection"]["desserts"] == {"Crème au chocolat": 1}
    assert "avertissement" not in lu and len(lu["desserts"]["produits"]) == 5

    # Payant + retrait : action à confirmer, rien d'enregistré.
    sortie = kitchen_daily.definir_extras("AAAA000002", {"desserts": {"Compote pomme poire": 2}, "jus": {"shot": 0}})
    assert sortie.startswith("CONFIRMATION_REQUISE:") and "+5,98 €" not in sortie and "estimé" in sortie
    assert not site.posts
    aid = int(sortie.split(":")[1])
    assert runner.main(["execute", str(aid)]) == 0
    sortie_runner = capsys.readouterr().out
    assert "Extras Kitchen Daily enregistrés pour la livraison du 19/11 (vérification : conforme, plats inchangés)" in sortie_runner
    assert "CAPTURE:" in sortie_runner and db.get_action(aid)["status"] == "done"
    assert site.etats["AAAA000002"]["desserts"] == {"D3": 1, "D1": 2} and site.etats["AAAA000002"]["jus"] == {"J2": 0}

    # Dessert offert (aucun pris sur cette livraison) : enregistré directement, sans confirmation.
    site.etats["AAAA000003"] = {"plats": {"P1": 6}, "desserts": {}, "entrees": {}, "jus": {}}
    with pytest.raises(kitchen_daily.ErreurKitchenDaily, match="n'est plus modifiable|introuvable"):
        kitchen_daily.definir_extras("AbCd12Ef34", {"desserts": {"Flan": 1}})
    sortie = kitchen_daily.definir_extras("AAAA000003", {"desserts": {"Flan pâtissier": 1}})
    assert sortie.startswith("Dessert offert enregistré pour la livraison du 26/11"), sortie
    assert site.etats["AAAA000003"]["desserts"] == {"D4": 1}
    assert kitchen_daily.definir_extras("AAAA000003", {"desserts": {"Flan pâtissier": 1}}).startswith("Rien à changer")


def test_bout_en_bout_semaine_sautee_et_plats(site: _Site, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import db, kitchen_daily, runner

    sortie = kitchen_daily.proposer_livraison("AAAA000002", False)
    assert sortie.startswith("CONFIRMATION_REQUISE:") and "Pas de livraison le jeudi 19/11" in sortie
    assert "AAAA000002" not in site.voulues  # rien de cliqué avant « Valider »
    aid = int(sortie.split(":")[1])
    assert runner.main(["execute", str(aid)]) == 0
    assert "Pas de livraison le 19/11 : semaine sautée (relu sur le site)." in capsys.readouterr().out
    assert site.voulues["AAAA000002"] is False and db.get_action(aid)["status"] == "done"
    assert kitchen_daily.proposer_livraison("AAAA000002", False).startswith("Rien à faire")

    # Les plats d'une autre livraison sont enregistrés sans perdre ses extras, même si « Remise à zéro » les vidait.
    site.options = {"razEffaceExtras": True}
    site.etats["AAAA000003"] = {"plats": {"P1": 6}, "desserts": {"D3": 1, "D2": 1}, "entrees": {"E2": 1}, "jus": {}}
    res = kitchen_daily.appliquer_selection("AAAA000003", {"Lasagnes aux légumes du soleil": 3, "Bœuf mijoté": 3})
    assert res["ok"] and res["verification_extras"] == "conforme", res
    enregistre = site.etats["AAAA000003"]
    assert enregistre["plats"] == {"P3": 3, "P5": 3}
    assert enregistre["desserts"] == {"D3": 1, "D2": 1} and enregistre["entrees"] == {"E2": 1}
