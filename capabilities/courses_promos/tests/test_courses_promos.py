"""Tests de la capacité « courses & promos » (sans réseau) : tickets fictifs de deux enseignes inventées
(« MARCHE DES TILLEULS » et « HYPER FICTIF »), lecture d'une photo simulée (JSON du modèle), stockage, prix
habituels, compteur d'économies, outils et carte ``economies``."""

from __future__ import annotations

import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from capabilities.courses_promos import economies as eco
from capabilities.courses_promos import stockage
from capabilities.courses_promos import tickets as tk
from tools import capacites

DOSSIER = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
JOUR = date(2026, 11, 8)


def _ticket_fixture(nom: str) -> tk.Ticket:
    texte = tk.texte_du_ticket((FIXTURES / nom).read_bytes(), nom)
    return tk.lire_texte(texte, enseigne="enseigne " + nom[-5], aujourd_hui=JOUR)


def _lecture_photo(**surcharge: Any) -> dict[str, Any]:
    lecture: dict[str, Any] = {
        "enseigne": "MARCHE DES TILLEULS", "magasin": "VILLENOUVELLE", "date": "2026-10-25", "heure": "11:02",
        "total": 7.39, "lisible": True, "doutes": [],
        "lignes": [
            {"libelle": "CAFE MOUL.ARAB 250G", "type": "article", "quantite": 1, "unite": "piece",
             "prix_unitaire": 3.59, "montant": 3.59},
            {"libelle": "PAIN DE MIE COMPLET", "type": "article", "quantite": 2, "unite": "piece",
             "prix_unitaire": 1.9, "montant": 3.8},
        ],
    }
    lecture.update(surcharge)
    return lecture


# --- manifeste --------------------------------------------------------------------------------------------------


def test_manifeste_valide() -> None:
    cap = capacites.lire_capacite(DOSSIER)
    assert cap.valide, cap.erreurs
    assert not cap.avertissements, cap.avertissements
    m = cap.manifeste
    assert m is not None
    assert cap.prefixe == "promos" and m.statut == "essai" and m.origine == "humain"
    assert m.actions_avec_effet == [] and m.reseau == []
    assert [c.nom for c in m.crons] == ["veille_promos"]
    assert all(s.type == "identifiants" for s in m.secrets)
    cles = {p.cle: p for p in m.parametres}
    assert {"enseigne", "magasin", "mode", "carte_fidelite"} <= set(cles)
    assert cles["mode"].defaut == "a_choisir"


# --- ticket texte (mail, PDF) -----------------------------------------------------------------------------------


def test_gabarit_enseigne_a_quantite_sur_ligne_suivante_et_remise() -> None:
    t = _ticket_fixture("ticket_enseigne_a.txt")
    assert t.date == date(2026, 11, 8) and t.heure == "10:32" and t.total == 16.29
    assert t.magasin == "MARCHE DES TILLEULS"
    assert [(lg.libelle, lg.type) for lg in t.lignes] == [
        ("CAFE MOUL.ARAB 250G", "article"), ("LAIT DEMI ECR 1L", "article"), ("REMISE IMMEDIATE", "remise"),
        ("BANANES", "article"), ("PQ COMPACT X12", "article"),
    ]
    lait, bananes = t.lignes[1], t.lignes[3]
    assert (lait.quantite, lait.prix_unitaire, lait.montant) == (6, 0.99, 5.94)
    assert (bananes.unite, bananes.quantite, bananes.prix_unitaire) == ("kg", 1.234, 1.99)
    assert t.somme_lignes() == t.total and not t.a_verifier


def test_gabarit_enseigne_b_quantite_en_ligne_et_bon_de_reduction() -> None:
    t = _ticket_fixture("ticket_enseigne_b.txt")
    assert t.date == date(2026, 11, 6) and t.heure == "18:05" and t.total == 18.17
    cafe = t.lignes[0]
    assert (cafe.libelle, cafe.quantite, cafe.prix_unitaire, cafe.montant) == ("CAFE MOULU ARABICA 250G", 2, 3.15, 6.3)
    assert t.lignes[1].type == "remise" and t.lignes[1].montant == -1.0
    assert [lg.libelle for lg in t.lignes if lg.unite == "kg"] == ["POMMES GALA VRAC"]
    assert t.somme_lignes() == 18.17 and not t.a_verifier


def test_donnees_sensibles_jamais_dans_le_ticket() -> None:
    for nom in ("ticket_enseigne_a.txt", "ticket_enseigne_b.txt"):
        brut = (FIXTURES / nom).read_text(encoding="utf-8")
        filtre = tk.masquer(brut)
        for secret in ("2912345678901", "************1234", "************4242", "client@exemple.fr", "0400000000",
                       "Exemple"):
            if secret in brut:
                assert secret not in filtre, (nom, secret)
        contenu = json.dumps(_ticket_fixture(nom).en_dict(), ensure_ascii=False)
        assert "4242" not in contenu and "2912345678901" not in contenu and "CB" not in contenu


def test_mail_html_et_eml() -> None:
    html = (FIXTURES / "ticket_mail_b.html").read_text(encoding="utf-8")
    t = tk.lire_texte(tk.texte_du_ticket(html, "ticket.html"), aujourd_hui=JOUR)
    assert t.magasin == "HYPER FICTIF - ZONE DES ACACIAS" and t.date == date(2026, 11, 1)
    assert [(lg.libelle, lg.montant) for lg in t.lignes] == [("CAFE MOULU ARABICA 250G", 3.25), ("YAOURT NATURE X8", 1.85)]
    assert "consignes" not in json.dumps(t.en_dict())  # le texte après le total n'est jamais lu

    mail = EmailMessage()
    mail["From"] = "tickets@enseigne.exemple.fr"
    mail["Subject"] = "Votre ticket"
    mail.set_content("Version texte")
    mail.add_alternative(html, subtype="html")
    texte = tk.texte_du_ticket(mail.as_bytes(), "ticket.eml")
    assert "CAFE MOULU ARABICA 250G" in texte and "<td>" not in texte


def test_gabarit_non_reconnu_renvoie_le_texte_filtre() -> None:
    brut = "Merci de votre visite\nVISA 4970 1012 3456 7893\nArticles : voir l'appli\n12/10/2026"
    with pytest.raises(tk.GabaritNonReconnu) as exc:
        tk.lire_texte(brut, aujourd_hui=JOUR)
    assert "4970 1012 3456 7893" not in exc.value.texte_filtre
    assert "[carte bancaire" in exc.value.texte_filtre


def test_pdf_abime() -> None:
    with pytest.raises(tk.TicketInvalide):
        tk.texte_du_ticket(b"%PDF-1.4 pas vraiment un pdf", "ticket.pdf")


# --- ticket photographié (JSON du modèle) ----------------------------------------------------------------------


def test_consigne_de_lecture() -> None:
    consigne = tk.consigne_lecture("enseigne X")
    assert "JSON" in consigne and "enseigne X" in consigne and "ne devine jamais" in consigne
    assert "numéros de carte" in consigne


def test_lecture_photo_valide_et_bloc_json() -> None:
    t = tk.valider_lecture("```json\n" + json.dumps(_lecture_photo()) + "\n```", aujourd_hui=JOUR)
    assert t.date == date(2026, 10, 25) and len(t.lignes) == 2 and not t.a_verifier
    assert t.empreinte() == tk.valider_lecture(_lecture_photo(), aujourd_hui=JOUR).empreinte()


@pytest.mark.parametrize(
    ("surcharge", "attendu"),
    [
        ({"prix_promo": 1}, "Clés inconnues"),
        ({"lisible": False}, "illisible"),
        ({"date": "2027-01-01"}, "plausible"),
        ({"date": "25/10/2026"}, "AAAA-MM-JJ"),
        ({"lignes": []}, "liste non vide"),
        ({"lignes": [{"libelle": "CAFE", "quantite": 2, "prix_unitaire": 3.49, "montant": 3.49}]}, "relis cette ligne"),
        ({"lignes": [{"libelle": "CAFE", "montant": -3.49}]}, "remise"),
        ({"lignes": [{"libelle": "REMISE", "type": "remise", "montant": -1}]}, "suit l'article"),
        ({"lignes": [{"libelle": "CAFE", "montant": "3,49"}]}, "nombre attendu"),
        ({"lignes": [{"libelle": "CAFE", "montant": 3.49, "magasin": "x"}]}, "clés inconnues"),
        ({"lignes": [{"libelle": "POMMES", "quantite": 0.85, "montant": 2.12}]}, "kg"),
        ({"total": True}, "nombre attendu"),
    ],
)
def test_lecture_photo_refusee(surcharge: dict[str, Any], attendu: str) -> None:
    with pytest.raises(tk.TicketInvalide, match=attendu):
        tk.valider_lecture(_lecture_photo(**surcharge), aujourd_hui=JOUR)


def test_lecture_photo_reponse_non_json() -> None:
    with pytest.raises(tk.TicketInvalide, match="JSON"):
        tk.valider_lecture("Voici le ticket : café 3,49", aujourd_hui=JOUR)
    with pytest.raises(tk.TicketInvalide, match="objet JSON"):
        tk.valider_lecture("[1, 2]", aujourd_hui=JOUR)


def test_lecture_photo_ligne_sensible_ecartee_et_total_a_verifier() -> None:
    lignes = _lecture_photo()["lignes"] + [{"libelle": "VISA ************4242", "montant": 7.39}]
    t = tk.valider_lecture(_lecture_photo(lignes=lignes, total=9.99, doutes=["LEGUME ?"]), aujourd_hui=JOUR)
    assert [lg.libelle for lg in t.lignes] == ["CAFE MOUL.ARAB 250G", "PAIN DE MIE COMPLET"]
    assert t.a_verifier
    assert any("donnée personnelle" in a for a in t.avertissements)
    assert any("écart" in a for a in t.avertissements)
    assert any("incertaine" in a for a in t.avertissements)


# --- stockage, prix habituels, historique ------------------------------------------------------------------------


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    with stockage.ouvrir(tmp_path) as conn:
        stockage.enregistrer_ticket(conn, tk.valider_lecture(_lecture_photo(), aujourd_hui=JOUR), "photo")
        stockage.enregistrer_ticket(conn, _ticket_fixture("ticket_enseigne_b.txt"), "mail")
        stockage.enregistrer_ticket(conn, _ticket_fixture("ticket_enseigne_a.txt"), "fichier")
    return tmp_path


def test_enregistrement_deduplique_et_prix_payes(base: Path) -> None:
    with stockage.ouvrir(base) as conn:
        again = stockage.enregistrer_ticket(conn, _ticket_fixture("ticket_enseigne_a.txt"), "fichier")
        assert again["deja_lu"]
        assert conn.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3
        lait = conn.execute("SELECT * FROM prix_observes WHERE libelle = 'LAIT DEMI ECR 1L'").fetchone()
        assert lait["en_promo"] == 1 and lait["prix_unitaire"] == pytest.approx((5.94 - 0.80) / 6, abs=0.001)
        cafe_b = conn.execute("SELECT * FROM prix_observes WHERE libelle = 'CAFE MOULU ARABICA 250G'").fetchone()
        assert cafe_b["en_promo"] == 1 and cafe_b["prix_unitaire"] == 2.65
        assert conn.execute("SELECT count(*) FROM achats WHERE type = 'remise'").fetchone()[0] == 0


def test_questions_et_association(base: Path) -> None:
    with stockage.ouvrir(base) as conn:
        info = stockage.enregistrer_ticket(conn, tk.valider_lecture(_lecture_photo(date="2026-10-11"),
                                                                    aujourd_hui=JOUR), "photo")
        assert 0 < len(info["questions"]) <= stockage.QUESTIONS_MAX
        stockage.associer(conn, "CAFE MOUL.ARAB 250G", "Café moulu")
        stockage.associer(conn, "cafe moulu arabica", "café moulu")
        with pytest.raises(stockage.ErreurStockage, match="Aucun libellé"):
            stockage.associer(conn, "chocolat", "chocolat")
        prix = stockage.prix_habituels(conn, "café", JOUR)
        assert len(prix) == 1
        cafe = prix[0]
        assert cafe["article"] == "café moulu" and cafe["nomme"] and cafe["achats"] == 4
        # hors promo : 3,59 (25/10), 3,59 (11/10), 3,49 (08/11) → médiane 3,59 ; plus bas : 2,65 (promo du 06/11)
        assert cafe["reference"] == 3.59 and not cafe["reference_fragile"]
        assert cafe["plus_bas_90j"] == 2.65 and cafe["dernier_prix"] == 3.49
        assert conn.execute("SELECT count(*) FROM articles_canoniques WHERE nom LIKE 'cafe%'").fetchone()[0] == 0


def test_reference_fragile_et_historique(base: Path) -> None:
    with stockage.ouvrir(base) as conn:
        lessive = stockage.prix_habituels(conn, "lessive", JOUR)[0]
        assert lessive["reference"] == 8.9 and lessive["reference_fragile"]
        assert stockage.prix_habituels(conn, "caviar", JOUR) == []
        tickets_recents = stockage.historique(conn, date(2026, 11, 1))["tickets"]
        assert [t["date"] for t in tickets_recents] == ["2026-11-08", "2026-11-06"]
        assert tickets_recents[1]["lignes"][0]["paye"] == 5.3
        achats = stockage.historique(conn, None, "lait")["achats"]
        assert achats[0]["paye"] == 5.14 and achats[0]["remise"] == -0.8


# --- compteur d'économies ----------------------------------------------------------------------------------------


def _article(conn: Any, nom: str) -> int:
    return int(stockage.article_par_nom(conn, nom)[0]["id"])


def test_economies_definition_honnete(base: Path) -> None:
    with stockage.ouvrir(base) as conn:
        assert eco.donnees_carte(eco.total(conn)) is None  # rien tant qu'aucun achat suivi
        pain = _article(conn, "pain de mie")
        # Référence observée (1,90, fragile), jamais passée par l'appelant.
        id_pain = eco.enregistrer_estimation(conn, origine="suggestion", article_id=pain, quantite=2, prix_promo=1.5,
                                             jour=date(2026, 11, 2), fin_promo=date(2026, 11, 9))
        assert eco.total(conn)["total"] == 0.8 and eco.total(conn)["dont_estime"] == 0.8
        with pytest.raises(eco.EconomieRefusee, match="choix suscités"):
            eco.enregistrer_estimation(conn, origine="promo_vue", article_id=pain, quantite=1, prix_promo=1,
                                       jour=JOUR)
        sans_ref = int(conn.execute("INSERT INTO articles_canoniques (nom, valide, cree_le) VALUES ('truffe', 1, 'x')"
                                    ).lastrowid or 0)
        with pytest.raises(eco.EconomieRefusee, match="référence"):
            eco.enregistrer_estimation(conn, origine="suggestion", article_id=sans_ref, quantite=1, prix_promo=1,
                                       jour=JOUR)
        # Le ticket du 10/11 montre un prix payé supérieur à la référence : perte comptée.
        stockage.enregistrer_ticket(conn, tk.valider_lecture(_lecture_photo(
            date="2026-11-09", total=4.2, lignes=[{"libelle": "PAIN DE MIE COMPLET", "quantite": 2,
                                                   "prix_unitaire": 2.1, "montant": 4.2}]), aujourd_hui=JOUR), "photo")
        assert eco.rapprocher_tickets(conn, JOUR) == {"confirmees": 1, "annulees": 0}
        resume = eco.total(conn)
        assert resume["total"] == -0.4 and resume["dont_estime"] == 0 and resume["achats_suivis"] == 1
        assert resume["lignes"][0]["statut"] == "confirmee" and resume["lignes"][0]["paye"] == 4.2
        with pytest.raises(eco.EconomieRefusee, match="déjà"):
            eco.confirmer(conn, id_pain, 1.0, "ticket:1")


def test_economies_annulee_sans_ticket(base: Path) -> None:
    with stockage.ouvrir(base) as conn:
        lessive = _article(conn, "lessive")
        eco.enregistrer_estimation(conn, origine="meilleur_jour", article_id=lessive, quantite=1, prix_promo=6.9,
                                   jour=date(2026, 11, 7), fin_promo=date(2026, 11, 8))
        # Pas encore 14 jours après la fin de la promo : l'estimation attend son ticket.
        assert eco.rapprocher_tickets(conn, date(2026, 11, 20)) == {"confirmees": 0, "annulees": 0}
        assert eco.total(conn)["dont_estime"] == 2.0
        # Un ticket plus récent arrive sans lessive, et 14 jours sont passés : annulée, hors du total.
        stockage.enregistrer_ticket(conn, tk.valider_lecture(_lecture_photo(date="2026-11-20"),
                                                             aujourd_hui=date(2026, 11, 25)), "photo")
        assert eco.rapprocher_tickets(conn, date(2026, 11, 25)) == {"confirmees": 0, "annulees": 1}
        assert eco.total(conn)["achats_suivis"] == 0
        assert conn.execute("SELECT count(*) FROM economies").fetchone()[0] == 2  # ajout seul


# --- outils, carte et routine (cadre des capacités) ---------------------------------------------------------------


@pytest.fixture()
def outils(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("assistant:\n  nom: Zébulon\n", encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-08T12:00:00")
    monkeypatch.delenv("HOMEOS_CAPABILITIES", raising=False)
    mcp = capacites.MCPFactice()
    noms = capacites.charger_capacite(capacites.lire_capacite(DOSSIER), mcp, donnees=data)
    assert all(n.startswith("promos_") for n in noms)
    return {"mcp": mcp.outils, "data": data}


def test_outils_ticket_photo_puis_prix(outils: dict[str, Any]) -> None:
    o = outils["mcp"]
    assert "JSON" in o["promos_ticket_consigne"]()
    refus = o["promos_ticket_enregistrer"](lecture=json.dumps(_lecture_photo(date="2030-01-01")))
    assert refus.startswith("Lecture refusée")
    texte = o["promos_ticket_enregistrer"](lecture=json.dumps(_lecture_photo()))
    assert "2 article(s)" in texte and "7,39 €" in texte and "« CAFE MOUL.ARAB 250G »" in texte
    assert "déjà lu" in o["promos_ticket_enregistrer"](lecture=json.dumps(_lecture_photo()))
    assert "café moulu" in o["promos_associer"](libelle="CAFE MOUL.ARAB 250G", article="café moulu")
    prix = o["promos_prix_habituels"](article="café")
    assert prix["articles"][0]["dernier_prix"] == 3.59
    assert o["promos_historique"](depuis="2026-10-01")["tickets"][0]["date"] == "2026-10-25"
    assert o["promos_economies"]() == {"achats_suivis": 0, "message": "Aucun achat suivi pour l'instant : rien à afficher."}


def test_outil_import_fichier(outils: dict[str, Any]) -> None:
    entrants = outils["data"] / "fichiers" / "entrants"
    entrants.mkdir(parents=True)
    (entrants / "ticket.html").write_bytes((FIXTURES / "ticket_mail_b.html").read_bytes())
    (entrants / "illisible.txt").write_text("Merci de votre visite\n12/10/2026\nVISA 4970 1012 3456 7893",
                                            encoding="utf-8")
    (entrants / "photo.jpg").write_bytes(b"\xff\xd8")
    o = outils["mcp"]
    assert "2 article(s)" in o["promos_ticket_importer"](fichier="fichiers/entrants/ticket.html")
    assert "déjà lu" in o["promos_ticket_importer"](fichier="fichiers/entrants/ticket.html")
    repli = o["promos_ticket_importer"](fichier="fichiers/entrants/illisible.txt")
    assert "Mise en page non reconnue" in repli and "4970 1012 3456 7893" not in repli
    assert "lis-la toi-même" in o["promos_ticket_importer"](fichier="fichiers/entrants/photo.jpg")


def test_carte_economies(outils: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from coeur import cartes as coeur_cartes
    from tools import cartes

    with stockage.ouvrir(outils["data"] / "capacites" / "courses_promos") as conn:
        stockage.enregistrer_ticket(conn, tk.valider_lecture(_lecture_photo(), aujourd_hui=JOUR), "photo")
        pain = _article(conn, "pain de mie")
        eco.enregistrer_estimation(conn, origine="suggestion", article_id=pain, quantite=2, prix_promo=1.5,
                                   jour=date(2026, 11, 7), fin_promo=date(2026, 11, 15))
    resultat = outils["mcp"]["promos_economies"](depuis="2026-11-01")
    assert resultat["total"] == 0.8 and resultat["achats_suivis"] == 1
    monkeypatch.setenv("HOMEOS_CANAL", "web")
    produites = cartes.produire("promos_economies", resultat, {})
    assert len(produites) == 1
    carte = produites[0]
    assert carte["type"] == "economies" and carte["titre"] == "Économisé grâce à Zébulon"
    assert coeur_cartes.erreurs_carte(carte) == []
    assert "0,80" in coeur_cartes.rendu_texte(carte) or "0.80" in coeur_cartes.rendu_texte(carte)
    assert cartes.produire("promos_economies", {"achats_suivis": 0}, {}) == []


def test_routine_inerte_tant_que_mode_a_choisir(tmp_path: Path) -> None:
    crons = capacites.importer_module(capacites.lire_capacite(DOSSIER), "crons")
    assert crons is not None
    ctx = SimpleNamespace(dossier=tmp_path / "cap", parametre=lambda cle, defaut=None: "a_choisir")
    assert crons.executer("veille_promos", ctx) is None
    assert not (tmp_path / "cap").exists()  # rien n'a été touché
    journal: list[str] = []
    ctx = SimpleNamespace(dossier=tmp_path / "cap", parametre=lambda cle, defaut=None: "tickets",
                          journal=journal.append)
    assert crons.executer("veille_promos", ctx) is None
    assert journal and (tmp_path / "cap" / stockage.NOM_BASE).exists()
    with pytest.raises(ValueError):
        crons.executer("autre", ctx)


def test_smoke(tmp_path: Path) -> None:
    smoke = capacites.importer_module(capacites.lire_capacite(DOSSIER), "smoke")
    assert smoke is not None
    res = smoke.verifier(SimpleNamespace(dossier=tmp_path, parametre=lambda cle, defaut=None: "a_choisir"))
    assert res["ok"] and "à choisir" in res["message"]
