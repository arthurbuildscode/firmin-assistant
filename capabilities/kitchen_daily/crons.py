"""Crons Kitchen Daily.

- `rappel` (mercredi 18:00 par défaut) : livraisons et menu lus en direct, préférences, puis :
  - **validation humaine** (défaut) : proposition rédigée par `claude --bare` avec
    `content/prompts/rappel-kitchen-daily.md`, postée dans le groupe ; la sélection part sur le site après « Valider » ;
  - **validation automatique** (`capacites.kitchen_daily.validation: automatique` dans config.yaml) : l'assistant
    choisit (JSON, `content/prompts/selection-kitchen-daily.md`), la sélection est enregistrée sur le site,
    notée dans l'historique, et le groupe reçoit la liste avec la date limite pour demander un changement.
    Seuls les plats sont concernés : les extras déjà pris sont conservés, une semaine sautée est ignorée ; extras
    payants et semaines sautées restent toujours à valider par bouton (`confirmation_toujours` du manifeste).
  Le texte renvoyé est posté dans le groupe par `scripts/capacite-cron.sh` et, avec `--publier`, remis dans le fil
  de l'appli de chaque adulte (`coeur.spontane`) avec une carte et une notification courte sans contenu (ni plat ni
  montant). Seuil déclaré `date_limite` (manifeste) : le choix est à faire avant la date limite du site, ce message
  est donc notifié même si le brief du jour l'a déjà été.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _validation_automatique() -> bool:
    from tools.common import load_config

    bloc = (load_config().get("capacites") or {}).get("kitchen_daily") or {}
    return str(bloc.get("validation", "")).lower() in ("automatique", "auto")


def _extraire_json(texte: str) -> dict[str, Any]:
    """Le modèle doit renvoyer un objet JSON seul ; on tolère un bloc de code ou du texte autour."""
    texte = texte.strip()
    m = re.search(r"\{.*\}", texte, re.DOTALL)
    if not m:
        raise ValueError("Réponse sans objet JSON.")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise TypeError("JSON inattendu.")
    return obj


NOTIFICATION_PROPOSITION = {"titre": "Kitchen Daily", "corps": "Une sélection de plats t'attend, à valider."}
NOTIFICATION_AUTOMATIQUE = {"titre": "Kitchen Daily", "corps": "Les plats de la semaine sont choisis."}
SEUIL = "date_limite"


def _resultat(texte: str, notification: dict[str, str], carte: dict[str, Any] | None = None) -> dict[str, Any]:
    """Résultat du cron (`tools.capacites.ResultatCron`) : texte du groupe, carte et notification de l'appli."""
    from coeur import spontane

    return {"texte": texte, "carte": carte or spontane.carte_routine(texte, "Kitchen Daily", "kitchen_daily", "rappel"),
            "notification": notification, "seuil": SEUIL}


def _carte_selection(plats: dict[str, int], date: Any, message: str) -> dict[str, Any]:
    from coeur import cartes

    return cartes.carte(
        "kitchen_daily_proposition",
        {"plats": [{"titre": t, "quantite": q} for t, q in plats.items()], "statut": "enregistree",
         "date_livraison": str(date or ""), "compact": True, "texte": message},
        titre="Plats de la semaine enregistrés",
        actions=[{"libelle": "Modifier", "action": "message", "args": {"texte": "Je veux changer les plats Kitchen Daily"}}],
    )


def _selection_automatique(ctx: Any) -> str | dict[str, Any]:
    from tools import kitchen_daily as kd
    from tools import runner

    donnees = runner.donnees_rappel_kitchen()
    prompt = runner.assembler_prompt("selection-kitchen-daily", donnees)
    brut = runner.appeler_claude(prompt, modele=runner.modele_cron("modele_decisions"))  # décision : choisir les plats
    try:
        obj = _extraire_json(brut)
    except (ValueError, TypeError):
        return "Je n'ai pas réussi à choisir les plats Kitchen Daily cette semaine (réponse illisible). Je réessaie si on me le demande."
    if obj.get("erreur"):
        return str(obj["erreur"])[:1200]
    livraison_id = str(obj.get("livraison_id") or "")
    selection = obj.get("selection") or {}
    if not livraison_id or not isinstance(selection, dict) or not selection:
        return "Je n'ai pas réussi à choisir les plats Kitchen Daily cette semaine (sélection vide)."
    choisie = next((liv for liv in donnees.get("livraisons", []) if liv.get("id") == livraison_id), None)
    if choisie is not None and choisie.get("livraison_voulue") is False:
        return f"Pas de plats choisis : la livraison du {choisie.get('date')} est sautée (pas de livraison demandée)."
    try:
        res = kd.appliquer_selection(livraison_id, {str(k): int(v) for k, v in selection.items()}, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        return (
            f"Je n'ai pas pu enregistrer les plats Kitchen Daily sur le site : {exc}. Je réessaie si on me le demande."
        )
    if not res.get("ok"):
        return (
            "Le site n'a pas gardé ma sélection Kitchen Daily "
            f"({res.get('verification_plats', '?')}). Rien n'est enregistré ; dites-moi de réessayer."
        )
    plats = [f"{t} ×{q}" if q > 1 else t for t, q in res["selection_appliquee"].items()]
    date = next((liv.get("date") for liv in donnees.get("livraisons", []) if liv.get("id") == livraison_id), None)
    try:
        kd.historique_ajouter(str(date or ""), plats, "sélection automatique")
        if hasattr(ctx, "journal"):
            ctx.journal(f"Kitchen Daily : sélection enregistrée automatiquement pour le {date or livraison_id}")
    except Exception:  # noqa: BLE001, S110
        pass  # l'enregistrement sur le site a réussi, l'historique n'est qu'un confort
    message = str(obj.get("message") or "").strip()
    if not message:
        message = "Plats Kitchen Daily enregistrés :\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(plats, 1))
    message = message[:1500]
    try:
        carte = _carte_selection(dict(res["selection_appliquee"]), date, message)
    except Exception:  # noqa: BLE001 - la carte n'est qu'un confort
        carte = None
    return _resultat(message, NOTIFICATION_AUTOMATIQUE, carte)


def executer(nom_cron: str, ctx: Any) -> str | dict[str, Any] | None:
    if nom_cron != "rappel":
        raise ValueError(f"Cron Kitchen Daily inconnu : « {nom_cron} ».")
    if _validation_automatique():
        return _selection_automatique(ctx)
    from tools import runner

    prompt = runner.assembler_prompt("rappel-kitchen-daily", runner.donnees_rappel_kitchen())
    # La proposition est un choix de plats : modèle des décisions (lot 4a, `ia.modele_decisions`).
    texte = runner.appeler_claude(prompt, modele=runner.modele_cron("modele_decisions"))
    return _resultat(texte, NOTIFICATION_PROPOSITION) if texte.strip() else None
