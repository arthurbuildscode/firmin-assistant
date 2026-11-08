"""Choix du modèle Claude (lot 4a) : réglages ``ia`` de ``config.yaml``, sans dépendance au bot.

    ia:
      modele_simple: haiku        # commandes, confirmations, demandes courtes (« ajoute du lait »)
      modele: sonnet              # conversation courante, briefs
      modele_decisions: opus      # choix, comparaisons, sélections (Kitchen Daily…)
      routeur: true               # false : toujours `modele`
      session_max_reponses: 20    # nouvelle session Claude après 20 réponses…
      session_ttl_heures: 2       # … ou 2 h d'inactivité (l'historique reste affiché dans l'appli)

Un nom de modèle est un alias (``haiku``, ``sonnet``, ``opus``) ou un identifiant complet (``claude-…``) : même
liste blanche que le wrapper ``homeos-claude`` (``--model``). Une valeur invalide est remplacée par le défaut.
Utilisé par le cœur (``coeur.routeur``, ``coeur.conversation``) et par les crons (``tools.runner``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MOTIF_MODELE = re.compile(r"^(haiku|sonnet|opus|claude-[a-z0-9.-]+)$")
NIVEAUX = ("simple", "standard", "decision")


def modele_valide(nom: Any) -> bool:
    return isinstance(nom, str) and bool(MOTIF_MODELE.fullmatch(nom))


@dataclass(frozen=True)
class ReglagesIA:
    modele_simple: str = "haiku"
    modele: str = "sonnet"
    modele_decisions: str = "opus"
    routeur: bool = True
    session_max_reponses: int = 20
    session_ttl_heures: float = 2.0

    def pour_niveau(self, niveau: str) -> str:
        """``simple`` → modèle simple, ``decision`` → modèle des décisions, sinon le modèle courant."""
        if niveau == "simple":
            return self.modele_simple
        if niveau == "decision":
            return self.modele_decisions
        return self.modele


def _modele(bloc: dict[str, Any], cle: str, defaut: str) -> str:
    valeur = bloc.get(cle)
    if valeur is None:
        return defaut
    valeur = str(valeur).strip().lower()
    if modele_valide(valeur):
        return valeur
    log.warning("ia.%s invalide dans config.yaml : %r (défaut %s)", cle, bloc.get(cle), defaut)
    return defaut


def _nombre(bloc: dict[str, Any], cle: str, defaut: float, minimum: float, conv: type) -> Any:
    try:
        valeur = conv(bloc.get(cle, defaut))
    except (TypeError, ValueError):
        return conv(defaut)
    return valeur if valeur >= minimum else conv(defaut)


def reglages_ia(raw: dict[str, Any] | None) -> ReglagesIA:
    """Bloc ``ia`` de config.yaml (absent ou partiel : valeurs par défaut)."""
    bloc = (raw or {}).get("ia") if isinstance(raw, dict) else None
    if not isinstance(bloc, dict):
        bloc = {}
    d = ReglagesIA()
    routeur = bloc.get("routeur", d.routeur)
    return ReglagesIA(
        modele_simple=_modele(bloc, "modele_simple", d.modele_simple),
        modele=_modele(bloc, "modele", d.modele),
        modele_decisions=_modele(bloc, "modele_decisions", d.modele_decisions),
        routeur=routeur if isinstance(routeur, bool) else str(routeur).strip().lower() in ("true", "oui", "1"),
        session_max_reponses=_nombre(bloc, "session_max_reponses", d.session_max_reponses, 1, int),
        session_ttl_heures=_nombre(bloc, "session_ttl_heures", d.session_ttl_heures, 0.05, float),
    )
