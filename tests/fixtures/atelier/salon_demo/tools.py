"""Capacité « Salon démo » : outils MCP (préfixe `salon_demo_`). Lecture d'abord, action ensuite.

Convention de l'atelier : une action avec effet ne fait que préparer la validation (`ctx.demander_confirmation`,
payload = arguments de l'outil) ; l'effet a lieu dans `executer`, après le bouton Valider.
"""

from __future__ import annotations

from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    from . import connecteur

    @mcp.tool()
    def salon_demo_creneaux(prestation: str = "") -> dict[str, Any]:
        """Créneaux libres au salon (lecture seule). `prestation` : filtre facultatif (« coupe », « couleur »),
        par défaut la prestation habituelle du foyer si elle est réglée."""
        choix = prestation or str(ctx.parametre("prestation", "") or "")
        creneaux = connecteur.lister_creneaux(ctx, choix)
        return {"prestation": choix or None, "creneaux": creneaux, "nombre": len(creneaux)}

    @mcp.tool()
    def salon_demo_reserver(creneau_id: str) -> str:
        """Réserver un créneau au salon (avec validation). `creneau_id` : l'id donné par salon_demo_creneaux.
        Renvoie CONFIRMATION_REQUISE (à laisser telle quelle)."""
        return ctx.demander_confirmation(
            "salon_demo_reserver", f"Réserver le créneau {creneau_id} au salon", {"creneau_id": creneau_id}
        )


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Action validée par bouton (appelée par `tools.runner execute`)."""
    from . import connecteur

    if action != "salon_demo_reserver":
        raise ValueError(f"Action inconnue : {action}")
    resultat = connecteur.reserver(ctx, str(payload.get("creneau_id") or ""))
    return f"Rendez-vous réservé : {resultat['creneau']} (référence {resultat['reference']})."
