"""Captures du front (lot 4b) : 390 px et desktop, clair et sombre, contre le backend de démonstration.

    python -m tests.front.captures [--sortie docs/maquette/captures]

Produit ``front-<écran>-<mobile|desktop>-<clair|sombre>.png``. Données de démonstration neutres (tests/front/demo.py).
``--seulement sensibles`` : captures des documents sensibles seulement (``front-sensibles-*.png``).
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from tests.front.outils import RACINE, executable_chromium, passkey_virtuelle, png, semer_sensibles, serveur_demo

MESSAGES = [
    "Qu'est-ce qu'on mange cette semaine, côté repas ?",
    "Compare les offres d'assurance habitation",
    "Montre-moi les documents rangés",
    "Ajoute du lait aux courses",
    "Prépare les identifiants du site de la cantine",
    "Le code du portail est 1234, note-le",
    "Qu'est-ce qu'on a à l'agenda samedi ?",
]
ECRANS = ["conversation", "semaine", "courses", "repas", "echeances", "journal", "fichiers", "memoire", "routines", "reglages", "plus"]


DOCUMENTS_SENSIBLES = [
    ("Carte d’identité d’Alex", "identite", "cni.png", (147, 197, 253)),
    ("Passeport d’Alex", "identite", "passeport.png", (165, 180, 252)),
    ("Carnet de santé d’Alex", "sante", "carnet.png", (134, 239, 172)),
    ("Ordonnance lunettes", "sante", "ordonnance.png", (252, 165, 165)),
    ("Carnet de santé de Rex", "animal", "rex.png", (253, 230, 138)),
    ("Attestation de sécurité sociale", "administratif", "attestation.png", (196, 181, 253)),
]


def capturer_sensibles(sortie: Path) -> None:
    """Documents sensibles : verrouillé, phrase de secours (première fois), ouvert (390 px et desktop, clair et
    sombre), aperçu d'un document, dépôt, clé absente de l'appareil. Passkey virtuelle avec PRF."""
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory() as tmp, serveur_demo(Path(tmp) / "demo") as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=executable_chromium(), args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="fr-FR",
                                          service_workers="block")
        page = contexte.new_page()
        passkey_virtuelle(contexte, page, prf=True)
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        expect(page.get_by_label("Message à Firmin")).to_be_visible()
        section = page.locator("#documents-sensibles")

        def aller(theme: str = "light") -> None:
            page.evaluate(
                "t => { const c = 'homeos:preferences:alex'; const v = JSON.parse(localStorage.getItem(c) || '{}');"
                " v.theme = t; localStorage.setItem(c, JSON.stringify(v)); }", theme)
            page.goto(f"{infos['origine']}/fichiers")
            expect(section).to_be_visible()

        def capturer(nom: str, haut: bool = True) -> None:
            if haut:
                section.scroll_into_view_if_needed()
                page.evaluate("() => { const s = document.querySelector('#documents-sensibles'); window.scrollBy(0, s.getBoundingClientRect().top - 72); }")
            page.wait_for_timeout(400)
            page.screenshot(path=str(sortie / f"front-sensibles-{nom}.png"))

        aller()
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        liste = page.locator("[data-phrase] li")
        expect(liste).to_have_count(11)
        capturer("phrase-mobile-clair", haut=False)
        groupes = liste.all_inner_texts()
        page.get_by_label("Pour vérifier, retape le dernier groupe", exact=False).fill(groupes[-1])
        page.get_by_role("button", name="Je l’ai notée").click()
        expect(section).to_have_attribute("data-sensibles", "ouvert")
        capturer("vide-mobile-clair")
        semer_sensibles(Path(tmp) / "demo" / "data", "alex", "-".join(groupes),
                        [(n, c, o, png(300, 190, couleur)) for n, c, o, couleur in DOCUMENTS_SENSIBLES])

        for theme, suffixe in (("light", "clair"), ("dark", "sombre")):
            for taille, viewport in (("mobile", {"width": 390, "height": 844}), ("desktop", {"width": 1440, "height": 900})):
                page.set_viewport_size(viewport)
                aller(theme)
                if theme == "light" and taille == "mobile":
                    expect(section.locator("[data-document]")).to_have_count(0)  # liste inconnue avant l'empreinte
                section.get_by_role("button", name="Déverrouiller", exact=True).click()
                expect(section).to_have_attribute("data-sensibles", "ouvert")
                expect(section.locator("[data-document]")).to_have_count(len(DOCUMENTS_SENSIBLES))
                capturer(f"{taille}-{suffixe}")
                section.get_by_role("button", name="Verrouiller").click()
                capturer(f"verrouille-{taille}-{suffixe}")

        page.set_viewport_size({"width": 390, "height": 844})
        aller()
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        section.get_by_role("button", name="Ouvrir Carte d’identité d’Alex").click()
        expect(page.get_by_role("dialog").get_by_role("img")).to_be_visible()
        capturer("apercu-mobile-clair", haut=False)
        page.keyboard.press("Escape")
        fichier = Path(tmp) / "photo.png"
        fichier.write_bytes(png(40, 30, (200, 200, 200)))
        page.locator("[data-depot-sensible]").set_input_files(str(fichier))
        expect(page.get_by_role("dialog", name="Ajouter un document sensible")).to_be_visible()
        capturer("depot-mobile-clair", haut=False)
        page.keyboard.press("Escape")

        navigateur.close()

    # Appareil sans la clé (passkey sans PRF, clé d'appareil effacée) : la phrase de secours est demandée.
    with tempfile.TemporaryDirectory() as tmp, serveur_demo(Path(tmp) / "demo") as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=executable_chromium(), args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="fr-FR",
                                          service_workers="block")
        page = contexte.new_page()
        passkey_virtuelle(contexte, page)
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        expect(page.get_by_label("Message à Firmin")).to_be_visible()
        section = page.locator("#documents-sensibles")
        page.goto(f"{infos['origine']}/fichiers")
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        expect(page.locator("[data-phrase] li")).to_have_count(11)
        groupes = page.locator("[data-phrase] li").all_inner_texts()
        page.get_by_label("Pour vérifier, retape le dernier groupe", exact=False).fill(groupes[-1])
        page.get_by_role("button", name="Je l’ai notée").click()
        fichier = Path(tmp) / "photo.png"
        fichier.write_bytes(png(40, 30, (200, 200, 200)))
        page.locator("[data-depot-sensible]").set_input_files(str(fichier))
        page.get_by_role("button", name="Chiffrer et ranger").click()
        expect(section.locator("[data-document]")).to_have_count(1)
        page.evaluate("() => new Promise(ok => { const r = indexedDB.deleteDatabase('homeos-sensibles'); r.onsuccess = r.onerror = r.onblocked = () => ok(true); })")
        page.reload()
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        expect(section.locator("[data-etat='phrase']")).to_be_visible()
        capturer("cle-absente-mobile-clair")
        navigateur.close()
def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--sortie", type=Path, default=RACINE / "docs" / "maquette" / "captures")
    analyseur.add_argument("--seulement", choices=["tout", "ecrans", "sensibles"], default="tout")
    args = analyseur.parse_args(argv)
    from playwright.sync_api import expect, sync_playwright

    args.sortie.mkdir(parents=True, exist_ok=True)
    if args.seulement in ("tout", "sensibles"):
        capturer_sensibles(args.sortie)
    if args.seulement == "sensibles":
        print(f"captures écrites dans {args.sortie}")
        return 0
    with tempfile.TemporaryDirectory() as tmp, serveur_demo(Path(tmp) / "demo") as infos, sync_playwright() as p:
        navigateur = p.chromium.launch(executable_path=executable_chromium(), args=["--no-sandbox"])
        contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="fr-FR",
                                          service_workers="block")
        page = contexte.new_page()
        passkey_virtuelle(contexte, page)
        page.goto(infos["invitation"])
        page.get_by_role("button", name="Activer cet appareil").click()
        champ = page.get_by_label("Message à Firmin")
        expect(champ).to_be_visible()
        for m in MESSAGES:
            expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0, timeout=15000)
            champ.fill(m)
            champ.press("Enter")
        expect(page.locator('[data-reflexion="en-cours"]')).to_have_count(0, timeout=15000)

        def capturer(nom: str) -> None:
            page.wait_for_timeout(350)
            page.screenshot(path=str(args.sortie / f"front-{nom}.png"))

        # Pendant la session : « Transmis à … » (masquage) tel qu'il s'affiche en direct.
        page.locator("[data-masquage]").last.scroll_into_view_if_needed()
        page.mouse.wheel(0, 160)
        capturer("conversation-masquage-mobile-clair")

        for theme in ("clair", "sombre"):
            page.evaluate(
                "t => { const c = 'homeos:preferences:alex';"
                " const v = JSON.parse(localStorage.getItem(c) || '{}'); v.theme = t;"
                " localStorage.setItem(c, JSON.stringify(v)); }",
                "dark" if theme == "sombre" else "light",
            )
            for taille, viewport in (("mobile", {"width": 390, "height": 844}), ("desktop", {"width": 1440, "height": 900})):
                page.set_viewport_size(viewport)
                for ecran in ECRANS:
                    if taille == "desktop" and ecran == "plus":
                        continue
                    page.goto(f"{infos['origine']}/{'' if ecran == 'conversation' else ecran}")
                    expect(page.get_by_label("Message à Firmin" if ecran == "conversation" else "Demander à Firmin", exact=False)).to_be_visible()
                    page.wait_for_timeout(600)
                    if ecran == "conversation":
                        page.evaluate("window.scrollTo(0, 0)")
                    capturer(f"{ecran}-{taille}-{theme}")
                    if ecran == "conversation" and theme == "clair":
                        page.locator('[data-carte="identifiants"]').last.scroll_into_view_if_needed()
                        capturer(f"conversation-cartes-{taille}-{theme}")
                        page.locator('[data-carte="kitchen_daily_proposition"]').last.scroll_into_view_if_needed()
                        capturer(f"conversation-repas-{taille}-{theme}")
                        if taille == "mobile":
                            page.get_by_label("Plus d’options : photos, fichiers, enregistrer").click()
                            capturer("menu-plus-mobile-clair")
                            page.keyboard.press("Escape")
        navigateur.close()
    print(f"captures écrites dans {args.sortie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
