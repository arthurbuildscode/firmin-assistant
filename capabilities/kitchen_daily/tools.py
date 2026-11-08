"""Capacité Kitchen Daily : outils MCP (noms du lot 2 inchangés, + extras et interrupteur de livraison au lot 2b)
et exécution des actions validées.

La logique (Playwright, parsing, proposition) reste dans `tools/kitchen_daily.py`, qui garde sa CLI.
"""

from __future__ import annotations

from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    from tools import kitchen_daily

    @mcp.tool()
    def kitchen_daily_livraisons() -> list[dict[str, Any]]:
        """Livraisons Kitchen Daily à venir, lues en direct sur le site (quelques secondes).

        Chaque livraison : id (à passer aux autres outils), date (AAAA-MM-JJ), jour, creneau, numero, statut,
        montant (€), prelevement_le, payee, modifiable_jusqua (date-heure limite pour changer les plats),
        modifiable (true si la limite n'est pas dépassée), livraison_voulue (interrupteur « Je veux ma livraison » :
        true, false = semaine sautée, null = non lu ; lu seulement pour les livraisons modifiables).
        Si la session du site a expiré, l'outil renvoie une erreur explicite : transmets-la telle quelle.
        """
        return kitchen_daily.livraisons()

    @mcp.tool()
    def kitchen_daily_menu(livraison_id: str) -> dict[str, Any]:
        """Plats proposés pour une livraison Kitchen Daily (onglet PLATS), lus en direct sur le site.

        - `livraison_id` : l'`id` renvoyé par `kitchen_daily_livraisons` (ex. AAAA000002).
        Renvoie formule_plats (nombre exact de plats à choisir, ex. 18), date_limite, modifiable, compteur actuel,
        et plats : titre (exact, à réutiliser tel quel), badges (protéines +, Végétarien, Contrôle calories…),
        selectionne / quantite (état actuel si détectable, sinon null — voir detection_selection).
        Lecture seule : rien n'est modifié sur le site.
        """
        return kitchen_daily.menu_avec_limite(livraison_id)

    @mcp.tool()
    def kitchen_daily_preferences() -> str:
        """Préférences alimentaires du foyer (data/alimentation/*.md) et historique des repas récents.

        À lire avant de proposer une sélection : c'est toi qui choisis les plats, en variant, en évitant les
        répétitions des 3 dernières semaines (historique) et en respectant goûts, refus et contraintes de chacun.
        """
        return kitchen_daily.preferences()

    @mcp.tool()
    def kitchen_daily_proposer(livraison_id: str, selection: dict[str, int], justification: str) -> str:
        """Prépare l'enregistrement d'une sélection de plats Kitchen Daily — rien n'est appliqué sans validation.

        - `livraison_id` : id de la livraison (via `kitchen_daily_livraisons`), qui doit encore être modifiable.
        - `selection` : {titre exact du menu: quantité}, la somme des quantités doit être égale à la formule (ex. 18).
          Les titres sont comparés sans accents ni majuscules ; un titre absent ou ambigu est refusé avec la liste.
        - `justification` : en une ou deux phrases, pourquoi cette sélection (goûts, variété, agenda).
        Renvoie `CONFIRMATION_REQUISE:<id>:<résumé>` à restituer tel quel : la personne validera par bouton,
        puis la sélection sera enregistrée sur le site et une capture d'écran envoyée dans le groupe.
        """
        return kitchen_daily.proposer(livraison_id, selection, justification)

    @mcp.tool()
    def kitchen_daily_extras(livraison_id: str) -> dict[str, Any]:
        """Extras d'une livraison (desserts, entrées-soupes-apéros, jus et shots), lus en direct (≈ 20 s), sans rien modifier.

        - `livraison_id` : l'`id` renvoyé par `kitchen_daily_livraisons`.
        Renvoie `desserts`, `entrees`, `jus` (chacun : produits avec titre exact, prix_unitaire en €, quantite
        actuelle, offert_possible = éligible au dessert offert, promo, stock_limite ; nombre et montant de l'étape),
        `selection` (ce qui est déjà pris, par catégorie), `dessert_offert` (éligibles, deja_choisi) et
        `montant_estime` des extras. Un dessert offert par livraison : le premier dessert éligible est gratuit.
        """
        return kitchen_daily.extras(livraison_id)

    @mcp.tool()
    def kitchen_daily_extras_definir(livraison_id: str, extras: dict[str, dict[str, int]]) -> str:
        """Change des quantités d'extras (desserts, entrées, jus) sans toucher aux plats.

        - `livraison_id` : id d'une livraison encore modifiable.
        - `extras` : {"desserts": {titre: quantité}, "entrees": {...}, "jus": {...}} — quantité FINALE voulue
          (0 = retirer) ; seuls les produits cités changent. Titres comparés sans accents ni majuscules ; un titre
          absent ou ambigu est refusé avec la liste des titres disponibles.
        Dessert offert seul (ajout d'un dessert éligible, montant inchangé, aucun retrait) : enregistré aussitôt et
        vérifié, l'outil le dit. Sinon (payant, retrait) : renvoie `CONFIRMATION_REQUISE:<id>:<résumé avec le
        supplément estimé>` à restituer tel quel ; rien ne change avant « Valider », même en validation automatique.
        """
        return kitchen_daily.definir_extras(livraison_id, extras)

    @mcp.tool()
    def kitchen_daily_livraison_definir(livraison_id: str, voulue: bool) -> str:
        """Saute une semaine ou la réactive : interrupteur « Je veux ma livraison » du site.

        - `livraison_id` : id d'une livraison encore modifiable (via `kitchen_daily_livraisons`).
        - `voulue` : false = pas de livraison cette semaine-là ; true = réactiver.
        Renvoie `CONFIRMATION_REQUISE:<id>:<résumé>` à restituer tel quel (toujours à valider par bouton), ou
        « Rien à faire » si l'interrupteur est déjà dans l'état demandé.
        """
        return kitchen_daily.proposer_livraison(livraison_id, voulue)

    @mcp.tool()
    def kitchen_daily_historique_ajouter(livraison_date: str, plats: list[str], notes: str | None = None) -> str:
        """Ajoute une ligne à l'historique des repas (data/alimentation/historique.md).

        - `livraison_date` : date de la livraison (AAAA-MM-JJ ou JJ/MM/AAAA).
        - `plats` : titres des plats (ex. ["Tagliatelles carbonara ×2", "Dahl de lentilles corail au lait de coco"]).
        - `notes` : avis après dégustation (« carbonara trop salée », « les enfants ont adoré le dahl »), facultatif.
        Sert à éviter les répétitions et à mémoriser les avis. Une sélection validée par bouton est déjà historisée.
        """
        return kitchen_daily.historique_ajouter(livraison_date, plats, notes)


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Action validée par bouton (appelée par `tools.runner execute`)."""
    from tools import kitchen_daily

    if action == "kitchen_daily_selection":
        return kitchen_daily.executer_selection(payload)
    if action == "kitchen_daily_extras_definir":
        return kitchen_daily.executer_extras(payload)
    if action == "kitchen_daily_livraison_definir":
        return kitchen_daily.executer_livraison(payload)
    raise ValueError(f"Action Kitchen Daily inconnue : « {action} ».")
