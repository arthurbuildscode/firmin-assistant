"""Agenda : lecture iCal (cache disque 5 min) et ajout d'événement (Google optionnel).

Les URLs iCal sont résolues depuis `/etc/homeos/ical*.env` (repli : `data/secrets.env`) via le nom de
variable indiqué dans `config.yaml` (`agendas[].ical_url_env`). Elles n'apparaissent jamais dans
une sortie d'outil ni dans `actions_log`.

Agenda privé (lot 5d, revue R3) : `prive: <linux_user>` dans `config.yaml` réserve l'agenda à cette personne.
Il n'est lu que par ses propres sessions (jeton de la personne pour le serveur d'outils HTTP du lot 7, utilisateur
Linux en stdio), jamais par l'autre conjoint ni par les crons (briefs du groupe). Son URL est rangée par
migrer-secrets.sh dans `ical-<linux_user>.env` (640 root:homeos en mode http, 640 root:<linux_user> en stdio).
"""

from __future__ import annotations

import hashlib
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tools import db, identite
from tools.common import (
    TZ,
    data_dir,
    get_logger,
    load_config,
    load_secrets,
    parse_datetime,
)

log = get_logger(__name__)

CACHE_TTL_S = 300
FETCH_TIMEOUT_S = 15

GOOGLE_CLIENT_FILE = "google-oauth.json"
GOOGLE_TOKEN_FILE = "google-token.json"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


# --- configuration ----------------------------------------------------------


def agendas_configures() -> list[dict[str, Any]]:
    """Agendas déclarés dans config.yaml (nom, variable d'URL, droit d'écriture)."""
    config = load_config()
    agendas = config.get("agendas") or []
    resultat: list[dict[str, Any]] = []
    for a in agendas:
        if not isinstance(a, dict) or not a.get("nom"):
            continue
        resultat.append(
            {
                "nom": str(a["nom"]),
                "ical_url_env": str(a.get("ical_url_env") or ""),
                "ecriture": bool(a.get("ecriture", True)),
                "google_calendar_id": a.get("google_calendar_id"),
                "prive": str(a["prive"]).strip() if a.get("prive") else None,
            }
        )
    return resultat


def utilisateur_courant() -> str | None:
    """Personne courante : jeton de la requête (serveur d'outils HTTP, lot 7), sinon HOMEOS_PERSONNE (tests, bot en
    clé API), sinon l'utilisateur Linux réel du processus (uid). Voir `tools.identite`."""
    return identite.utilisateur_courant()


def agenda_visible(agenda: dict[str, Any], utilisateur: str | None = None) -> bool:
    """Un agenda `prive` n'est visible que de sa personne ; les autres le sont de tous."""
    proprietaire = agenda.get("prive")
    if not proprietaire:
        return True
    qui = utilisateur if utilisateur is not None else utilisateur_courant()
    return bool(qui) and str(qui).casefold() == str(proprietaire).casefold()


def _url_pour(agenda: dict[str, Any], secrets: dict[str, str]) -> str | None:
    env = str(agenda.get("ical_url_env") or "")
    # Seules les variables ICAL_* sont des URLs d'agenda : sous homeos (serveur d'outils), d'autres secrets sont
    # lisibles, une configuration erronée ne doit pas en faire une « URL ».
    if not env.startswith("ICAL_"):
        return None
    url = secrets.get(env, "").strip()
    return url or None


# --- récupération + cache ---------------------------------------------------


def _cache_dir() -> Path:
    d = data_dir() / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _telecharger(url: str) -> bytes:
    """Lit l'URL (https, file:// ou chemin local). Les erreurs sont neutralisées (pas d'URL dans le message)."""
    if url.startswith("file://"):
        return Path(url[len("file://") :]).read_bytes()
    if "://" not in url:
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "homeos/0.1"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read()


def _lire_ics(url: str, nom: str) -> bytes | None:
    """Renvoie le contenu iCal, depuis le cache si frais (< 5 min), sinon téléchargé."""
    cle = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    fichier = _cache_dir() / f"{cle}.ics"
    if fichier.exists() and time.time() - fichier.stat().st_mtime < CACHE_TTL_S:
        return fichier.read_bytes()
    try:
        contenu = _telecharger(url)
    except Exception as exc:  # noqa: BLE001 — jamais l'URL dans le message
        log.warning("agenda « %s » : téléchargement impossible (%s)", nom, exc.__class__.__name__)
        if fichier.exists():
            log.info("agenda « %s » : utilisation du cache périmé", nom)
            return fichier.read_bytes()
        return None
    try:
        fichier.write_bytes(contenu)
    except OSError as exc:
        log.warning("cache agenda non écrit : %s", exc.__class__.__name__)
    return contenu


# --- lecture ----------------------------------------------------------------


def _to_local(valeur: Any) -> tuple[datetime | date, bool]:
    """Normalise un DTSTART/DTEND icalendar : (valeur locale, journee_entiere)."""
    if isinstance(valeur, datetime):
        dt = valeur if valeur.tzinfo else valeur.replace(tzinfo=TZ)
        return dt.astimezone(TZ), False
    if isinstance(valeur, date):
        return valeur, True
    return parse_datetime(str(valeur)), False


def evenements_depuis_ics(contenu: bytes, nom: str, debut: date, fin: date) -> list[dict[str, Any]]:
    """Développe les occurrences (récurrences incluses) entre `debut` et `fin` inclus."""
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(contenu)
    except Exception as exc:  # noqa: BLE001
        log.warning("agenda « %s » : iCal illisible (%s)", nom, exc.__class__.__name__)
        return []

    debut_dt = datetime(debut.year, debut.month, debut.day, tzinfo=TZ)
    fin_dt = datetime(fin.year, fin.month, fin.day, tzinfo=TZ) + timedelta(days=1)
    try:
        occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(debut_dt, fin_dt)
    except Exception as exc:  # noqa: BLE001
        log.warning("agenda « %s » : développement des récurrences impossible (%s)", nom, exc.__class__.__name__)
        return []

    resultat: list[dict[str, Any]] = []
    for ev in occurrences:
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        d_debut, journee = _to_local(dtstart.dt)
        dtend = ev.get("DTEND")
        if dtend is not None:
            d_fin, _ = _to_local(dtend.dt)
        else:
            d_fin = d_debut + timedelta(days=1) if journee else d_debut
        if journee and isinstance(d_fin, date) and not isinstance(d_fin, datetime):
            # iCal : DTEND exclusif pour les journées entières → on affiche le dernier jour inclus
            d_fin = d_fin - timedelta(days=1)
            d_fin = max(d_fin, d_debut)
        resultat.append(
            {
                "agenda": nom,
                "titre": str(ev.get("SUMMARY", "")).strip() or "(sans titre)",
                "debut": d_debut.isoformat(),
                "fin": d_fin.isoformat(),
                "lieu": (str(ev.get("LOCATION")).strip() if ev.get("LOCATION") else None),
                "journee_entiere": journee,
                "_tri": d_debut.isoformat(),
            }
        )
    return resultat


def agenda_lire(debut: date, fin: date, agendas: list[str] | None = None) -> list[dict[str, Any]]:
    """Événements des agendas configurés entre `debut` et `fin` (inclus), triés par début."""
    if fin < debut:
        debut, fin = fin, debut
    configures = [a for a in agendas_configures() if agenda_visible(a)]
    if agendas:
        voulus = {a.strip().casefold() for a in agendas}
        configures = [a for a in configures if a["nom"].casefold() in voulus]
    secrets = load_secrets()
    evenements: list[dict[str, Any]] = []
    for agenda in configures:
        url = _url_pour(agenda, secrets)
        if not url:
            log.info("agenda « %s » : aucune URL iCal configurée", agenda["nom"])
            continue
        contenu = _lire_ics(url, agenda["nom"])
        if contenu is None:
            continue
        evenements.extend(evenements_depuis_ics(contenu, agenda["nom"], debut, fin))

    # Tri : par jour, puis journées entières en premier, puis heure de début
    evenements.sort(key=lambda e: (e["_tri"][:10], 0 if e["journee_entiere"] else 1, e["_tri"]))
    for e in evenements:
        e.pop("_tri", None)
    return evenements


# --- ajout ------------------------------------------------------------------


def google_configure() -> bool:
    """Vrai si l'écriture Google Calendar est configurée (client OAuth + jeton présents)."""
    d = data_dir()
    return (d / GOOGLE_CLIENT_FILE).exists() and (d / GOOGLE_TOKEN_FILE).exists()


def _texte_manuel(agenda: str, titre: str, debut: str, fin: str, lieu: str | None, description: str | None) -> str:
    lignes = [
        "L'ajout automatique n'est pas configuré : il faut créer l'événement à la main dans l'agenda. Texte prêt à copier :",
        f"Agenda : {agenda}",
        f"Titre : {titre}",
        f"Début : {debut}",
        f"Fin : {fin}",
    ]
    if lieu:
        lignes.append(f"Lieu : {lieu}")
    if description:
        lignes.append(f"Description : {description}")
    return "\n".join(lignes)


def agenda_ajouter(
    agenda: str,
    titre: str,
    debut: str,
    fin: str,
    lieu: str | None = None,
    description: str | None = None,
) -> str:
    """Prépare l'ajout d'un événement.

    - Google configuré et agenda ouvert en écriture → action `pending` + `CONFIRMATION_REQUISE:<id>:<résumé>`.
    - Sinon → message avec le texte prêt à copier.
    """
    d_debut = parse_datetime(debut)
    d_fin = parse_datetime(fin)
    if d_fin < d_debut:
        raise ValueError("La fin de l'événement est antérieure à son début.")
    payload = {
        "agenda": agenda,
        "titre": titre.strip(),
        "debut": d_debut.isoformat(),
        "fin": d_fin.isoformat(),
        "lieu": lieu,
        "description": description,
    }
    conf = next((a for a in agendas_configures() if a["nom"].casefold() == agenda.strip().casefold()), None)
    ecriture_ok = conf is not None and conf.get("ecriture", True)

    if google_configure() and ecriture_ok:
        resume = f"Ajouter « {payload['titre']} » le {d_debut.strftime('%d/%m/%Y %H:%M')} dans l'agenda {agenda}"
        action_id = db.log_action("agenda_ajouter", resume, payload, status="pending")
        return f"CONFIRMATION_REQUISE:{action_id}:{resume}"

    raison = ""
    if conf is not None and not ecriture_ok:
        raison = f"L'agenda « {agenda} » est en lecture seule. "
    texte = raison + _texte_manuel(agenda, payload["titre"], payload["debut"], payload["fin"], lieu, description)
    db.log_action("agenda_ajouter", f"Texte manuel pour « {payload['titre']} » ({agenda})", payload, status="manual")
    return texte


def executer_ajout_google(payload: dict[str, Any]) -> str:
    """Crée réellement l'événement via l'API Google Calendar (appelé par le runner après confirmation)."""
    try:
        from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Bibliothèques Google absentes : installer google-api-python-client et google-auth-oauthlib."
        ) from exc

    d = data_dir()
    creds = Credentials.from_authorized_user_file(str(d / GOOGLE_TOKEN_FILE), GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request  # type: ignore[import-not-found]

        creds.refresh(Request())
        try:
            (d / GOOGLE_TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
        except OSError:
            log.warning("jeton Google rafraîchi mais non sauvegardé")

    conf = next((a for a in agendas_configures() if a["nom"].casefold() == str(payload["agenda"]).casefold()), None)
    calendar_id = (conf or {}).get("google_calendar_id") or "primary"

    corps: dict[str, Any] = {
        "summary": payload["titre"],
        "start": {"dateTime": payload["debut"], "timeZone": "Europe/Paris"},
        "end": {"dateTime": payload["fin"], "timeZone": "Europe/Paris"},
    }
    if payload.get("lieu"):
        corps["location"] = payload["lieu"]
    if payload.get("description"):
        corps["description"] = payload["description"]

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cree = service.events().insert(calendarId=calendar_id, body=corps).execute()
    return f"Événement « {payload['titre']} » ajouté à l'agenda {payload['agenda']} (id {cree.get('id', '?')})."
