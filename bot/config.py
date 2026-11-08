"""Chargement de la configuration (data/config.yaml) et des secrets (/etc/homeos/*.env).

Le répertoire de données est lu dans la variable d'environnement ``HOMEOS_DATA``
(défaut : ``/srv/home-os/data``). Le dépôt (racine du code) est déduit de ce fichier,
ou forcé par ``HOMEOS_ROOT``. Secrets : ``HOMEOS_SECRETS`` (défaut ``/etc/homeos``, un fichier par usage ;
systemd en fournit l'essentiel par ``EnvironmentFile=``), repli ``data/secrets.env`` avant migration.
État sensible (sessions, Kitchen Daily, coffre) : ``HOMEOS_ETAT`` (défaut ``/var/lib/homeos``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tools.common import NOM_ASSISTANT_DEFAUT, etat_pour, parse_env
from tools.modeles import ReglagesIA, reglages_ia
from tools.securite import dossier_travail

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("/srv/home-os/data")
DEFAULT_SECRETS_DIR = Path("/etc/homeos")
# Wrapper root (install/bin/homeos-claude) : seule commande que homeos peut lancer sous une personne (sudoers).
DEFAULT_WRAPPER = Path("/usr/local/sbin/homeos-claude")
# Rôles (lot 7a) : seuls les propriétaires administrent le système par la conversation.
ROLES = ("proprietaire", "membre", "invite")
ROLE_DEFAUT = "proprietaire"  # une personne listée sans rôle est un adulte du foyer


@dataclass(frozen=True)
class Personne:
    telegram_id: int
    prenom: str
    linux_user: str
    role: str = ROLE_DEFAUT
    auth: str = (
        "abonnement"  # "abonnement" (session Claude Code de la personne) | "cle_api" (ANTHROPIC_API_KEY, --bare)
    )
    # Appli web : compte Tailscale attendu (en-tête ``Tailscale-User-Login`` posé par ``tailscale serve``), en
    # minuscules. Absent : le login est appris à la connexion et affiché dans Réglages → appareils (CONTRACT §12).
    tailscale_login: str | None = None

    @property
    def cle_api(self) -> bool:
        return self.auth == "cle_api"

    @property
    def proprietaire(self) -> bool:
        return self.role == "proprietaire"


@dataclass
class Config:
    data_dir: Path
    root_dir: Path
    assistant_nom: str = NOM_ASSISTANT_DEFAUT
    tutoiement: bool = True
    langue: str = "fr"
    personnes: list[Personne] = field(default_factory=list)
    groupe_telegram_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    # Empreinte (mtime, taille) de config.yaml au chargement : rechargement à chaud par le bot (lot 7a).
    empreinte: tuple[int, int] | None = None

    # --- Chemins dérivés -------------------------------------------------
    @property
    def etat_dir(self) -> Path:
        """État sensible hors de data/ (``/var/lib/homeos``), ou data/ en développement."""
        return etat_pour(self.data_dir)

    @property
    def secrets_dir(self) -> Path:
        return Path(os.environ.get("HOMEOS_SECRETS") or DEFAULT_SECRETS_DIR)

    @property
    def sessions_path(self) -> Path:
        return self.etat_dir / "sessions.json"

    @property
    def kd_captures_dir(self) -> Path:
        """Captures Kitchen Daily (seuls fichiers que le bot accepte d'envoyer sur signal d'un outil)."""
        if os.path.abspath(self.etat_dir) != os.path.abspath(self.data_dir):
            return self.etat_dir / "kd" / "captures"
        return self.data_dir / ".kd" / "captures"

    @property
    def wrapper_path(self) -> Path:
        return Path(os.environ.get("HOMEOS_CLAUDE_WRAPPER") or DEFAULT_WRAPPER)

    @property
    def ia(self) -> ReglagesIA:
        """Bloc ``ia`` (lot 4a) : modèles du routeur et renouvellement des sessions Claude (``tools.modeles``)."""
        return reglages_ia(self.raw)

    def capacites_automatiques(self) -> set[str]:
        """Capacités dont les actions avec effet sont exécutées sans bouton Valider
        (``capacites.<nom>.validation: automatique`` dans config.yaml ; défaut : validation humaine)."""
        bloc = self.raw.get("capacites") or {}
        if not isinstance(bloc, dict):
            return set()
        return {
            str(nom)
            for nom, reglages in bloc.items()
            if isinstance(reglages, dict) and str(reglages.get("validation", "")).lower() in ("automatique", "auto")
        }

    def action_automatique(self, kind: str) -> bool:
        """Une action ``<prefixe>_…`` est automatique si sa capacité l'est (le préfixe est le nom de la capacité,
        sauf déclaration ``prefixe:`` du manifeste, ex. ``administration`` → ``admin_…``)."""
        automatiques = self.capacites_automatiques()
        if not automatiques:
            return False
        try:
            from tools import capacites

            cap = capacites.capacite_pour_kind(kind, racine=self.capabilities_dir)
        except Exception:
            log.debug("capacité du kind %s introuvable", kind, exc_info=True)
            cap = None
        if cap is not None:
            return cap.nom in automatiques
        return any(kind == nom or kind.startswith(f"{nom}_") for nom in automatiques)

    def actions_confirmation_toujours(self) -> set[str]:
        """Kinds jamais exécutés sans bouton, même en validation automatique : union des champs
        ``confirmation_toujours`` des manifestes ``capabilities/*/capability.yaml`` (relus à chaque appel), et de
        ceux des capacités fabriquées par l'atelier (lot 7b, ``<etat>/capacites-atelier``)."""
        kinds: set[str] = set()
        manifestes = list(self.capabilities_dir.glob("*/capability.yaml"))
        atelier = self.etat_dir / "capacites-atelier"
        if atelier.resolve() != self.capabilities_dir.resolve():
            manifestes += list(atelier.glob("*/capability.yaml"))
        for manifeste in sorted(manifestes):
            try:
                brut = yaml.safe_load(manifeste.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                log.warning("manifeste illisible : %s", manifeste)
                continue
            valeurs = brut.get("confirmation_toujours") if isinstance(brut, dict) else None
            if isinstance(valeurs, list):
                kinds.update(str(v).strip() for v in valeurs if isinstance(v, str) and v.strip())
        return kinds

    def dossier_travail(self, linux_user: str) -> Path:
        """``cwd`` vide de la session Claude de cette personne."""
        return dossier_travail(self.etat_dir, self.data_dir, linux_user)

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.yaml"

    def config_modifiee(self) -> bool:
        """Vrai si config.yaml a changé (mtime ou taille) depuis le chargement de cette configuration."""
        return empreinte_fichier(self.config_path) != self.empreinte

    @property
    def db_path(self) -> Path:
        return self.data_dir / "homeos.sqlite"

    @property
    def pause_path(self) -> Path:
        return self.data_dir / "PAUSE"

    @property
    def content_dir(self) -> Path:
        return self.root_dir / "content"

    @property
    def capabilities_dir(self) -> Path:
        return self.root_dir / "capabilities"

    @property
    def system_prompt_source(self) -> Path:
        return self.content_dir / "CLAUDE.md"

    @property
    def system_prompt_path(self) -> Path:
        return self.data_dir / ".claude-system-prompt.md"

    @property
    def mcp_config_path(self) -> Path:
        return self.root_dir / "tools" / "mcp.json"

    @property
    def mode_outils(self) -> str:
        """« http » (serveur d'outils persistant, lot 7) ou « stdio » : /etc/default/homeos-outils, relu à chaque
        message (retour arrière sans redémarrer le bot)."""
        from tools.common import mode_outils

        return mode_outils()

    @property
    def python_bin(self) -> str:
        """Interpréteur utilisé pour ``python -m tools.runner`` (venv du dépôt si présent)."""
        venv = self.root_dir / ".venv" / "bin" / "python"
        return str(venv) if venv.exists() else "python3"

    @property
    def telegram_token(self) -> str:
        token = self.secrets.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        return token

    @property
    def no_sudo(self) -> bool:
        return os.environ.get("HOMEOS_NO_SUDO", "") == "1"

    # --- Allowlist -----------------------------------------------------------
    def personne_par_telegram(self, telegram_id: int) -> Personne | None:
        for p in self.personnes:
            if p.telegram_id == telegram_id:
                return p
        return None

    def personne_par_linux_user(self, linux_user: str) -> Personne | None:
        for p in self.personnes:
            if p.linux_user == linux_user:
                return p
        return None

    def est_autorise(self, telegram_id: int) -> bool:
        return self.personne_par_telegram(telegram_id) is not None

    def est_groupe(self, chat_id: int) -> bool:
        return self.groupe_telegram_id is not None and chat_id == self.groupe_telegram_id


def empreinte_fichier(chemin: Path) -> tuple[int, int] | None:
    try:
        st = chemin.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def normaliser_role(valeur: Any) -> str:
    """Rôle d'une personne : proprietaire (défaut), membre ou invite ; une valeur inconnue donne invite."""
    if valeur is None or str(valeur).strip() == "":
        return ROLE_DEFAUT
    role = str(valeur).strip().lower().replace("é", "e").replace("è", "e")
    if role not in ROLES:
        log.warning("Rôle inconnu %r dans config.yaml : traité comme « invite »", valeur)
        return "invite"
    return role


def data_dir_from_env() -> Path:
    return Path(os.environ.get("HOMEOS_DATA", str(DEFAULT_DATA_DIR))).expanduser()


def root_dir_from_env() -> Path:
    env = os.environ.get("HOMEOS_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent


def parse_secrets_env(text: str) -> dict[str, str]:
    """Parse un fichier ``KEY=value`` minimal (commentaires ``#``, guillemets simples/doubles)."""
    return parse_env(text)


def charger_secrets(data_dir: Path, secrets_dir: Path | None = None) -> dict[str, str]:
    """Secrets lisibles par le bot : ``/etc/homeos/*.env`` (illisibles ignorés), repli ``data/secrets.env``."""
    dossier = secrets_dir or Path(os.environ.get("HOMEOS_SECRETS") or DEFAULT_SECRETS_DIR)
    resultat: dict[str, str] = {}
    fichiers: list[Path] = []
    try:
        if dossier.is_dir():
            fichiers = sorted(dossier.glob("*.env"))
    except OSError:
        fichiers = []
    for chemin in [*fichiers, data_dir / "secrets.env"]:
        try:
            valeurs = parse_env(chemin.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.debug("%s non lisible par le bot (%s)", chemin.name, exc.__class__.__name__)
            continue
        for cle, valeur in valeurs.items():
            resultat.setdefault(cle, valeur)
    return resultat


def parse_config(raw: dict[str, Any], data_dir: Path, root_dir: Path) -> Config:
    assistant = raw.get("assistant") or {}
    personnes: list[Personne] = []
    for item in raw.get("personnes") or []:
        try:
            personnes.append(
                Personne(
                    telegram_id=int(item["telegram_id"]),
                    prenom=str(item.get("prenom") or item.get("linux_user") or "?"),
                    linux_user=str(item["linux_user"]),
                    role=normaliser_role(item.get("role")),
                    auth=str(item.get("auth") or "abonnement"),
                    tailscale_login=str(item.get("tailscale_login") or "").strip().lower() or None,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Entrée 'personnes' ignorée dans config.yaml : %r (%s)", item, exc)
    groupe = raw.get("groupe_telegram_id")
    return Config(
        data_dir=data_dir,
        root_dir=root_dir,
        assistant_nom=str(assistant.get("nom") or NOM_ASSISTANT_DEFAUT),
        tutoiement=bool(assistant.get("tutoiement", True)),
        langue=str(assistant.get("langue") or "fr"),
        personnes=personnes,
        groupe_telegram_id=int(groupe) if groupe is not None else None,
        raw=raw,
    )


def load_config(data_dir: Path | None = None, root_dir: Path | None = None) -> Config:
    data_dir = data_dir or data_dir_from_env()
    root_dir = root_dir or root_dir_from_env()
    config_path = data_dir / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration introuvable : {config_path}")
    empreinte = empreinte_fichier(config_path)
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"config.yaml invalide (attendu un mapping) : {config_path}")
    cfg = parse_config(raw, data_dir, root_dir)
    cfg.empreinte = empreinte

    cfg.secrets = charger_secrets(data_dir)
    if not cfg.secrets and not os.environ.get("TELEGRAM_BOT_TOKEN"):
        log.warning(
            "Aucun secret lisible (%s/*.env, data/secrets.env) ni TELEGRAM_BOT_TOKEN dans l'environnement",
            cfg.secrets_dir,
        )

    if not cfg.personnes:
        log.warning("Aucune personne dans config.yaml : tous les messages seront ignorés")
    return cfg
