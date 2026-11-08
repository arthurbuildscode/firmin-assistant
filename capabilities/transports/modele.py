"""Trajets habituels du foyer (`data/capacites/transports/trajets.yaml`) et migration depuis l'ancienne capacité bus.

Un **trajet** = origine, destination, mode, fournisseur, heure et jours habituels, pour une personne :

    alex:
      - id: t3f9a2                  # court et stable (suppression, alertes)
        mode: bus
        fournisseur: cartreize
        origine: Mairie (Centre-ville)
        destination: Gare routière
        ligne: "7"
        heure: "07:40"              # heure habituelle de départ (passage à l'arrêt)
        jours: [lun, mar, mer, jeu, ven]
        seuil_alerte_min: 5         # 5 minimum (filtre « ça vaut le coup »)
        details: {sens: gare-routiere, arret: mairie-centre-ville}   # identifiants propres au fournisseur

Migration douce (lot transports) : au premier accès, `data/capacites/bus/trajets.yaml` (format de la capacité bus)
est converti en trajets `cartreize`, la grille GTFS déjà téléchargée est déplacée, le réseau Cartreize est ajouté aux
réseaux choisis par le foyer ; l'ancien dossier est conservé tel quel (marqueur `.migre_bus` dans le nouveau).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from . import interface
from .fournisseurs import NOM_CAPACITE
from .interface import ErreurTransports

ENTETE = (
    "# Trajets habituels, par personne (slug du prénom). Écrit par l'assistant (transports_trajet_ajouter) ;\n"
    "# modifiable à la main. heure : HH:MM entre guillemets ; jours : lun … dim ; seuil_alerte_min >= 5.\n"
)
MARQUEUR_MIGRATION = ".migre_bus"


def dossier(donnees: Path) -> Path:
    return Path(donnees) / "capacites" / NOM_CAPACITE


def _creer(d: Path) -> Path:
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o2770)
    return d


def chemin_trajets(donnees: Path) -> Path:
    return dossier(donnees) / "trajets.yaml"


def identifiant(personne: str, trajet: dict[str, Any]) -> str:
    base = "|".join(str(trajet.get(k) or "") for k in ("fournisseur", "mode", "origine", "destination", "ligne", "heure"))
    return "t" + hashlib.sha1(f"{personne}|{base}".encode()).hexdigest()[:6]


def heure_sure(t: dict[str, Any]) -> str | None:
    try:
        return interface.lire_heure(t.get("heure")) if t.get("heure") not in (None, "") else None
    except ErreurTransports:
        return None


def jours_surs(t: dict[str, Any]) -> list[str]:
    try:
        return interface.lire_jours(t.get("jours"))
    except ErreurTransports:
        return list(interface.JOURS)


def seuil(t: dict[str, Any], defaut: int = interface.SEUIL_MIN) -> int:
    try:
        valeur = int(t.get("seuil_alerte_min") or defaut)
    except (TypeError, ValueError):
        valeur = defaut
    return max(interface.SEUIL_MIN, valeur)


def lire(donnees: Path) -> dict[str, list[dict[str, Any]]]:
    """Trajets par personne (migration bus faite au passage si besoin)."""
    migrer_bus(donnees)
    chemin = chemin_trajets(donnees)
    if not chemin.exists():
        return {}
    try:
        brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ErreurTransports("Le fichier des trajets habituels est illisible : corrige-le ou redéfinis le trajet.") from exc
    if not isinstance(brut, dict):
        return {}
    trajets: dict[str, list[dict[str, Any]]] = {}
    for personne, liste in brut.items():
        if isinstance(liste, dict):
            liste = [liste]
        if isinstance(liste, list):
            propres = []
            for t in liste:
                if isinstance(t, dict):
                    t = dict(t)
                    t.setdefault("id", identifiant(str(personne), t))
                    t.setdefault("mode", "bus")
                    propres.append(t)
            trajets[str(personne)] = propres
    return trajets


def ecrire(donnees: Path, trajets: dict[str, list[dict[str, Any]]]) -> None:
    d = _creer(dossier(donnees))
    propres = {p: t for p, t in sorted(trajets.items()) if t}
    texte = ENTETE + (yaml.safe_dump(propres, allow_unicode=True, sort_keys=False, default_flow_style=None)
                      if propres else "{}\n")
    cible = d / "trajets.yaml"
    tmp = cible.with_suffix(".tmp")
    tmp.write_text(texte, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o664)
    os.replace(tmp, cible)


def fournisseurs_utilises(trajets: dict[str, list[dict[str, Any]]]) -> set[str]:
    return {str(t.get("fournisseur")) for liste in trajets.values() for t in liste if t.get("fournisseur")}


# --- migration depuis la capacité bus ---------------------------------------------------------------------------------


def convertir_trajet_bus(personne: str, t: dict[str, Any]) -> dict[str, Any]:
    """Un trajet de l'ancien format bus → trajet transports (fournisseur cartreize)."""
    arret = str(t.get("arret_nom") or t.get("arret") or "")
    commune = str(t.get("commune") or "")
    origine = f"{arret} ({commune})" if commune and commune.casefold() not in arret.casefold() else arret
    sens_libelle = str(t.get("sens_libelle") or t.get("sens") or "")
    destination = sens_libelle[5:] if sens_libelle.casefold().startswith("vers ") else sens_libelle
    nouveau = {
        "mode": "bus",
        "fournisseur": "cartreize",
        "origine": origine,
        "destination": destination,
        "ligne": str(t.get("ligne") or ""),
        "heure": heure_sure(t) or "",
        "jours": jours_surs(t),
        "seuil_alerte_min": seuil(t),
        "details": {k: v for k, v in {"sens": t.get("sens"), "sens_libelle": t.get("sens_libelle"),
                                      "arret": t.get("arret"), "arret_nom": t.get("arret_nom"),
                                      "commune": t.get("commune")}.items() if v not in (None, "")},
    }
    nouveau = {"id": identifiant(personne, nouveau), **nouveau}
    return nouveau


def migrer_bus(donnees: Path) -> dict[str, Any] | None:
    """Reprend les trajets, la grille et le choix du réseau de l'ancienne capacité bus (idempotent).

    Renvoie un résumé si une migration a eu lieu, sinon None."""
    nouveau = dossier(donnees)
    ancien = Path(donnees) / "capacites" / "bus"
    if (nouveau / MARQUEUR_MIGRATION).exists() or not ancien.is_dir():
        return None
    resume: dict[str, Any] = {"trajets": 0, "grille": False}
    _creer(nouveau)
    source = ancien / "trajets.yaml"
    if source.exists() and not chemin_trajets(donnees).exists():
        try:
            brut = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            brut = {}
        trajets: dict[str, list[dict[str, Any]]] = {}
        if isinstance(brut, dict):
            for personne, liste in brut.items():
                if isinstance(liste, dict):
                    liste = [liste]
                if isinstance(liste, list):
                    trajets[str(personne)] = [convertir_trajet_bus(str(personne), t) for t in liste if isinstance(t, dict)]
        ecrire(donnees, trajets)
        resume["trajets"] = sum(len(v) for v in trajets.values())
    grille = ancien / "gtfs"
    cible = nouveau / "cartreize" / "gtfs"
    if grille.is_dir() and not grille.is_symlink() and not cible.exists():
        _creer(cible.parent)
        try:
            grille.rename(cible)
            resume["grille"] = True
        except OSError:
            pass
    # Le réseau Cartreize devient un réseau choisi par le foyer (paramètre `fournisseurs` de la capacité).
    parametres = nouveau / "parametres.yaml"
    try:
        valeurs = yaml.safe_load(parametres.read_text(encoding="utf-8")) or {} if parametres.exists() else {}
    except (OSError, yaml.YAMLError):
        valeurs = {}
    if isinstance(valeurs, dict):
        choisis = [x.strip() for x in str(valeurs.get("fournisseurs") or "").split(",") if x.strip()]
        if "cartreize" not in choisis:
            valeurs["fournisseurs"] = ", ".join([*choisis, "cartreize"])
            tmp = parametres.with_suffix(".tmp")
            tmp.write_text("# Paramètres de la capacité « Transports » (remplis par l'onboarding)\n"
                           + yaml.safe_dump(valeurs, allow_unicode=True, sort_keys=True), encoding="utf-8")
            tmp.replace(parametres)
    (nouveau / MARQUEUR_MIGRATION).write_text(json.dumps({"depuis": "bus", **resume}) + "\n", encoding="utf-8")
    return resume
