"""Réglages de sécurité des sessions Claude (lot 5d, audit points 1 et 7, revue R1/R3).

Fonctions pures, partagées par le bot (`bot.claude_runner`), le runner des briefs (`tools.runner`) et
les tests. Le wrapper `/usr/local/sbin/homeos-claude` (install/bin/homeos-claude) applique les mêmes
règles côté root : il refuse tout réglage qui ne fait que restreindre moins.

Trois couches, de la plus forte à la plus faible :

1. droits Unix : les secrets sont dans `/etc/homeos` (fichiers par usage), l'état sensible dans
   `/var/lib/homeos` ; les sessions Claude des personnes ne peuvent pas lire ce qui ne les concerne pas ;
2. dossiers de travail : `cwd` vide (`/var/lib/homeos-cwd/<personne>`), `--add-dir` limité aux
   sous-dossiers de données non sensibles + `content/` + `capabilities/`, et
   `permissions.blockReadsOutsideWorkingDirectories` : l'outil Read refuse tout le reste ;
3. règles `permissions.deny` explicites sur les chemins sensibles (au cas où un dossier de travail les
   contiendrait un jour).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Sous-dossiers de data/ que l'outil Read peut lire (fichiers reçus, photos, mémoire Markdown).
SOUS_DOSSIERS_LISIBLES = ("foyer", "personnes", "alimentation", "fichiers", "photos", "voitures", "animaux")
OUTILS_AUTORISES = "mcp__homeos__*,Read"
DUREE_TRANSCRIPTIONS_JOURS = 30


def _absolu(chemin: Path | str) -> str:
    return os.path.abspath(str(chemin))


def regle_lecture(chemin: Path | str, motif: str = "") -> str:
    """Règle `Read(//chemin/absolu[/motif])` (le double slash = chemin absolu pour Claude Code)."""
    base = _absolu(chemin).rstrip("/")
    return f"Read(/{base}{'/' + motif if motif else ''})"


def dossiers_lisibles(donnees: Path, racine: Path, existants_seulement: bool = True) -> list[Path]:
    """Dossiers passés en `--add-dir` : sous-dossiers non sensibles de data/, content/, capabilities/.

    `--add-dir` refuse un dossier absent : par défaut seuls les dossiers existants sont gardés."""
    candidats = [donnees / d for d in SOUS_DOSSIERS_LISIBLES] + [racine / "content", racine / "capabilities"]
    if not existants_seulement:
        return candidats
    return [c for c in candidats if c.is_dir()]


def regles_refus(
    donnees: Path,
    etat: Path,
    secrets: Path,
    personne: str | None = None,
    personnes: Iterable[str] = (),
) -> list[str]:
    """Règles `permissions.deny` : secrets, état, bases, historique git, credentials Claude, `prive/` des autres."""
    regles = [
        regle_lecture(secrets, "**"),
        regle_lecture(donnees, "secrets*"),
        regle_lecture(donnees, "**/secrets*"),
        regle_lecture(donnees, "*.sqlite*"),
        regle_lecture(donnees, "sessions.json"),
        regle_lecture(donnees, "coffre.json"),
        regle_lecture(donnees, ".coffre*"),
        regle_lecture(donnees, ".kd/**"),
        regle_lecture(donnees, ".git/**"),
        # Documents sensibles chiffrés (web/sensibles.py) : aucun accès pour le modèle, même chiffrés.
        regle_lecture(donnees, "fichiers/sensibles/**"),
        "Read(//home/**/.claude/**)",
        "Read(//home/*/.claude.json)",
        # Lot 7 : jeton du serveur d'outils et config MCP générée par le wrapper (~/.config/homeos, 700).
        "Read(//home/*/.config/homeos/**)",
        "Read(//root/**)",
    ]
    if _absolu(etat) != _absolu(donnees):
        regles.insert(1, regle_lecture(etat, "**"))
    for autre in sorted(set(personnes)):
        if autre and autre != personne:
            regles.append(regle_lecture(donnees / "personnes" / autre / "prive", "**"))
    return regles


def reglages_session(
    donnees: Path,
    etat: Path,
    secrets: Path,
    personne: str | None = None,
    personnes: Iterable[str] = (),
) -> dict[str, Any]:
    """Contenu de `--settings` : lecture bloquée hors des dossiers de travail, refus explicites, rétention,
    connecteurs claude.ai coupés."""
    return {
        "permissions": {
            "blockReadsOutsideWorkingDirectories": True,
            "deny": regles_refus(donnees, etat, secrets, personne, personnes),
        },
        "cleanupPeriodDays": DUREE_TRANSCRIPTIONS_JOURS,
        # Les connecteurs claude.ai du compte de la personne (Gmail, Drive…) n'ont rien à faire dans le bot.
        "disableClaudeAiConnectors": True,
    }


def reglages_json(*args: Any, **kwargs: Any) -> str:
    """`reglages_session` sérialisé (compact, ordre stable) pour `--settings`."""
    return json.dumps(reglages_session(*args, **kwargs), ensure_ascii=False, separators=(",", ":"))


def racine_travail(etat: Path) -> Path:
    """Dossier des `cwd` vides : `<etat>-cwd` (`/var/lib/homeos-cwd`, 750 root:foyer).

    Lot 7 : hors de `/var/lib/homeos`, qui passe en 700 homeos (les personnes n'ont plus rien à y faire), alors que
    leurs sessions Claude doivent toujours pouvoir entrer dans leur dossier de travail."""
    return Path(_absolu(etat).rstrip("/") + "-cwd")


def dossier_travail(etat: Path, donnees: Path, personne: str) -> Path:
    """`cwd` vide de la session : `<etat>-cwd/<personne>`, ou `data/.cwd/<personne>` sans état séparé (dev)."""
    if _absolu(etat) != _absolu(donnees):
        return racine_travail(etat) / personne
    return donnees / ".cwd" / personne
