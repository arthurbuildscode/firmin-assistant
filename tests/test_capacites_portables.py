"""Portabilité des capacités : aucune donnée personnelle dans `capabilities/<nom>/`.

- Motifs interdits lus dans HOMEOS_MOTIFS_INTERDITS (séparés par des virgules, insensibles à la casse et aux
  accents ; vide ou absent = seule la vérification des identifiants Telegram s'applique). Exemple sur le VPS :
  HOMEOS_MOTIFS_INTERDITS="Dupont,Villeneuve-sur-Lot,06 12" pytest tests/test_capacites_portables.py
- Aucun identifiant Telegram numérique en dur (telegram_id, chat_id, groupe : 5 chiffres ou plus, ou -100…).
Ce qui dépend du foyer se déclare dans `parametres:` du manifeste (valeurs dans data/capacites/<nom>/).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

import pytest

RACINE_CAPACITES = Path(__file__).resolve().parent.parent / "capabilities"
TAILLE_MAX = 2_000_000  # au-delà (ex. grille horaire GTFS), le fichier est une donnée publique générée
IGNORES = {"__pycache__", ".pytest_cache", "node_modules"}
RE_TELEGRAM = re.compile(
    r"(?:telegram_id|chat_id|groupe_telegram_id|user_id)\W{0,4}-?\d{5,}|(?<![\d.])-100\d{9,}(?!\d)",
    re.IGNORECASE,
)


def _normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte.casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


def motifs_interdits() -> list[str]:
    return [m.strip() for m in os.environ.get("HOMEOS_MOTIFS_INTERDITS", "").split(",") if m.strip()]


def fichiers_texte(dossier: Path) -> list[Path]:
    fichiers = []
    for f in sorted(dossier.rglob("*")):
        rel = f.relative_to(dossier)
        if not f.is_file() or any(p in IGNORES for p in rel.parts) or f.stat().st_size > TAILLE_MAX:
            continue
        try:
            f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binaire
        fichiers.append(f)
    return fichiers


def dossiers_capacites() -> list[Path]:
    if not RACINE_CAPACITES.is_dir():
        return []
    return sorted(d for d in RACINE_CAPACITES.iterdir() if d.is_dir() and not d.name.startswith((".", "_")))


@pytest.mark.parametrize("dossier", dossiers_capacites(), ids=lambda d: d.name)
def test_capacite_sans_donnee_personnelle(dossier: Path) -> None:
    motifs = [(m, _normaliser(m)) for m in motifs_interdits()]
    problemes: list[str] = []
    for f in fichiers_texte(dossier):
        texte = f.read_text(encoding="utf-8")
        normalise = _normaliser(texte)
        rel = f.relative_to(RACINE_CAPACITES)
        for brut, motif in motifs:
            if motif in normalise:
                problemes.append(f"{rel} : motif interdit « {brut} »")
        for m in RE_TELEGRAM.finditer(texte):
            problemes.append(f"{rel} : identifiant Telegram en dur « {m.group(0)} »")
    assert not problemes, "Données personnelles dans une capacité :\n" + "\n".join(problemes)


def test_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le contrôle lui-même : motifs (accents, casse) et identifiants Telegram."""
    monkeypatch.setenv("HOMEOS_MOTIFS_INTERDITS", " Camille , ,Rue des Lilas")
    assert motifs_interdits() == ["Camille", "Rue des Lilas"]
    assert _normaliser("CAMILLE") == _normaliser("Camille")
    assert RE_TELEGRAM.search("telegram_id: 123456789") and RE_TELEGRAM.search("chat = -1001234567890")
    assert not RE_TELEGRAM.search("version: 1\nquand: 18:00\nligne 51, arrêt 12345")
    (tmp_path / "a.bin").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "b.md").write_text("ok", encoding="utf-8")
    assert [f.name for f in fichiers_texte(tmp_path)] == ["b.md"]
