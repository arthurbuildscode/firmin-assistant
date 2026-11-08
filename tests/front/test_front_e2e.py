"""Lot 4b — test de bout en bout du front contre le backend réel (faux ``claude``) dans Chromium :
activation par lien d'invitation (passkey virtuelle), carte du jour, envoi d'un message, réception de cartes,
validation d'une confirmation, masquage d'une donnée sensible, carte identifiants sous step-up, écran courses,
aucune violation de CSP ni défilement horizontal à 390 px.

    pytest -m navigateur tests/front          # Chromium de Playwright, ou Chrome du système (HOMEOS_CHROMIUM)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.front.outils import RACINE, executable_chromium, passkey_virtuelle, serveur_demo

pytestmark = pytest.mark.navigateur

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def chromium() -> str | None:
    if not (RACINE / "web" / "static" / "app" / "index.html").exists():
        pytest.skip("build du front absent (web/static/app)")
    try:
        return executable_chromium()
    except FileNotFoundError:
        pytest.skip("aucun Chromium disponible")


def test_parcours_complet(tmp_path: Path, chromium: str | None) -> None:
    from playwright.sync_api import expect, sync_playwright

    with serveur_demo(tmp_path / "demo") as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR", service_workers="block")
        page = contexte.new_page()
        erreurs: list[str] = []
        page.on("console", lambda m: erreurs.append(m.text) if "Content Security Policy" in m.text or m.type == "error" else None)
        page.on("pageerror", lambda e: erreurs.append(str(e)))
        passkey_virtuelle(contexte, page)

        # Activation de l'appareil par le lien d'invitation (passkey créée), puis l'accueil.
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        champ = page.get_by_label("Message à Firmin")
        expect(champ).to_be_visible()
        expect(page.get_by_role("heading", name="Bonjour Alex")).to_be_visible()
        expect(page.locator('[data-carte="a_noter"]')).to_contain_text("Vermifuge du chien")

        # Message → ligne de réflexion, cartes, texte en streaming, confirmation.
        champ.fill("Qu'est-ce qu'on a à l'agenda samedi ?")
        champ.press("Enter")
        expect(page.locator('[data-carte="agenda_jour"]')).to_contain_text("Piscine")
        evenement = page.locator('[data-carte="evenement"]')
        expect(evenement).to_contain_text("Dentiste")
        expect(page.get_by_role("button", name="Réfléchi")).to_be_visible()
        evenement.get_by_role("button", name="Valider").click()
        expect(evenement).to_contain_text("Validé")
        expect(page.locator(".bulle.systeme").last).to_contain_text("ajouté à l'agenda")

        # Donnée sensible : le cœur la masque, l'appli montre ce qui a été transmis (cadenas).
        expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0)
        champ.fill("Le mot de passe : Zorglub!2026xx pour le portail")
        champ.press("Enter")
        masquage = page.locator("[data-masquage]").last
        expect(masquage).to_contain_text("Transmis à Firmin")
        expect(masquage).to_contain_text("[mot de passe masqué]")
        expect(masquage).not_to_contain_text("Zorglub")

        # Carte identifiants (lot 5c) : saisie sous step-up, jamais renvoyée ni affichée.
        expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0)
        champ.fill("Prépare les identifiants du site de la cantine")
        champ.press("Enter")
        carte = page.locator('[data-carte="identifiants"]').last
        expect(carte).to_contain_text("Site de la cantine")
        carte.get_by_label("Identifiant").fill("parent@example.org")
        carte.get_by_label("Mot de passe").fill("Tr3s-secret-cantine")
        carte.get_by_role("button", name="Enregistrer").click()
        expect(carte).to_contain_text("Enregistré dans le coffre")
        assert "Tr3s-secret-cantine" not in page.content()

        # Courses : la carte du fil alimente l'écran dédié ; cocher ne réordonne rien.
        expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0)
        champ.fill("Ajoute du lait")
        champ.press("Enter")
        expect(page.locator('[data-carte="courses"]')).to_contain_text("(nouveau)")
        page.get_by_role("navigation", name="Navigation").get_by_role("link", name="Courses").click()
        expect(page.get_by_role("heading", name="Courses")).to_be_visible()
        # Écran plein : la liste (point d'accès direct), sans bulle ni « Réfléchi », sans panneau par-dessus.
        expect(page.locator(".article")).to_have_count(16)
        expect(page.locator(".contenu .reflexion")).to_have_count(0)
        expect(page.locator(".contenu .bulle")).to_have_count(0)
        expect(page.locator(".contenu [data-carte='bonjour']")).to_have_count(0)
        avant = page.locator(".article .nm").all_inner_texts()
        poireaux = page.locator(".article", has_text="Poireaux")
        poireaux.click()
        expect(poireaux).to_have_class("article coche")  # cochée sur place (PATCH), rien ne bouge
        assert page.locator(".article .nm").all_inner_texts() == avant
        expect(page.get_by_text("changement à enregistrer")).to_have_count(0)
        # Le champ d'un écran dédié : « Demander à Firmin… », bouton voix quand il est vide.
        expect(page.get_by_placeholder("Demander à Firmin…")).to_have_value("")
        expect(page.get_by_role("button", name="Mode voix", exact=False)).to_be_visible()
        # Un texte prérempli sur un écran (« Ajoute à l’agenda : ») ne suit pas la personne sur un autre écran.
        page.get_by_role("navigation", name="Navigation").get_by_role("link", name="Semaine").click()
        page.get_by_role("button", name="Ajouter", exact=True).click()
        expect(page.get_by_placeholder("Demander à Firmin…")).to_have_value("Ajoute à l’agenda : ")
        page.get_by_role("navigation", name="Navigation").get_by_role("link", name="Courses").click()
        expect(page.get_by_placeholder("Demander à Firmin…")).to_have_value("")
        # Demande depuis l'écran : la réponse tient sur une ligne dans le dock, la liste reste visible.
        champ_ecran = page.get_by_placeholder("Demander à Firmin…")
        champ_ecran.fill("Ajoute du lait aux courses")
        champ_ecran.press("Enter")
        expect(page.locator("[data-reponse-ecran]")).to_contain_text("le lait est sur la liste")
        expect(page.locator(".article").first).to_be_visible()

        # Barre d'outils sous une réponse : copier, écouter, pouces, réessayer ; pouce bas → commentaire facultatif.
        page.get_by_role("navigation", name="Navigation").get_by_role("link", name="Conversation").click()
        barre = page.get_by_role("toolbar", name="Actions sur la réponse").last
        expect(barre.get_by_role("button", name="Copier la réponse")).to_be_attached()
        expect(barre.get_by_role("button", name="Écouter")).to_be_attached()
        barre.get_by_role("button", name="Réponse pas utile").click()
        page.get_by_label("Qu’est-ce qui n’allait pas ? (facultatif)").fill("Trop court")
        page.get_by_label("Qu’est-ce qui n’allait pas ? (facultatif)").press("Enter")
        expect(barre.get_by_role("button", name="Réponse pas utile")).to_have_attribute("aria-pressed", "true")

        largeur = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert largeur[0] <= largeur[1], f"défilement horizontal : {largeur}"
        assert not [e for e in erreurs if "Content Security Policy" in e], erreurs
        navigateur.close()


def test_hors_ligne_et_ecrans_sans_defilement_horizontal(tmp_path: Path, chromium: str | None) -> None:
    """Service worker : après une visite, l'appli se rouvre hors ligne et l'historique reste lisible ; aucun écran ne
    défile horizontalement à 390 px."""
    from playwright.sync_api import expect, sync_playwright

    with serveur_demo(tmp_path / "demo") as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR")
        page = contexte.new_page()
        passkey_virtuelle(contexte, page)
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        champ = page.get_by_label("Message à Firmin")
        champ.fill("Ajoute du lait")
        champ.press("Enter")
        expect(page.locator('[data-carte="courses"]')).to_be_visible()
        expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0)
        for ecran in ("semaine", "courses", "repas", "echeances", "journal", "fichiers", "memoire", "routines", "reglages", "plus"):
            page.goto(f"{infos['origine']}/{ecran}")
            expect(page.get_by_label("Demander à Firmin", exact=False)).to_be_visible()
            page.wait_for_timeout(300)
            largeur = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            assert largeur[0] <= largeur[1], f"{ecran} : défilement horizontal {largeur}"
        page.goto(infos["origine"] + "/")
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.reload()  # la page est désormais contrôlée par le service worker
        expect(page.locator('[data-carte="courses"]')).to_be_visible()
        page.wait_for_timeout(500)
        contexte.set_offline(True)
        page.reload()
        expect(page.locator('[data-carte="courses"]')).to_be_visible()
        expect(page.get_by_text("Hors ligne", exact=False)).to_be_visible()
        navigateur.close()


def test_ecran_courses_sans_point_d_acces(tmp_path: Path, chromium: str | None) -> None:
    """Retour de test du 8 nov. (écran Courses, téléphone, palette jaune, thème sombre) : la première demande du jour
    faite depuis Courses ne superpose plus rien (ni carte « Bonjour » ni bulle, ni « Réfléchi ») ; la liste s'affiche
    en plein écran depuis la carte du fil, la réponse tient sur une ligne dans le dock, au-dessus du champ."""
    from playwright.sync_api import expect, sync_playwright

    with serveur_demo(tmp_path / "demo", sans_points_acces=True) as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR", service_workers="block",
                                          color_scheme="dark")
        page = contexte.new_page()
        passkey_virtuelle(contexte, page)
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        expect(page.get_by_label("Message à Firmin")).to_be_visible()
        page.get_by_role("navigation", name="Navigation").get_by_role("link", name="Courses").click()
        expect(page.get_by_text("La liste de courses s’affiche ici")).to_be_visible()
        page.get_by_role("button", name="Afficher la liste").click()
        expect(page.locator(".article")).to_have_count(9)
        expect(page.locator("[data-reponse-ecran]")).to_contain_text("le lait est sur la liste")
        contenu = page.locator(".contenu")
        for selecteur in ("[data-carte='bonjour']", "[data-carte='a_noter']", ".bulle", ".reflexion", ".meta-message"):
            expect(contenu.locator(selecteur)).to_have_count(0)
        # Rien ne passe sous le dock : le bas du contenu (marge comprise) s'arrête au-dessus du champ.
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        dock = page.locator(".dock").bounding_box()
        dernier = page.locator(".article").last.bounding_box()
        assert dock and dernier and dernier["y"] + dernier["height"] <= dock["y"] + 1, (dernier, dock)
        expect(page.get_by_placeholder("Demander à Firmin…")).to_have_value("")
        # Repli sans point d'accès : cocher reste sur place, les changements partent ensemble.
        page.locator(".article", has_text="Poireaux").click()
        expect(page.get_by_text("1 changement à enregistrer")).to_be_visible()
        navigateur.close()
