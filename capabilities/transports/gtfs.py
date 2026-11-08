"""Moteur GTFS / GTFS-RT de la capacité transports (sans dépendance : décodage protobuf maison).

- **GTFS-RT** : :func:`decoder_flux` lit un flux protobuf (ou sa forme JSON) → dictionnaire aux noms de champs de la
  spécification (`entity[].trip_update.stop_time_update[]`, `alert`…). :func:`encoder_flux` fait l'inverse (flux
  fictifs des tests, démonstrations). Seuls les messages utiles sont décodés (TripUpdate, Alert) ; le reste est ignoré.
- **GTFS statique** (zip) : :class:`Grille` lit les arrêts, les jours de circulation (calendar, calendar_dates), les
  courses qui desservent un arrêt puis un autre (« de A vers B ») et les arrêts d'une course par numéro. Les gros
  fichiers (stop_times.txt) sont parcourus en flux avec un préfiltre sur les octets : rien n'est chargé en entier.
- :class:`SourceGtfs` : fournisseur générique (`interface.Source`) qui combine les deux. Le modèle `gtfs_rt`
  (n'importe quelle autorité qui publie GTFS + GTFS-RT) et le fournisseur `sncf` s'en servent.

Spécification : https://gtfs.org/realtime/reference/ et https://gtfs.org/schedule/reference/.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import interface
from .interface import TZ, ErreurTransports, NonPrisEnCharge, SourceIndisponible

# --- GTFS-RT : protobuf ------------------------------------------------------------------------------------------------

# message → {numéro de champ: (nom, type, répété)} ; type = str | int | sint (int32/int64 signé) | bool | <message>
SCHEMA: dict[str, dict[int, tuple[str, str, bool]]] = {
    "FeedMessage": {1: ("header", "FeedHeader", False), 2: ("entity", "FeedEntity", True)},
    "FeedHeader": {1: ("gtfs_realtime_version", "str", False), 2: ("incrementality", "int", False),
                   3: ("timestamp", "int", False)},
    "FeedEntity": {1: ("id", "str", False), 2: ("is_deleted", "bool", False), 3: ("trip_update", "TripUpdate", False),
                   5: ("alert", "Alert", False)},
    "TripUpdate": {1: ("trip", "TripDescriptor", False), 2: ("stop_time_update", "StopTimeUpdate", True),
                   4: ("timestamp", "int", False), 5: ("delay", "sint", False)},
    "TripDescriptor": {1: ("trip_id", "str", False), 2: ("start_time", "str", False), 3: ("start_date", "str", False),
                       4: ("schedule_relationship", "int", False), 5: ("route_id", "str", False),
                       6: ("direction_id", "int", False)},
    "StopTimeUpdate": {1: ("stop_sequence", "int", False), 2: ("arrival", "StopTimeEvent", False),
                       3: ("departure", "StopTimeEvent", False), 4: ("stop_id", "str", False),
                       5: ("schedule_relationship", "int", False)},
    "StopTimeEvent": {1: ("delay", "sint", False), 2: ("time", "sint", False), 3: ("uncertainty", "sint", False)},
    "Alert": {1: ("active_period", "TimeRange", True), 5: ("informed_entity", "EntitySelector", True),
              6: ("cause", "int", False), 7: ("effect", "int", False), 8: ("url", "TranslatedString", False),
              10: ("header_text", "TranslatedString", False), 11: ("description_text", "TranslatedString", False)},
    "TimeRange": {1: ("start", "int", False), 2: ("end", "int", False)},
    "EntitySelector": {1: ("agency_id", "str", False), 2: ("route_id", "str", False), 3: ("route_type", "sint", False),
                       4: ("trip", "TripDescriptor", False), 5: ("stop_id", "str", False),
                       6: ("direction_id", "int", False)},
    "TranslatedString": {1: ("translation", "Translation", True)},
    "Translation": {1: ("text", "str", False), 2: ("language", "str", False)},
}
CANCELED = 3          # TripDescriptor.ScheduleRelationship
SKIPPED = 1           # StopTimeUpdate.ScheduleRelationship
EFFETS = {1: "pas de service", 2: "service réduit", 3: "retards importants", 4: "déviation", 5: "service ajouté",
          6: "service modifié", 7: "autre", 8: "inconnu", 9: "arrêt déplacé", 10: "aucun effet", 11: "accessibilité"}
TAILLE_MAX_FLUX = 30_000_000


class FluxInvalide(ErreurTransports):
    """Flux GTFS-RT illisible."""


def _varint(b: bytes, i: int) -> tuple[int, int]:
    resultat = decalage = 0
    while True:
        if i >= len(b):
            raise FluxInvalide("Flux temps réel tronqué.")
        c = b[i]
        i += 1
        resultat |= (c & 0x7F) << decalage
        decalage += 7
        if c < 0x80:
            return resultat, i
        if decalage > 70:
            raise FluxInvalide("Flux temps réel illisible.")


def _decoder(b: bytes, message: str, profondeur: int = 0) -> dict[str, Any]:
    if profondeur > 12:
        raise FluxInvalide("Flux temps réel trop imbriqué.")
    champs = SCHEMA[message]
    sortie: dict[str, Any] = {}
    i = 0
    while i < len(b):
        cle, i = _varint(b, i)
        numero, filaire = cle >> 3, cle & 7
        if filaire == 0:
            valeur: Any
            valeur, i = _varint(b, i)
        elif filaire == 2:
            n, i = _varint(b, i)
            valeur = b[i : i + n]
            if len(valeur) != n:
                raise FluxInvalide("Flux temps réel tronqué.")
            i += n
        elif filaire == 1:
            valeur, i = b[i : i + 8], i + 8
        elif filaire == 5:
            valeur, i = b[i : i + 4], i + 4
        else:
            raise FluxInvalide("Flux temps réel illisible (type de champ inconnu).")
        if numero not in champs:
            continue
        nom, type_, repete = champs[numero]
        if type_ == "str":
            valeur = bytes(valeur).decode("utf-8", "replace") if isinstance(valeur, (bytes, bytearray)) else str(valeur)
        elif type_ == "int":
            valeur = int(valeur) if isinstance(valeur, int) else 0
        elif type_ == "sint":
            valeur = int(valeur) if isinstance(valeur, int) else 0
            if valeur >= 1 << 63:
                valeur -= 1 << 64
        elif type_ == "bool":
            valeur = bool(valeur)
        else:
            if not isinstance(valeur, (bytes, bytearray)):
                continue
            valeur = _decoder(bytes(valeur), type_, profondeur + 1)
        if repete:
            sortie.setdefault(nom, []).append(valeur)
        else:
            sortie[nom] = valeur
    return sortie


def _snake(nom: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", nom).lower()


def _normaliser_json(obj: Any) -> Any:
    """Forme JSON (protobuf JSON : camelCase) → noms de la spécification (snake_case), entiers pour les nombres."""
    if isinstance(obj, dict):
        sortie = {}
        for k, v in obj.items():
            cle = _snake(str(k))
            if cle in ("time", "timestamp", "start", "end", "delay") and isinstance(v, str) and re.fullmatch(r"-?\d+", v):
                v = int(v)
            sortie[cle] = _normaliser_json(v)
        return sortie
    if isinstance(obj, list):
        return [_normaliser_json(x) for x in obj]
    return obj


def decoder_flux(contenu: bytes) -> dict[str, Any]:
    """Flux GTFS-RT (protobuf, ou JSON) → dict `{header, entity: [...]}`. Lève FluxInvalide."""
    if not contenu:
        raise FluxInvalide("Flux temps réel vide.")
    if len(contenu) > TAILLE_MAX_FLUX:
        raise FluxInvalide("Flux temps réel trop gros.")
    debut = contenu.lstrip()[:1]
    if debut in (b"{", b"["):
        try:
            brut = json.loads(contenu.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FluxInvalide("Flux temps réel JSON illisible.") from exc
        flux = _normaliser_json(brut)
        if not isinstance(flux, dict):
            raise FluxInvalide("Flux temps réel JSON inattendu.")
        flux.setdefault("entity", [])
        return flux
    try:
        flux = _decoder(bytes(contenu), "FeedMessage")
    except (KeyError, IndexError) as exc:
        raise FluxInvalide("Flux temps réel illisible.") from exc
    flux.setdefault("entity", [])
    return flux


def _ecrire_varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    sortie = bytearray()
    while True:
        octet = n & 0x7F
        n >>= 7
        if n:
            sortie.append(octet | 0x80)
        else:
            sortie.append(octet)
            return bytes(sortie)


def _encoder(obj: dict[str, Any], message: str) -> bytes:
    sortie = bytearray()
    for numero, (nom, type_, repete) in sorted(SCHEMA[message].items()):
        if nom not in obj or obj[nom] is None:
            continue
        valeurs = obj[nom] if repete else [obj[nom]]
        for v in valeurs:
            if type_ in ("int", "sint", "bool"):
                sortie += _ecrire_varint((numero << 3) | 0) + _ecrire_varint(int(v))
            else:
                corps = str(v).encode("utf-8") if type_ == "str" else _encoder(v, type_)
                sortie += _ecrire_varint((numero << 3) | 2) + _ecrire_varint(len(corps)) + corps
    return bytes(sortie)


def encoder_flux(flux: dict[str, Any]) -> bytes:
    """dict `{header, entity}` → protobuf GTFS-RT (flux fictifs des tests et démonstrations)."""
    flux = dict(flux)
    flux.setdefault("header", {"gtfs_realtime_version": "2.0", "timestamp": 0})
    return _encoder(flux, "FeedMessage")


def texte_traduit(ts: Any, langue: str = "fr") -> str:
    """TranslatedString → texte (français d'abord), balises HTML retirées."""
    if not isinstance(ts, dict):
        return ""
    traductions = [t for t in ts.get("translation") or [] if isinstance(t, dict) and t.get("text")]
    if not traductions:
        return ""
    choisie = next((t for t in traductions if str(t.get("language") or "").lower().startswith(langue)), None)
    choisie = choisie or next((t for t in traductions if not t.get("language")), traductions[0])
    texte = re.sub(r"<[^>]+>", " ", str(choisie["text"]))
    return " ".join(texte.replace("&nbsp;", " ").split())


# --- GTFS statique -------------------------------------------------------------------------------------------------------


def secondes(hms: str) -> int | None:
    """« 25:10:00 » → 90600 (les horaires GTFS dépassent 24 h pour les courses après minuit)."""
    m = re.fullmatch(r"\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\s*", hms or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)


def _lignes_csv(z: zipfile.ZipFile, nom: str, prefiltre: Callable[[bytes], bool] | None = None) -> Iterator[dict[str, str]]:
    """Lignes d'un fichier du zip en dictionnaires ; `prefiltre` (sur les octets bruts) évite d'analyser tout le fichier."""
    try:
        fh = z.open(nom)
    except KeyError:
        return
    with fh:
        entete_brut = fh.readline().decode("utf-8-sig")
        entete = next(csv.reader([entete_brut]))
        entete = [c.strip() for c in entete]
        for brut in fh:
            if prefiltre is not None and not prefiltre(brut):
                continue
            try:
                valeurs = next(csv.reader([brut.decode("utf-8", "replace")]))
            except StopIteration:
                continue
            yield dict(zip(entete, valeurs, strict=False))


def _prefiltre(motifs: Iterable[str]) -> Callable[[bytes], bool]:
    motifs_b = sorted({m.encode("utf-8") for m in motifs if m}, key=len, reverse=True)
    if not motifs_b:
        return lambda _b: False
    regex = re.compile(b"|".join(re.escape(m) for m in motifs_b))
    return lambda b: regex.search(b) is not None


class Grille:
    """Grille GTFS statique (zip) d'un fournisseur ; les résultats coûteux sont mis en cache dans `dossier`."""

    def __init__(self, zip_chemin: Path, dossier: Path | None = None) -> None:
        self.zip = Path(zip_chemin)
        self.dossier = Path(dossier) if dossier else self.zip.parent
        if not self.zip.is_file():
            raise NonPrisEnCharge("La grille horaire de ce réseau n'est pas encore téléchargée.")
        self._empreinte = f"{int(self.zip.stat().st_mtime)}-{self.zip.stat().st_size}"
        self._services: dict[Date, set[str]] = {}
        self._arrets: list[dict[str, Any]] | None = None

    def _cache(self, nom: str) -> Path:
        return self.dossier / "cache" / f"{nom}-{self._empreinte}.json"

    def _lire_cache(self, nom: str) -> Any:
        try:
            return json.loads(self._cache(nom).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _ecrire_cache(self, nom: str, valeur: Any) -> None:
        chemin = self._cache(nom)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        for ancien in chemin.parent.glob(f"{nom.split('-')[0]}-*.json"):
            if self._empreinte not in ancien.name:
                ancien.unlink(missing_ok=True)  # cache d'une grille précédente
        tmp = chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(valeur, ensure_ascii=False), encoding="utf-8")
        tmp.replace(chemin)

    # -- arrêts
    def arrets(self) -> list[dict[str, Any]]:
        """Arrêts : {id, nom, parent, type} (type 1 = gare ou station regroupant des quais)."""
        if self._arrets is None:
            cache = self._lire_cache("arrets")
            if cache is None:
                with zipfile.ZipFile(self.zip) as z:
                    cache = [
                        {"id": r.get("stop_id", ""), "nom": r.get("stop_name", ""), "parent": r.get("parent_station", ""),
                         "type": r.get("location_type", "") or "0"}
                        for r in _lignes_csv(z, "stops.txt")
                        if r.get("stop_id")
                    ]
                self._ecrire_cache("arrets", cache)
            self._arrets = cache
        return self._arrets

    def chercher_arret(self, texte: str) -> list[dict[str, Any]]:
        """Lieux qui correspondent à `texte` (tous les mots, sans accents) : gares/stations d'abord, un par nom.

        Chaque lieu : {nom, ids (le lieu et ses quais), id}. Un nom exact passe devant les autres."""
        demande = interface.normaliser(texte)
        if not demande:
            return []
        mots = demande.split()
        tous = self.arrets()
        enfants: dict[str, list[str]] = {}
        for a in tous:
            if a["parent"]:
                enfants.setdefault(a["parent"], []).append(a["id"])
        trouves: dict[str, dict[str, Any]] = {}
        for a in tous:
            nom_n = interface.normaliser(a["nom"])
            if a["parent"] or not all(m in nom_n for m in mots):
                continue
            lieu = trouves.setdefault(nom_n, {"nom": a["nom"], "ids": [], "id": a["id"], "exact": nom_n == demande})
            lieu["ids"] += [a["id"], *enfants.get(a["id"], [])]
        resultat = sorted(trouves.values(), key=lambda x: (not x["exact"], len(x["nom"])))
        for r in resultat:
            r.pop("exact", None)
        return resultat

    def nom_arret(self, stop_id: str) -> str:
        index = {a["id"]: a for a in self.arrets()}
        a = index.get(stop_id)
        if a is None:
            return stop_id
        if a["parent"] and a["parent"] in index:
            return index[a["parent"]]["nom"] or a["nom"]
        return a["nom"]

    # -- jours de circulation
    def services(self, jour: Date) -> set[str]:
        if jour in self._services:
            return self._services[jour]
        cle = jour.strftime("%Y%m%d")
        actifs: set[str] = set()
        champ_jour = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[jour.weekday()]
        with zipfile.ZipFile(self.zip) as z:
            for r in _lignes_csv(z, "calendar.txt"):
                if r.get(champ_jour) == "1" and r.get("start_date", "") <= cle <= r.get("end_date", ""):
                    actifs.add(r.get("service_id", ""))
            for r in _lignes_csv(z, "calendar_dates.txt", _prefiltre([cle])):
                if r.get("date") != cle:
                    continue
                if r.get("exception_type") == "1":
                    actifs.add(r.get("service_id", ""))
                elif r.get("exception_type") == "2":
                    actifs.discard(r.get("service_id", ""))
        self._services[jour] = actifs
        return actifs

    # -- courses
    def _details_courses(self, z: zipfile.ZipFile, trip_ids: set[str]) -> dict[str, dict[str, Any]]:
        trips: dict[str, dict[str, Any]] = {}
        for r in _lignes_csv(z, "trips.txt", _prefiltre(trip_ids)):
            if r.get("trip_id") in trip_ids:
                numero = (r.get("trip_short_name") or r.get("trip_headsign") or "").strip()
                direction = (r.get("trip_headsign") or "").strip()
                trips[r["trip_id"]] = {"service": r.get("service_id", ""), "route": r.get("route_id", ""),
                                       "numero": numero if numero != direction or numero.isdigit() else "",
                                       "direction": direction if direction != numero else ""}
        routes_voulues = {t["route"] for t in trips.values()}
        routes: dict[str, dict[str, str]] = {}
        for r in _lignes_csv(z, "routes.txt", _prefiltre(routes_voulues)):
            if r.get("route_id") in routes_voulues:
                routes[r["route_id"]] = {"ligne": r.get("route_short_name") or r.get("route_long_name") or "",
                                         "type": r.get("route_type", "")}
        for t in trips.values():
            t.update(routes.get(t["route"], {"ligne": "", "type": ""}))
        return trips

    def courses_entre(self, origine: list[str], destination: list[str]) -> list[dict[str, Any]]:
        """Courses qui desservent un arrêt de `origine` puis un arrêt de `destination` (ids, quais compris).

        Chaque course : {trip, service, route, ligne, type, numero, direction, dep (s), arr (s), arret, seq}."""
        cle = "courses-" + re.sub(r"[^A-Za-z0-9]+", "_", f"{'+'.join(sorted(origine))}__{'+'.join(sorted(destination))}")[:120]
        cache = self._lire_cache(cle)
        if cache is not None:
            return cache
        o_set, d_set = set(origine), set(destination)
        passages_o: dict[str, tuple[int, int, str]] = {}
        passages_d: dict[str, list[tuple[int, int]]] = {}
        with zipfile.ZipFile(self.zip) as z:
            for r in _lignes_csv(z, "stop_times.txt", _prefiltre(o_set | d_set)):
                stop, trip = r.get("stop_id", ""), r.get("trip_id", "")
                try:
                    seq = int(r.get("stop_sequence") or 0)
                except ValueError:
                    continue
                if stop in o_set:
                    dep = secondes(r.get("departure_time") or r.get("arrival_time") or "")
                    if dep is not None and r.get("pickup_type") != "1":
                        passages_o[trip] = (seq, dep, stop)
                if stop in d_set:
                    arr = secondes(r.get("arrival_time") or r.get("departure_time") or "")
                    if arr is not None and r.get("drop_off_type") != "1":
                        passages_d.setdefault(trip, []).append((seq, arr))
            retenues: dict[str, dict[str, Any]] = {}
            for trip, (seq_o, dep, stop) in passages_o.items():
                apres = [arr for seq_d, arr in passages_d.get(trip, []) if seq_d > seq_o]
                if apres:
                    retenues[trip] = {"trip": trip, "dep": dep, "arr": min(apres), "arret": stop, "seq": seq_o}
            details = self._details_courses(z, set(retenues))
        courses = []
        for trip, c in retenues.items():
            courses.append({**c, **details.get(trip, {"service": "", "route": "", "ligne": "", "type": "",
                                                       "numero": "", "direction": ""})})
        courses.sort(key=lambda c: c["dep"])
        self._ecrire_cache(cle, courses)
        return courses

    def course_par_numero(self, numero: str, jour: Date) -> dict[str, Any] | None:
        """Course dont le numéro (trip_short_name ou trip_headsign) vaut `numero`, circulant `jour` ; avec ses arrêts."""
        numero = str(numero).strip().lstrip("0")
        if not numero:
            return None
        actifs = self.services(jour)
        with zipfile.ZipFile(self.zip) as z:
            candidates = [
                r for r in _lignes_csv(z, "trips.txt", _prefiltre([numero]))
                if (r.get("trip_short_name") or r.get("trip_headsign") or "").strip().lstrip("0") == numero
                and r.get("service_id") in actifs
            ]
            if not candidates:
                return None
            trip = candidates[0]["trip_id"]
            arrets = []
            for r in _lignes_csv(z, "stop_times.txt", _prefiltre([trip])):
                if r.get("trip_id") == trip:
                    arrets.append({"stop_id": r.get("stop_id", ""), "seq": int(r.get("stop_sequence") or 0),
                                   "arr": secondes(r.get("arrival_time") or ""), "dep": secondes(r.get("departure_time") or "")})
            details = self._details_courses(z, {trip})
        arrets.sort(key=lambda a: a["seq"])
        for a in arrets:
            a["nom"] = self.nom_arret(a["stop_id"])
        return {"trip": trip, "service_jour": jour.isoformat(), "arrets": arrets, **details.get(trip, {})}


def heure_de(jour: Date, secs: int) -> datetime:
    """Jour de service + secondes depuis minuit (peut dépasser 24 h) → datetime de Paris."""
    return datetime(jour.year, jour.month, jour.day, tzinfo=TZ) + timedelta(seconds=secs)


# --- temps réel appliqué ---------------------------------------------------------------------------------------------------


def cle_trip_defaut(trip_id: str, numero: str, jour: str) -> str:
    return f"{trip_id}|{jour}"


class IndexTempsReel:
    """Trip updates indexés par course, selon la clé du fournisseur (identifiant de course, ou numéro + jour)."""

    def __init__(self, flux: dict[str, Any], cle: Callable[[str, str, str], str] = cle_trip_defaut,
                 numero_rt: Callable[[str], str] | None = None) -> None:
        self.cle = cle
        self.par_cle: dict[str, dict[str, Any]] = {}
        self.par_trip: dict[str, dict[str, Any]] = {}
        self.horodatage = int((flux.get("header") or {}).get("timestamp") or 0)
        for e in flux.get("entity") or []:
            tu = e.get("trip_update") if isinstance(e, dict) else None
            if not isinstance(tu, dict) or e.get("is_deleted"):
                continue
            trip = tu.get("trip") or {}
            trip_id = str(trip.get("trip_id") or "")
            numero = numero_rt(trip_id) if numero_rt else ""
            jour = str(trip.get("start_date") or "")
            if trip_id:
                self.par_trip[trip_id] = tu
            self.par_cle[cle(trip_id, numero, jour)] = tu
            if not jour:
                self.par_cle.setdefault(cle(trip_id, numero, ""), tu)

    def trouver(self, trip_id: str, numero: str, jour: Date) -> dict[str, Any] | None:
        j = jour.strftime("%Y%m%d")
        return self.par_cle.get(self.cle(trip_id, numero, j)) or self.par_cle.get(self.cle(trip_id, numero, "")) \
            or self.par_trip.get(trip_id)


def evenement_arret(tu: dict[str, Any], stop_ids: Iterable[str], seq: int | None, depart: bool = True) -> tuple[str, dict[str, Any] | None]:
    """(« supprime » | « ok » | « inconnu », événement) pour l'arrêt d'une course dans un trip update."""
    trip = tu.get("trip") or {}
    if int(trip.get("schedule_relationship") or 0) == CANCELED:
        return "supprime", None
    ids = set(stop_ids)
    for stu in tu.get("stop_time_update") or []:
        if stu.get("stop_id") in ids or (seq is not None and stu.get("stop_sequence") == seq and not stu.get("stop_id")):
            if int(stu.get("schedule_relationship") or 0) == SKIPPED:
                return "supprime", None
            ev = (stu.get("departure") if depart else stu.get("arrival")) or stu.get("arrival") or stu.get("departure")
            return "ok", ev if isinstance(ev, dict) else None
    if tu.get("delay") is not None:
        return "ok", {"delay": tu.get("delay")}
    return "inconnu", None


def heure_estimee(prevue: datetime, ev: dict[str, Any] | None) -> datetime | None:
    if not ev:
        return None
    if ev.get("time"):
        return datetime.fromtimestamp(int(ev["time"]), TZ)
    if ev.get("delay") is not None:
        return prevue + timedelta(seconds=int(ev["delay"]))
    return None


def perturbations_flux(flux: dict[str, Any], *, routes: set[str], arrets: set[str], trips: set[str],
                       instant: datetime, numero_rt: Callable[[str], str] | None = None,
                       numeros: set[str] | None = None) -> list[dict[str, Any]]:
    """Alertes du flux qui touchent les lignes, arrêts ou courses donnés, actives à `instant`."""
    resultat = []
    vus: set[str] = set()
    t = int(instant.timestamp())
    for e in flux.get("entity") or []:
        alerte = e.get("alert") if isinstance(e, dict) else None
        if not isinstance(alerte, dict):
            continue
        periodes = alerte.get("active_period") or []
        if periodes and not any((p.get("start") or 0) <= t <= (p.get("end") or 1 << 62) for p in periodes):
            continue
        touche = False
        for ent in alerte.get("informed_entity") or []:
            trip_id = str((ent.get("trip") or {}).get("trip_id") or "")
            if ent.get("route_id") in routes or ent.get("stop_id") in arrets or (trip_id and trip_id in trips) or trip_id and numero_rt and numeros and numero_rt(trip_id) in numeros:
                touche = True
            if touche:
                break
        if not touche:
            continue
        titre = texte_traduit(alerte.get("header_text")) or "Perturbation"
        texte = texte_traduit(alerte.get("description_text"))
        if (titre, texte) in {tuple(x.split("\x00")) for x in vus}:
            continue
        vus.add(f"{titre}\x00{texte}")
        def _iso(v: Any) -> str | None:
            return datetime.fromtimestamp(int(v), TZ).isoformat(timespec="minutes") if v else None
        p0 = periodes[0] if periodes else {}
        resultat.append(interface.perturbation(titre, texte, effet=EFFETS.get(int(alerte.get("effect") or 8), ""),
                                               debut=_iso(p0.get("start")), fin=_iso(p0.get("end"))))
    return resultat


# --- fournisseur générique ------------------------------------------------------------------------------------------------

TYPES_ROUTE = {"0": "tram", "1": "metro", "2": "train", "3": "bus", "4": "bateau", "5": "tram", "6": "navette",
               "7": "navette", "11": "bus", "12": "train"}


class SourceGtfs(interface.Source):
    """Fournisseur GTFS + GTFS-RT. Paramètres : `url_grille` (zip GTFS), `url_temps_reel` (trip updates, facultatif),
    `url_alertes` (service alerts, facultatif), `frequence_jours` (rafraîchissement de la grille, défaut 7).

    Un trajet est résolu en identifiants d'arrêts (`details.origine_ids`, `details.destination_ids`) à partir des noms
    donnés par la personne ; les départs sont ceux des courses qui desservent l'origine puis la destination."""

    FICHIER_GRILLE = "grille.zip"
    DUREE_CACHE_FLUX_S = 45

    # -- surcharges possibles
    def numero_rt(self, trip_id: str) -> str:
        """Numéro de course tiré d'un identifiant temps réel (SNCF : « OCESN6105F… » → « 6105 »)."""
        return ""

    def cle_course(self, trip_id: str, numero: str, jour: str) -> str:
        return cle_trip_defaut(trip_id, numero, jour)

    # -- grille et flux
    def grille(self) -> Grille:
        """Grille du réseau ; téléchargée à la première utilisation si elle manque (ensuite : cron)."""
        chemin = self.client.dossier / self.FICHIER_GRILLE
        if not chemin.is_file() and self.parametres.get("url_grille"):
            self.rafraichir(forcer=True)
        return Grille(chemin, self.client.dossier)

    def rafraichir(self, forcer: bool = False) -> dict[str, Any] | None:
        url = self.parametres.get("url_grille")
        if not url:
            return None
        chemin = self.client.dossier / self.FICHIER_GRILLE
        frequence = float(self.parametres.get("frequence_jours") or 7)
        if chemin.is_file() and not forcer:
            age = (time.time() - chemin.stat().st_mtime) / 86400
            if age < frequence - 0.1:
                return None
        tmp = chemin.with_suffix(".tmp")
        self.client.telecharger(str(url), tmp, max_octets=int(self.parametres.get("taille_max_mo") or 300) * 1_000_000)
        try:
            with zipfile.ZipFile(tmp) as z:
                noms = set(z.namelist())
        except zipfile.BadZipFile as exc:
            tmp.unlink(missing_ok=True)
            raise SourceIndisponible(f"Grille de {self.titre} illisible (zip invalide) : grille inchangée.") from exc
        if not {"stops.txt", "stop_times.txt", "trips.txt"} <= noms:
            tmp.unlink(missing_ok=True)
            raise SourceIndisponible(f"Grille de {self.titre} incomplète : grille inchangée.")
        tmp.replace(chemin)
        return {"fichier": chemin.name, "octets": chemin.stat().st_size}

    def flux(self, quoi: str) -> dict[str, Any] | None:
        url = self.parametres.get("url_temps_reel" if quoi == "temps_reel" else "url_alertes")
        if not url:
            return None
        return decoder_flux(self.client.octets(str(url), cache_s=self.DUREE_CACHE_FLUX_S, max_octets=TAILLE_MAX_FLUX))

    # -- trajets
    def _lieu(self, g: Grille, texte: str, role: str) -> dict[str, Any]:
        lieux = g.chercher_arret(texte)
        if not lieux:
            raise ErreurTransports(f"{role.capitalize()} « {texte} » introuvable sur {self.titre}.")
        if len(lieux) > 1 and interface.normaliser(lieux[0]["nom"]) != interface.normaliser(texte):
            noms = " ; ".join(lx["nom"] for lx in lieux[:8])
            raise ErreurTransports(f"Plusieurs arrêts correspondent à « {texte} » sur {self.titre} : {noms}. Précise.")
        return lieux[0]

    def resoudre_trajet(self, trajet: dict[str, Any]) -> dict[str, Any]:
        g = self.grille()
        o = self._lieu(g, trajet.get("origine", ""), "départ")
        d = self._lieu(g, trajet.get("destination", ""), "arrivée")
        courses = g.courses_entre(o["ids"], d["ids"])
        ligne = interface.normaliser(trajet.get("ligne") or "")
        if ligne:
            courses = [c for c in courses if interface.normaliser(c.get("ligne")) == ligne]
        if not courses:
            raise ErreurTransports(f"Aucune course de {self.titre} ne va de {o['nom']} à {d['nom']}"
                                   + (f" sur la ligne {trajet.get('ligne')}" if ligne else "") + ".")
        lignes = sorted({c.get("ligne") for c in courses if c.get("ligne")})
        return {**trajet, "origine": o["nom"], "destination": d["nom"],
                "ligne": trajet.get("ligne") or (lignes[0] if len(lignes) == 1 else ""),
                "details": {"origine_ids": o["ids"], "destination_ids": d["ids"]}}

    def _ids(self, trajet: dict[str, Any]) -> tuple[list[str], list[str]]:
        details = trajet.get("details") or {}
        o, d = details.get("origine_ids"), details.get("destination_ids")
        if not o or not d:
            t = self.resoudre_trajet(trajet)
            o, d = t["details"]["origine_ids"], t["details"]["destination_ids"]
        return list(o), list(d)

    def _courses_du_jour(self, g: Grille, trajet: dict[str, Any], instant: datetime, horizon: int) -> list[tuple[dict[str, Any], Date, datetime]]:
        o, d = self._ids(trajet)
        courses = g.courses_entre(o, d)
        ligne = interface.normaliser(trajet.get("ligne") or "")
        if ligne:
            courses = [c for c in courses if not c.get("ligne") or interface.normaliser(c.get("ligne")) == ligne]
        debut, fin = instant - timedelta(minutes=30), instant + timedelta(minutes=max(horizon, 1))
        resultat = []
        for decalage in (-1, 0, 1):
            jour = instant.date() + timedelta(days=decalage)
            actifs = g.services(jour)
            for c in courses:
                if c.get("service") not in actifs:
                    continue
                h = heure_de(jour, int(c["dep"]))
                if debut <= h <= fin:
                    resultat.append((c, jour, h))
        resultat.sort(key=lambda x: x[2])
        return resultat

    def prochains_departs(self, trajet: dict[str, Any], horizon: int = 60) -> dict[str, Any]:
        instant = interface.maintenant()
        g = self.grille()
        candidats = self._courses_du_jour(g, trajet, instant, horizon)
        avertissement = None
        index = None
        try:
            flux = self.flux("temps_reel")
            index = IndexTempsReel(flux, self.cle_course, self.numero_rt) if flux is not None else None
            if flux is None:
                avertissement = "Pas de temps réel sur ce réseau : horaires théoriques."
        except ErreurTransports:
            avertissement = "Temps réel indisponible pour l'instant : horaires théoriques."
        o, _d = self._ids(trajet)
        a_venir, passes = [], []
        for c, jour, prevue in candidats:
            statut, estimee = None, None
            tu = index.trouver(c["trip"], c.get("numero", ""), jour) if index else None
            if tu is not None:
                etat_rt, ev = evenement_arret(tu, o, c.get("seq"))
                if etat_rt == "supprime":
                    statut = "supprime"
                else:
                    estimee = heure_estimee(prevue, ev)
                    if estimee is None and etat_rt != "inconnu":
                        estimee = prevue
            reference = estimee or prevue
            dans = int((reference - instant).total_seconds() // 60)
            dep = interface.depart(
                prevue.strftime("%H:%M"),
                heure_estimee=estimee.strftime("%H:%M") if estimee else None,
                retard_min=int((estimee - prevue).total_seconds() // 60) if estimee else None,
                statut=statut, dans_min=dans, ligne=c.get("ligne") or None,
                destination=c.get("direction") or None, numero=c.get("numero") or None, suivi=tu is not None,
                course=c["trip"],
            )
            (passes if dans < 0 and dep["statut"] != "supprime" else a_venir).append(dep)
        temps_reel = index is not None and avertissement is None
        return interface.departs(a_venir, temps_reel=temps_reel, avertissement=avertissement, passes=passes)

    def perturbations(self, trajet: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            flux = self.flux("alertes")
        except ErreurTransports:
            return []
        if flux is None:
            return []
        instant = interface.maintenant()
        g = self.grille()
        o, d = self._ids(trajet)
        courses = [c for c, _j, _h in self._courses_du_jour(g, trajet, instant, 180)]
        return perturbations_flux(flux, routes={c.get("route") for c in courses}, arrets=set(o) | set(d),
                                  trips={c["trip"] for c in courses}, instant=instant, numero_rt=self.numero_rt,
                                  numeros={c.get("numero") for c in courses if c.get("numero")})

    def etat_course(self, numero: str, date: str, mode: str) -> dict[str, Any]:
        """État d'une course par numéro (train) : grille du jour + temps réel + alertes."""
        jour = interface.lire_date(date)
        g = self.grille()
        course = g.course_par_numero(numero, jour)
        if course is None or not course["arrets"]:
            raise ErreurTransports(f"Je ne trouve pas de {'train' if mode == 'train' else 'course'} {numero} le "
                                   f"{jour.strftime('%d/%m')} sur {self.titre}.")
        index = None
        try:
            flux = self.flux("temps_reel")
            index = IndexTempsReel(flux, self.cle_course, self.numero_rt) if flux else None
        except ErreurTransports:
            index = None
        tu = index.trouver(course["trip"], course.get("numero") or numero, jour) if index else None
        arrets, retards, supprime = [], [], False
        for a in course["arrets"]:
            prevu_s = a["dep"] if a["dep"] is not None else a["arr"]
            if prevu_s is None:
                continue
            prevue = heure_de(jour, prevu_s)
            estimee = None
            if tu is not None:
                etat_rt, ev = evenement_arret(tu, [a["stop_id"]], a["seq"])
                supprime = supprime or (etat_rt == "supprime" and int((tu.get("trip") or {}).get("schedule_relationship") or 0) == CANCELED)
                estimee = heure_estimee(prevue, ev)
                if estimee is not None:
                    retards.append(int((estimee - prevue).total_seconds() // 60))
            arrets.append({"lieu": a["nom"], "prevu": prevue.strftime("%H:%M"),
                           "estime": estimee.strftime("%H:%M") if estimee else None,
                           "retard_min": int((estimee - prevue).total_seconds() // 60) if estimee else None})
        instant = interface.maintenant()
        perturbations: list[dict[str, Any]] = []
        try:
            alertes = self.flux("alertes")
            if alertes:
                perturbations = perturbations_flux(alertes, routes=set(), arrets=set(), trips={course["trip"]},
                                                   instant=instant, numero_rt=self.numero_rt,
                                                   numeros={course.get("numero") or numero})
        except ErreurTransports:
            pass
        retard = max(retards) if retards else None
        premier, dernier = arrets[0], arrets[-1]
        if supprime:
            statut = "supprime"
        elif retard is not None:
            statut = "en_retard" if retard >= interface.SEUIL_MIN else "a_l_heure"
        else:
            statut = "prevu"
        source = "temps_reel" if tu is not None else "theorique"
        message = None if tu is not None else (
            "Pas encore d'information en temps réel pour ce train (elle arrive dans l'heure qui précède le départ)."
            if mode == "train" else "Pas d'information en temps réel pour cette course.")
        return interface.etat(
            numero, mode, statut, date=jour.isoformat(), retard_min=retard,
            depart_=premier, arrivee=dernier, arrets=arrets, perturbations=perturbations, source=source, message=message,
        )

    def verifier(self) -> dict[str, Any]:
        morceaux = []
        chemin = self.client.dossier / self.FICHIER_GRILLE
        if self.parametres.get("url_grille"):
            if not chemin.is_file():
                return {"ok": False, "message": f"{self.titre} : grille horaire absente (cron rafraichir_grilles)"}
            age = (time.time() - chemin.stat().st_mtime) / 86400
            limite = 3 * float(self.parametres.get("frequence_jours") or 7)
            if age > limite:
                return {"ok": False, "message": f"{self.titre} : grille horaire vieille de {age:.0f} jours"}
            morceaux.append(f"grille de {age:.0f} j")
        if self.parametres.get("url_temps_reel"):
            try:
                flux = self.flux("temps_reel") or {}
                morceaux.append(f"temps réel OK ({len(flux.get('entity') or [])} courses suivies)")
            except ErreurTransports as exc:
                morceaux.append(f"temps réel indisponible ({exc})")
        return {"ok": True, "message": f"{self.titre} : " + ("; ".join(morceaux) or "rien à vérifier")}


def zip_grille(fichiers: dict[str, str]) -> bytes:
    """Petit zip GTFS en mémoire à partir de contenus CSV (tests, démonstrations)."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in fichiers.items():
            z.writestr(nom, contenu)
    return tampon.getvalue()
