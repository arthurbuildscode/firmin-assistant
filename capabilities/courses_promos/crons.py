"""Routines de la capacité « courses & promos ».

`veille_promos` (lundi 05:30) ne fait **rien** tant que le paramètre `mode` vaut « a_choisir » (défaut) : la routine
existe dans le manifeste pour pouvoir être réglée en conversation, mais elle reste muette et inactive jusqu'à ce que
le foyer ait choisi d'où viennent ses prix. Dans ce lot, une fois le mode choisi, elle rapproche seulement les
tickets reçus des économies estimées (confirmation ou annulation). Elle ne lit aucun site et ne parle jamais : elle
renvoie toujours None (aucun seuil déclaré, skill ux-capacites §2).
"""

from __future__ import annotations

from typing import Any

MODES_ACTIFS = ("tickets", "tickets_et_catalogue", "drive_lecture")


def executer(nom_cron: str, ctx: Any) -> None:
    if nom_cron != "veille_promos":
        raise ValueError(f"Routine inconnue : {nom_cron}")
    try:
        mode = ctx.parametre("mode", "a_choisir")
    except Exception:  # noqa: BLE001 - paramètre illisible : on ne fait rien
        return
    if mode not in MODES_ACTIFS:
        return
    from tools.common import today

    from . import economies, stockage

    with stockage.ouvrir(ctx.dossier) as conn:
        bilan = economies.rapprocher_tickets(conn, today())
    if hasattr(ctx, "journal"):
        ctx.journal(f"veille_promos : {bilan['confirmees']} économie(s) confirmée(s), {bilan['annulees']} annulée(s)")
