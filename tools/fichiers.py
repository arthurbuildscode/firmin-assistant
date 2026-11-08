"""Fichiers reçus (lot 3) : rangement, index, photos du journal, purge.

Arborescence (relative à `data/`) :

    fichiers/entrants/<YYYY-MM-DD>_<HHMMSS>_<prenom>_<nom>.<ext>   déposés par le bot, non rangés
    fichiers/<categorie>/<YYYY>/<nom>.<ext>                         rangés par `fichiers_ranger`
    fichiers/<categorie>/index.md                                   une ligne par fichier rangé
    fichiers/sensibles/<personne>/                                  documents sensibles chiffrés (web/sensibles.py) :
                                                                    jamais lus, listés ni déplacés par ces outils
    photos/<YYYY>/<MM>/<YYYY-MM-DD>_<slug>.jpg                      photos souvenirs réduites

Seuls les fichiers situés sous `data/fichiers/` peuvent être rangés ou réduits (jamais ailleurs).
Les fichiers binaires ne sont pas versionnés dans `data/.git` (voir `memoire.GITIGNORE`) ;
les `index.md`, les fiches et le journal le sont.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from tools import memoire
from tools.common import chemin_kd, data_dir, get_logger, now, parse_date

log = get_logger(__name__)

CATEGORIES: tuple[str, ...] = (
    "voiture",
    "animaux",
    "enfants",
    "maison",
    "sante",
    "administratif",
    "factures",
    "photos",
    "autre",
)

# Catégories renommées : l'ancien nom reste accepté (alias) et `migrer_categories` déplace les fichiers déjà rangés.
CATEGORIES_RENOMMEES: dict[str, str] = {"chien": "animaux"}

# Documents sensibles chiffrés côté client (web/sensibles.py) : `fichiers/sensibles/`, hors de portée des outils
# (ni listés, ni rangés, ni réduits, ni purgés).
DOSSIER_SENSIBLES = "sensibles"

EXTENSIONS_CONNUES: frozenset[str] = frozenset({".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})

PHOTO_MAX_PX = 1600
PHOTO_QUALITE = 85

# Purge : (sous-dossier de data/, âge maximal en jours)
PURGE_ENTRANTS_JOURS = 30
PURGE_CAPTURES_JOURS = 90
PURGE_VOCAUX_JOURS = 7
PREFIXE_VOCAUX_TMP = "homeos-voice-"  # dossiers temporaires du bot (bot/handlers.py, on_vocal)

_LIGNE_INDEX = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (.+?) · (.*)$")


class FichierInvalide(ValueError):
    """Fichier source introuvable, hors zone ou catégorie refusée."""


# --- chemins ----------------------------------------------------------------


def racine_fichiers() -> Path:
    return data_dir() / "fichiers"


def dossier_entrants() -> Path:
    return racine_fichiers() / "entrants"


def nettoyer_nom(nom: str, longueur_max: int = 60) -> str:
    """Nom de fichier sûr : sans accent, minuscules, `[a-z0-9-_]`, tirets compactés. Jamais vide."""
    texte = unicodedata.normalize("NFKD", str(nom)).encode("ascii", "ignore").decode("ascii").lower()
    texte = re.sub(r"[^a-z0-9_-]+", "-", texte)
    texte = re.sub(r"-{2,}", "-", texte).strip("-_")
    texte = texte[:longueur_max].strip("-_")
    return texte or "fichier"


def _sans_extension(nom: str) -> str:
    p = PurePosixPath(str(nom).strip())
    return p.stem if p.suffix.lower() in EXTENSIONS_CONNUES else str(nom).strip()


def _rel(chemin: Path) -> str:
    return chemin.resolve().relative_to(data_dir().resolve()).as_posix()


def _disponible(cible: Path) -> Path:
    """`cible`, ou `cible-2`, `cible-3`… si le nom est déjà pris."""
    if not cible.exists():
        return cible
    for i in range(2, 1000):
        candidat = cible.with_name(f"{cible.stem}-{i}{cible.suffix}")
        if not candidat.exists():
            return candidat
    raise FichierInvalide("Trop de fichiers portent déjà ce nom.")


def resoudre_source(chemin_source: str) -> Path:
    """Chemin absolu d'un fichier existant sous `data/fichiers/` (absolu, ou relatif à `data/`)."""
    brut = str(chemin_source or "").strip()
    if not brut or "\x00" in brut:
        raise FichierInvalide("Chemin du fichier vide.")
    p = Path(brut)
    if not p.is_absolute():
        p = data_dir() / brut.removeprefix("data/")
    cible = p.resolve()
    zone = racine_fichiers().resolve()
    if zone not in cible.parents:
        raise FichierInvalide("Seuls les fichiers reçus (dossier fichiers/) peuvent être rangés.")
    if zone / DOSSIER_SENSIBLES in (cible, *cible.parents):
        # Documents sensibles (web/sensibles.py) : chiffrés dans le navigateur, jamais touchés par un outil.
        raise FichierInvalide("Les documents sensibles ne passent jamais par l'assistant.")
    if not cible.is_file():
        raise FichierInvalide(f"Fichier introuvable : {PurePosixPath(brut).name}.")
    return cible


def verifier_categorie(categorie: str) -> str:
    cat = nettoyer_nom(categorie or "")
    cat = CATEGORIES_RENOMMEES.get(cat, cat)
    if cat not in CATEGORIES:
        raise FichierInvalide(f"Catégorie « {categorie} » refusée. Catégories possibles : {', '.join(CATEGORIES)}.")
    return cat


def _une_ligne(texte: str) -> str:
    return " ".join(str(texte or "").split())


# --- rangement --------------------------------------------------------------


def _ajouter_index(cat: str, jour: date, nom: str, description: str) -> Path:
    index = racine_fichiers() / cat / "index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        index.write_text(f"# Fichiers — {cat}\n\n_Une ligne par fichier rangé : date · nom · description._\n\n", encoding="utf-8")
    existant = index.read_text(encoding="utf-8")
    with index.open("a", encoding="utf-8") as fh:
        if existant and not existant.endswith("\n"):
            fh.write("\n")
        fh.write(f"- {jour.isoformat()} · {nom} · {description}\n")
    return index


def ranger(
    chemin_source: str,
    categorie: str,
    nouveau_nom: str,
    description: str,
    fiche: str | None = None,
) -> dict[str, Any]:
    """Déplace un fichier reçu vers `fichiers/<categorie>/<YYYY>/`, l'indexe et (option) le cite dans une fiche."""
    cat = verifier_categorie(categorie)
    desc = _une_ligne(description)
    if not desc:
        raise FichierInvalide("Description vide : dis en quelques mots ce qu'est ce fichier.")
    # Valider la fiche AVANT de déplacer quoi que ce soit.
    fiche_abs = memoire.resoudre(fiche) if fiche else None
    source = resoudre_source(chemin_source)

    extension = source.suffix.lower()
    if extension == ".jpeg":
        extension = ".jpg"
    nom = nettoyer_nom(_sans_extension(nouveau_nom or source.stem)) + extension
    jour = now().date()
    dossier = racine_fichiers() / cat / f"{jour:%Y}"
    dossier.mkdir(parents=True, exist_ok=True)
    destination = _disponible(dossier / nom)
    shutil.move(str(source), str(destination))
    nom_final = destination.name

    index = _ajouter_index(cat, jour, nom_final, desc)
    rel_fichier = _rel(destination)
    resultat: dict[str, Any] = {
        "chemin": rel_fichier,
        "chemin_absolu": str(destination.resolve()),  # pour Read : la session Claude ne tourne plus dans data/
        "categorie": cat,
        "nom": nom_final,
        "index": _rel(index),
        "fiche": None,
    }
    if fiche_abs is not None:
        lien = os.path.relpath(destination, fiche_abs.parent).replace(os.sep, "/")
        memoire.ajouter(fiche or "", f"- {jour.isoformat()} · {desc} ([{nom_final}]({lien}))")
        resultat["fiche"] = _rel(fiche_abs)
    else:
        memoire.commit("fichiers_ranger", f"{cat}/{nom_final}")
    resultat["message"] = f"Rangé dans {rel_fichier}" + (f", noté dans {resultat['fiche']}." if resultat["fiche"] else ".")
    return resultat


def lister(categorie: str | None = None, limite: int = 20) -> list[dict[str, Any]]:
    """Derniers fichiers rangés (d'après les index), du plus récent au plus ancien."""
    cats = (verifier_categorie(categorie),) if categorie else CATEGORIES
    limite = max(1, min(int(limite or 20), 200))
    entrees: list[tuple[str, int, dict[str, Any]]] = []
    for cat in cats:
        index = racine_fichiers() / cat / "index.md"
        if not index.exists():
            continue
        for rang, ligne in enumerate(index.read_text(encoding="utf-8").splitlines()):
            m = _LIGNE_INDEX.match(ligne.strip())
            if not m:
                continue
            jour, nom, desc = m.groups()
            chemin = racine_fichiers() / cat / jour[:4] / nom
            entrees.append(
                (
                    jour,
                    rang,
                    {
                        "date": jour,
                        "categorie": cat,
                        "nom": nom,
                        "description": desc,
                        "chemin": str(chemin.resolve()) if chemin.exists() else str(chemin),
                        "existe": chemin.exists(),
                    },
                )
            )
    entrees.sort(key=lambda e: (e[0], e[1]), reverse=True)
    return [e[2] for e in entrees[:limite]]


RE_NOM_FICHIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}(-[0-9]{1,3})?\.(pdf|jpg|png|heic|heif|webp)$")
RE_NOM_ENTRANT = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_([a-z0-9-]{1,20})_[a-z0-9_-]{1,80}(-[0-9]{1,3})?\.[a-z]{3,4}$")


def chemin_range(categorie: str, annee: str, nom: str) -> Path:
    """Chemin d'un fichier rangé `fichiers/<categorie>/<annee>/<nom>` (écran Fichiers de l'appli) ; lève
    `FichierInvalide` pour une catégorie, une année ou un nom hors format, ou un fichier absent."""
    cat = verifier_categorie(categorie)
    if not re.fullmatch(r"\d{4}", str(annee or "")) or not RE_NOM_FICHIER.fullmatch(str(nom or "")):
        raise FichierInvalide("Fichier introuvable.")
    chemin = (racine_fichiers() / cat / annee / nom).resolve()
    if racine_fichiers().resolve() / cat not in chemin.parents or not chemin.is_file():
        raise FichierInvalide("Fichier introuvable.")
    return chemin


def auteur_entrant(nom: str) -> str | None:
    """Prénom (nettoyé) de la personne qui a envoyé un fichier entrant, lu dans son nom ; None si hors format."""
    m = RE_NOM_ENTRANT.fullmatch(str(nom or ""))
    return m.group(1) if m else None


def entrants_de(prenom: str) -> list[dict[str, Any]]:
    """Fichiers entrants envoyés par cette personne (prénom lu dans le nom du fichier), sans chemin du serveur."""
    cible = nettoyer_nom(prenom, 20)
    return [{k: v for k, v in f.items() if k != "chemin"} for f in entrants() if auteur_entrant(f["nom"]) == cible]


def chemin_entrant_de(prenom: str, nom: str) -> Path:
    """Chemin d'un fichier entrant de cette personne ; `FichierInvalide` sinon (fichier d'une autre personne inclus)."""
    if auteur_entrant(nom) != nettoyer_nom(prenom, 20):
        raise FichierInvalide("Fichier introuvable.")
    chemin = (dossier_entrants() / nom).resolve()
    if dossier_entrants().resolve() not in chemin.parents or not chemin.is_file():
        raise FichierInvalide("Fichier introuvable.")
    return chemin


def _age(mtime: float, reference: datetime) -> float:
    return (reference.timestamp() - mtime) / 86400


def entrants() -> list[dict[str, Any]]:
    """Fichiers reçus encore dans `entrants/` (non rangés), du plus ancien au plus récent."""
    dossier = dossier_entrants()
    if not dossier.exists():
        return []
    ref = now()
    resultat: list[dict[str, Any]] = []
    for f in sorted(dossier.iterdir(), key=lambda p: p.stat().st_mtime):
        if not f.is_file() or f.name.startswith("."):
            continue
        st = f.stat()
        age_j = _age(st.st_mtime, ref)
        resultat.append(
            {
                "nom": f.name,
                "chemin": str(f.resolve()),
                "taille": taille_lisible(st.st_size),
                "age_jours": round(age_j, 1),
                "age": f"{int(age_j)} j" if age_j >= 1 else f"{max(int(age_j * 24), 0)} h",
            }
        )
    return resultat


def taille_lisible(octets: int) -> str:
    if octets < 1024:
        return f"{octets} o"
    if octets < 1024 * 1024:
        return f"{octets / 1024:.0f} Ko"
    return f"{octets / (1024 * 1024):.1f} Mo".replace(".", ",")


# --- migration des catégories renommées -------------------------------------


def _fiches_markdown() -> list[Path]:
    """Fichiers Markdown de `data/` qui peuvent citer un fichier rangé (fiches, journal), hors fichiers et `.git`."""
    d = data_dir()
    if not d.is_dir():
        return []
    exclus = {".git", "fichiers"}
    return [p for p in sorted(d.rglob("*.md")) if p.is_file() and not exclus.intersection(p.relative_to(d).parts[:1])]


def migrer_categories() -> list[str]:
    """Catégories renommées (`chien` → `animaux`) : déplace les fichiers rangés sous `fichiers/<ancienne>/` vers
    `fichiers/<nouvelle>/` (même année, nom suffixé `-2`… s'il est déjà pris), reporte les lignes de l'index et
    réécrit les liens des fiches. Idempotente : sans ancien dossier, ne fait rien. Renvoie les migrations faites."""
    faites: list[str] = []
    racine = racine_fichiers()
    for ancienne, nouvelle in CATEGORIES_RENOMMEES.items():
        source = racine / ancienne
        if not source.is_dir() or source.is_symlink():
            continue
        cible = racine / nouvelle
        renommes: dict[str, str] = {}  # "<annee>/<nom>" ancien → nouveau
        deplaces = 0
        for f in sorted(source.rglob("*")):
            if not f.is_file() or f.relative_to(source).as_posix() == "index.md":
                continue
            rel = f.relative_to(source)
            destination = _disponible(cible / rel)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(destination))
            deplaces += 1
            nouveau_rel = destination.relative_to(cible).as_posix()
            if nouveau_rel != rel.as_posix():
                renommes[rel.as_posix()] = nouveau_rel
        ancien_index = source / "index.md"
        if ancien_index.is_file():
            lignes = []
            for ligne in ancien_index.read_text(encoding="utf-8").splitlines():
                m = _LIGNE_INDEX.match(ligne.strip())
                if not m:
                    continue
                jour, nom, desc = m.groups()
                nom = PurePosixPath(renommes.get(f"{jour[:4]}/{nom}", f"{jour[:4]}/{nom}")).name
                lignes.append(f"- {jour} · {nom} · {desc}")
            for ligne in lignes:
                _ajouter_index_ligne(nouvelle, ligne)
            ancien_index.unlink()
        for fiche in _fiches_markdown():
            texte = fiche.read_text(encoding="utf-8")
            if f"fichiers/{ancienne}/" not in texte:
                continue
            for avant, apres in renommes.items():
                texte = texte.replace(f"fichiers/{ancienne}/{avant}", f"fichiers/{nouvelle}/{apres}")
            fiche.write_text(texte.replace(f"fichiers/{ancienne}/", f"fichiers/{nouvelle}/"), encoding="utf-8")
        for dossier in sorted((p for p in source.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                dossier.rmdir()
            except OSError:
                pass
        try:
            source.rmdir()
        except OSError:
            log.warning("migration : fichiers/%s/ non vide après déplacement, laissé en place", ancienne)
        faites.append(f"fichiers {ancienne} → {nouvelle} ({deplaces} fichier{'s' if deplaces > 1 else ''})")
        memoire.commit("migration", f"catégorie {ancienne} → {nouvelle}")
    return faites


def _ajouter_index_ligne(cat: str, ligne: str) -> None:
    """Ajoute une ligne déjà formatée à l'index de la catégorie (créé avec son en-tête s'il manque)."""
    index = racine_fichiers() / cat / "index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        index.write_text(f"# Fichiers — {cat}\n\n_Une ligne par fichier rangé : date · nom · description._\n\n", encoding="utf-8")
    existant = index.read_text(encoding="utf-8")
    if ligne in existant.splitlines():
        return
    with index.open("a", encoding="utf-8") as fh:
        if existant and not existant.endswith("\n"):
            fh.write("\n")
        fh.write(ligne + "\n")


# --- photos du journal ------------------------------------------------------


def _ouvrir_image(chemin: Path):  # -> PIL.Image.Image
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise FichierInvalide("La réduction des photos n'est pas disponible (Pillow non installé).") from exc
    if chemin.suffix.lower() in (".heic", ".heif"):
        try:
            from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

            register_heif_opener()
        except ImportError as exc:
            raise FichierInvalide("Les photos HEIC ne sont pas lisibles sur ce serveur : renvoie-la en JPG.") from exc
    try:
        image = Image.open(chemin)
        image.load()
    except Exception as exc:
        raise FichierInvalide("Ce fichier n'est pas une image lisible.") from exc
    return ImageOps.exif_transpose(image) or image


def reduire_photo(source: Path, destination: Path, max_px: int = PHOTO_MAX_PX, qualite: int = PHOTO_QUALITE) -> tuple[int, int]:
    """Réduit `source` (côté max `max_px`) en JPEG `qualite` vers `destination`. Renvoie (largeur, hauteur)."""
    image = _ouvrir_image(source)
    if image.mode not in ("RGB", "L"):
        from PIL import Image

        if "A" in image.getbands():
            fond = Image.new("RGB", image.size, (255, 255, 255))
            fond.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
            image = fond
        else:
            image = image.convert("RGB")
    image.thumbnail((max_px, max_px))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Écriture atomique : fichier temporaire dans le même dossier puis renommage.
    fd, tmp = tempfile.mkstemp(dir=destination.parent, suffix=".jpg.tmp")
    os.close(fd)
    try:
        image.save(tmp, "JPEG", quality=qualite, optimize=True)
        os.chmod(tmp, 0o664 & ~_umask())
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return image.size


def _umask() -> int:
    courant = os.umask(0)
    os.umask(courant)
    return courant


def photos_journal_ajouter(chemin_source: str, legende: str, date_photo: str | None = None) -> dict[str, Any]:
    """Réduit la photo vers `photos/<YYYY>/<MM>/` et ajoute une ligne au journal du mois de la photo."""
    texte = _une_ligne(legende)
    if not texte:
        raise FichierInvalide("Légende vide : dis en quelques mots ce que montre la photo.")
    source = resoudre_source(chemin_source)
    jour = parse_date(date_photo) if date_photo else now().date()
    dossier = data_dir() / "photos" / f"{jour:%Y}" / f"{jour:%m}"
    destination = _disponible(dossier / f"{jour.isoformat()}_{nettoyer_nom(texte, 40)}.jpg")
    largeur, hauteur = reduire_photo(source, destination)
    rel_photo = _rel(destination)

    journal_rel = f"foyer/journal/{jour:%Y-%m}.md"
    cible = memoire.resoudre(journal_rel)
    if not cible.exists():
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(f"# Journal {jour:%Y-%m}\n\n", encoding="utf-8")
    memoire.ajouter(journal_rel, f"- {jour.isoformat()} 📷 {texte} ({rel_photo})")

    # L'original quitte la zone d'attente : la version réduite fait foi.
    if dossier_entrants().resolve() in source.parents:
        try:
            source.unlink()
        except OSError:
            log.warning("original non supprimé : %s", source.name)
    return {
        "photo": rel_photo,
        "chemin_absolu": str(destination.resolve()),
        "dimensions": f"{largeur}x{hauteur}",
        "journal": journal_rel,
        "message": f"Photo ajoutée au journal de {jour:%m/%Y} ({rel_photo}).",
    }


# --- purge ------------------------------------------------------------------


def _purger_dossier(dossier: Path, jours: int, reference: datetime) -> int:
    """Supprime les fichiers de `dossier` (récursif) plus vieux que `jours`. Renvoie le nombre supprimé."""
    if not dossier.is_dir():
        return 0
    n = 0
    for f in sorted(dossier.rglob("*")):
        try:
            if (f.is_file() or f.is_symlink()) and _age(f.lstat().st_mtime, reference) > jours:
                f.unlink()
                n += 1
        except OSError as exc:
            log.warning("purge : %s non supprimé (%s)", f.name, exc.__class__.__name__)
    return n


def purger(reference: datetime | None = None, dossier_tmp: Path | None = None) -> dict[str, int]:
    """Purge : entrants > 30 j, captures Kitchen Daily > 90 j, vocaux temporaires > 7 j. Horloge : `now()` (HOMEOS_NOW)."""
    ref = reference or now()
    d = data_dir()
    resultat = {
        "entrants": _purger_dossier(d / "fichiers" / "entrants", PURGE_ENTRANTS_JOURS, ref),
        "captures": _purger_dossier(chemin_kd() / "captures", PURGE_CAPTURES_JOURS, ref),
        "vocaux": _purger_dossier(d / "fichiers" / "vocaux", PURGE_VOCAUX_JOURS, ref),
    }
    # Dossiers temporaires de vocaux laissés par un arrêt brutal du bot.
    tmp = dossier_tmp or Path(tempfile.gettempdir())
    try:
        candidats = list(tmp.glob(f"{PREFIXE_VOCAUX_TMP}*"))
    except OSError:
        candidats = []
    for dossier in candidats:
        try:
            if dossier.is_dir() and _age(dossier.stat().st_mtime, ref) > PURGE_VOCAUX_JOURS:
                shutil.rmtree(dossier)
                resultat["vocaux"] += 1
        except OSError as exc:
            log.warning("purge : %s non supprimé (%s)", dossier.name, exc.__class__.__name__)
    return resultat


if __name__ == "__main__":  # python -m tools.fichiers migrer (install.sh, scripts/appliquer-config.sh)
    import sys

    if sys.argv[1:] != ["migrer"]:
        print("usage : python -m tools.fichiers migrer", file=sys.stderr)
        raise SystemExit(2)
    for fait in migrer_categories():
        print(fait)
