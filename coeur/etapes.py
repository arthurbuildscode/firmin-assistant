"""Ligne de réflexion (lot 4a) : libellé lisible d'un appel d'outil (« Je lis l'agenda… »), sans nom technique.

Le libellé vient du nom de l'outil (``mcp__homeos__agenda_lire`` → ``agenda_lire``) : outils de base d'abord,
puis le titre de la capacité qui porte le préfixe (« Je consulte Kitchen Daily… »), sinon un libellé neutre.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

PREFIXE_MCP = "mcp__homeos__"

LIBELLES: dict[str, str] = {
    "agenda_lire": "Je lis l'agenda…",
    "agenda_ajouter": "Je prépare l'événement…",
    "courses_lister": "Je regarde la liste de courses…",
    "courses_ajouter": "J'ajoute à la liste de courses…",
    "courses_cocher": "Je coche la liste de courses…",
    "courses_vider_coches": "Je range la liste de courses…",
    "echeances_lister": "Je vérifie les échéances…",
    "echeances_ajouter": "J'enregistre l'échéance…",
    "echeances_supprimer": "Je retire l'échéance…",
    "anniversaires_lister": "Je regarde les anniversaires…",
    "anniversaires_ajouter": "J'enregistre l'anniversaire…",
    "memoire_lire": "Je relis ce que je sais du foyer…",
    "memoire_ecrire": "Je mets à jour la mémoire du foyer…",
    "memoire_ajouter": "Je note ça…",
    "journal_ajouter": "J'écris dans le journal…",
    "onboarding_etat": "Je regarde où on en est…",
    "onboarding_terminer": "Je termine la découverte…",
    "feedback_noter": "Je note ton avis…",
    "rappels_du_jour": "Je cherche ce qu'il ne faut pas oublier…",
    "meteo_lire": "Je regarde la météo…",
    "fichiers_ranger": "Je range le document…",
    "fichiers_lister": "Je cherche dans les documents…",
    "fichiers_entrants": "Je regarde les documents reçus…",
    "photos_journal_ajouter": "J'ajoute la photo au journal…",
    "capacites_lister": "Je fais le point sur ce que je sais faire…",
    "capacites_statut": "Je vérifie une capacité…",
    "capacites_parametrer": "J'enregistre le réglage…",
    # Lot 7b : atelier (fabrication d'une capacité en arrière-plan)
    "atelier_creer": "Je prépare la demande pour l'atelier…",
    "atelier_etat": "Je regarde où en est l'atelier…",
    "atelier_approuver_reseau": "Je prépare l'autorisation du site…",
    "Read": "Je lis le document…",
}
LIBELLE_DEFAUT = "Je m'en occupe…"

_titres_capacites: dict[str, str] | None = None


def nom_court(outil: str | None) -> str:
    nom = str(outil or "")
    return nom.removeprefix(PREFIXE_MCP)


def _titres(racine: Path | None = None) -> dict[str, str]:
    """Préfixe → titre de chaque capacité (lu une fois par processus)."""
    global _titres_capacites
    if _titres_capacites is None:
        titres: dict[str, str] = {}
        try:
            from tools import capacites

            for cap in capacites.decouvrir(racine):
                if cap.manifeste is not None:
                    titres[cap.prefixe] = cap.titre
        except Exception:
            log.debug("titres des capacités indisponibles", exc_info=True)
        _titres_capacites = titres
    return _titres_capacites


def libelle(outil: str | None, racine: Path | None = None) -> str:
    """« Je lis l'agenda… » pour ``mcp__homeos__agenda_lire`` ; « Je consulte <titre>… » pour une capacité."""
    nom = nom_court(outil)
    if nom in LIBELLES:
        return LIBELLES[nom]
    for prefixe, titre in sorted(_titres(racine).items(), key=lambda x: -len(x[0])):
        if nom.startswith(f"{prefixe}_"):
            if prefixe == "admin":
                return "Je regarde les réglages…"
            return f"Je consulte {titre}…"
    return LIBELLE_DEFAUT
