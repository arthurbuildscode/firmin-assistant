"""Tests du lot 3 (fichiers reçus) : rangement, index, fiches, photos, purge. Sans réseau."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-07T10:00:00")
    return data


def _entrant(data: Path, nom: str, contenu: bytes = b"%PDF-1.4 faux") -> Path:
    dossier = data / "fichiers" / "entrants"
    dossier.mkdir(parents=True, exist_ok=True)
    f = dossier / nom
    f.write_bytes(contenu)
    return f


def _image(chemin: Path, taille: tuple[int, int], mode: str = "RGB") -> Path:
    from PIL import Image

    chemin.parent.mkdir(parents=True, exist_ok=True)
    couleur = (200, 120, 40, 128) if mode == "RGBA" else (200, 120, 40)
    Image.new(mode, taille, couleur).save(chemin)
    return chemin


# --- noms -------------------------------------------------------------------


def test_nettoyer_nom() -> None:
    from tools.fichiers import nettoyer_nom

    assert nettoyer_nom("Facture Garage Éléphant (2) !!") == "facture-garage-elephant-2"
    assert nettoyer_nom("../../etc/passwd") == "etc-passwd"
    assert nettoyer_nom("   ") == "fichier"
    assert nettoyer_nom("a" * 200) == "a" * 60


def test_nom_entrant_bot(tmp_path: Path) -> None:
    from bot.handlers import (
        chemin_entrant,
        construire_prompt_fichiers,
        extension_document,
    )

    quand = datetime(2026, 11, 7, 18, 5, 9).astimezone()
    c = chemin_entrant(tmp_path, "Camille", "Ordonnance Dr Été.PDF", ".pdf", quand)
    assert c.name == "2026-11-07_180509_camille_ordonnance-dr-ete.pdf"
    c.touch()
    assert chemin_entrant(tmp_path, "Camille", "Ordonnance Dr Été.PDF", ".pdf", quand).name.endswith("-2.pdf")
    assert extension_document("scan.JPEG", None) == ".jpg"
    assert extension_document(None, "application/pdf") == ".pdf"
    assert extension_document("x.docx", "application/msword") is None
    p = construire_prompt_fichiers([Path("/d/a.jpg")], "")
    assert p.startswith("[fichier reçu: /d/a.jpg]\nLis ce fichier")
    p = construire_prompt_fichiers([Path("/d/a.jpg"), Path("/d/b.jpg")], "vacances")
    assert p == "[fichier reçu: /d/a.jpg]\n[fichier reçu: /d/b.jpg] vacances"


# --- rangement --------------------------------------------------------------


def test_ranger_deplace_indexe_et_fiche(data_tmp: Path) -> None:
    from tools import fichiers

    source = _entrant(data_tmp, "2026-11-07_100000_alex_photo.JPEG", b"jpeg")
    res = fichiers.ranger(
        str(source), "Voiture", "Facture garage vidange.jpg", "Vidange, 84 200 km, 312 €", fiche="voitures/twingo.md"
    )
    dest = data_tmp / "fichiers" / "voiture" / "2026" / "facture-garage-vidange.jpg"
    assert dest.read_bytes() == b"jpeg"
    assert not source.exists()
    assert res["chemin"] == "fichiers/voiture/2026/facture-garage-vidange.jpg"
    assert res["fiche"] == "voitures/twingo.md"

    index = (data_tmp / "fichiers" / "voiture" / "index.md").read_text(encoding="utf-8")
    assert "- 2026-11-07 · facture-garage-vidange.jpg · Vidange, 84 200 km, 312 €" in index

    fiche = (data_tmp / "voitures" / "twingo.md").read_text(encoding="utf-8")
    assert "- 2026-11-07 · Vidange, 84 200 km, 312 € ([facture-garage-vidange.jpg](../fichiers/voiture/2026/facture-garage-vidange.jpg))" in fiche

    # Même nom une deuxième fois : suffixe, pas d'écrasement
    source2 = _entrant(data_tmp, "autre.pdf")
    res2 = fichiers.ranger(str(source2), "voiture", "facture garage vidange", "Deuxième")
    assert res2["nom"] == "facture-garage-vidange.pdf"
    source3 = _entrant(data_tmp, "encore.pdf")
    assert fichiers.ranger("fichiers/entrants/encore.pdf", "voiture", "facture garage vidange", "3e")["nom"] == (
        "facture-garage-vidange-2.pdf"
    )
    assert not source3.exists()

    liste = fichiers.lister("voiture")
    assert liste[0]["nom"] == "facture-garage-vidange-2.pdf"
    assert len(liste) == 3 and all(e["existe"] for e in liste)
    assert fichiers.lister(limite=1)[0]["categorie"] == "voiture"


def test_categorie_chien_devient_animaux(data_tmp: Path) -> None:
    """Ancienne catégorie `chien` : alias accepté, et migration (fichiers, index, liens des fiches) idempotente."""
    from tools import fichiers

    assert "chien" not in fichiers.CATEGORIES and fichiers.verifier_categorie("chien") == "animaux"
    ancien = data_tmp / "fichiers" / "chien"
    (ancien / "2025").mkdir(parents=True)
    (ancien / "2025" / "carnet.pdf").write_bytes(b"%PDF ancien")
    (ancien / "2026").mkdir()
    (ancien / "2026" / "vaccin.pdf").write_bytes(b"%PDF 2026")
    (ancien / "index.md").write_text(
        "# Fichiers — chien\n\n_Une ligne par fichier rangé : date · nom · description._\n\n"
        "- 2025-03-02 · carnet.pdf · Carnet de santé\n- 2026-10-01 · vaccin.pdf · Rappel vaccin\n",
        encoding="utf-8",
    )
    # Un fichier du même nom existe déjà côté animaux : le déplacé est suffixé, le lien suit.
    deja = data_tmp / "fichiers" / "animaux" / "2026"
    deja.mkdir(parents=True)
    (deja / "vaccin.pdf").write_bytes(b"%PDF deja")
    fiche = data_tmp / "animaux" / "rex.md"
    fiche.parent.mkdir(parents=True)
    fiche.write_text(
        "# Rex\n\n- 2025-03-02 · Carnet ([carnet.pdf](../fichiers/chien/2025/carnet.pdf))\n"
        "- 2026-10-01 · Vaccin ([vaccin.pdf](../fichiers/chien/2026/vaccin.pdf))\n",
        encoding="utf-8",
    )

    faites = fichiers.migrer_categories()
    assert faites == ["fichiers chien → animaux (2 fichiers)"]
    assert not ancien.exists()
    nouveau = data_tmp / "fichiers" / "animaux"
    assert (nouveau / "2025" / "carnet.pdf").read_bytes() == b"%PDF ancien"
    assert (nouveau / "2026" / "vaccin-2.pdf").read_bytes() == b"%PDF 2026"
    assert (nouveau / "2026" / "vaccin.pdf").read_bytes() == b"%PDF deja"
    index = (nouveau / "index.md").read_text(encoding="utf-8")
    assert index.startswith("# Fichiers — animaux")
    assert "- 2025-03-02 · carnet.pdf · Carnet de santé" in index and "- 2026-10-01 · vaccin-2.pdf · Rappel vaccin" in index
    texte = fiche.read_text(encoding="utf-8")
    assert "fichiers/chien/" not in texte
    assert "(../fichiers/animaux/2025/carnet.pdf)" in texte and "(../fichiers/animaux/2026/vaccin-2.pdf)" in texte
    assert [f["nom"] for f in fichiers.lister("animaux")] == ["vaccin-2.pdf", "carnet.pdf"]

    assert fichiers.migrer_categories() == []  # idempotente
    assert index == (nouveau / "index.md").read_text(encoding="utf-8")


def test_migration_en_ligne_de_commande(data_tmp: Path) -> None:
    import subprocess
    import sys

    (data_tmp / "fichiers" / "chien" / "2026").mkdir(parents=True)
    (data_tmp / "fichiers" / "chien" / "2026" / "a.pdf").write_bytes(b"%PDF")
    racine = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(racine)}
    r = subprocess.run([sys.executable, "-m", "tools.fichiers", "migrer"], cwd=racine, env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "chien → animaux (1 fichier)" in r.stdout
    assert (data_tmp / "fichiers" / "animaux" / "2026" / "a.pdf").exists()
    r = subprocess.run([sys.executable, "-m", "tools.fichiers", "migrer"], cwd=racine, env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout == ""


def test_ranger_fiche_animaux_et_gitignore(data_tmp: Path) -> None:
    from tools import fichiers, memoire

    source = _entrant(data_tmp, "vaccin.pdf")
    fichiers.ranger(str(source), "animaux", "carnet vaccin", "Rappel CHPL, prochain 2027-11", fiche="animaux/rex.md")
    assert "carnet-vaccin.pdf" in (data_tmp / "animaux" / "rex.md").read_text(encoding="utf-8")
    gitignore = data_tmp / ".gitignore"
    if gitignore.exists():  # git disponible : les binaires restent hors du dépôt data/.git
        lignes = gitignore.read_text(encoding="utf-8").splitlines()
        assert "fichiers/entrants/" in lignes and "photos/" in lignes and "secrets.env" in lignes
    assert "voitures" in memoire.RACINES_AUTORISEES and "animaux" in memoire.RACINES_AUTORISEES


def test_categorie_refusee_et_source_hors_zone(data_tmp: Path, tmp_path: Path) -> None:
    from tools import fichiers, memoire

    source = _entrant(data_tmp, "x.pdf")
    with pytest.raises(fichiers.FichierInvalide, match="Catégorie"):
        fichiers.ranger(str(source), "secret", "x", "desc")
    assert source.exists()  # rien n'a bougé
    with pytest.raises(memoire.CheminInterdit):
        fichiers.ranger(str(source), "autre", "x", "desc", fiche="../secrets.md")
    assert source.exists()
    ailleurs = tmp_path / "ailleurs.pdf"
    ailleurs.write_bytes(b"x")
    with pytest.raises(fichiers.FichierInvalide):
        fichiers.ranger(str(ailleurs), "autre", "x", "desc")
    (data_tmp / "secrets.env").write_text("X=1", encoding="utf-8")
    with pytest.raises(fichiers.FichierInvalide):
        fichiers.ranger("fichiers/entrants/../../secrets.env", "autre", "x", "desc")
    with pytest.raises(fichiers.FichierInvalide):
        fichiers.lister("inconnue")


def test_entrants(data_tmp: Path) -> None:
    from tools import fichiers

    assert fichiers.entrants() == []
    f = _entrant(data_tmp, "a.pdf", b"x" * 2048)
    ancien = datetime.fromisoformat("2026-11-04T10:00:00+01:00").timestamp()
    os.utime(f, (ancien, ancien))
    (res,) = fichiers.entrants()
    assert res["nom"] == "a.pdf" and res["age"] == "3 j" and res["taille"] == "2 Ko"
    assert res["chemin"] == str(f.resolve())


# --- photos -----------------------------------------------------------------


def test_photos_journal_reduction(data_tmp: Path) -> None:
    from PIL import Image

    from tools import fichiers

    source = _image(data_tmp / "fichiers" / "entrants" / "grande.png", (4000, 3000), "RGBA")
    res = fichiers.photos_journal_ajouter(str(source), "Première balade au parc !", "2026-07-14")
    assert res["photo"] == "photos/2026/07/2026-07-14_premiere-balade-au-parc.jpg"
    photo = data_tmp / res["photo"]
    with Image.open(photo) as img:
        assert img.format == "JPEG" and max(img.size) == 1600 and img.size == (1600, 1200)
    assert not source.exists()  # l'original quitte entrants/
    journal = (data_tmp / "foyer" / "journal" / "2026-07.md").read_text(encoding="utf-8")
    assert f"- 2026-07-14 📷 Première balade au parc ! ({res['photo']})" in journal

    # Petite image : pas d'agrandissement ; date par défaut = aujourd'hui (HOMEOS_NOW)
    petite = _image(data_tmp / "fichiers" / "entrants" / "petite.jpg", (300, 200))
    res2 = fichiers.photos_journal_ajouter(str(petite), "Le chien au soleil")
    assert res2["photo"].startswith("photos/2026/11/2026-11-07_")
    with Image.open(data_tmp / res2["photo"]) as img:
        assert img.size == (300, 200)


def test_photos_journal_fichier_invalide(data_tmp: Path) -> None:
    from tools import fichiers

    faux = _entrant(data_tmp, "pas-une-image.jpg", b"pas une image")
    with pytest.raises(fichiers.FichierInvalide):
        fichiers.photos_journal_ajouter(str(faux), "test")
    assert faux.exists()


# --- purge ------------------------------------------------------------------


def _vieillir(chemin: Path, iso: str) -> None:
    t = datetime.fromisoformat(iso).timestamp()
    os.utime(chemin, (t, t))


def test_purge_dates_simulees(data_tmp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import fichiers

    vieux = _entrant(data_tmp, "vieux.pdf")
    _vieillir(vieux, "2026-10-01T10:00:00+02:00")  # 37 jours
    recent = _entrant(data_tmp, "recent.pdf")
    _vieillir(recent, "2026-10-20T10:00:00+02:00")  # 18 jours
    captures = data_tmp / ".kd" / "captures"
    captures.mkdir(parents=True)
    cap_vieille = captures / "2026-07-01-avant.png"
    cap_vieille.write_bytes(b"x")
    _vieillir(cap_vieille, "2026-07-01T10:00:00+02:00")  # 129 jours
    cap_recente = captures / "2026-09-01-avant.png"
    cap_recente.write_bytes(b"x")
    _vieillir(cap_recente, "2026-09-01T10:00:00+02:00")  # 67 jours
    tmp = tmp_path / "tmp"
    vocal_vieux = tmp / "homeos-voice-abc"
    vocal_vieux.mkdir(parents=True)
    (vocal_vieux / "vocal.ogg").write_bytes(b"x")
    _vieillir(vocal_vieux, "2026-10-25T10:00:00+02:00")
    vocal_recent = tmp / "homeos-voice-def"
    vocal_recent.mkdir()
    _vieillir(vocal_recent, "2026-11-05T10:00:00+01:00")
    rangé = data_tmp / "fichiers" / "voiture" / "2020" / "ancien.pdf"
    rangé.parent.mkdir(parents=True)
    rangé.write_bytes(b"x")
    _vieillir(rangé, "2020-01-01T10:00:00+01:00")

    res = fichiers.purger(dossier_tmp=tmp)
    assert res == {"entrants": 1, "captures": 1, "vocaux": 1}
    assert not vieux.exists() and recent.exists()
    assert not cap_vieille.exists() and cap_recente.exists()
    assert not vocal_vieux.exists() and vocal_recent.exists()
    assert rangé.exists()  # les fichiers rangés ne sont jamais purgés

    # L'horloge simulée fait foi : 40 jours plus tard, le récent part aussi.
    monkeypatch.setenv("HOMEOS_NOW", "2026-12-17T10:00:00")
    assert fichiers.purger(dossier_tmp=tmp)["entrants"] == 1
    assert not recent.exists()


def test_runner_purge(data_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import db, runner

    vieux = _entrant(data_tmp, "vieux.pdf")
    _vieillir(vieux, "2026-09-01T10:00:00+02:00")
    assert runner.main(["purge"]) == 0
    assert not vieux.exists()
    assert '"entrants": 1' in capsys.readouterr().out
    assert any(a["kind"] == "purge" and a["status"] == "done" for a in db.recent_actions(5))
