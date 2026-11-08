"""Journal du foyer : entrées datées dans `data/foyer/journal/YYYY-MM.md`."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from tools import memoire
from tools.common import data_dir, now, who

_ENTREE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\*\* \((.*?)\) ?: (.*)$")
# Ligne d'une photo souvenir (tools.fichiers.photos_journal_ajouter) : « - 2026-11-07 📷 légende (photos/…jpg) ».
_PHOTO = re.compile(r"^- (\d{4}-\d{2}-\d{2}) 📷 (.*?) \((photos/\d{4}/\d{2}/[A-Za-z0-9._-]+\.jpg)\)$")
RE_MOIS = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def chemin_du_mois(d: date | None = None) -> str:
    d = d or now().date()
    return f"foyer/journal/{d:%Y-%m}.md"


def ajouter(texte: str, auteur: str | None = None) -> str:
    """Ajoute une entrée horodatée au journal du mois courant."""
    texte = " ".join(texte.strip().split())
    if not texte:
        raise ValueError("Entrée vide.")
    horodatage = now().strftime("%Y-%m-%d %H:%M")
    ligne = f"- **{horodatage}** ({auteur or who()}) : {texte}"
    rel = chemin_du_mois()
    cible = memoire.resoudre(rel)
    if not cible.exists():
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(f"# Journal {now():%B %Y}\n\n", encoding="utf-8")
    with cible.open("a", encoding="utf-8") as fh:
        fh.write(ligne + "\n")
    memoire.commit("journal_ajouter", texte[:60])
    return f"Entrée ajoutée au journal ({rel})."


def dernieres_entrees(n: int = 5) -> list[dict[str, Any]]:
    """Les `n` dernières entrées (mois courant puis précédents), de la plus récente à la plus ancienne."""
    dossier = data_dir() / "foyer" / "journal"
    if not dossier.exists():
        return []
    entrees: list[dict[str, Any]] = []
    for fichier in sorted(dossier.glob("*.md"), reverse=True):
        lignes = fichier.read_text(encoding="utf-8").splitlines()
        for ligne in reversed(lignes):
            m = _ENTREE.match(ligne.strip())
            if m:
                entrees.append({"quand": m.group(1), "qui": m.group(2), "texte": m.group(3)})
            elif ligne.strip().startswith("- "):
                entrees.append({"quand": None, "qui": None, "texte": ligne.strip()[2:]})
            if len(entrees) >= n:
                return entrees
    return entrees


def mois_disponibles() -> list[str]:
    """Mois (AAAA-MM) qui ont un journal, du plus récent au plus ancien."""
    dossier = data_dir() / "foyer" / "journal"
    if not dossier.exists():
        return []
    return sorted((f.stem for f in dossier.glob("*.md") if RE_MOIS.fullmatch(f.stem)), reverse=True)


def entrees_du_mois(mois: str) -> list[dict[str, Any]]:
    """Entrées du journal d'un mois (AAAA-MM), dans l'ordre du fichier (chronologique) : ``quand``, ``qui``,
    ``texte`` et, pour une photo souvenir, ``photo`` (chemin relatif à data/, ``photos/AAAA/MM/…jpg``)."""
    if not RE_MOIS.fullmatch(str(mois or "")):
        raise ValueError("Mois attendu au format AAAA-MM.")
    fichier = data_dir() / "foyer" / "journal" / f"{mois}.md"
    if not fichier.exists():
        return []
    entrees: list[dict[str, Any]] = []
    for ligne in fichier.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        m = _ENTREE.match(ligne)
        if m:
            entrees.append({"quand": m.group(1), "qui": m.group(2), "texte": m.group(3)})
            continue
        p = _PHOTO.match(ligne)
        if p:
            entrees.append({"quand": p.group(1), "qui": None, "texte": p.group(2), "photo": p.group(3)})
            continue
        if ligne.startswith("- "):
            entrees.append({"quand": None, "qui": None, "texte": ligne[2:]})
    return entrees
