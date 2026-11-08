"""Documents sensibles dans Chromium, contre le backend de démonstration : interopérabilité du chiffrement du front
avec l'implémentation de référence Python (``tests/sensibles_reference.py``), dans les deux sens.

- Passkey sans PRF : première fois (phrase de secours affichée une fois, dernier groupe retapé), clé d'appareil ;
  un document déposé par le front se déchiffre en Python avec la clé tirée de la phrase ; un document chiffré en
  Python s'ouvre dans le front ; suppression avec confirmation ; appareil sans la clé → phrase de secours.
- Passkey avec PRF : trousseau (enveloppe PRF seulement) sur le serveur, réouverture par l'empreinte seule.

    pytest -m navigateur tests/front/test_front_sensibles.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.front.outils import RACINE, executable_chromium, passkey_virtuelle, png, semer_sensibles, serveur_demo
from tests.sensibles_reference import cle_depuis_phrase, dechiffrer_document

pytestmark = pytest.mark.navigateur

playwright = pytest.importorskip("playwright.sync_api")

MOTS_TECHNIQUES = ("PRF", "AES", "HKDF", "GCM", "WebAuthn", "IndexedDB", "HSB1", "base32")


@pytest.fixture(scope="module")
def chromium() -> str | None:
    if not (RACINE / "web" / "static" / "app" / "index.html").exists():
        pytest.skip("build du front absent (web/static/app)")
    try:
        return executable_chromium()
    except FileNotFoundError:
        pytest.skip("aucun Chromium disponible")


def _ouvrir_appli(p, chromium, infos, *, prf: bool):  # type: ignore[no-untyped-def]
    from playwright.sync_api import expect

    navigateur = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"])
    contexte = navigateur.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR", service_workers="block")
    page = contexte.new_page()
    erreurs: list[str] = []
    page.on("console", lambda m: erreurs.append(m.text) if "Content Security Policy" in m.text or m.type == "error" else None)
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    passkey_virtuelle(contexte, page, prf=prf)
    page.goto(infos["invitation"])
    page.get_by_role("button", name="Activer cet appareil").click()
    expect(page.get_by_label("Message à Firmin")).to_be_visible()
    page.goto(f"{infos['origine']}/fichiers")
    return navigateur, page, erreurs


def _premiere_fois(page) -> str:  # type: ignore[no-untyped-def]
    """Déverrouiller la première fois : phrase affichée une fois, dernier groupe retapé, « Je l'ai notée »."""
    from playwright.sync_api import expect

    section = page.locator("#documents-sensibles")
    expect(section).to_contain_text("Chiffrés, visibles par toi seul")
    section.get_by_role("button", name="Déverrouiller", exact=True).click()
    phrase_liste = page.locator("[data-phrase] li")
    expect(phrase_liste).to_have_count(11)
    groupes = phrase_liste.all_inner_texts()
    phrase = "-".join(g.strip() for g in groupes)
    assert len(phrase) == 65
    bouton = page.get_by_role("button", name="Je l’ai notée")
    expect(bouton).to_be_disabled()
    page.get_by_label("Pour vérifier, retape le dernier groupe", exact=False).fill(groupes[-1].lower())
    bouton.click()
    expect(section).to_have_attribute("data-sensibles", "ouvert")
    expect(page.locator("[data-phrase]")).to_have_count(0)
    return phrase


def _deposer(page, chemin: Path, categorie: str, nom: str | None = None) -> None:  # type: ignore[no-untyped-def]
    from playwright.sync_api import expect

    page.locator("[data-depot-sensible]").set_input_files(str(chemin))
    dialogue = page.get_by_role("dialog", name="Ajouter un document sensible")
    expect(dialogue).to_be_visible()
    dialogue.get_by_role("radio", name=categorie).click()
    if nom:
        dialogue.get_by_label("Nom").fill(nom)
    dialogue.get_by_role("button", name="Chiffrer et ranger").click()
    expect(dialogue).to_have_count(0)


def _verifier_sans_bruit(page, erreurs: list[str]) -> None:  # type: ignore[no-untyped-def]
    texte = page.locator("#documents-sensibles").inner_text()
    assert not [m for m in MOTS_TECHNIQUES if m in texte], texte
    largeur = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert largeur[0] <= largeur[1], f"défilement horizontal : {largeur}"
    assert not [e for e in erreurs if "Content Security Policy" in e], erreurs


def test_interoperabilite_cle_d_appareil_et_phrase(tmp_path: Path, chromium: str | None) -> None:
    from playwright.sync_api import expect, sync_playwright

    dossier = tmp_path / "demo"
    stockage = dossier / "data" / "fichiers" / "sensibles" / "alex"
    with serveur_demo(dossier) as infos, sync_playwright() as p:
        navigateur, page, erreurs = _ouvrir_appli(p, chromium, infos, prf=False)
        section = page.locator("#documents-sensibles")
        phrase = _premiere_fois(page)
        expect(section).to_contain_text("Protégé sur cet appareil")
        expect(section.locator("[data-etat='vide']")).to_contain_text("Aucun document pour l’instant")
        assert not (stockage / "trousseau.json").exists()  # l'enveloppe d'appareil reste dans le navigateur

        # Front → Python : photo déposée depuis l'écran, chiffrée dans le navigateur.
        photo = tmp_path / "IMG_2041.png"
        photo.write_bytes(png(6, 4, (200, 30, 30)))
        _deposer(page, photo, "Pièce d’identité", "Carte d’identité")
        expect(section.locator("[data-document]")).to_have_count(1)
        index = json.loads((stockage / "index.json").read_text(encoding="utf-8"))
        assert [(d["nom"], d["categorie"]) for d in index] == [("Carte d’identité", "identite")]
        blob = (stockage / f"{index[0]['id']}.bin").read_bytes()
        assert blob[:5] == b"HSB1\x01" and b"PNG" not in blob and b"IMG_2041" not in blob
        meta, contenu = dechiffrer_document(cle_depuis_phrase(phrase), blob)
        assert meta == {"nom": "IMG_2041.png", "type": "image/png", "taille": len(contenu), "cree": meta["cree"]}
        assert contenu == photo.read_bytes()

        # Python → front : document chiffré par la référence (clé de la phrase, même identifiant de clé).
        semer_sensibles(dossier / "data", "alex", phrase, [("Carnet de santé", "sante", "carnet.png", png(7, 5, (30, 160, 60)))])
        section.get_by_role("button", name="Verrouiller").click()
        expect(section).to_have_attribute("data-sensibles", "verrouille")
        expect(section).to_contain_text("Verrouillé · titulaire seul")
        section.get_by_role("button", name="Déverrouiller", exact=True).click()  # clé d'appareil : pas de phrase
        expect(section).to_have_attribute("data-sensibles", "ouvert")
        expect(section.locator("[data-document]")).to_have_count(2)
        section.get_by_role("button", name="Ouvrir Carnet de santé").click()
        apercu = page.get_by_role("dialog", name="Carnet de santé")
        image = apercu.get_by_role("img", name="Carnet de santé")
        expect(image).to_be_visible()
        assert image.evaluate("i => [i.naturalWidth, i.naturalHeight, i.src.startsWith('blob:')]") == [7, 5, True]
        # Suppression avec confirmation.
        apercu.get_by_role("button", name="Supprimer").click()
        apercu.get_by_role("alertdialog").get_by_role("button", name="Supprimer").click()
        expect(apercu).to_have_count(0)
        expect(section.locator("[data-document]")).to_have_count(1)
        assert len(list(stockage.glob("*.bin"))) == 1

        # Le menu « + » propose le dépôt d'un document sensible (jamais envoyé à la conversation).
        page.get_by_label("Plus d’options : photos, fichiers, enregistrer").click()
        expect(page.get_by_role("button", name="Document sensible", exact=False)).to_be_visible()
        page.keyboard.press("Escape")

        # Appareil sans la clé (données du site effacées) : phrase de secours, puis documents ouverts.
        page.evaluate("() => new Promise(ok => { const r = indexedDB.deleteDatabase('homeos-sensibles'); r.onsuccess = r.onerror = r.onblocked = () => ok(true); })")
        page.reload()
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        expect(section.locator("[data-etat='phrase']")).to_contain_text("Cet appareil n’a pas la clé : utilise ta phrase de secours.")
        _verifier_sans_bruit(page, erreurs)
        page.get_by_label("Phrase de secours").fill(phrase.replace("-", " ").lower())
        section.get_by_role("button", name="Ouvrir").click()
        expect(section).to_have_attribute("data-sensibles", "ouvert")
        expect(section.locator("[data-document]")).to_have_count(1)
        _verifier_sans_bruit(page, erreurs)
        navigateur.close()


def test_passkey_prf_trousseau_et_reouverture(tmp_path: Path, chromium: str | None) -> None:
    from playwright.sync_api import expect, sync_playwright

    dossier = tmp_path / "demo"
    stockage = dossier / "data" / "fichiers" / "sensibles" / "alex"
    with serveur_demo(dossier) as infos, sync_playwright() as p:
        navigateur, page, erreurs = _ouvrir_appli(p, chromium, infos, prf=True)
        section = page.locator("#documents-sensibles")
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        expect(page.get_by_role("dialog", name="Ta phrase de secours")).to_contain_text("Sans elle et sans ta passkey, tes documents sont perdus.")
        page.keyboard.press("Escape")  # « Plus tard » : rien n'est enregistré
        expect(section).to_have_attribute("data-sensibles", "verrouille")
        assert not (stockage / "trousseau.json").exists()

        phrase = _premiere_fois(page)
        expect(section).to_contain_text("Protégé par ton empreinte")
        trousseau = json.loads((stockage / "trousseau.json").read_text(encoding="utf-8"))
        assert [e["type"] for e in trousseau["enveloppes"]] == ["prf"]
        assert set(trousseau["enveloppes"][0]) <= {"type", "credential_id", "sel", "iv", "cle_chiffree", "cree"}

        doc = tmp_path / "ordonnance.pdf"
        doc.write_bytes(b"%PDF-1.4\n% ordonnance de test\n")
        _deposer(page, doc, "Carnet de l’animal")
        index = json.loads((stockage / "index.json").read_text(encoding="utf-8"))
        assert index[0]["cle"] == trousseau["cle"] and index[0]["nom"] == "Carnet de l’animal"
        _, contenu = dechiffrer_document(cle_depuis_phrase(phrase), (stockage / f"{index[0]['id']}.bin").read_bytes())
        assert contenu == doc.read_bytes()

        # Nouvel onglet (clé oubliée) : l'empreinte seule rouvre, sans phrase.
        page.reload()
        section.get_by_role("button", name="Déverrouiller", exact=True).click()
        expect(section).to_have_attribute("data-sensibles", "ouvert")
        expect(section).to_contain_text("Protégé par ton empreinte")
        section.get_by_role("button", name="Ouvrir Carnet de l’animal").click()
        expect(page.locator("[data-apercu='application/pdf'] iframe")).to_be_attached()
        page.get_by_role("dialog").get_by_role("button", name="Fermer").last.click()  # URL blob: révoquée
        expect(page.locator("[data-apercu]")).to_have_count(0)
        _verifier_sans_bruit(page, erreurs)
        navigateur.close()
