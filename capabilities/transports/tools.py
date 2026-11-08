"""Capacité « transports » : bus, car, métro, tram, train, avion — paramétrable par foyer.

Chaque réseau ou opérateur est un **fournisseur** (`fournisseurs.py`, interface `interface.py`) : standard quand une
source ouverte existe (Cartreize, SNCF, modèle GTFS-RT), fabriqué par l'atelier pour un réseau inconnu, ou « manuel ».
Les trajets habituels du foyer sont dans `data/capacites/transports/trajets.yaml` (`modele.py`).

Outils : `transports_prochains`, `transports_trajets`, `transports_trajet_ajouter`, `transports_trajet_supprimer`,
`transports_etat`, `transports_reseau_ajouter` (action validée par un propriétaire). Contrat des capacités :
CONTRACT.md §9 ; le `ctx` est duck-typé (`donnees`, `personne`, `config`, et s'ils existent `parametre`,
`secret`, `carte_pour`, `demander_confirmation`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import fournisseurs as fr
from . import interface, modele
from .interface import ErreurTransports, NonPrisEnCharge

FENETRE_DEPART_MIN = 60   # un trajet habituel « part dans l'heure »…
DEPART_RECENT_MIN = 15    # …ou son heure vient de passer (le bus en retard n'est peut-être pas encore là)
TOLERANCE_PASSAGE_MIN = 20  # écart max entre l'heure habituelle et l'horaire théorique du départ associé
MODES_TITRES = {"bus": "Bus", "car": "Car", "metro": "Métro", "tram": "Tram", "train": "Train", "avion": "Vol",
                "navette": "Navette", "bateau": "Bateau", "velo": "Vélo"}
_SYNONYMES_MODES = {"autobus": "bus", "autocar": "car", "cars": "car", "metros": "metro", "tramway": "tram",
                    "trains": "train", "ter": "train", "tgv": "train", "rer": "train", "intercites": "train",
                    "ouigo": "train", "vol": "avion", "avions": "avion", "navettes": "navette", "bac": "bateau",
                    "ferry": "bateau", "velo": "velo"}
MESSAGE_SANS_TRAJET = (
    "Aucun trajet habituel enregistré pour {personne}. Propose de le noter (deux minutes) : « tu prends quoi, "
    "d'où, vers où, quels jours ? »"
)
RE_TRAIN = re.compile(r"^\s*(?:train|tgv|ter|ouigo|intercit[ée]s|ic|inoui)?\s*n?[°o]?\s*(\d{3,6})\s*$", re.IGNORECASE)
RE_VOL = re.compile(r"^\s*(?:vol\s+)?([A-Z0-9]{2})\s?(\d{1,4})[A-Z]?\s*$", re.IGNORECASE)


# --- contexte ------------------------------------------------------------------------------------------------------------


def donnees(ctx: Any) -> Path:
    return Path(ctx.donnees)


def parametres(ctx: Any) -> dict[str, Any]:
    """Réglages du foyer (`data/capacites/transports/parametres.yaml`), lus sans dépendre du cadre."""
    chemin = modele.dossier(donnees(ctx)) / "parametres.yaml"
    try:
        valeurs = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        valeurs = {}
    return valeurs if isinstance(valeurs, dict) else {}


def seuil_defaut(ctx: Any) -> int:
    try:
        return max(interface.SEUIL_MIN, int(parametres(ctx).get("seuil_retard_min") or interface.SEUIL_MIN))
    except (TypeError, ValueError):
        return interface.SEUIL_MIN


def slug_personne(personne: Any) -> str:
    s = interface.slug(personne)
    if not s:
        raise ErreurTransports("Précise pour qui est ce trajet (prénom).")
    return s


def lire_mode(mode: Any) -> str:
    m = interface.normaliser(mode).replace(" ", "")
    m = _SYNONYMES_MODES.get(m, m)
    if m not in interface.MODES:
        raise ErreurTransports(f"Mode « {mode} » inconnu (bus, car, métro, tram, train, avion…).")
    return m


# --- fournisseurs ----------------------------------------------------------------------------------------------------------


def lister_fournisseurs(ctx: Any) -> list[fr.Fournisseur]:
    try:
        utilises = modele.fournisseurs_utilises(modele.lire(donnees(ctx)))
    except ErreurTransports:
        utilises = set()
    return fr.disponibles(donnees(ctx), parametres(ctx), utilises)


def choisir_fournisseur(ctx: Any, mode: str, nom: str = "") -> fr.Fournisseur:
    if nom:
        f = fr.trouver(nom, donnees(ctx))
        if not f.valide:
            raise ErreurTransports(f"Le réseau « {f.titre} » est mal décrit : {'; '.join(f.erreurs)}.")
        return f
    candidats = [f for f in lister_fournisseurs(ctx) if mode in f.modes and f.source != "manuel"]
    if len(candidats) == 1:
        return candidats[0]
    if len(candidats) > 1:
        locaux = [f for f in candidats if f.portee == "locale"]
        if len(locaux) == 1:
            return locaux[0]
        raise ErreurTransports("Plusieurs réseaux possibles : " + ", ".join(f.titre for f in candidats) + ". Lequel ?")
    raise ErreurTransports(
        f"Je ne connais pas encore de réseau de {MODES_TITRES.get(mode, mode).lower()} pour le foyer. Un propriétaire "
        "peut me demander d'ajouter ce réseau (je le fabrique à l'atelier), ou je peux noter le trajet sans suivi en "
        "temps réel (fournisseur « manuel »)."
    )


def source_de(ctx: Any, f: fr.Fournisseur) -> interface.Source:
    return fr.charger(f, donnees(ctx), ctx)


# --- prochains départs ------------------------------------------------------------------------------------------------------


def titre_trajet(t: dict[str, Any]) -> str:
    mode = MODES_TITRES.get(str(t.get("mode") or ""), "Trajet")
    ligne = f" {t['ligne']}" if t.get("ligne") else ""
    lieux = " → ".join(x for x in (t.get("origine"), t.get("destination")) if x)
    return f"{mode}{ligne} · {lieux}" if lieux else f"{mode}{ligne}"


def departs_trajet(ctx: Any, t: dict[str, Any], nombre: int = 3, horizon: int = 90) -> dict[str, Any]:
    f = fr.trouver(str(t.get("fournisseur") or "manuel"), donnees(ctx))
    source = source_de(ctx, f)
    res = source.prochains_departs(t, horizon)
    nombre = max(1, min(int(nombre or 3), 10))
    return {
        "fournisseur": f.titre,
        "source": "temps_reel" if res.get("temps_reel") else "theorique",
        "avertissement": res.get("avertissement"),
        "prochains": list(res.get("departs") or [])[:nombre],
        "passes_recents": list(res.get("passes") or []),
    }


def _depart_habituel(res: dict[str, Any], heure: str) -> dict[str, Any] | None:
    """Le départ dont l'horaire théorique est le plus proche de l'heure habituelle (± 20 min)."""
    cible = interface.minutes(heure)
    meilleur, ecart_min = None, TOLERANCE_PASSAGE_MIN + 1
    for p in res.get("passes_recents", []) + res.get("prochains", []):
        if not p.get("heure_theorique"):
            continue
        ecart = abs(interface.minutes(p["heure_theorique"]) - cible)
        ecart = min(ecart, 1440 - ecart)
        if ecart < ecart_min:
            meilleur, ecart_min = p, ecart
    return meilleur


def ecart_heure(heure: str, instant: Any) -> int:
    """Écart signé (min) entre l'heure habituelle et maintenant, dans ]-12 h ; 12 h]."""
    return ((interface.minutes(heure) - (instant.hour * 60 + instant.minute) + 720) % 1440) - 720


def part_dans_l_heure(t: dict[str, Any], instant: Any) -> bool:
    heure = modele.heure_sure(t)
    if not heure or interface.JOURS[instant.weekday()] not in modele.jours_surs(t):
        return False
    return -DEPART_RECENT_MIN <= ecart_heure(heure, instant) <= FENETRE_DEPART_MIN


def etat_trajet(ctx: Any, t: dict[str, Any], nombre: int = 4) -> dict[str, Any]:
    """Un trajet habituel avec ses prochains départs et son alerte (retard ≥ seuil ou suppression, départ dans l'heure)."""
    instant = interface.maintenant()
    jour = interface.JOURS[instant.weekday()]
    heure = modele.heure_sure(t)
    jours = modele.jours_surs(t)
    item: dict[str, Any] = {
        "id": t.get("id"), "titre": titre_trajet(t), "mode": t.get("mode"), "fournisseur": t.get("fournisseur"),
        "ligne": str(t.get("ligne") or ""), "origine": t.get("origine"), "destination": t.get("destination"),
        "heure_habituelle": heure, "jours": jours, "aujourd_hui": jour in jours, "seuil_alerte_min": modele.seuil(t),
    }
    try:
        res = departs_trajet(ctx, t, nombre=nombre)
    except ErreurTransports as exc:
        item["erreur"] = str(exc)
        return item
    avant = ecart_heure(heure, instant) if heure else None
    dans_l_heure = part_dans_l_heure(t, instant)
    habituel = _depart_habituel(res, heure) if heure else None
    alerte = None
    if dans_l_heure and habituel:
        if habituel["statut"] == "supprime":
            alerte = f"départ de {habituel['heure_theorique']} supprimé"
        elif (habituel.get("retard_min") or 0) >= item["seuil_alerte_min"]:
            alerte = f"retard {habituel['retard_min']} min sur le départ de {habituel['heure_theorique']}"
    item.update({
        "depart_dans_min": avant if item["aujourd_hui"] else None, "part_dans_l_heure": dans_l_heure,
        "depart_habituel": habituel, "alerte": alerte, **res,
    })
    return item


def trajets_de(ctx: Any, personne: str | None) -> tuple[str, list[dict[str, Any]]]:
    qui = slug_personne(personne or getattr(ctx, "personne", None) or "")
    return qui, modele.lire(donnees(ctx)).get(qui, [])


def _trouver_trajet(liste: list[dict[str, Any]], cle: str) -> list[dict[str, Any]]:
    c = interface.normaliser(cle)
    if not c:
        return liste
    par_id = [t for t in liste if t.get("id") == cle]
    if par_id:
        return par_id
    try:
        h = interface.lire_heure(cle)
        par_heure = [t for t in liste if modele.heure_sure(t) == h]
        if par_heure:
            return par_heure
    except ErreurTransports:
        pass
    return [t for t in liste if all(m in interface.normaliser(f"{titre_trajet(t)} {t.get('fournisseur')}") for m in c.split())]


def prochains(
    ctx: Any,
    personne: str | None = None,
    trajet: str = "",
    origine: str = "",
    destination: str = "",
    mode: str = "",
    fournisseur: str = "",
    ligne: str = "",
    nombre: int = 3,
) -> dict[str, Any]:
    """Sans origine : les trajets habituels (tous, ou celui désigné par `trajet`) ; avec origine/destination : un
    trajet ponctuel, résolu par le fournisseur."""
    if origine or destination:
        m = lire_mode(mode) if mode else ""
        if fournisseur:
            f = choisir_fournisseur(ctx, m, fournisseur)
            m = m or (f.modes[0] if f.modes else "bus")
        else:
            m = m or "bus"
            f = choisir_fournisseur(ctx, m)
        brut = {"mode": m, "fournisseur": f.nom, "origine": origine, "destination": destination, "ligne": str(ligne or "")}
        t = source_de(ctx, f).resoudre_trajet(brut)
        res = departs_trajet(ctx, t, nombre=nombre)
        return {"trajets": [{"titre": titre_trajet(t), **{k: t.get(k) for k in ("mode", "fournisseur", "origine",
                                                                                   "destination", "ligne")}, **res,
                             "perturbations": _perturbations(ctx, t)}]}
    qui, liste = trajets_de(ctx, personne)
    if not liste:
        return {"personne": qui, "trajets": [], "message": MESSAGE_SANS_TRAJET.format(personne=qui)}
    choisis = _trouver_trajet(liste, trajet) if trajet else liste
    if not choisis:
        raise ErreurTransports(f"Je ne trouve pas le trajet « {trajet} » de {qui}.")
    instant = interface.maintenant()
    sortie = [etat_trajet(ctx, t, nombre=max(nombre, 4)) for t in choisis]
    for item, t in zip(sortie, choisis, strict=True):
        if "erreur" not in item and (item.get("part_dans_l_heure") or trajet):
            item["perturbations"] = _perturbations(ctx, t)
    return {"personne": qui, "jour": interface.JOURS[instant.weekday()], "heure": instant.strftime("%H:%M"),
            "trajets": sortie}


def _perturbations(ctx: Any, t: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        f = fr.trouver(str(t.get("fournisseur") or "manuel"), donnees(ctx))
        return source_de(ctx, f).perturbations(t)[:5]
    except ErreurTransports:
        return []


# --- trajets habituels ----------------------------------------------------------------------------------------------------


def lister_trajets(ctx: Any, personne: str | None = None) -> dict[str, Any]:
    tous = modele.lire(donnees(ctx))
    if personne:
        qui = slug_personne(personne)
        tous = {qui: tous.get(qui, [])}
    resume = {
        p: [{"id": t.get("id"), "titre": titre_trajet(t), "heure": modele.heure_sure(t), "jours": modele.jours_surs(t),
             "fournisseur": t.get("fournisseur"), "seuil_alerte_min": modele.seuil(t)} for t in liste]
        for p, liste in tous.items()
    }
    reseaux = [f.resume() for f in lister_fournisseurs(ctx)]
    return {"trajets": resume, "reseaux": reseaux}


def ajouter_trajet(
    ctx: Any,
    personne: str,
    mode: str,
    origine: str,
    destination: str,
    heure: str,
    jours: list[str] | str | None = None,
    fournisseur: str = "",
    ligne: str = "",
    seuil_alerte_min: int | None = None,
) -> dict[str, Any]:
    qui = slug_personne(personne)
    m = lire_mode(mode)
    h = interface.lire_heure(heure)
    j = interface.lire_jours(jours)
    f = choisir_fournisseur(ctx, m, fournisseur)
    if m not in f.modes:
        raise ErreurTransports(f"{f.titre} ne couvre pas le mode « {MODES_TITRES.get(m, m)} ».")
    brut = {"mode": m, "fournisseur": f.nom, "origine": str(origine or "").strip(),
            "destination": str(destination or "").strip(), "ligne": str(ligne or "").strip(), "heure": h}
    if not brut["origine"] or not brut["destination"]:
        raise ErreurTransports("Il me faut le départ et l'arrivée (« de la Mairie vers la gare »).")
    t = source_de(ctx, f).resoudre_trajet(brut)
    seuil = max(interface.SEUIL_MIN, int(seuil_alerte_min or seuil_defaut(ctx)))
    trajet = {"mode": m, "fournisseur": f.nom, "origine": t.get("origine"), "destination": t.get("destination"),
              "ligne": str(t.get("ligne") or ""), "heure": h, "jours": j, "seuil_alerte_min": seuil}
    if t.get("details"):
        trajet["details"] = t["details"]
    trajet = {"id": modele.identifiant(qui, trajet), **trajet}
    trajets = modele.lire(donnees(ctx))
    liste = trajets.get(qui, [])
    garde = [x for x in liste if x.get("id") != trajet["id"]]
    remplace = len(garde) != len(liste)
    trajets[qui] = [*garde, trajet]
    modele.ecrire(donnees(ctx), trajets)
    temps_reel = "" if f.source != "manuel" else " (sans suivi en temps réel)"
    return {
        "personne": qui, "trajet": trajet, "remplace": remplace, "nombre_trajets": len(trajets[qui]),
        "message": f"Trajet noté : {titre_trajet(trajet)}, {h}, {' '.join(j)}{temps_reel}.",
    }


def supprimer_trajet(ctx: Any, personne: str, trajet: str = "") -> dict[str, Any]:
    qui = slug_personne(personne)
    trajets = modele.lire(donnees(ctx))
    liste = trajets.get(qui, [])
    a_supprimer = {id(t) for t in _trouver_trajet(liste, trajet)} if trajet else {id(t) for t in liste}
    garde = [t for t in liste if id(t) not in a_supprimer]
    trajets[qui] = garde
    modele.ecrire(donnees(ctx), trajets)
    return {"personne": qui, "supprimes": len(liste) - len(garde), "restants": len(garde)}


# --- état d'un train ou d'un vol ---------------------------------------------------------------------------------------------


def deviner_mode(numero: str) -> str:
    if RE_TRAIN.match(numero or ""):
        return "train"
    if RE_VOL.match(numero or ""):
        return "avion"
    raise ErreurTransports(f"« {numero} » : numéro de train (6105) ou de vol (AF7664) ?")


def etat(ctx: Any, numero: str, date: str = "", mode: str = "", fournisseur: str = "") -> dict[str, Any]:
    m = lire_mode(mode) if mode else deviner_mode(numero)
    if m not in ("train", "avion"):
        raise ErreurTransports("L'état par numéro concerne un train ou un vol.")
    jour = interface.lire_date(date).isoformat()
    candidats = [fr.trouver(fournisseur, donnees(ctx))] if fournisseur else [
        f for f in lister_fournisseurs(ctx) if m in f.modes and f.nom != "manuel"]
    if not candidats:
        raise NonPrisEnCharge(f"Aucun réseau ne donne l'état des {'trains' if m == 'train' else 'vols'} pour le foyer.")
    dernier: ErreurTransports | None = None
    for f in sorted(candidats, key=lambda x: x.portee != "nationale"):
        source = source_de(ctx, f)
        try:
            if m == "train":
                numero_propre = RE_TRAIN.match(numero).group(1) if RE_TRAIN.match(numero) else numero
                res = source.etat_train(numero_propre, jour)
            else:
                res = source.etat_vol(numero, jour)
        except ErreurTransports as exc:
            dernier = exc
            continue
        res["fournisseur"] = f.titre
        res["resume"] = resume_etat(res)
        return res
    raise dernier or NonPrisEnCharge("Information indisponible.")


def resume_etat(e: dict[str, Any]) -> str:
    quoi = ("Train " if e.get("mode") == "train" else "Vol ") + str(e.get("numero") or "")
    dep, arr = e.get("depart") or {}, e.get("arrivee") or {}
    trajet = f" {dep.get('lieu')} → {arr.get('lieu')}" if dep.get("lieu") and arr.get("lieu") else ""
    horaire = f", départ {dep.get('estime') or dep.get('prevu')}" if dep.get("prevu") else ""
    statut = e.get("statut")
    if statut == "supprime":
        return f"{quoi}{trajet} : supprimé."
    if statut == "en_retard":
        return f"{quoi}{trajet}{horaire} : retard {e.get('retard_min')} min."
    if statut == "a_l_heure":
        return f"{quoi}{trajet}{horaire} : à l'heure."
    if statut == "prevu":
        return f"{quoi}{trajet}{horaire} (horaire prévu, pas encore de temps réel)."
    return f"{quoi} : {e.get('message') or 'état inconnu'}"


# --- réseau GTFS-RT ajouté par le foyer ------------------------------------------------------------------------------------


def preparer_reseau(ctx: Any, titre: str, url_grille: str, url_temps_reel: str = "", url_alertes: str = "",
                    modes: list[str] | str | None = None, zone: str = "") -> tuple[str, dict[str, Any]]:
    """Reformulation et payload pour ajouter un réseau qui publie GTFS (+ GTFS-RT) : propriétaires seulement."""
    try:
        from tools import administration

        administration.verifier_proprietaire(getattr(ctx, "personne", None), getattr(ctx, "config", None))
    except ImportError:
        pass
    titre = " ".join(str(titre or "").split())
    nom = interface.slug(titre, "_")[:40]
    if not titre or not fr.RE_NOM.fullmatch(nom or "-"):
        raise ErreurTransports("Donne un nom court au réseau (« Métro de Lyon »).")
    if any(f.nom == nom for f in fr.decouvrir(donnees(ctx), avec_modeles=True)):
        raise ErreurTransports(f"Le réseau « {titre} » existe déjà.")
    urls = {"url_grille": url_grille, "url_temps_reel": url_temps_reel, "url_alertes": url_alertes}
    for cle, url in urls.items():
        if url and not re.match(r"^https://[^\s/]+\.[^\s/]+/\S*$", str(url)):
            raise ErreurTransports(f"Adresse refusée ({cle}) : https seulement.")
    if not url_grille:
        raise ErreurTransports("Il me faut au moins l'adresse de la grille horaire GTFS (fichier zip).")
    liste_modes = [lire_mode(x) for x in fr._liste(modes)] or ["bus"]
    hotes = fr.hotes_url(*urls.values())
    resume = (f"Ajouter le réseau « {titre} » ({', '.join(MODES_TITRES[m].lower() for m in liste_modes)}) : horaires "
              f"{'et temps réel ' if url_temps_reel else ''}lus en données ouvertes. Il parlera à {', '.join(hotes)}.")
    payload = {"action": "transports_reseau_ajouter", "nom": nom, "titre": titre, "modes": liste_modes, "zone": zone,
               **{k: v for k, v in urls.items() if v}, "hotes": hotes}
    return resume, payload


def executer_reseau(payload: dict[str, Any], ctx: Any) -> str:
    from tools import capacites

    nom = str(payload.get("nom") or "")
    if not fr.RE_NOM.fullmatch(nom):
        raise ErreurTransports("Demande invalide.")
    instances = [r for r in fr.lire_instances(donnees(ctx)) if r.get("nom") != nom]
    instance = {"nom": nom, "titre": payload.get("titre") or nom, "modele": "gtfs_rt", "modes": payload.get("modes") or ["bus"],
                **{k: payload[k] for k in ("zone", "url_grille", "url_temps_reel", "url_alertes") if payload.get(k)}}
    instances.append(instance)
    fr.ecrire_instances(donnees(ctx), instances)
    hotes = [h for h in payload.get("hotes") or [] if isinstance(h, str) and fr.RE_HOTE.fullmatch(h)]
    with capacites._modifier_etat(donnees(ctx)) as etat_global:
        entree = etat_global.setdefault(fr.NOM_CAPACITE, {})
        entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | set(hotes))
    f = fr.trouver(nom, donnees(ctx))
    try:
        source_de(ctx, f).rafraichir(forcer=True)
        suite = " La grille horaire est téléchargée : dis-moi ton trajet et je le note."
    except ErreurTransports:
        suite = " La grille horaire sera téléchargée cette nuit (le site ne répond pas pour l'instant)."
    return f"Réseau « {instance['titre']} » ajouté.{suite}"


# --- cartes (appli) ----------------------------------------------------------------------------------------------------------


def carte_prochains(resultat: Any) -> dict[str, Any] | None:
    from coeur import cartes

    trajets = []
    for t in (resultat or {}).get("trajets") or []:
        if "erreur" in t:
            continue
        trajets.append({
            "titre": t.get("titre"), "mode": t.get("mode"), "ligne": t.get("ligne"), "origine": t.get("origine"),
            "destination": t.get("destination"), "heure_habituelle": t.get("heure_habituelle"),
            "alerte": t.get("alerte"), "avertissement": t.get("avertissement"),
            "temps_reel": t.get("source") == "temps_reel",
            "departs": [{k: d.get(k) for k in ("heure_theorique", "heure_estimee", "retard_min", "dans_min", "statut",
                                               "resume", "quai", "ligne", "numero")} for d in (t.get("prochains") or [])[:4]],
            "perturbations": [{"titre": p.get("titre"), "effet": p.get("effet")} for p in (t.get("perturbations") or [])[:2]],
        })
    if not trajets:
        return None
    titre = trajets[0]["titre"] if len(trajets) == 1 else "Prochains départs"
    return cartes.carte("transports_prochains", {"trajets": trajets}, titre=titre)


def carte_etat(resultat: Any) -> dict[str, Any] | None:
    from coeur import cartes

    if not isinstance(resultat, dict) or not resultat.get("numero"):
        return None
    donnees_carte = {k: resultat.get(k) for k in ("numero", "mode", "date", "statut", "retard_min", "depart", "arrivee",
                                                  "source", "message", "resume")}
    donnees_carte["arrets"] = (resultat.get("arrets") or [])[:30]
    donnees_carte["perturbations"] = [{"titre": p.get("titre"), "texte": p.get("texte")} for p in (resultat.get("perturbations") or [])[:3]]
    titre = ("Train " if resultat.get("mode") == "train" else "Vol ") + str(resultat["numero"])
    return cartes.carte("transports_etat", donnees_carte, titre=titre)


# --- enregistrement MCP ------------------------------------------------------------------------------------------------------


def enregistrer(mcp: Any, ctx: Any) -> None:
    """Ajoute les outils `transports_*` au serveur MCP."""

    @mcp.tool()
    def transports_prochains(
        personne: str | None = None,
        trajet: str = "",
        origine: str = "",
        destination: str = "",
        mode: str = "",
        fournisseur: str = "",
        ligne: str = "",
        nombre: int = 3,
    ) -> dict[str, Any]:
        """Prochains départs : sur les trajets habituels d'une personne (par défaut celle qui parle), ou sur un trajet
        ponctuel (`origine`, `destination`, et si utile `mode`, `ligne`, `fournisseur`).

        Pour « il est où mon bus ? », « mon train est à l'heure ? », /bus, /transports et le brief. `trajet` choisit
        un trajet habituel (identifiant, heure « 7h40 » ou mots du titre). Chaque trajet donne `prochains` (départs :
        `heure_theorique`, `heure_estimee`, `retard_min`, `dans_min`, `statut`, `resume`), `source`
        (temps_reel/theorique), `avertissement`, `alerte` (texte si retard ≥ seuil ou suppression et départ dans
        l'heure, sinon null), `perturbations`. Liste vide → propose de noter le trajet."""
        return prochains(ctx, personne, trajet, origine, destination, mode, fournisseur, ligne, nombre)

    @mcp.tool()
    def transports_trajets(personne: str | None = None) -> dict[str, Any]:
        """Trajets habituels notés (tout le foyer, ou une personne) et réseaux connus du foyer (`reseaux`). Sans appel
        aux réseaux : pour « qu'est-ce que tu as noté ? », « quels réseaux tu connais ? »."""
        return lister_trajets(ctx, personne)

    @mcp.tool()
    def transports_trajet_ajouter(
        personne: str,
        mode: str,
        origine: str,
        destination: str,
        heure: str,
        jours: list[str] | str | None = None,
        fournisseur: str = "",
        ligne: str = "",
        seuil_alerte_min: int | None = None,
    ) -> dict[str, Any]:
        """Note (ou corrige) un trajet habituel après la découverte « tu prends quoi, d'où, vers où, quels jours ? ».

        - `personne` : prénom (celle qui parle, ou un enfant via son parent) ;
        - `mode` : bus, car, métro, tram, train… ; `origine`, `destination` : arrêts ou gares, dans les mots de la
          personne (vérifiés auprès du réseau ; s'il y a plusieurs possibilités, l'outil les liste : fais choisir) ;
        - `heure` : départ habituel (« 07:40 ») ; `jours` : « lun-ven », « week-end »… ;
        - `fournisseur` : le réseau si plusieurs sont possibles (« cartreize », « sncf », « manuel » pour un trajet
          sans suivi en temps réel) ; `ligne` : numéro de ligne si connu ; `seuil_alerte_min` : 5 minimum.
        L'aller et le retour font deux trajets."""
        return ajouter_trajet(ctx, personne, mode, origine, destination, heure, jours, fournisseur, ligne, seuil_alerte_min)

    @mcp.tool()
    def transports_trajet_supprimer(personne: str, trajet: str = "") -> dict[str, Any]:
        """Supprime des trajets habituels d'une personne : celui désigné par `trajet` (identifiant, heure ou mots du
        titre), sinon tous. Demande confirmation à la personne avant de tout supprimer."""
        return supprimer_trajet(ctx, personne, trajet)

    @mcp.tool()
    def transports_etat(numero: str, date: str = "", mode: str = "", fournisseur: str = "") -> dict[str, Any]:
        """État d'un train (« 6105 », « TGV 6105 ») ou d'un vol (« AF7664 ») par son numéro, pour une date
        (« demain », « 2026-11-09 » ; défaut aujourd'hui) : statut (a_l_heure, en_retard, supprime, prevu, inconnu),
        `retard_min`, départ et arrivée, arrêts, perturbations annoncées, `resume`. Un vol n'a pas de suivi en temps
        réel : transmets le message tel quel."""
        return etat(ctx, numero, date, mode, fournisseur)

    @mcp.tool()
    def transports_reseau_ajouter(titre: str, url_grille: str, url_temps_reel: str = "", url_alertes: str = "",
                                  modes: list[str] | str | None = None, zone: str = "") -> str:
        """Ajouter un réseau qui publie ses données ouvertes GTFS (zip `url_grille`) et éventuellement GTFS-RT
        (`url_temps_reel`, `url_alertes`), trouvées par exemple sur transport.data.gouv.fr. Propriétaires seulement ;
        renvoie CONFIRMATION_REQUISE (la validation approuve les sites). Sans ces adresses : l'atelier."""
        resume, payload = preparer_reseau(ctx, titre, url_grille, url_temps_reel, url_alertes, modes, zone)
        return ctx.demander_confirmation("transports_reseau_ajouter", resume, payload)

    if hasattr(ctx, "carte_pour"):
        ctx.carte_pour("transports_prochains")(lambda resultat, _args: carte_prochains(resultat))
        ctx.carte_pour("transports_etat")(lambda resultat, _args: carte_etat(resultat))


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Action validée par bouton (`tools.runner execute`)."""
    if action == "transports_reseau_ajouter":
        return executer_reseau(payload, ctx)
    raise ValueError(f"Action inconnue : {action}")

