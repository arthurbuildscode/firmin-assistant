"""Télécharge la grille théorique GTFS du réseau (service/config.json) et prépare les données du site :
  - data/reseau.json   : lignes, sens et arrêts (dans l'ordre du trajet)
  - data/horaires.json : calendriers, courses et passages par point d'arrêt

À relancer quand la grille change (rentrée, vacances...) : python prepare.py
(option --sans-telechargement pour réutiliser data/gtfs.zip)
"""
# Firmin Assistant : script repris tel quel du projet d'origine, exclu du lint du kit.
# ruff: noqa
import collections, csv, io, json, os, re, sys, unicodedata, urllib.request, zipfile, datetime

ICI = os.path.dirname(os.path.abspath(__file__))
config = json.load(open(os.path.join(ICI, "config.json"), encoding="utf-8"))
os.makedirs(os.path.join(ICI, "data"), exist_ok=True)
zip_path = os.path.join(ICI, "data", "gtfs.zip")

if "--sans-telechargement" not in sys.argv or not os.path.exists(zip_path):
    print("Téléchargement de la grille GTFS...")
    urllib.request.urlretrieve(config["gtfsUrl"], zip_path)

z = zipfile.ZipFile(zip_path)
def lire(nom):
    return csv.DictReader(io.TextIOWrapper(z.open(nom), encoding="utf-8-sig"))

def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "x"

def joli(s):
    """'CENTRE - GARE par RN' -> 'Centre ⇄ Gare par RN' (on ne touche pas aux noms déjà en casse mixte)."""
    if s.upper() == s:
        s = re.sub(r"[A-ZÀ-Ý]+", lambda m: m.group(0).capitalize(), s)
    return s.replace(" - ", " ⇄ ")

stops = {s["stop_id"]: s for s in lire("stops.txt")}
routes = {r["route_id"]: r for r in lire("routes.txt")}
trips = {r["trip_id"]: r for r in lire("trips.txt")}


print("Lecture des horaires (gros fichier, patience)...")
par_course = collections.defaultdict(list)  # trip_id -> [(seq, stop_id, minutes, montee)]
for st in lire("stop_times.txt"):
    h, m = st["departure_time"].split(":")[:2]
    par_course[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"], int(h) * 60 + int(m), st["pickup_type"] != "1"))

# ---------- Courses, terminus, passages ----------
courses = []          # [ligne, n° de course (lien avec le temps réel), service, index du terminus, sens GTFS "0"/"1"]
terminus_noms, terminus_idx = [], {}
passages = collections.defaultdict(list)  # stop_id -> [[index course, minutes], ...]
services_utiles = set()
sequences = collections.defaultdict(collections.Counter)  # (route, direction) -> Counter(tuple des arrêts)
fins = collections.defaultdict(collections.Counter)       # (route, direction) -> Counter((ville, arrêt) du terminus)

for trip_id, arrets in par_course.items():
    t = trips[trip_id]
    arrets.sort()
    dernier = stops[arrets[-1][1]]
    nom_t = dernier["stop_name"]
    ville = dernier.get("city_name", "")
    nom_t = f"{ville} – {nom_t}" if ville and ville.lower() not in nom_t.lower() else nom_t
    if nom_t not in terminus_idx:
        terminus_idx[nom_t] = len(terminus_noms)
        terminus_noms.append(nom_t)
    idx = len(courses)
    courses.append([routes[t["route_id"]]["route_short_name"], t.get("ext_id_course") or "", t["service_id"], terminus_idx[nom_t], t["direction_id"]])
    services_utiles.add(t["service_id"])
    sequences[(t["route_id"], t["direction_id"])][tuple(a[1] for a in arrets)] += 1
    fins[(t["route_id"], t["direction_id"])][(ville or dernier["stop_name"], dernier["stop_name"])] += 1
    for i, (_, stop_id, minutes, montee) in enumerate(arrets):
        if montee and i < len(arrets) - 1:  # on ne propose pas le terminus (personne n'y monte)
            passages[stop_id].append([idx, minutes])

for p in passages.values():
    p.sort(key=lambda x: x[1])

# ---------- Calendriers ----------
services = {}
for c in lire("calendar.txt"):
    if c["service_id"] in services_utiles:
        services[c["service_id"]] = {
            "jours": [c[j] == "1" for j in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
            "debut": c["start_date"], "fin": c["end_date"], "ajout": [], "retrait": [],
        }
for d in lire("calendar_dates.txt"):
    s = d["service_id"]
    if s in services_utiles:
        services.setdefault(s, {"jours": [False] * 7, "debut": "", "fin": "", "ajout": [], "retrait": []})
        services[s]["ajout" if d["exception_type"] == "1" else "retrait"].append(d["date"])

# ---------- Réseau : lignes > sens > arrêts ordonnés ----------
def ordre_arrets(compteur):
    """Fusionne les différents parcours d'un sens en une seule liste ordonnée."""
    ordre = []
    for seq, _ in sorted(compteur.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        for i, s in enumerate(seq):
            if s in ordre:
                continue
            avant = next((seq[j] for j in range(i - 1, -1, -1) if seq[j] in ordre), None)
            ordre.insert(ordre.index(avant) + 1 if avant else 0, s)
    return ordre

flux_par_ligne = {l: f for f, v in config["tempsReel"].items() for l in v["lignes"]}
lignes = []
for route_id, r in routes.items():
    flux = flux_par_ligne.get(r["route_short_name"])
    sens_liste, slugs = [], set()
    # Nom du sens = ville du terminus le plus fréquent ; si les deux sens finissent dans la même ville, on précise l'arrêt.
    villes = {d: fins[(route_id, d)].most_common(1)[0][0] for d in ("0", "1") if fins.get((route_id, d))}
    meme_ville = len({v[0] for v in villes.values()}) < len(villes)
    for direction in ("0", "1"):
        compteur = sequences.get((route_id, direction))
        if not compteur:
            continue
        ville, arret_fin = villes[direction]
        nom_sens = f"{ville} ({arret_fin})" if meme_ville and arret_fin != ville else ville
        s_id = slug(nom_sens)
        if s_id in slugs:
            s_id += f"-{direction}"
        slugs.add(s_id)
        arrets, vus = [], {}
        for stop_id in ordre_arrets(compteur):
            if not any(courses[c][0] == r["route_short_name"] and courses[c][4] == direction for c, _ in passages.get(stop_id, [])):
                continue  # terminus ou arrêt sans montée pour cette ligne dans ce sens
            st = stops[stop_id]
            cle = (st["stop_name"], st.get("city_name", ""))
            if cle in vus:  # plusieurs quais du même arrêt
                vus[cle]["points"].append(stop_id.replace("MAMP-", ""))
                continue
            a_slug = slug(f"{st['stop_name']}-{st.get('city_name', '')}")
            a = {"id": a_slug, "nom": st["stop_name"], "commune": st.get("city_name", ""), "points": [stop_id.replace("MAMP-", "")]}
            vus[cle] = a
            arrets.append(a)
        sens_liste.append({"id": s_id, "direction": direction, "libelle": f"Vers {nom_sens}", "arrets": arrets})
    lignes.append({
        "id": slug(r["route_short_name"]),
        "ligne": r["route_short_name"],
        "nom": config.get("nomsLignes", {}).get(r["route_short_name"]) or joli(r["route_long_name"]),
        "couleur": "#" + (r["route_color"] or "555555"),
        "texte": "#" + (r["route_text_color"] or "FFFFFF"),
        "flux": flux,
        "tempsReel": bool(flux),
        "sens": sens_liste,
    })

def cle_ligne(l):
    m = re.match(r"([A-Z]*)(\d+)", l["ligne"])
    return (m.group(1), int(m.group(2))) if m else (l["ligne"], 0)
lignes.sort(key=cle_ligne)

json.dump({"genere": datetime.datetime.now().isoformat(timespec="seconds"), "lignes": lignes},
          open(os.path.join(ICI, "data", "reseau.json"), "w", encoding="utf-8"), ensure_ascii=False)
json.dump({"services": services, "courses": courses, "terminus": terminus_noms,
           "passages": {k.replace("MAMP-", ""): v for k, v in passages.items()}},
          open(os.path.join(ICI, "data", "horaires.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(f"{len(lignes)} lignes, {sum(len(s['arrets']) for l in lignes for s in l['sens'])} arrêts-sens, {len(courses)} courses")
print("Temps réel :", ", ".join(l["ligne"] for l in lignes if l["tempsReel"]))
print("OK -> data/reseau.json, data/horaires.json")
