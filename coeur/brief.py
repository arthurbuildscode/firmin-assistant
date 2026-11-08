"""Cartes du jour (lot 4a) : ``bonjour``, ``brief`` et ``a_noter``, construites sans le modèle à partir des outils.

Utilisées par ``GET /api/brief/jour?date=`` (carte du jour feuilletable J+1, J+2) et par le cœur au premier
message du jour d'une personne dans l'appli. Les outils sont appelés dans le processus, sous l'identité de la
personne (``tools.identite.requete``) : ses agendas privés, jamais ceux de l'autre.

Météo : Open-Meteo, commune du foyer (``meteo.commune`` de ``config.yaml``) ; la logique (géocodage mis en cache,
prévisions gardées 30 min) est dans ``tools/meteo.py``, partagée avec l'outil ``meteo_lire``. Sans réseau ou sans
commune : pas de météo (la carte reste utile).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from bot.config import Config, Personne
from coeur.cartes import carte, valides
from tools import meteo as _meteo
from tools.meteo import Telecharger, date_longue

log = logging.getLogger(__name__)

# Champs météo de la carte ``bonjour`` (CONTRACT §14) ; le détail (moments, vent) est réservé à la carte ``meteo``.
CHAMPS_METEO_BONJOUR = ("commune", "min", "max", "pluie_pct", "libelle", "icone")


def lieu_meteo(cfg: Config, telecharger: Telecharger | None = None) -> dict[str, Any] | None:
    return _meteo.lieu_meteo(cfg.raw, cfg.data_dir, telecharger)


def meteo_jour(cfg: Config, jour: date, telecharger: Telecharger | None = None) -> dict[str, Any] | None:
    """Météo d'un jour (min, max, libellé, icône, pluie %) pour la commune du foyer ; None si indisponible."""
    m = _meteo.meteo_jour(jour, cfg.raw, cfg.data_dir, telecharger)
    return None if m is None else {k: m.get(k) for k in CHAMPS_METEO_BONJOUR}


def _sur(fn: Callable[[], Any], defaut: Any) -> Any:
    try:
        return fn()
    except Exception:
        log.info("source du brief indisponible", exc_info=True)
        return defaut


def texte_brief_du_jour(cfg: Config, jour: date) -> str | None:
    """Texte du brief du matin généré par le cron (``actions_log``, kind ``brief_jour``) pour ce jour."""
    import sqlite3

    try:
        conn = sqlite3.connect(cfg.db_path, timeout=5)
        try:
            row = conn.execute(
                "SELECT result FROM actions_log WHERE kind = 'brief_jour' AND status = 'done' AND substr(ts, 1, 10) = ? "
                "ORDER BY id DESC LIMIT 1",
                (jour.isoformat(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]).strip() if row and row[0] else None


def cartes_du_jour(
    cfg: Config,
    personne: Personne,
    jour: date,
    *,
    aujourdhui: date | None = None,
    meteo: Callable[[Config, date], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """``bonjour`` (date, prénom, météo, événements), ``brief`` (si le brief du matin existe) et ``a_noter``."""
    from tools import agenda, anniversaires, cartes, echeances, identite

    aujourdhui = aujourdhui or datetime.now().astimezone().date()
    with identite.requete(personne.linux_user):
        evenements = _sur(lambda: agenda.agenda_lire(jour, jour), [])
        echeances_l = _sur(lambda: echeances.lister(14, jour), [])
        anniversaires_l = _sur(lambda: anniversaires.lister(14, jour), [])
    meteo_j = _sur(lambda: (meteo or meteo_jour)(cfg, jour), None)
    decalage = (jour - aujourdhui).days
    navigation = [
        {"libelle": "Jour suivant", "action": "ouvrir", "args": {"ecran": "jour", "date": (jour + timedelta(days=1)).isoformat()}},
    ]
    if decalage > 0:
        navigation.insert(0, {"libelle": "Jour précédent", "action": "ouvrir",
                              "args": {"ecran": "jour", "date": (jour - timedelta(days=1)).isoformat()}})
    navigation.append({"libelle": "Écouter", "action": "ecouter", "args": {}})
    bonjour = carte(
        "bonjour",
        {
            "date": jour.isoformat(),
            "date_longue": date_longue(jour),
            "decalage_jours": decalage,
            "prenom": personne.prenom,
            "meteo": meteo_j,
            "evenements": [
                {k: e.get(k) for k in ("agenda", "titre", "debut", "fin", "lieu", "journee_entiere")}
                for e in evenements[:4]
            ],
            "evenements_total": len(evenements),
        },
        titre=f"Bonjour {personne.prenom}" if decalage == 0 else date_longue(jour).capitalize(),
        actions=navigation,
    )
    sortie = [bonjour]
    if decalage == 0:
        texte = texte_brief_du_jour(cfg, jour)
        if texte:
            sortie.append(carte("brief", {"texte": texte, "date": jour.isoformat()}, titre="Brief du jour",
                                actions=[{"libelle": "Écouter", "action": "ecouter", "args": {"texte": texte}}]))
    elements = cartes.elements_echeances(echeances_l) + cartes.elements_anniversaires(anniversaires_l)
    sortie.append(cartes.carte_a_noter(elements))
    return valides(sortie)
