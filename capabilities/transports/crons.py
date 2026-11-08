"""Crons de la capacité transports.

- `rafraichir_grilles` (manifeste : chaque jour 04:00) : grilles théoriques des réseaux utilisés par le foyer (trajets,
  réseaux choisis, grilles déjà présentes) ; chaque source décide si c'est utile (Cartreize : une fois par semaine,
  `prepare.py` puis redémarrage du service local via `.maj` ; SNCF : chaque jour ; GTFS-RT : selon `frequence_jours`).
  Un échec laisse la grille en place.
- `alertes_retard` (toutes les 10 min de 6 h à 21 h) : pour chaque personne dont un trajet habituel part dans
  l'heure, regarde le départ associé ; retard ≥ seuil du trajet (5 min au moins) ou suppression → alerte **à cette
  personne seulement** (`ctx.publier`, seuil déclaré `retard`, carte `transports_prochains`), une fois par jour et par
  départ (`data/capacites/transports/alertes.json`). Rien dans le groupe.
- `veille_reservations` (toutes les 15 min de 6 h à 22 h) : trains et vols lus dans l'agenda du foyer (pas les
  agendas privés) : la veille (à partir de `heure_veille`, 18 h par défaut) un rappel avec l'état connu ; le jour
  même, dans les 3 h avant le départ, une alerte si le train est supprimé ou en retard d'au moins le seuil (seuil
  déclaré `reservation`) ; pour un vol, sans source fiable, un rappel de vérifier la porte. Une fois chacun.

Hors du cadre (installation) :

    python -m capabilities.transports.crons rafraichir_grilles [--donnees /srv/home-os/data] [--fournisseur cartreize] [--forcer]
    python -m capabilities.transports.crons migrer [--donnees …]
    python -m capabilities.transports.crons utilise <fournisseur> [--donnees …]   (code 0 si le foyer s'en sert)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import fournisseurs as fr
from . import interface, modele, reservations
from . import tools as outils
from .interface import ErreurTransports

SEUIL_RETARD = "retard"
SEUIL_RESERVATION = "reservation"
FICHIER_ALERTES = "alertes.json"
FICHIER_VEILLE = "veille.json"
HEURE_VEILLE_DEFAUT = 18
AVANCE_JOUR_MEME_MIN = 180


def _lire_json(chemin: Path) -> dict[str, Any]:
    try:
        brut = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return brut if isinstance(brut, dict) else {}


def _ecrire_json(chemin: Path, valeur: dict[str, Any]) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(valeur, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, chemin)


# --- grilles -------------------------------------------------------------------------------------------------------------


def fournisseurs_utilises(donnees: Path) -> set[str]:
    try:
        noms = modele.fournisseurs_utilises(modele.lire(donnees))
    except ErreurTransports:
        noms = set()
    noms |= fr.choisis_par_foyer(outils.parametres(SimpleNamespace(donnees=donnees)))
    base = fr.dossier_donnees(donnees)
    for f in fr.decouvrir(donnees):
        if (base / f.nom / "grille.zip").exists() or (base / f.nom / "gtfs").is_dir() or f.provenance == "instance":
            noms.add(f.nom)
    return noms


def rafraichir_grilles(ctx: Any, seulement: str | None = None, forcer: bool = False) -> dict[str, Any]:
    donnees = Path(ctx.donnees)
    resultats: dict[str, Any] = {}
    noms = {seulement} if seulement else fournisseurs_utilises(donnees)
    for f in fr.decouvrir(donnees):
        if f.nom not in noms or not f.valide or f.source == "manuel":
            continue
        try:
            resultats[f.nom] = fr.charger(f, donnees, ctx).rafraichir(forcer=forcer)
        except ErreurTransports as exc:
            resultats[f.nom] = {"erreur": str(exc)}
            print(f"Grille {f.titre} : {exc}", file=sys.stderr)
    for nom, res in resultats.items():
        if res and "erreur" not in res:
            print(f"Grille {nom} rafraîchie : {res}", file=sys.stderr)
    erreurs = {n: r["erreur"] for n, r in resultats.items() if isinstance(r, dict) and "erreur" in r}
    if erreurs and len(erreurs) == len([r for r in resultats.values() if r is not None]):
        raise ErreurTransports("; ".join(f"{n} : {e}" for n, e in erreurs.items()))
    return resultats


# --- alertes de retard ---------------------------------------------------------------------------------------------------


def _personnes(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in config.get("personnes") or [] if isinstance(p, dict) and p.get("linux_user")]


def _cle_personne(p: dict[str, Any], trajets: dict[str, Any]) -> str | None:
    for c in (str(p.get("linux_user") or ""), str(p.get("prenom") or "")):
        if c.strip():
            s = interface.slug(c)
            if s in trajets:
                return s
    return None


def _carte_trajet(item: dict[str, Any]) -> dict[str, Any] | None:
    return outils.carte_prochains({"trajets": [item]})


def alertes_retard(ctx: Any) -> list[dict[str, Any]]:
    """Alertes du moment, remises chacune à sa personne ; renvoie la liste des alertes remises."""
    donnees = Path(ctx.donnees)
    config = getattr(ctx, "config", None) or {}
    try:
        trajets = modele.lire(donnees)
    except ErreurTransports:
        return []
    if not trajets:
        return []
    instant = interface.maintenant()
    jour = instant.date().isoformat()
    chemin = modele.dossier(donnees) / FICHIER_ALERTES
    deja = _lire_json(chemin)
    remises = set(deja.get("remises") or []) if deja.get("jour") == jour else set()
    nouvelles: list[dict[str, Any]] = []
    for p in _personnes(config):
        linux_user = str(p["linux_user"])
        cle = _cle_personne(p, trajets)
        if cle is None:
            continue
        for t in trajets[cle]:
            if not outils.part_dans_l_heure(t, instant):
                continue
            item = outils.etat_trajet(ctx, t)
            alerte = item.get("alerte")
            if not alerte:
                continue
            habituel = item.get("depart_habituel") or {}
            marque = "|".join([linux_user, str(t.get("id")), str(habituel.get("heure_theorique")),
                               "supprime" if "supprim" in alerte else "retard"])
            if marque in remises:
                continue
            titre_court = outils.MODES_TITRES.get(str(t.get("mode")), "Trajet") + (f" {t['ligne']}" if t.get("ligne") else "")
            texte = f"{titre_court} : {alerte} ({item.get('origine')})."
            notification = {"titre": titre_court, "corps": alerte[:1].upper() + alerte[1:] + "."}
            publication = ctx.publier(linux_user, texte, _carte_trajet(item), notification, SEUIL_RETARD, "alerte")
            if publication is None:
                continue
            remises.add(marque)
            nouvelles.append({"personne": linux_user, "trajet": t.get("id"), "alerte": alerte})
    if nouvelles:
        _ecrire_json(chemin, {"jour": jour, "remises": sorted(remises)})
    return nouvelles


# --- veille des réservations (agenda) --------------------------------------------------------------------------------------


def lire_agenda(debut: Any, fin: Any) -> list[dict[str, Any]]:
    """Événements des agendas du foyer (jamais les agendas privés depuis un cron). Surchargé dans les tests."""
    from tools import agenda

    return agenda.agenda_lire(debut, fin)


def destinataires_reservation(r: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """La personne nommée dans le titre ; sinon tous les adultes (propriétaires et membres)."""
    adultes = [p for p in _personnes(config) if str(p.get("role") or "proprietaire") in ("proprietaire", "membre")]
    titre = interface.normaliser(r.get("titre"))
    nommes = [str(p["linux_user"]) for p in adultes
              if p.get("prenom") and interface.normaliser(p["prenom"]) in titre.split()]
    return nommes or [str(p["linux_user"]) for p in adultes]


def _etat_reservation(ctx: Any, r: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return outils.etat(ctx, r["numero"], r["date"], r["mode"])
    except ErreurTransports:
        return None


def _quoi(r: dict[str, Any]) -> str:
    return ("train " if r["mode"] == "train" else "vol ") + r["numero"]


def message_veille(r: dict[str, Any], e: dict[str, Any] | None) -> tuple[str, dict[str, str], bool]:
    """(texte, notification, important) du rappel de la veille."""
    heure = f" à {r['heure']}" if r.get("heure") else ""
    texte = f"Demain : {_quoi(r)}{heure}."
    important = False
    if e and e.get("statut") == "supprime":
        texte = f"Demain : ton {_quoi(r)}{heure} est annoncé supprimé. Vérifie ta réservation."
        important = True
    elif e and e.get("perturbations"):
        texte += f" Annoncé : {e['perturbations'][0].get('titre')}."
    if r["mode"] == "avion":
        texte += " Je n'ai pas de suivi fiable des vols : je te rappellerai de vérifier la porte."
    elif not important:
        texte += " Je surveille les retards et je te préviens demain s'il y a du changement."
    notif = {"titre": "Ton voyage de demain", "corps": "Changement annoncé sur ton train de demain." if important
             else "Rappel de ton voyage de demain."}
    return texte, notif, important


def veille_reservations(ctx: Any) -> list[dict[str, Any]]:
    donnees = Path(ctx.donnees)
    config = getattr(ctx, "config", None) or {}
    instant = interface.maintenant()
    aujourd_hui, demain = instant.date(), instant.date() + timedelta(days=1)
    try:
        evenements = lire_agenda(aujourd_hui, demain)
    except Exception as exc:  # noqa: BLE001 - agenda injoignable : silence
        print(f"veille_reservations : agenda illisible ({exc.__class__.__name__})", file=sys.stderr)
        return []
    resas = reservations.depuis_evenements(evenements)
    if not resas:
        return []
    chemin = modele.dossier(donnees) / FICHIER_VEILLE
    memoire = _lire_json(chemin)
    limite = (aujourd_hui - timedelta(days=3)).isoformat()
    memoire = {k: v for k, v in memoire.items() if str(k)[:10] >= limite}
    heure_veille = int(outils.parametres(ctx).get("heure_veille") or HEURE_VEILLE_DEFAUT)
    seuil = outils.seuil_defaut(ctx)
    remis: list[dict[str, Any]] = []
    for r in resas:
        cle = f"{r['date']}|{r['mode']}|{r['numero']}"
        faits = set(memoire.get(cle) or [])
        envois: list[tuple[str, str, dict[str, str], str | None, str, dict[str, Any] | None]] = []
        if r["date"] == demain.isoformat() and instant.hour >= heure_veille and "veille" not in faits:
            e = _etat_reservation(ctx, r) if r["mode"] == "train" else None
            texte, notif, important = message_veille(r, e)
            envois.append(("veille", texte, notif, SEUIL_RESERVATION if important else None, "rappel", e))
        elif r["date"] == aujourd_hui.isoformat() and r.get("heure"):
            depart = datetime.fromisoformat(f"{r['date']}T{r['heure']}").replace(tzinfo=interface.TZ)
            avant = (depart - instant).total_seconds() / 60
            if -30 <= avant <= AVANCE_JOUR_MEME_MIN:
                if r["mode"] == "avion":
                    if "jour" not in faits and avant <= 150:
                        texte = (f"Ton {_quoi(r)} part à {r['heure']} : je n'ai pas de suivi fiable des vols, vérifie la "
                                 "porte et l'heure sur l'appli de la compagnie.")
                        envois.append(("jour", texte, {"titre": "Ton vol", "corps": "Pense à vérifier la porte de ton vol."},
                                       None, "rappel", None))
                else:
                    e = _etat_reservation(ctx, r)
                    if e and e.get("statut") == "supprime" and "supprime" not in faits:
                        envois.append(("supprime", f"Ton {_quoi(r)} de {r['heure']} est supprimé.",
                                       {"titre": "Ton train", "corps": "Ton train est supprimé."}, SEUIL_RESERVATION,
                                       "alerte", e))
                    elif e and (e.get("retard_min") or 0) >= seuil:
                        palier = f"retard{(int(e['retard_min']) // 10) * 10}"
                        if palier not in faits:
                            envois.append((palier, f"Ton {_quoi(r)} de {r['heure']} a {e['retard_min']} min de retard.",
                                           {"titre": "Ton train", "corps": f"Retard de {e['retard_min']} min sur ton train."},
                                           SEUIL_RESERVATION, "alerte", e))
        for marque, texte, notif, seuil_decl, motif, e in envois:
            carte = outils.carte_etat(e) if e else None
            for personne in destinataires_reservation(r, config):
                ctx.publier(personne, texte, carte, notif, seuil_decl, motif)
            faits.add(marque)
            remis.append({"reservation": cle, "envoi": marque})
        if faits:
            memoire[cle] = sorted(faits)
    _ecrire_json(chemin, memoire)
    return remis


# --- point d'entrée ------------------------------------------------------------------------------------------------------------


def executer(nom_cron: str, ctx: Any) -> None:
    """Point d'entrée du cadre des capacités (`python -m tools.capacites cron transports <nom>`)."""
    if nom_cron == "alertes_retard":
        alertes_retard(ctx)  # remises à chaque personne : rien à poster dans le groupe
        return
    if nom_cron == "veille_reservations":
        veille_reservations(ctx)
        return
    if nom_cron == "rafraichir_grilles":
        rafraichir_grilles(ctx)
        return
    raise ValueError(f"cron inconnu pour la capacité transports : {nom_cron}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crons et maintenance de la capacité transports")
    parser.add_argument("commande", choices=["rafraichir_grilles", "migrer", "utilise"])
    parser.add_argument("fournisseur", nargs="?")
    parser.add_argument("--donnees", default=os.environ.get("HOMEOS_DATA", "/srv/home-os/data"))
    parser.add_argument("--forcer", action="store_true")
    args = parser.parse_args(argv)
    donnees = Path(args.donnees)
    ctx = SimpleNamespace(donnees=donnees, personne=None, config={})
    try:
        if args.commande == "migrer":
            print(json.dumps(modele.migrer_bus(donnees), ensure_ascii=False))
        elif args.commande == "utilise":
            return 0 if args.fournisseur in fournisseurs_utilises(donnees) else 1
        else:
            rafraichir_grilles(ctx, args.fournisseur, forcer=args.forcer)
    except ErreurTransports as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
