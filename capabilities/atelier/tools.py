"""Capacité « Atelier » : outils MCP `atelier_*` (lot 7b). Réservés aux propriétaires du foyer.

`atelier_creer` reformule la demande et renvoie CONFIRMATION_REQUISE ; après « Valider », `executer()` met la demande
en file et l'atelier tourne en arrière-plan (`tools.atelier`). Rien n'est jamais présenté comme fait avant le message
de fin (« Capacité … prête à l'essai » ou « Ça n'a pas marché … »).
"""

from __future__ import annotations

from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    from tools import administration
    from tools import atelier as at

    @mcp.tool()
    def atelier_creer(demande: str, site: str, compte: str = "", action: str = "", titre: str = "",
                      fabrication: str = "capacite", modes: list[str] | str | None = None, zone: str = "") -> str:
        """Fabriquer une nouvelle capacité (« ajoute Planity pour prendre les rendez-vous coiffeur ») ou un nouveau
        réseau de transports (« ajoute le métro de Lyon »), après les questions de découverte posées une à une :
        - `demande` : la demande avec les mots de la personne ;
        - `site` : l'adresse du site (« planity.com », ou le site du réseau / de ses données ouvertes) ;
        - `compte` : « moi » (compte personnel de la personne qui parle) ou « foyer » (compte commun) — inutile pour
          un réseau de transports (lecture seule) ;
        - `action` : ce qui est attendu (« voir les créneaux libres et réserver une coupe ») ;
        - `titre` : nom court (« Planity », « Métro de Lyon »), sinon tiré du site ;
        - `fabrication` : « capacite » (défaut) ou « fournisseur_transports » (un réseau pour la capacité Transports,
          pas une capacité entière) ; alors `modes` (« metro, tram ») et `zone` (« Lyon ») facultative.
        Réservé aux propriétaires. Refuse ce qui touche à l'espace professionnel. Renvoie CONFIRMATION_REQUISE
        (reformulation + site approuvé) à laisser telle quelle."""
        resume, payload = at.preparer(demande, site, compte, action, titre, ctx.personne, ctx.config,
                                      fabrication=fabrication, modes=modes, zone=zone)
        return ctx.demander_confirmation("atelier_creer", resume, payload)

    @mcp.tool()
    def atelier_etat(nom: str = "") -> Any:
        """Où en est l'atelier (« c'est prêt, Planity ? ») : étape en cours en langage courant, message de fin,
        sites qui attendent une approbation. `nom` vide : tous les travaux de l'atelier."""
        administration.verifier_proprietaire(ctx.personne, ctx.config)
        return at.etat_public(nom or None)

    @mcp.tool()
    def atelier_approuver_reseau(nom: str) -> str:
        """Approuver les sites supplémentaires qu'une capacité ou un réseau de transports de l'atelier veut joindre
        (« Planity veut aussi parler à cdn.planity.com »). Renvoie CONFIRMATION_REQUISE."""
        resume, payload = at.preparer_reseau(nom, ctx.personne, ctx.donnees)
        return ctx.demander_confirmation("atelier_approuver_reseau", resume, payload)


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Action validée par bouton (`tools.runner execute`)."""
    from tools import atelier as at

    if action == "atelier_creer":
        return at.executer_creation(payload, ctx.donnees)
    if action == "atelier_approuver_reseau":
        return at.executer_reseau(payload, ctx.donnees)
    raise ValueError(f"Action inconnue : {action}")
