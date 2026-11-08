"""Réservations de train ou d'avion repérées dans l'agenda (routine `veille_reservations`).

Un événement dont le titre ou le lieu porte un numéro de train (« TGV 6105 », « train n° 17512 », « OUIGO 7841 ») ou
de vol (« vol AF7664 », « ✈ U2 4321 », « AF7664 Marseille → Paris » quand le préfixe est une compagnie connue) devient
une réservation : {mode, numero, date, heure, titre, agenda}. Les agendas privés ne sont jamais lus par les crons
(CONTRACT §6) : seule la personne elle-même y voit ses réservations, en conversation.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from typing import Any

from . import interface

RE_TRAIN = re.compile(
    r"\b(?:tgv(?:\s+inoui)?|inoui|ouigo|ter|intercit[ée]s|ic|train|lyria|eurostar|tgv\s+lyria)\s*(?:n\s*[°o]\s*|no\.?\s*|#\s*)?(\d{3,6})\b",
    re.IGNORECASE,
)
RE_VOL_MOT = re.compile(r"(?:\bvol\b|\bflight\b|✈️?)\s*(?:n\s*[°o]\s*)?([A-Z0-9]{2})\s?(\d{1,4})\b", re.IGNORECASE)
RE_VOL_SEUL = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{2,4})\b")


def detecter(texte: str) -> list[tuple[str, str]]:
    """[(mode, numéro)] repérés dans un texte (sans doublon, dans l'ordre)."""
    trouves: list[tuple[str, str]] = []
    for m in RE_TRAIN.finditer(texte or ""):
        trouves.append(("train", m.group(1).lstrip("0") or m.group(1)))
    for m in RE_VOL_MOT.finditer(texte or ""):
        code = m.group(1).upper()
        if not code.isdigit():
            trouves.append(("avion", f"{code}{m.group(2)}"))
    if not any(mode == "avion" for mode, _ in trouves):
        for m in RE_VOL_SEUL.finditer(texte or ""):
            if m.group(1) in interface.COMPAGNIES:
                trouves.append(("avion", f"{m.group(1)}{m.group(2)}"))
    vus: set[tuple[str, str]] = set()
    return [t for t in trouves if not (t in vus or vus.add(t))]


def depuis_evenements(evenements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Réservations des événements de l'agenda (journées entières comprises, heure alors inconnue)."""
    resultat = []
    for ev in evenements:
        texte = " ".join(str(ev.get(k) or "") for k in ("titre", "lieu"))
        for mode, numero in detecter(texte):
            debut = str(ev.get("debut") or "")
            try:
                if "T" in debut:
                    dt = datetime.fromisoformat(debut)
                    dt = dt if dt.tzinfo else dt.replace(tzinfo=interface.TZ)
                    jour, heure = dt.astimezone(interface.TZ).date(), dt.astimezone(interface.TZ).strftime("%H:%M")
                else:
                    jour, heure = Date.fromisoformat(debut[:10]), None
            except ValueError:
                continue
            resultat.append({"mode": mode, "numero": numero, "date": jour.isoformat(), "heure": heure,
                             "titre": ev.get("titre"), "agenda": ev.get("agenda")})
    return resultat
