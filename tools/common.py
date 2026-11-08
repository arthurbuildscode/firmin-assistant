"""Fonctions partagées : chemins, configuration, secrets, horloge, journalisation.

Règles :
- `stdout` est réservé au protocole MCP : toute journalisation va vers `stderr`.
- Les secrets vivent hors des données, dans `/etc/homeos/*.env` (`HOMEOS_SECRETS`) : un fichier par usage,
  lisible seulement par qui en a besoin (lot 7, outils en http : tout par root ou homeos, plus rien par les
  personnes ; stdio : `ical.env` par le groupe foyer). Les outils ne lisent que les URLs iCal (`ICAL_*`) et
  `COFFRE_CLE` ; `outils-jetons.env` n'est jamais chargé par `load_secrets` ; rien de ces fichiers ne doit
  transiter dans une sortie d'outil. `data/secrets.env` n'est plus lu qu'en repli
  (VPS non migré, développement).
- L'état sensible (session Kitchen Daily, coffre chiffré, sessions du bot) vit dans `/var/lib/homeos`
  (`HOMEOS_ETAT`), hors des dossiers que la session Claude peut lire.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

DEFAULT_ROOT = Path("/srv/home-os")
DEFAULT_SECRETS = Path("/etc/homeos")
DEFAULT_ETAT = Path("/var/lib/homeos")
# Proxy sortant filtrant (install.sh --filtrer-sortant) : fichier lisible par tous, sans secret.
FICHIER_PROXY = Path("/etc/default/homeos-proxy")
VARIABLES_PROXY = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")

_LOG_CONFIGURED = False


def setup_logging(level: int | None = None) -> None:
    """Configure la journalisation vers stderr (idempotent)."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    niveau = level if level is not None else getattr(logging, os.environ.get("HOMEOS_LOG", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stderr,
        level=niveau,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def root_dir() -> Path:
    """Racine du dépôt (HOMEOS_ROOT, sinon /srv/home-os, sinon le parent de ce package)."""
    env = os.environ.get("HOMEOS_ROOT")
    if env:
        return Path(env)
    if DEFAULT_ROOT.exists():
        return DEFAULT_ROOT
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Répertoire des données runtime (HOMEOS_DATA, sinon <root>/data). Créé si absent."""
    env = os.environ.get("HOMEOS_DATA")
    chemin = Path(env) if env else root_dir() / "data"
    chemin.mkdir(parents=True, exist_ok=True)
    return chemin


def secrets_dir() -> Path:
    """Dossier des secrets (HOMEOS_SECRETS, défaut /etc/homeos)."""
    env = os.environ.get("HOMEOS_SECRETS")
    return Path(env) if env else DEFAULT_SECRETS


def etat_pour(donnees: Path) -> Path:
    """Dossier d'état associé à un dossier de données (voir `etat_dir`)."""
    env = os.environ.get("HOMEOS_ETAT")
    if env:
        return Path(env)
    if os.path.abspath(donnees) == str(DEFAULT_ROOT / "data") and DEFAULT_ETAT.is_dir():
        return DEFAULT_ETAT
    return Path(donnees)


def etat_dir() -> Path:
    """État sensible hors de `data/` : HOMEOS_ETAT, sinon /var/lib/homeos pour l'installation standard.

    Pour un dossier de données personnalisé (tests, développement) sans HOMEOS_ETAT, l'état reste dans
    `data/` (ancienne disposition) : jamais de lecture accidentelle du vrai /var/lib/homeos.
    """
    return etat_pour(data_dir())


def etat_separe() -> bool:
    """Vrai si l'état sensible est hors de `data/` (disposition du lot 5d)."""
    return os.path.abspath(etat_dir()) != os.path.abspath(data_dir())


def chemin_kd() -> Path:
    """Dossier de l'état Kitchen Daily (session, captures) : `<etat>/kd`, ou `data/.kd` sans état séparé."""
    return etat_dir() / "kd" if etat_separe() else data_dir() / ".kd"


def content_dir() -> Path:
    env = os.environ.get("HOMEOS_CONTENT")
    return Path(env) if env else root_dir() / "content"


def who() -> str:
    """Auteur de l'action : HOMEOS_WHO (CLI, tests), sinon la personne de la requête HTTP en cours (serveur
    d'outils du lot 7, `tools.identite`), sinon « assistant »."""
    env = os.environ.get("HOMEOS_WHO", "").strip()
    if env:
        return env
    from tools import identite  # import tardif : identite importe ce module

    return identite.auteur_requete() or "assistant"


# --- serveur d'outils (lot 7) -------------------------------------------------------------------------------------

# Mode des outils MCP (lu par le wrapper homeos-claude, le bot, migrer-secrets.sh, le healthcheck) : sans secret.
FICHIER_OUTILS = Path("/etc/default/homeos-outils")
MODES_OUTILS = ("stdio", "http")
URL_OUTILS_DEFAUT = "http://127.0.0.1:8090/mcp"
# Jetons des personnes (`JETON_<linux_user>=…`, 640 root:homeos) : lus par le serveur d'outils et le bot seulement.
FICHIER_JETONS = "outils-jetons.env"


def reglages_outils() -> dict[str, str]:
    """`HOMEOS_OUTILS` (stdio | http) et `HOMEOS_OUTILS_URL` : environnement, sinon /etc/default/homeos-outils."""
    fichier = Path(os.environ.get("HOMEOS_FICHIER_OUTILS", str(FICHIER_OUTILS)))
    lus = _lire_env(fichier)
    mode = (os.environ.get("HOMEOS_OUTILS") or lus.get("HOMEOS_OUTILS") or "stdio").strip().lower()
    url = (os.environ.get("HOMEOS_OUTILS_URL") or lus.get("HOMEOS_OUTILS_URL") or URL_OUTILS_DEFAUT).strip()
    return {"mode": mode if mode in MODES_OUTILS else "stdio", "url": url}


def mode_outils() -> str:
    """« http » si le serveur d'outils persistant est en service, « stdio » sinon (défaut, développement, repli)."""
    return reglages_outils()["mode"]


def now() -> datetime:
    """Horloge locale Europe/Paris (surchargeable par HOMEOS_NOW pour les tests)."""
    forced = os.environ.get("HOMEOS_NOW")
    if forced:
        dt = datetime.fromisoformat(forced)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def load_config() -> dict[str, Any]:
    """Lit data/config.yaml ; renvoie {} si absent ou illisible."""
    chemin = data_dir() / "config.yaml"
    if not chemin.exists():
        return {}
    try:
        import yaml  # import local : dépendance optionnelle au chargement du module

        with chemin.open("r", encoding="utf-8") as fh:
            donnees = yaml.safe_load(fh) or {}
        return donnees if isinstance(donnees, dict) else {}
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).warning("config.yaml illisible : %s", exc.__class__.__name__)
        return {}


# --- gabarits des prompts (lot 6 : rien du foyer en dur, tout vient de config.yaml) ---------------------------

# Prénom de l'assistant quand `assistant.nom` est absent de config.yaml (l'installateur le demande toujours).
NOM_ASSISTANT_DEFAUT = "Firmin"
ROLES_ADULTES = ("proprietaire", "membre")


def nom_assistant(config: dict[str, Any] | None = None) -> str:
    """`assistant.nom` de config.yaml, sinon `NOM_ASSISTANT_DEFAUT`."""
    cfg = load_config() if config is None else config
    assistant = cfg.get("assistant") if isinstance(cfg, dict) else None
    nom = str((assistant or {}).get("nom") or "").strip() if isinstance(assistant, dict) else ""
    return nom or NOM_ASSISTANT_DEFAUT


def enumerer(elements: list[str]) -> str:
    """« Alex », « Alex et Camille », « Alex, Camille et Sam »."""
    elements = [e for e in elements if e]
    if len(elements) <= 1:
        return "".join(elements)
    return ", ".join(elements[:-1]) + " et " + elements[-1]


def prenoms_adultes(config: dict[str, Any] | None = None) -> str:
    """Prénoms des adultes du foyer (personnes de rôle propriétaire ou membre), « les adultes du foyer » à défaut."""
    cfg = load_config() if config is None else config
    prenoms: list[str] = []
    for p in (cfg.get("personnes") if isinstance(cfg, dict) else None) or []:
        if not isinstance(p, dict):
            continue
        role = str(p.get("role") or "proprietaire").strip().lower().replace("é", "e").replace("è", "e")
        prenom = str(p.get("prenom") or "").strip()
        if role in ROLES_ADULTES and prenom and prenom not in prenoms:
            prenoms.append(prenom)
    return enumerer(prenoms) or "les adultes du foyer"


def prenom_personne(slug: str, config: dict[str, Any] | None = None) -> str | None:
    """Prénom de la personne dont l'utilisateur Linux est `slug` (None si inconnue)."""
    cfg = load_config() if config is None else config
    for p in (cfg.get("personnes") if isinstance(cfg, dict) else None) or []:
        if isinstance(p, dict) and str(p.get("linux_user") or "") == slug and p.get("prenom"):
            return str(p["prenom"]).strip()
    return None


def rendre_gabarit(texte: str, config: dict[str, Any] | None = None, *, nom: str | None = None) -> str:
    """Remplace les variables des prompts (`content/`, capacités) par les valeurs de config.yaml :
    `{{assistant.nom}}` (prénom de l'assistant) et `{{foyer.adultes}}` (« Alex et Camille »)."""
    cfg = load_config() if config is None else config
    return texte.replace("{{assistant.nom}}", nom or nom_assistant(cfg)).replace(
        "{{foyer.adultes}}", prenoms_adultes(cfg)
    )


def parse_env(texte: str) -> dict[str, str]:
    """Parse un fichier `KEY=VALUE` (commentaires `#`, `export `, guillemets) sans l'exécuter."""
    resultat: dict[str, str] = {}
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        ligne = ligne.removeprefix("export ")
        cle, valeur = ligne.split("=", 1)
        valeur = valeur.strip()
        if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
            valeur = valeur[1:-1]
        if cle.strip():
            resultat[cle.strip()] = valeur
    return resultat


def _lire_env(chemin: Path) -> dict[str, str]:
    """Contenu d'un fichier d'environnement ; {} s'il est absent ou illisible pour l'utilisateur courant."""
    try:
        return parse_env(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:  # PermissionError attendue : chaque fichier n'est lisible que par qui en a besoin
        get_logger(__name__).debug("%s non lisible : %s", chemin.name, exc.__class__.__name__)
        return {}


def load_secrets() -> dict[str, str]:
    """Secrets lisibles par l'utilisateur courant, puis l'environnement pour les `ICAL_*`.

    Ordre : `HOMEOS_SECRETS/*.env` (défaut /etc/homeos, fichiers illisibles ignorés), puis en repli
    `data/secrets.env` (VPS pas encore migré par scripts/migrer-secrets.sh, développement).
    Usage limité aux URLs iCal et à `COFFRE_CLE` (§6 du contrat).
    """
    secrets: dict[str, str] = {}
    dossier = secrets_dir()
    try:
        fichiers = sorted(dossier.glob("*.env")) if dossier.is_dir() else []
    except OSError:
        fichiers = []
    for chemin in fichiers:
        if chemin.name == FICHIER_JETONS:  # jetons du serveur d'outils : jamais utiles aux outils eux-mêmes
            continue
        secrets.update(_lire_env(chemin))
    for cle, valeur in _lire_env(data_dir() / "secrets.env").items():
        secrets.setdefault(cle, valeur)
    for cle, valeur in os.environ.items():
        if cle.startswith("ICAL_"):
            secrets.setdefault(cle, valeur)
    return secrets


def proxy_sortant() -> dict[str, str]:
    """Variables du proxy filtrant (environnement, sinon /etc/default/homeos-proxy) ; {} si non activé."""
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return {k: v for k, v in os.environ.items() if k in VARIABLES_PROXY}
    fichier = Path(os.environ.get("HOMEOS_FICHIER_PROXY", str(FICHIER_PROXY)))
    return {k: v for k, v in _lire_env(fichier).items() if k in VARIABLES_PROXY and v}


def appliquer_proxy() -> dict[str, str]:
    """Exporte le proxy filtrant dans `os.environ` (sous-processus, urllib, Playwright) ; renvoie les variables."""
    variables = proxy_sortant()
    for cle, valeur in variables.items():
        os.environ.setdefault(cle, valeur)
    return variables


def parse_date(valeur: str | date | datetime) -> date:
    """Convertit une chaîne ISO (ou un objet) en `date`."""
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    texte = str(valeur).strip()
    try:
        return datetime.fromisoformat(texte).date()
    except ValueError:
        pass
    # Formats français courants : 07/11/2026, 07-11-2026
    for sep in ("/", "-", "."):
        parts = texte.split(sep)
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            jour, mois, annee = (int(p) for p in parts)
            if annee < 100:
                annee += 2000
            return date(annee, mois, jour)
    raise ValueError(f"Date invalide : « {texte} » (attendu AAAA-MM-JJ)")


def parse_datetime(valeur: str | date | datetime) -> datetime:
    """Convertit une chaîne ISO en datetime Europe/Paris ; une date seule devient minuit."""
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=TZ)
    if isinstance(valeur, date):
        return datetime(valeur.year, valeur.month, valeur.day, tzinfo=TZ)
    texte = str(valeur).strip().replace(" ", "T", 1) if "T" not in str(valeur) and " " in str(valeur) else str(valeur).strip()
    try:
        dt = datetime.fromisoformat(texte)
    except ValueError:
        return parse_datetime(parse_date(texte))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
