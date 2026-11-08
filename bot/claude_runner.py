"""Compatibilité : le lancement de ``claude -p`` vit dans :mod:`coeur.claude` depuis le lot 4a.

Les noms historiques restent importables d'ici (bot, tests, capacités).
"""

from __future__ import annotations

from coeur.claude import (  # noqa: F401
    CANAUX,
    LIMITE_LIGNE,
    PLACEHOLDER_NOM,
    TAILLE_MAX_MEMOIRE,
    TIMEOUT_S,
    ClaudeError,
    ClaudeMissing,
    ClaudeNotConnected,
    ClaudeResult,
    ClaudeRun,
    ClaudeRunner,
    ClaudeTimeout,
    EvenementClaude,
    StreamEvent,
    chemin_prompt_systeme,
    config_mcp_http,
    construire_commande,
    contexte_memoire,
    environnement_claude,
    est_erreur_auth,
    fragments_capacites,
    mesures_resultat,
    parse_line,
    preparer_prompt_systeme,
    substituer_nom,
)
