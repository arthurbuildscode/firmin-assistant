"""Mémoire Markdown du foyer : lecture/écriture restreinte + commit git automatique.

Chemins autorisés (relatifs à `data/`) : `foyer/`, `personnes/`, `alimentation/`, `animaux/`, `voitures/`.
Jamais de `..`, jamais de chemin absolu, extension `.md` uniquement.
Le dépôt `data/.git` est séparé du dépôt du code ; git absent ⇒ on continue sans commit.

Droits par personne (écran Mémoire de l'appli, `GET/PUT /api/memoire/fiche`, et outils `memoire_*` pour les
dossiers privés) : :func:`niveau` — **famille** (vu et modifié par tous les adultes : `foyer/`, `animaux/`,
`voitures/`, `personnes/enfants.md`, `alimentation/foyer.md`…), **personnel** (`personnes/<moi>.md`,
`alimentation/<moi>.md` : la personne seulement) ; refusé : la fiche personnelle d'un autre adulte et tout dossier
`prive/` (notes privées, lues seulement par la session Claude de leur propriétaire, CONTRACT §3).
"""

from __future__ import annotations

import shutil
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from tools.common import data_dir, get_logger, load_config, now, prenom_personne

log = get_logger(__name__)

RACINES_AUTORISEES: tuple[str, ...] = ("foyer", "personnes", "alimentation", "animaux", "voitures")

# Lignes garanties dans data/.gitignore : secrets et bases hors git, fichiers binaires reçus hors git
# (les index.md des fichiers rangés restent versionnés).
GITIGNORE: tuple[str, ...] = (
    "secrets.env",
    "secrets.env.*",
    ".cwd/",
    ".derniere-*",
    ".restic-*",
    ".healthcheck-*",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    ".cache/",
    "sessions.json",
    "config.yaml.bak-*",
    "administration/.verrou",
    "google-token.json",
    "google-oauth.json",
    ".kd/",
    "coffre.json",
    ".coffre.lock",
    ".coffre.json.*.tmp",
    "vaultwarden/",
    "fichiers/entrants/",
    "fichiers/sensibles/",
    "fichiers/vocaux/",
    "fichiers/*/[0-9][0-9][0-9][0-9]/",
    "photos/",
)

EN_TETES: dict[str, str] = {
    "foyer/identite.md": "# Identité du foyer\n\n_Qui nous sommes, rempli lors de la première séance d'onboarding._\n",
    "foyer/regles.md": "# Règles de l'assistant\n\n_Niveaux d'action : libre / à confirmer / interdit. Tutoiement par défaut._\n",
    "foyer/contacts.md": "# Contacts utiles\n\n_Prestataires, numéros d'urgence, voisins…_\n",
    "foyer/infos-pratiques.md": "# Infos pratiques\n\n_Codes, horaires, routines, où sont les choses…_\n",
    "personnes/enfants.md": "# Les enfants\n\n_Goûts, préférences, rythmes._\n",
    "alimentation/foyer.md": "# Alimentation — foyer\n\n_Habitudes communes, budget, organisation des repas._\n",
    "alimentation/enfants.md": "# Alimentation — enfants\n",
    "alimentation/historique.md": "# Historique des repas\n\n_Une ligne par repas ou par semaine._\n",
}


class CheminInterdit(ValueError):
    """Chemin hors des dossiers autorisés ou mal formé."""


def resoudre(chemin: str) -> Path:
    """Valide un chemin relatif et renvoie le chemin absolu dans `data/`.

    Lève `CheminInterdit` si le chemin sort des racines autorisées.
    """
    brut = str(chemin).strip().replace("\\", "/")
    brut = brut.removeprefix("data/")
    if not brut:
        raise CheminInterdit("Chemin vide.")
    if brut.startswith(("/", "~")) or (len(brut) > 1 and brut[1] == ":"):
        raise CheminInterdit("Chemin absolu interdit : indique un chemin relatif comme foyer/identite.md.")
    pure = PurePosixPath(brut)
    parties = pure.parts
    if any(p in ("..", ".") for p in parties) or "\x00" in brut:
        raise CheminInterdit("Chemin interdit : « .. » et « . » ne sont pas autorisés.")
    if parties[0] not in RACINES_AUTORISEES:
        raise CheminInterdit(
            f"Dossier « {parties[0]} » non autorisé. Dossiers possibles : {', '.join(RACINES_AUTORISEES)}."
        )
    if len(parties) < 2:
        raise CheminInterdit("Indique un fichier, pas un dossier (ex. foyer/identite.md).")
    if pure.suffix.lower() != ".md":
        raise CheminInterdit("Seuls les fichiers Markdown (.md) sont acceptés.")
    base = data_dir().resolve()
    cible = (base / pure).resolve()
    if base not in cible.parents:
        raise CheminInterdit("Chemin hors de la zone autorisée.")
    return cible


def _relatif(cible: Path) -> str:
    return cible.relative_to(data_dir().resolve()).as_posix()


def _en_tete_par_defaut(rel: str) -> str:
    if rel in EN_TETES:
        return EN_TETES[rel]
    # Fiches des personnes : prénom lu dans config.yaml (`personnes[].linux_user` → `prenom`), jamais en dur.
    pure = PurePosixPath(rel)
    if len(pure.parts) == 2 and pure.parts[0] in ("personnes", "alimentation"):
        prenom = prenom_personne(pure.stem)
        if prenom:
            if pure.parts[0] == "personnes":
                return f"# {prenom}\n\n_Goûts, préférences, habitudes, voix._\n"
            return f"# Alimentation — {prenom}\n"
    nom = PurePosixPath(rel).stem.replace("-", " ").replace("_", " ").capitalize()
    return f"# {nom}\n\n_Créé automatiquement le {now().strftime('%d/%m/%Y')}._\n"


def _assurer(cible: Path) -> bool:
    """Crée le fichier avec son en-tête s'il manque ; renvoie True s'il a été créé."""
    if cible.exists():
        return False
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(_en_tete_par_defaut(_relatif(cible)), encoding="utf-8")
    return True


# --- git --------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        return subprocess.run(
            [exe, "-C", str(data_dir()), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git indisponible : %s", exc.__class__.__name__)
        return None


def _assurer_gitignore(gitignore: Path) -> None:
    """Ajoute à data/.gitignore les lignes de `GITIGNORE` qui manquent (sans toucher aux autres)."""
    try:
        existant = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        presentes = {ligne.strip() for ligne in existant.splitlines()}
        manquantes = [ligne for ligne in GITIGNORE if ligne not in presentes]
        if manquantes:
            prefixe = "" if not existant or existant.endswith("\n") else "\n"
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.write(prefixe + "\n".join(manquantes) + "\n")
    except OSError as exc:
        log.warning(".gitignore non mis à jour : %s", exc.__class__.__name__)


def commit(outil: str, resume: str) -> bool:
    """`git -C data add -A && git commit -qm "homeos: <outil> <résumé>"`. N'échoue jamais."""
    d = data_dir()
    if not (d / ".git").exists():
        r = _git("init", "-q")
        if r is None or r.returncode != 0:
            log.info("dépôt data/.git non initialisé (git absent ?) — commit ignoré")
            return False
        _git("config", "user.email", "homeos@localhost")
        _git("config", "user.name", "homeos")
    _assurer_gitignore(d / ".gitignore")
    r = _git("add", "-A")
    if r is None or r.returncode != 0:
        return False
    message = f"homeos: {outil} {resume}".strip()[:200]
    r = _git("-c", "user.email=homeos@localhost", "-c", "user.name=homeos", "commit", "-qm", message)
    if r is None:
        return False
    if r.returncode != 0:
        # Rien à committer n'est pas une erreur
        if "nothing to commit" not in (r.stdout + r.stderr):
            log.info("commit git ignoré (%s)", (r.stderr or r.stdout).strip()[:120])
        return False
    return True


# --- outils -----------------------------------------------------------------


def lire(chemin: str) -> str:
    """Contenu du fichier ; créé avec un en-tête (et commité) s'il manque."""
    cible = resoudre(chemin)
    if _assurer(cible):
        commit("memoire_lire", f"création {_relatif(cible)}")
    return cible.read_text(encoding="utf-8")


def ecrire(chemin: str, contenu: str) -> str:
    """Remplace tout le contenu du fichier."""
    cible = resoudre(chemin)
    cible.parent.mkdir(parents=True, exist_ok=True)
    texte = contenu if contenu.endswith("\n") else contenu + "\n"
    cible.write_text(texte, encoding="utf-8")
    rel = _relatif(cible)
    commit("memoire_ecrire", rel)
    return f"{rel} enregistré ({len(texte.splitlines())} lignes)."


def ajouter(chemin: str, ligne: str) -> str:
    """Ajoute une ligne en fin de fichier (créé avec en-tête si besoin)."""
    cible = resoudre(chemin)
    _assurer(cible)
    texte = ligne.rstrip("\n")
    if not texte.strip():
        raise ValueError("Ligne vide : rien à ajouter.")
    with cible.open("a", encoding="utf-8") as fh:
        existant = cible.read_text(encoding="utf-8") if cible.stat().st_size else ""
        if existant and not existant.endswith("\n"):
            fh.write("\n")
        fh.write(texte + "\n")
    rel = _relatif(cible)
    commit("memoire_ajouter", rel)
    return f"Ligne ajoutée à {rel}."


# --- droits par personne (appli) ---------------------------------------------------------------------------------

NIVEAU_FAMILLE = "famille"
NIVEAU_PERSONNEL = "personnel"


def _slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii").casefold()
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in t).strip("-")


def _noms_personnes(config: dict[str, Any]) -> dict[str, set[str]]:
    """linux_user → noms de fichier possibles de ses fiches personnelles (linux_user, prénom sans accent)."""
    noms: dict[str, set[str]] = {}
    for p in config.get("personnes") or []:
        if isinstance(p, dict) and p.get("linux_user"):
            u = str(p["linux_user"])
            noms[u] = {u, _slug(u)} | ({_slug(str(p["prenom"]))} if p.get("prenom") else set())
    return noms


def niveau(chemin: str, linux_user: str, config: dict[str, Any] | None = None) -> str | None:
    """Niveau d'une fiche pour `linux_user` : `famille`, `personnel` (la sienne), ou None (refusée : fiche
    personnelle d'un autre adulte, dossier `prive/`). Lève `CheminInterdit` pour un chemin hors mémoire."""
    cible = resoudre(chemin)
    parties = PurePosixPath(_relatif(cible)).parts
    if "prive" in parties:
        return None
    if parties[0] not in ("personnes", "alimentation"):
        return NIVEAU_FAMILLE
    noms = _noms_personnes(load_config() if config is None else config)
    moi = noms.get(linux_user, {linux_user})
    autres = set().union(*(v for k, v in noms.items() if k != linux_user)) - moi if noms else set()
    proprietaire = parties[1] if len(parties) > 2 else PurePosixPath(parties[1]).stem
    if proprietaire in moi:
        return NIVEAU_PERSONNEL
    if proprietaire in autres or len(parties) > 2:
        return None
    return NIVEAU_FAMILLE


def verifier_prive(chemin: str, linux_user: str | None) -> None:
    """Refuse (`CheminInterdit`) un chemin du dossier `prive/` d'une autre personne (outils `memoire_*`)."""
    parties = PurePosixPath(str(chemin).strip().replace("\\", "/").removeprefix("data/")).parts
    if "prive" in parties and (len(parties) < 3 or parties[0] != "personnes" or parties[1] != linux_user):
        raise CheminInterdit("Ces notes privées ne sont pas les tiennes.")


def _titre(cible: Path) -> str:
    try:
        with cible.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.startswith("# "):
                    return ligne[2:].strip()
    except OSError:
        pass
    return cible.stem.replace("-", " ").replace("_", " ").capitalize()


def fiches(linux_user: str, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fiches Markdown existantes que `linux_user` peut lire (hors journal, qui a son propre écran) :
    `chemin`, `titre`, `niveau`, `maj`, `taille`, triées par dossier puis chemin."""
    cfg = load_config() if config is None else config
    base = data_dir().resolve()
    sortie: list[dict[str, Any]] = []
    for racine in RACINES_AUTORISEES:
        dossier = base / racine
        if not dossier.is_dir():
            continue
        for f in sorted(dossier.rglob("*.md")):
            rel = f.relative_to(base).as_posix()
            if rel.startswith("foyer/journal/") or "prive" in PurePosixPath(rel).parts or f.is_symlink():
                continue
            try:
                n = niveau(rel, linux_user, cfg)
            except CheminInterdit:
                continue
            if n is None:
                continue
            st = f.stat()
            sortie.append({"chemin": rel, "titre": _titre(f), "niveau": n, "taille": st.st_size,
                           "maj": now().fromtimestamp(st.st_mtime, now().tzinfo).isoformat(timespec="seconds")})
    return sortie
