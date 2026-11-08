"""Smoke test de la capacité transports (healthcheck) : chaque réseau utilisé par le foyer répond-il ?

`verifier(ctx) -> {"ok": bool, "message": str}` : appelle `verifier()` de la source de chaque fournisseur utilisé
(trajets, réseaux choisis, grilles présentes) — service local Cartreize en moins de 3 s et grille de moins de
21 jours, grille SNCF ou GTFS récente, flux temps réel joignable (un temps réel indisponible est signalé sans être un
échec : les horaires théoriques prennent le relais). Sans réseau utilisé : OK, rien à vérifier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import crons
from . import fournisseurs as fr


def verifier(ctx: Any) -> dict[str, Any]:
    donnees = Path(ctx.donnees)
    utilises = crons.fournisseurs_utilises(donnees)
    resultats = []
    invalides = [f for f in fr.decouvrir(donnees) if not f.valide]
    for f in fr.decouvrir(donnees):
        if f.nom in utilises and f.valide:
            resultats.append(fr.verifier_source(f, donnees, ctx))
    morceaux = [r["message"] for r in resultats]
    morceaux += [f"réseau « {f.titre} » mal décrit : {'; '.join(f.erreurs)}" for f in invalides]
    ok = all(r["ok"] for r in resultats) and not [f for f in invalides if f.provenance != "atelier"]
    return {"ok": ok, "message": " | ".join(morceaux) or "Transports : aucun réseau utilisé, rien à vérifier"}
