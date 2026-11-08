"""Météo de la commune du foyer (Open-Meteo, open source, sans clé) : outil ``meteo_lire`` et carte du jour.

Source unique de la logique Open-Meteo : l'outil MCP ``meteo_lire`` (``tools/server.py``) et la carte ``bonjour``
du cœur (``coeur/brief.py``) passent par ici. Commune du foyer lue dans ``config.yaml`` :

    meteo:
      commune: "<commune du foyer>"   # libellé affiché
      latitude: 45.0                  # facultatif
      longitude: 2.0

Sans latitude/longitude, la commune est géocodée une fois (geocoding-api.open-meteo.com) et mise en cache dans
``data/web/meteo-lieu.json``. Les prévisions sont gardées 30 min en mémoire par lieu. Les deux hôtes sont dans la
liste blanche du proxy sortant (``install/reseau-autorise.txt``). Sans réseau ou sans commune : ``None`` côté carte
du jour, message clair (avec une alternative) côté outil.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tools import cartes
from tools.common import data_dir, get_logger, load_config, parse_date, today

log = get_logger("homeos.meteo")

URL_PREVISIONS = "https://api.open-meteo.com/v1/forecast"
URL_GEOCODAGE = "https://geocoding-api.open-meteo.com/v1/search"
DUREE_CACHE_METEO_S = 30 * 60
JOURS_PREVISION = 14  # la carte du jour se feuillette jusqu'à J+14
JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre")

# Codes WMO d'Open-Meteo → (libellé, icône du front).
CODES_METEO: dict[int, tuple[str, str]] = {
    0: ("Ciel dégagé", "soleil"), 1: ("Plutôt dégagé", "soleil"), 2: ("Partiellement nuageux", "nuage-soleil"),
    3: ("Couvert", "nuage"), 45: ("Brouillard", "brouillard"), 48: ("Brouillard givrant", "brouillard"),
    51: ("Bruine légère", "pluie"), 53: ("Bruine", "pluie"), 55: ("Bruine forte", "pluie"),
    61: ("Pluie faible", "pluie"), 63: ("Pluie", "pluie"), 65: ("Forte pluie", "pluie"),
    71: ("Neige faible", "neige"), 73: ("Neige", "neige"), 75: ("Forte neige", "neige"),
    80: ("Averses", "averse"), 81: ("Averses", "averse"), 82: ("Fortes averses", "averse"),
    95: ("Orage", "orage"), 96: ("Orage et grêle", "orage"), 99: ("Orage et grêle", "orage"),
}
# Moments de la journée (heures locales incluses) et heure représentative.
MOMENTS: tuple[tuple[str, int, int, int], ...] = (("matin", 6, 11, 9), ("après-midi", 12, 17, 15), ("soir", 18, 23, 20))

LIEN_SECOURS = "https://meteofrance.com"

Telecharger = Callable[[str], bytes]
_cache_meteo: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}
_cache_lieux: dict[str, dict[str, Any]] = {}  # communes demandées hors commune du foyer (en mémoire)


class MeteoIndisponible(ValueError):
    """Message en français, avec une solution, renvoyé tel quel au modèle par l'outil."""


def date_longue(jour: date) -> str:
    return f"{JOURS_FR[jour.weekday()]} {jour.day} {MOIS_FR[jour.month - 1]}"


def _telecharger(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=8) as reponse:  # proxy sortant pris dans l'environnement
        return reponse.read(512 * 1024)


def _normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", sans_accents.lower()).strip()


def commune_du_foyer(config: dict[str, Any] | None = None) -> str:
    cfg = load_config() if config is None else config
    bloc = cfg.get("meteo") if isinstance(cfg.get("meteo"), dict) else {}
    return str(bloc.get("commune") or "").strip()


def designe_le_foyer(commune: str | None, config: dict[str, Any] | None = None) -> bool:
    """Vrai si `commune` est vide ou désigne la commune du foyer, même abrégée (premier mot, début du nom)."""
    demande = _normaliser(commune or "")
    if not demande:
        return True
    foyer = _normaliser(commune_du_foyer(config))
    return bool(foyer) and (demande == foyer or foyer.startswith(demande + " ") or foyer.split(" ")[0] == demande)


def geocoder(commune: str, telecharger: Telecharger | None = None) -> dict[str, Any] | None:
    """Première commune française trouvée : ``{commune, latitude, longitude}`` ; None si introuvable ou hors ligne."""
    params = {"name": commune, "count": 1, "language": "fr", "countryCode": "FR"}
    try:
        brut = json.loads((telecharger or _telecharger)(f"{URL_GEOCODAGE}?{urllib.parse.urlencode(params)}"))
        premier = (brut.get("results") or [])[0]
        return {"commune": str(premier.get("name") or commune), "latitude": float(premier["latitude"]),
                "longitude": float(premier["longitude"])}
    except Exception:
        log.info("géocodage de « %s » impossible", commune, exc_info=True)
        return None


def lieu_meteo(
    config: dict[str, Any] | None = None,
    donnees: Path | None = None,
    telecharger: Telecharger | None = None,
) -> dict[str, Any] | None:
    """Lieu de la commune du foyer : coordonnées de config.yaml, sinon géocodage mis en cache ; None sans commune."""
    cfg = load_config() if config is None else config
    bloc = cfg.get("meteo") if isinstance(cfg.get("meteo"), dict) else {}
    commune = str(bloc.get("commune") or "").strip()
    try:
        if bloc.get("latitude") is not None and bloc.get("longitude") is not None:
            return {"commune": commune, "latitude": float(bloc["latitude"]), "longitude": float(bloc["longitude"])}
    except (TypeError, ValueError):
        log.warning("meteo.latitude / meteo.longitude invalides dans config.yaml")
    if not commune:
        return None
    cache = (donnees or data_dir()) / "web" / "meteo-lieu.json"
    try:
        lu = json.loads(cache.read_text(encoding="utf-8"))
        if lu.get("commune") == commune:
            return lu
    except (OSError, ValueError):
        pass
    trouve = geocoder(commune, telecharger)
    if trouve is None:
        return None
    lieu = {"commune": commune, "latitude": trouve["latitude"], "longitude": trouve["longitude"]}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(lieu, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return lieu


def _lieu_autre(commune: str, telecharger: Telecharger | None) -> dict[str, Any] | None:
    cle = _normaliser(commune)
    if cle not in _cache_lieux:
        trouve = geocoder(commune, telecharger)
        if trouve is None:
            return None
        _cache_lieux[cle] = trouve
    return _cache_lieux[cle]


def previsions(lieu: dict[str, Any], telecharger: Telecharger | None = None) -> dict[str, Any] | None:
    """Réponse brute d'Open-Meteo (journalier, horaire, actuel) pour un lieu, gardée 30 min ; None hors ligne."""
    cle = (round(float(lieu["latitude"]), 3), round(float(lieu["longitude"]), 3))
    maintenant = time.monotonic()
    en_cache = _cache_meteo.get(cle)
    if en_cache is not None and maintenant - en_cache[0] <= DUREE_CACHE_METEO_S:
        return en_cache[1]
    params = {
        "latitude": lieu["latitude"], "longitude": lieu["longitude"], "timezone": "Europe/Paris",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "forecast_days": JOURS_PREVISION,
    }
    try:
        brut = json.loads((telecharger or _telecharger)(f"{URL_PREVISIONS}?{urllib.parse.urlencode(params)}"))
    except Exception:
        log.info("météo indisponible", exc_info=True)
        return None
    if not isinstance(brut, dict):
        return None
    _cache_meteo[cle] = (maintenant, brut)
    return brut


def _libelle(code: Any) -> tuple[str, str]:
    try:
        return CODES_METEO.get(int(code), ("—", "nuage"))
    except (TypeError, ValueError):
        return ("—", "nuage")


def _arrondi(v: Any) -> Any:
    return round(v, 1) if isinstance(v, float) else v


def _moments(brut: dict[str, Any], jour: date) -> list[dict[str, Any]]:
    horaire = brut.get("hourly") or {}
    heures = list(horaire.get("time") or [])
    if not heures:
        return []
    index = {h: i for i, h in enumerate(heures)}

    def serie(nom: str, i: int) -> Any:
        s = horaire.get(nom) or []
        return s[i] if i < len(s) else None

    sortie: list[dict[str, Any]] = []
    for nom, debut, fin, repere in MOMENTS:
        positions = [index[k] for h in range(debut, fin + 1) if (k := f"{jour.isoformat()}T{h:02d}:00") in index]
        if not positions:
            continue
        i_repere = index.get(f"{jour.isoformat()}T{repere:02d}:00", positions[len(positions) // 2])
        pluies = [p for i in positions if isinstance(p := serie("precipitation_probability", i), (int, float))]
        libelle, icone = _libelle(serie("weather_code", i_repere))
        sortie.append({"moment": nom, "temperature": _arrondi(serie("temperature_2m", i_repere)),
                       "pluie_pct": max(pluies) if pluies else None, "libelle": libelle, "icone": icone})
    return sortie


def meteo_jour(
    jour: date,
    config: dict[str, Any] | None = None,
    donnees: Path | None = None,
    telecharger: Telecharger | None = None,
    *,
    commune: str | None = None,
    aujourdhui: date | None = None,
) -> dict[str, Any] | None:
    """Météo d'un jour : commune, min, max, libellé, icône, pluie % (champs de la carte ``bonjour``), plus pluie en
    mm, vent, rafales, moments (matin, après-midi, soir) et, pour aujourd'hui, conditions actuelles.

    `commune` vide ou désignant celle du foyer : commune du foyer ; sinon la commune demandée (géocodée).
    None si indisponible (pas de commune, hors ligne, jour hors prévisions)."""
    cfg = load_config() if config is None else config
    if designe_le_foyer(commune, cfg):
        lieu = lieu_meteo(cfg, donnees, telecharger)
    else:
        lieu = _lieu_autre(str(commune), telecharger)
    if lieu is None:
        return None
    brut = previsions(lieu, telecharger)
    if brut is None:
        return None
    quotidien = brut.get("daily") or {}
    try:
        i = list(quotidien.get("time") or []).index(jour.isoformat())
    except ValueError:
        return None

    def valeur(nom: str) -> Any:
        serie = quotidien.get(nom) or []
        return _arrondi(serie[i]) if i < len(serie) else None

    libelle, icone = _libelle(valeur("weather_code"))
    sortie: dict[str, Any] = {
        "commune": lieu.get("commune") or "",
        "date": jour.isoformat(),
        "min": valeur("temperature_2m_min"),
        "max": valeur("temperature_2m_max"),
        "pluie_pct": valeur("precipitation_probability_max"),
        "pluie_mm": valeur("precipitation_sum"),
        "vent_kmh": valeur("wind_speed_10m_max"),
        "rafales_kmh": valeur("wind_gusts_10m_max"),
        "libelle": libelle,
        "icone": icone,
        "moments": _moments(brut, jour),
    }
    actuel = brut.get("current") if isinstance(brut.get("current"), dict) else None
    if actuel and jour == (aujourdhui or today()):
        lib_a, ico_a = _libelle(actuel.get("weather_code"))
        sortie["actuel"] = {"temperature": _arrondi(actuel.get("temperature_2m")), "libelle": lib_a, "icone": ico_a,
                            "vent_kmh": _arrondi(actuel.get("wind_speed_10m")), "heure": str(actuel.get("time") or "")[11:16]}
    return sortie


# --- outil meteo_lire -------------------------------------------------------------------------------------------


def jour_demande(jour: str | date | None, aujourdhui: date | None = None) -> date:
    """`None`, « aujourd'hui », « demain », « après-demain », un jour de la semaine (le prochain) ou une date."""
    ref = aujourdhui or today()
    if isinstance(jour, date):
        return jour
    texte = _normaliser(str(jour or ""))
    if texte in ("", "aujourd hui", "aujourdhui", "ce jour", "maintenant", "ce soir", "cet apres midi", "ce matin"):
        return ref
    if texte == "demain":
        return ref + timedelta(days=1)
    if texte in ("apres demain", "apres-demain"):
        return ref + timedelta(days=2)
    noms = [_normaliser(j) for j in JOURS_FR]
    mot = texte.removesuffix(" prochain")
    if mot in noms:
        ecart = (noms.index(mot) - ref.weekday()) % 7
        return ref + timedelta(days=ecart)
    try:
        return parse_date(str(jour).strip())
    except (TypeError, ValueError):
        raise MeteoIndisponible(
            f"Je n'ai pas compris le jour « {jour} » : donne « aujourd'hui », « demain », un jour de la semaine ou "
            "une date AAAA-MM-JJ."
        ) from None


def lire(
    jour: str | date | None = None,
    commune: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    donnees: Path | None = None,
    telecharger: Telecharger | None = None,
    aujourdhui: date | None = None,
) -> dict[str, Any]:
    """Logique de l'outil ``meteo_lire`` : météo d'un jour pour la commune du foyer (ou `commune`)."""
    cfg = load_config() if config is None else config
    ref = aujourdhui or today()
    cible = jour_demande(jour, ref)
    ecart = (cible - ref).days
    if ecart < 0:
        raise MeteoIndisponible("Je n'ai que les prévisions, pas la météo des jours passés.")
    if ecart >= JOURS_PREVISION:
        raise MeteoIndisponible(
            f"Les prévisions vont jusqu'à {JOURS_PREVISION - 1} jours : pour le {date_longue(cible)}, c'est trop tôt. "
            "Redemande à l'approche de la date."
        )
    foyer = designe_le_foyer(commune, cfg)
    nom_lieu = commune_du_foyer(cfg) if foyer else str(commune).strip()
    if foyer and not nom_lieu and lieu_meteo(cfg, donnees, telecharger) is None:
        raise MeteoIndisponible(
            "La commune du foyer n'est pas réglée (réglage meteo.commune de la configuration). Demande-la à la "
            "personne et rappelle l'outil avec `commune` ; un propriétaire peut l'enregistrer pour la suite."
        )
    resultat = meteo_jour(cible, cfg, donnees, telecharger, commune=None if foyer else commune, aujourdhui=ref)
    if resultat is None:
        raise MeteoIndisponible(
            f"Le service météo ne répond pas pour {nom_lieu or 'la commune'} en ce moment. Propose de réessayer dans "
            f"quelques minutes, ou donne le lien {LIEN_SECOURS} (chercher « {nom_lieu or 'la commune'} »)."
        )
    resultat["date_longue"] = date_longue(cible)
    resultat["quand"] = {0: "aujourd'hui", 1: "demain", 2: "après-demain"}.get(ecart, date_longue(cible))
    resultat["commune_du_foyer"] = foyer
    resultat["source"] = "Open-Meteo"
    return resultat


@cartes.pour("meteo_lire")
def _carte_meteo(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Carte ``meteo`` (canal web) : commune, jour, températures, ciel, pluie, moments de la journée."""
    if not isinstance(resultat, dict) or not resultat.get("date"):
        return None
    champs = ("commune", "date", "date_longue", "quand", "min", "max", "pluie_pct", "pluie_mm", "vent_kmh",
              "rafales_kmh", "libelle", "icone", "moments", "actuel")
    donnees = {k: resultat[k] for k in champs if k in resultat}
    titre = f"Météo {resultat.get('quand') or ''} à {resultat.get('commune') or ''}".replace("  ", " ").strip()
    actions = []
    if resultat.get("commune_du_foyer", True):
        actions.append(cartes.action("Carte du jour", "ouvrir", {"ecran": "jour", "date": resultat["date"]}))
    return cartes.carte("meteo", donnees, titre=titre, actions=actions)
