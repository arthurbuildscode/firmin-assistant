"""Capacité « Administration » : outils MCP `admin_*` (lot 7a).

Chaque outil vérifie que la personne qui parle est propriétaire du foyer. Les changements avec effet passent par
`ctx.demander_confirmation` (bouton Valider), puis `executer()` les applique via `tools.administration`.
"""

from __future__ import annotations

from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    from tools import administration as adm

    def proprietaire() -> str:
        return adm.verifier_proprietaire(ctx.personne, ctx.config)

    def demander(preparation: tuple[str, dict[str, Any]]) -> str:
        resume, payload = preparation
        return ctx.demander_confirmation(payload["action"], resume, payload)

    @mcp.tool()
    def admin_etat() -> dict[str, Any]:
        """État du système, à résumer en langage courant (« où en est le système ? », « qu'est-ce que tu sais
        faire ? ») : capacités (statut, dernière vérification, rendez-vous programmés, validation humaine ou
        automatique), briefs, pause, dépense et latence du mois, sauvegardes et restauration testée, session
        Kitchen Daily, espace disque, application des horaires sur le serveur, derniers réglages."""
        proprietaire()
        return adm.etat(donnees=ctx.donnees)

    @mcp.tool()
    def admin_capacite_activer(nom: str) -> str:
        """Active une capacité (`nom` : nom ou titre, ex. « bus », « Kitchen Daily »). Une capacité écrite par
        l'assistant est validée par cette activation. Renvoie CONFIRMATION_REQUISE (à laisser telle quelle)."""
        return demander(adm.preparer_activation(nom, True, proprietaire(), donnees=ctx.donnees))

    @mcp.tool()
    def admin_capacite_desactiver(nom: str) -> str:
        """Désactive une capacité (données conservées, réactivable). Renvoie CONFIRMATION_REQUISE."""
        return demander(adm.preparer_activation(nom, False, proprietaire(), donnees=ctx.donnees))

    @mcp.tool()
    def admin_capacite_retirer(nom: str) -> str:
        """Retire une capacité fabriquée par l'atelier (« enlève Planity ») : son code est archivé, ses données
        conservées ; pas de retour arrière (il faudra la redemander à l'atelier). Les capacités du système se
        désactivent seulement. Renvoie CONFIRMATION_REQUISE."""
        return demander(adm.preparer_retrait(nom, proprietaire()))

    @mcp.tool()
    def admin_validation(nom: str, mode: str) -> str:
        """Mode de validation des actions d'une capacité : `mode` = « humaine » (bouton Valider avant chaque
        action, défaut) ou « automatique » (l'assistant agit puis informe). Renvoie CONFIRMATION_REQUISE."""
        return demander(adm.preparer_validation(nom, mode, proprietaire()))

    @mcp.tool()
    def admin_cron(nom: str, cron: str, quand: str) -> str:
        """Change l'heure d'un rendez-vous programmé.

        - `nom` : la capacité (« kitchen_daily », « bus »…) ou `brief_jour` / `brief_hebdo` pour les briefs ;
        - `cron` : le rendez-vous de la capacité (ex. « rappel » ; vide s'il n'y en a qu'un, ou pour un brief) ;
        - `quand` : en français (« mercredi 12h », « tous les jours à 7h15 », « du lundi au vendredi 7h »,
          « le week-end à 9h ») ou au format « Wed 18:00 ».
        Renvoie CONFIRMATION_REQUISE ; le changement d'heure est appliqué sur le serveur dans la minute."""
        return demander(adm.preparer_cron(nom, cron, quand, proprietaire()))

    @mcp.tool()
    def admin_parametre(nom: str, cle: str, valeur: str) -> dict[str, Any]:
        """Change un réglage déclaré d'une capacité (comme `capacites_parametrer`), journalisé et annulable.
        Jamais pour un mot de passe ou un identifiant. Renvoie le message à donner et les réglages."""
        return adm.definir_parametre(nom, cle, valeur, proprietaire(), donnees=ctx.donnees)

    @mcp.tool()
    def admin_reconnexion(nom: str) -> str:
        """Explique comment reconnecter une capacité dont la connexion a expiré (Kitchen Daily : commande à
        lancer sur le serveur ; services à identifiants : /identifiants). Ne demande jamais d'identifiant."""
        proprietaire()
        return adm.reconnexion(nom, donnees=ctx.donnees)

    @mcp.tool()
    def admin_pause() -> str:
        """Met le foyer en pause (comme /stop) : rappels, briefs et validations suspendus. CONFIRMATION_REQUISE."""
        return demander(adm.preparer_pause(True, proprietaire(), ctx.donnees))

    @mcp.tool()
    def admin_reprendre() -> str:
        """Fin de la pause (comme /reprendre). Renvoie CONFIRMATION_REQUISE (validable même en pause)."""
        return demander(adm.preparer_pause(False, proprietaire(), ctx.donnees))

    @mcp.tool()
    def admin_journal(n: int = 10) -> list[dict[str, Any]]:
        """Les n dernières actions d'administration : id (numéro à citer pour annuler), quand, auteur, résumé,
        valeur avant et après, `annule_par` si elle a été annulée."""
        proprietaire()
        return adm.journal(n, ctx.donnees)

    @mcp.tool()
    def admin_annuler(id: int) -> str:
        """Retour arrière d'une action d'administration (`id` du journal) : la valeur précédente est restaurée.
        Refusé si le réglage a changé depuis. Renvoie CONFIRMATION_REQUISE."""
        return demander(adm.preparer_annulation(id, proprietaire(), donnees=ctx.donnees))


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Action d'administration validée (appelée par `tools.runner execute`)."""
    from tools import administration as adm

    return adm.executer(action, payload, donnees=ctx.donnees)
