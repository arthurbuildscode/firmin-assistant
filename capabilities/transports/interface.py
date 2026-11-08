"""Interface commune des fournisseurs de la capacité « transports ».

Un **fournisseur** = un réseau ou un opérateur (Cartreize, SNCF, un métro, les vols…) : un dossier
`fournisseurs/<nom>/` avec un manifeste déclaratif `fournisseur.yaml` et un `source.py` qui définit une classe
`Source` (sous-classe de :class:`Source` ci-dessous, ou duck-typée avec les mêmes méthodes) :

    class Source(interface.Source):
        def prochains_departs(self, trajet, horizon=60): ...   # → interface.departs(...)
        def perturbations(self, trajet): ...                   # → [interface.perturbation(...)]
        def etat_train(self, numero, date): ...                # → interface.etat(...)
        def etat_vol(self, numero, date): ...                  # → interface.etat(...)

Ce qu'un réseau ne sait pas donner lève :class:`NonPrisEnCharge` (comportement par défaut). Tout accès réseau
passe par `self.client` (hôtes du manifeste approuvés seulement, https, robots.txt pour une page web) : un
fournisseur n'ouvre jamais de connexion lui-même. Ses réglages (adresse d'un flux, etc.) sont dans
`self.parametres`.

Ce module ne dépend que de la bibliothèque standard : un fournisseur fabriqué par l'atelier peut l'importer
(`from capabilities.transports import interface`), c'est le seul module de la capacité qui lui est permis.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

MODES = ("bus", "car", "metro", "tram", "train", "avion", "navette", "bateau", "velo")
TYPES_SOURCE = ("api_ouverte", "page_web", "manuel")
STATUTS_DEPART = ("a_venir", "a_l_arret", "supprime", "prevu", "passe", "passe_theorique")
STATUTS_ETAT = ("a_l_heure", "en_retard", "supprime", "prevu", "parti", "arrive", "inconnu")
# Compagnies aériennes courantes (code IATA → nom) : repérer un vol dans l'agenda, dire où vérifier son état.
COMPAGNIES: dict[str, str] = {
    "AF": "Air France", "KL": "KLM", "U2": "easyJet", "EC": "easyJet Europe", "FR": "Ryanair", "TO": "Transavia France",
    "HV": "Transavia", "V7": "Volotea", "VY": "Vueling", "IB": "Iberia", "LH": "Lufthansa", "BA": "British Airways",
    "AZ": "ITA Airways", "SN": "Brussels Airlines", "LX": "Swiss", "TP": "TAP Air Portugal", "AT": "Royal Air Maroc",
    "TU": "Tunisair", "AH": "Air Algérie", "SS": "Corsair", "TX": "Air Caraïbes", "BF": "French bee", "XK": "Air Corsica",
    "EK": "Emirates", "QR": "Qatar Airways", "TK": "Turkish Airlines", "DL": "Delta", "UA": "United", "AA": "American Airlines",
}
SEUIL_MIN = 5  # filtre « ça vaut le coup » : jamais d'alerte pour un retard plus petit

JOURS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
_NOMS_JOURS = {
    "lun": 0, "lundi": 0, "mon": 0, "monday": 0,
    "mar": 1, "mardi": 1, "tue": 1, "tuesday": 1,
    "mer": 2, "mercredi": 2, "wed": 2, "wednesday": 2,
    "jeu": 3, "jeudi": 3, "thu": 3, "thursday": 3,
    "ven": 4, "vendredi": 4, "fri": 4, "friday": 4,
    "sam": 5, "samedi": 5, "sat": 5, "saturday": 5,
    "dim": 6, "dimanche": 6, "sun": 6, "sunday": 6,
}  # fmt: skip
_MOTS_IGNORES = {"le", "les", "du", "et", "de", "chaque", "tous", "jours", "jour"}


class ErreurTransports(ValueError):
    """Erreur métier, message en français destiné à la personne (relayé tel quel par l'assistant)."""


class NonPrisEnCharge(ErreurTransports):
    """Le réseau ne donne pas cette information (ex. retard d'un vol sans source ouverte)."""


class SourceIndisponible(ErreurTransports):
    """Le serveur du réseau ne répond pas (ou refuse) : réessayer plus tard."""


# --- utilitaires purs ----------------------------------------------------------------------------------------------


def normaliser(texte: Any) -> str:
    """Minuscules, sans accents, ponctuation → espace, espaces réduits."""
    t = unicodedata.normalize("NFKD", str(texte or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slug(texte: Any, sep: str = "-") -> str:
    """« Élodie » → « elodie », « Jean Paul » → « jean-paul »."""
    return re.sub(r"[^a-z0-9_]+", sep, normaliser(texte).replace("_", sep)).strip(sep)


def maintenant() -> datetime:
    """Heure de Paris (surchargeable par HOMEOS_NOW, comme le reste du dépôt, pour les tests)."""
    force = os.environ.get("HOMEOS_NOW")
    if force:
        dt = datetime.fromisoformat(force)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
    return datetime.now(TZ)


def hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes(heure: str) -> int:
    h, m = str(heure).split(":")[:2]
    return int(h) * 60 + int(m)


def lire_heure(valeur: Any) -> str:
    """« 7h40 », « 07:40 », « 7h » → « 07:40 ». Un entier (YAML lit 07:40 non quoté en base 60) est accepté."""
    if isinstance(valeur, bool):
        raise ErreurTransports(f"Heure invalide : « {valeur} » (attendu HH:MM).")
    if isinstance(valeur, int):
        return hhmm(valeur)
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:h|:)\s*(\d{2})?(?::\d{2})?\s*", str(valeur), re.IGNORECASE)
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ErreurTransports(f"Heure invalide : « {valeur} » (attendu HH:MM, par exemple 07:40).")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def lire_jours(valeur: Any) -> list[str]:
    """Jours de la semaine → codes `lun`…`dim`, dans l'ordre.

    Accepte une liste ou un texte : « lun-ven », « du lundi au vendredi », « en semaine », « week-end »,
    « tous les jours », « lundi, mercredi et vendredi ». Vide → tous les jours.
    """
    if valeur is None:
        return list(JOURS)
    texte = " ".join(str(x) for x in valeur) if isinstance(valeur, (list, tuple, set)) else str(valeur)
    texte = re.sub(r"week\s*-?\s*end", " sam dim ", texte, flags=re.IGNORECASE)
    t = normaliser(re.sub(r"\s*-\s*", " au ", texte))  # « lun-ven » → « lun au ven »
    if not t:
        return list(JOURS)
    t = re.sub(r"\b(en )?semaine\b", " lun au ven ", t)
    if re.search(r"\btous les jours\b|\bquotidien\w*\b|\b7 7\b", t):
        return list(JOURS)
    jours: set[int] = set()
    mots = [m for m in re.split(r"[\s,;/]+", t) if m]
    i = 0
    while i < len(mots):
        mot = mots[i]
        if mot in _NOMS_JOURS:
            debut = _NOMS_JOURS[mot]
            if i + 2 < len(mots) and mots[i + 1] in {"au", "a"} and mots[i + 2] in _NOMS_JOURS:
                fin = _NOMS_JOURS[mots[i + 2]]
                k = debut
                while True:
                    jours.add(k)
                    if k == fin:
                        break
                    k = (k + 1) % 7
                i += 3
                continue
            jours.add(debut)
        elif mot not in _MOTS_IGNORES and mot not in {"au", "a"}:
            raise ErreurTransports(f"Jour non reconnu : « {mot} » (exemples : lun-ven, week-end, lundi et jeudi).")
        i += 1
    return [JOURS[k] for k in sorted(jours)] or list(JOURS)


def lire_date(valeur: Any, defaut: Date | None = None) -> Date:
    """« 2026-11-09 », « 09/11/2026 », « 9/11 », « aujourd'hui », « demain » → date."""
    instant = maintenant().date()
    texte = normaliser(valeur)
    if not texte or texte in {"aujourd hui", "ce jour", "today"}:
        return defaut or instant
    if texte in {"demain", "tomorrow"}:
        return instant + timedelta(days=1)
    if texte in {"apres demain"}:
        return instant + timedelta(days=2)
    brut = str(valeur).strip()
    try:
        return Date.fromisoformat(brut[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", brut)
    if m:
        annee = int(m.group(3)) if m.group(3) else instant.year
        annee += 2000 if annee < 100 else 0
        try:
            return Date(annee, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    raise ErreurTransports(f"Date non reconnue : « {valeur} » (exemples : 2026-11-09, 9/11, demain).")


# --- objets échangés -------------------------------------------------------------------------------------------------


def _resume_depart(d: dict[str, Any]) -> str:
    statut = d.get("statut")
    prevu, estime, retard, dans = d.get("heure_theorique"), d.get("heure_estimee"), d.get("retard_min"), d.get("dans_min")
    if statut == "supprime":
        return f"supprimé (prévu à {prevu})"
    if statut == "a_l_arret":
        return "à l'arrêt maintenant"
    if statut in {"passe", "passe_theorique"}:
        return f"passé il y a {abs(dans or 0)} min ({estime or prevu})"
    quand = f"dans {dans} min" if dans is not None and 0 <= dans <= 60 else f"à {estime or prevu}"
    if estime is None or retard is None:
        return f"{quand} ({prevu}, horaire théorique)"
    if retard > 0:
        precision = f"retard {retard} min"
    elif retard < 0:
        precision = f"en avance de {-retard} min"
    else:
        precision = "à l'heure"
    if d.get("signal_perdu"):
        precision += ", signal perdu : dernière estimation"
    elif d.get("deduit"):
        precision += ", estimé via l'arrêt suivant"
    if d.get("quai"):
        precision += f", voie {d['quai']}"
    return f"{quand} ({estime}, {precision})"


def depart(
    heure_theorique: str,
    *,
    heure_estimee: str | None = None,
    retard_min: int | None = None,
    statut: str | None = None,
    dans_min: int | None = None,
    ligne: str | None = None,
    destination: str | None = None,
    quai: str | None = None,
    numero: str | None = None,
    suivi: bool = False,
    reference: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Un départ normalisé (champs stables pour l'assistant, les cartes et les alertes).

    `dans_min` est calculé depuis `reference` (défaut : maintenant) si absent ; `retard_min` est déduit des deux
    heures si une seule information manque. `source` vaut `temps_reel` dès qu'une heure estimée est connue."""
    prevu = lire_heure(heure_theorique)
    estime = lire_heure(heure_estimee) if heure_estimee else None
    if estime is not None and retard_min is None:
        retard_min = ((minutes(estime) - minutes(prevu) + 720) % 1440) - 720
    if dans_min is None:
        ref = reference or maintenant()
        cible = estime or prevu
        dans_min = ((minutes(cible) - (ref.hour * 60 + ref.minute) + 720) % 1440) - 720
    if statut is None:
        statut = "a_venir" if estime is not None else "prevu"
        if dans_min < 0:
            statut = "passe" if estime is not None else "passe_theorique"
    d: dict[str, Any] = {
        "heure_theorique": prevu,
        "heure_estimee": estime,
        "retard_min": retard_min if estime is not None else None,
        "dans_min": dans_min,
        "source": "temps_reel" if estime is not None else "theorique",
        "statut": statut,
        "ligne": ligne,
        "destination": destination,
        "quai": quai,
        "numero": numero,
        "suivi": bool(suivi),
    }
    d.update({k: v for k, v in extra.items() if k in ("deduit", "signal_perdu", "course")})
    d["resume"] = _resume_depart(d)
    for cle in ("deduit", "signal_perdu"):
        d.pop(cle, None)
    return d


def departs(
    liste: list[dict[str, Any]],
    *,
    temps_reel: bool,
    avertissement: str | None = None,
    passes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Résultat de `prochains_departs` : départs à venir (triés), départs récents, état du temps réel."""
    return {
        "departs": list(liste),
        "passes": list(passes or []),
        "temps_reel": bool(temps_reel),
        "avertissement": avertissement,
    }


def perturbation(titre: str, texte: str = "", *, effet: str = "", debut: str | None = None, fin: str | None = None) -> dict[str, Any]:
    """Une perturbation annoncée par le réseau (travaux, grève, suppression…)."""
    return {"titre": " ".join(str(titre).split())[:200], "texte": " ".join(str(texte).split())[:600],
            "effet": effet, "debut": debut, "fin": fin}


def etat(
    numero: str,
    mode: str,
    statut: str,
    *,
    date: str | None = None,
    retard_min: int | None = None,
    depart_: dict[str, Any] | None = None,
    arrivee: dict[str, Any] | None = None,
    arrets: list[dict[str, Any]] | None = None,
    perturbations: list[dict[str, Any]] | None = None,
    source: str = "temps_reel",
    message: str | None = None,
) -> dict[str, Any]:
    """État d'un train ou d'un vol par numéro. `depart_`/`arrivee` : {lieu, prevu, estime, quai|porte, terminal}."""
    if statut not in STATUTS_ETAT:
        statut = "inconnu"
    return {
        "numero": numero, "mode": mode, "date": date, "statut": statut, "retard_min": retard_min,
        "depart": depart_ or {}, "arrivee": arrivee or {}, "arrets": list(arrets or []),
        "perturbations": list(perturbations or []), "source": source, "message": message,
    }


# --- classe de base ---------------------------------------------------------------------------------------------------


class Source:
    """Interface commune d'un fournisseur. Les méthodes non surchargées lèvent `NonPrisEnCharge`.

    - `client` : accès réseau du fournisseur (`json`, `octets`, `telecharger`, `navigation`, `cle_api`, `lire_json`,
      `ecrire_json`, `dossier`) ;
    - `parametres` : réglages du fournisseur (manifeste, instance de modèle, réglages du foyer) ;
    - `manifeste` : le `fournisseur.yaml` validé (nom, titre, modes…).
    """

    def __init__(self, client: Any, parametres: dict[str, Any] | None = None, manifeste: Any = None) -> None:
        self.client = client
        self.parametres = dict(parametres or {})
        self.manifeste = manifeste

    @property
    def titre(self) -> str:
        return str(getattr(self.manifeste, "titre", "") or "ce réseau")

    def _non(self, quoi: str) -> NonPrisEnCharge:
        return NonPrisEnCharge(f"{self.titre} ne donne pas {quoi}.")

    def resoudre_trajet(self, trajet: dict[str, Any]) -> dict[str, Any]:
        """Complète un trajet (identifiants d'arrêts, ligne, sens) ; lève ErreurTransports si ambigu (candidats)."""
        return trajet

    def prochains_departs(self, trajet: dict[str, Any], horizon: int = 60) -> dict[str, Any]:
        raise self._non("d'horaires de départ")

    def perturbations(self, trajet: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def etat_train(self, numero: str, date: str) -> dict[str, Any]:
        raise self._non("l'état des trains")

    def etat_vol(self, numero: str, date: str) -> dict[str, Any]:
        raise self._non("l'état des vols")

    def rafraichir(self, forcer: bool = False) -> dict[str, Any] | None:
        """Grille théorique à télécharger ou préparer (cron `rafraichir_grilles`) ; None si rien à faire."""
        return None

    def verifier(self) -> dict[str, Any]:
        """Smoke test du fournisseur : {"ok": bool, "message": str}. Rapide, sans consommer de quota inutilement."""
        return {"ok": True, "message": f"{self.titre} : rien à vérifier"}


def __getattr__(nom: str) -> Any:
    """`interface.SourceGtfs` : moteur GTFS / GTFS-RT générique (gtfs.py), pour un fournisseur dont le réseau publie
    une grille GTFS (paramètres url_grille, url_temps_reel, url_alertes) — y compris un fournisseur de l'atelier."""
    if nom == "SourceGtfs":
        from .gtfs import SourceGtfs

        return SourceGtfs
    raise AttributeError(nom)
