"""Fichiers reçus (photos, documents), indépendamment du canal : nommage, formats acceptés, HEIC, prompt.

Extrait de ``bot/handlers.py`` au lot 4a (le bot réexporte ces noms) ; utilisé aussi par ``POST /api/fichiers``.
Les fichiers sont rangés dans ``data/fichiers/entrants/`` puis confiés à Claude avec une ligne
``[fichier reçu: <chemin absolu>]``.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

TAILLE_MAX_FICHIER = 20 * 1024 * 1024  # limite de téléchargement de l'API bot Telegram
EXTENSIONS_ACCEPTEES = {
    ".pdf": ".pdf",
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".png": ".png",
    ".heic": ".heic",
    ".heif": ".heic",
}
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heic",
}
CONSIGNE_FICHIER = "Lis ce fichier, dis en une ligne ce que c'est, extrais ce qui compte, propose où le ranger."
CONSIGNE_ALBUM = "Lis ces fichiers, dis en une ligne ce que c'est, extrais ce qui compte, propose où les ranger."


def nettoyer_nom_fichier(nom: str, longueur_max: int = 50) -> str:
    """Nom sûr pour le disque : sans accent, minuscules, ``[a-z0-9-_]``. Jamais vide."""
    import unicodedata

    texte = unicodedata.normalize("NFKD", nom or "").encode("ascii", "ignore").decode("ascii").lower()
    texte = re.sub(r"[^a-z0-9_-]+", "-", texte)
    texte = re.sub(r"-{2,}", "-", texte).strip("-_")
    return texte[:longueur_max].strip("-_") or "fichier"


def chemin_entrant(
    dossier: Path, prenom: str, nom_original: str, extension: str, quand: datetime | None = None
) -> Path:
    """``<dossier>/<YYYY-MM-DD>_<HHMMSS>_<prenom>_<nom-nettoyé>.<ext>``, suffixé ``-2``, ``-3``… si déjà pris."""
    quand = quand or datetime.now().astimezone()
    base = f"{quand:%Y-%m-%d_%H%M%S}_{nettoyer_nom_fichier(prenom, 20)}_{nettoyer_nom_fichier(Path(nom_original).stem)}"
    cible = dossier / f"{base}{extension}"
    i = 2
    while cible.exists():
        cible = dossier / f"{base}-{i}{extension}"
        i += 1
    return cible


def extension_document(nom: str | None, mime: str | None) -> str | None:
    """Extension normalisée d'un document accepté (pdf, jpg, png, heic), sinon ``None``."""
    ext = Path(nom or "").suffix.lower()
    if ext in EXTENSIONS_ACCEPTEES:
        return EXTENSIONS_ACCEPTEES[ext]
    return MIME_EXTENSIONS.get((mime or "").lower())


def taille_lisible(octets: int) -> str:
    if octets < 1024 * 1024:
        return f"{max(octets // 1024, 1)} Ko"
    return f"{octets / (1024 * 1024):.1f} Mo".replace(".", ",")


def convertir_heic(chemin: Path) -> Path:
    """Convertit un HEIC en JPG (pillow-heif) et supprime l'original. Lève ``RuntimeError`` si impossible."""
    try:
        from PIL import Image
        from pillow_heif import register_heif_opener  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("heic-indisponible") from exc
    register_heif_opener()
    cible = chemin.with_suffix(".jpg")
    i = 2
    while cible.exists():
        cible = chemin.with_name(f"{chemin.stem}-{i}.jpg")
        i += 1
    with Image.open(chemin) as img:
        img.convert("RGB").save(cible, "JPEG", quality=90)
    chemin.unlink(missing_ok=True)
    return cible


def construire_prompt_fichiers(chemins: list[Path], legende: str) -> str:
    """Lignes ``[fichier reçu: <chemin absolu>]`` + légende, ou la consigne par défaut sans légende."""
    lignes = [f"[fichier reçu: {c}]" for c in chemins]
    legende = legende.strip()
    if legende:
        lignes[-1] = f"{lignes[-1]} {legende}"
        return "\n".join(lignes)
    lignes.append(CONSIGNE_FICHIER if len(chemins) == 1 else CONSIGNE_ALBUM)
    return "\n".join(lignes)
