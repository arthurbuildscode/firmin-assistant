"""Fournisseur « cartreize » : cars de la Métropole Aix-Marseille-Provence, via le service local Node (ex-capacité bus).

Le service (`service/`, copie du projet d'origine, lancé par `homeos-transports-cartreize.service`) écoute sur
127.0.0.1:8081 et fait tout le travail : grille théorique GTFS, temps réel SIRI avec cache et quota. Cette source ne
fait que l'interroger en HTTP (via le client du fournisseur, hôte local seulement) et normaliser les réponses.

Routes utilisées (voir `gerer()` dans `service/app.mjs`) :
  GET /api/lignes                              lignes (id, ligne, nom, tempsReel)
  GET /api/ligne/<ligne>                       sens et arrêts d'une ligne
  GET /api/passages/<ligne>/<sens>/<arrêt>     passages croisés théorique / temps réel
  GET /api/diagnostic                          état du service (smoke test)

Adresse du service : `HOMEOS_TRANSPORTS_CARTREIZE_URL`, sinon `HOMEOS_BUS_URL` (ancienne capacité), sinon
`transports.cartreize.url` puis `bus.url` de config.yaml, sinon http://127.0.0.1:8081.
Aucun arrêt, trajet ni prénom n'est écrit ici : les trajets du foyer sont dans data/capacites/transports/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from capabilities.transports import interface
from capabilities.transports.interface import ErreurTransports, SourceIndisponible, normaliser

URL_DEFAUT = "http://127.0.0.1:8081"
DELAI_COURT_S = 5.0
# Un appel /api/passages peut attendre SIRI : 2 essais de 3 s, puis éventuellement les arrêts suivants.
DELAI_PASSAGES_S = 15.0
SERVICE = Path(__file__).resolve().parent / "service"
FICHIERS_GRILLE = ("horaires.json", "reseau.json")  # reseau.json en dernier : il porte la date `genere`
DELAI_PREPARE_S = 1800
AGE_MAX_GRILLE_JOURS = 21
MESSAGE_INDISPONIBLE = "Service des horaires Cartreize indisponible : le serveur ne répond pas. Réessaie dans quelques minutes."


def url_service(config: dict[str, Any] | None = None) -> str:
    for env in ("HOMEOS_TRANSPORTS_CARTREIZE_URL", "HOMEOS_BUS_URL"):
        valeur = os.environ.get(env, "").strip()
        if valeur:
            return valeur.rstrip("/")
    config = config if isinstance(config, dict) else {}
    transports = config.get("transports") if isinstance(config.get("transports"), dict) else {}
    for bloc in ((transports or {}).get("cartreize"), config.get("bus")):
        if isinstance(bloc, dict) and bloc.get("url"):
            return str(bloc["url"]).rstrip("/")
    return URL_DEFAUT


def _texte_arret(a: dict[str, Any]) -> str:
    return normaliser(f"{a.get('nom', '')} {a.get('commune', '')}")


def filtrer_arrets(arrets: list[dict[str, Any]], recherche: Any) -> list[dict[str, Any]]:
    """Filtre accent-insensible sur le nom et la commune ; tous les mots de la recherche doivent apparaître."""
    mots = normaliser(recherche or "").split()
    return [a for a in arrets if all(m in _texte_arret(a) for m in mots)]


def _arrets_exacts(arrets: list[dict[str, Any]], demande: str) -> list[dict[str, Any]]:
    d = normaliser(demande)
    return [a for a in arrets if d and d in {normaliser(a.get("id")), normaliser(a.get("nom")), _texte_arret(a)}]


def _sens_vise(s: dict[str, Any], destination: str) -> bool:
    d = normaliser(destination).removeprefix("vers ").strip()
    return bool(d) and d in {normaliser(s.get("id")), normaliser(s.get("libelle")).removeprefix("vers ")}


def lire_genere(dossier_grille: Path) -> datetime | None:
    try:
        with (dossier_grille / "reseau.json").open("r", encoding="utf-8") as fh:
            debut = fh.read(200)
    except OSError:
        return None
    import re

    m = re.search(r'"genere"\s*:\s*"([^"]+)"', debut)
    if not m:
        return None
    try:
        genere = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    return genere if genere.tzinfo else genere.replace(tzinfo=interface.TZ)


class Source(interface.Source):
    """Cartreize : horaires théoriques GTFS et temps réel SIRI via le service local."""

    # -- service local
    @property
    def url(self) -> str:
        return url_service(getattr(self.client.ctx, "config", None))

    def appeler(self, *segments: str, delai: float = DELAI_COURT_S) -> Any:
        """GET /api/<segments…> ; renvoie le JSON, ou None si 404 (ligne/arrêt inconnu)."""
        chemin = "/".join(urllib.parse.quote(str(s), safe="") for s in segments)
        base = self.url
        hote = urllib.parse.urlsplit(base).hostname or ""
        if hote not in self.client.hotes:
            self.client.hotes.append(hote)  # le service local (même configuré ailleurs) n'est joint que par cette source
        try:
            return self.client.json(f"{base}/api/{chemin}", delai=delai)
        except SourceIndisponible as exc:
            if "erreur 404" in str(exc):
                return None
            raise SourceIndisponible(MESSAGE_INDISPONIBLE) from None

    def _lignes(self) -> list[dict[str, Any]]:
        lignes = self.appeler("lignes")
        if not isinstance(lignes, list):
            raise SourceIndisponible(MESSAGE_INDISPONIBLE)
        return lignes

    def resoudre_ligne(self, ligne: Any) -> dict[str, Any]:
        """« 12 », « N12 », « ligne 12 » → détail de la ligne (sens et arrêts)."""
        demande = normaliser(ligne).removeprefix("ligne ").replace(" ", "")
        lignes = self._lignes()
        trouvee = next((x for x in lignes if demande in {normaliser(x.get("id", "")), normaliser(x.get("ligne", ""))}), None)
        if trouvee is None:
            connues = ", ".join(str(x.get("ligne")) for x in lignes)
            raise ErreurTransports(f"Ligne « {ligne} » inconnue. Lignes disponibles : {connues}.")
        detail = self.appeler("ligne", trouvee["id"])
        if not isinstance(detail, dict):
            raise ErreurTransports(f"Ligne « {ligne} » introuvable sur le service.")
        return detail

    def lignes(self) -> list[dict[str, Any]]:
        resultat = []
        for x in self._lignes():
            detail = self.appeler("ligne", x["id"]) or {}
            resultat.append({"ligne": x.get("ligne"), "titre": x.get("nom"), "temps_reel": bool(x.get("tempsReel")),
                             "sens": [s.get("libelle") for s in detail.get("sens") or []]})
        return resultat

    # -- interface
    def resoudre_trajet(self, trajet: dict[str, Any]) -> dict[str, Any]:
        """Ligne (donnée ou cherchée), sens (vers la destination) et arrêt de montée (origine)."""
        details = trajet.get("details") or {}
        if details.get("sens") and details.get("arret") and trajet.get("ligne"):
            return trajet
        origine, destination = str(trajet.get("origine") or ""), str(trajet.get("destination") or "")
        if not origine:
            raise ErreurTransports("À quel arrêt tu montes ?")
        if trajet.get("ligne"):
            detail_lignes = [self.resoudre_ligne(trajet["ligne"])]
        else:
            detail_lignes = [d for d in (self.appeler("ligne", x["id"]) for x in self._lignes()) if isinstance(d, dict)]
        possibles: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []
        ambigus: list[dict[str, Any]] = []
        for detail in detail_lignes:
            for s in detail.get("sens") or []:
                arrets = s.get("arrets") or []
                exacts = _arrets_exacts(arrets, origine)
                candidats = exacts or filtrer_arrets(arrets, origine)
                if not candidats:
                    continue
                if len(candidats) > 1:
                    ambigus += candidats
                    continue
                a = candidats[0]
                index = arrets.index(a)
                if _sens_vise(s, destination):
                    possibles.append((detail, s, a, str(s["libelle"]).removeprefix("Vers ").removeprefix("vers ")))
                    continue
                apres = filtrer_arrets(arrets[index + 1 :], destination) if destination else []
                if apres:
                    exact = _arrets_exacts(apres, destination)
                    possibles.append((detail, s, a, (exact or apres)[0]["nom"]))
        if not possibles and ambigus:
            noms = " ; ".join(sorted({f"{a['nom']} ({a.get('commune', '')})" for a in ambigus})[:8])
            raise ErreurTransports(f"Plusieurs arrêts correspondent à « {origine} » : {noms}. Précise.")
        if not possibles:
            ligne = f"la ligne {trajet['ligne']}" if trajet.get("ligne") else "aucune ligne Cartreize"
            raise ErreurTransports(f"Je ne trouve pas de car de {origine} vers {destination} sur {ligne}. "
                                   "Donne-moi le numéro de la ligne et l'arrêt exact.")
        if len(possibles) > 1:
            options = " ; ".join(f"ligne {d.get('ligne')} {s['libelle'].lower()} depuis {a['nom']}" for d, s, a, _ in possibles[:8])
            raise ErreurTransports(f"Plusieurs possibilités : {options}. Laquelle ?")
        detail, s, a, vers = possibles[0]
        commune = a.get("commune") or ""
        return {
            **trajet,
            "ligne": str(detail.get("ligne")),
            "origine": f"{a['nom']} ({commune})" if commune else a["nom"],
            "destination": vers,
            "details": {"sens": s["id"], "sens_libelle": s["libelle"], "arret": a["id"], "arret_nom": a["nom"],
                        "commune": commune},
        }

    def prochains_departs(self, trajet: dict[str, Any], horizon: int = 60) -> dict[str, Any]:
        t = self.resoudre_trajet(trajet)
        details = t["details"]
        detail = self.resoudre_ligne(t["ligne"])
        etat = self.appeler("passages", detail["id"], details["sens"], details["arret"], delai=DELAI_PASSAGES_S)
        if not isinstance(etat, dict):
            raise ErreurTransports(f"Arrêt « {details.get('arret_nom') or details['arret']} » inconnu du service pour ce sens.")
        return self.formater(etat, horizon)

    @staticmethod
    def formater_passage(p: dict[str, Any], ligne: str | None = None) -> dict[str, Any]:
        return interface.depart(
            p.get("prevu") or "00:00", heure_estimee=p.get("estime"), retard_min=p.get("retard"),
            statut=p.get("statut"), dans_min=p.get("dans"), ligne=ligne, destination=p.get("destination"),
            suivi=bool(p.get("suivi")), deduit=p.get("deduit"), signal_perdu=p.get("signalPerdu"),
        )

    def formater(self, etat: dict[str, Any], horizon: int = 60, nombre: int = 10) -> dict[str, Any]:
        tr = etat.get("tempsReel") or {}
        ok = bool(tr.get("disponible")) and bool(tr.get("ok"))
        if not tr.get("disponible"):
            avertissement = "Pas de temps réel sur cette ligne : horaires théoriques."
        elif not tr.get("ok"):
            avertissement = "Temps réel indisponible pour l'instant : horaires théoriques."
        else:
            avertissement = None
        ligne = (etat.get("ligne") or {}).get("ligne")
        passes = [self.formater_passage(p, ligne) for p in etat.get("passes") or [] if (p.get("dans") or 0) >= -30]
        prochains = [self.formater_passage(p, ligne) for p in (etat.get("prochains") or [])[:nombre]]
        return interface.departs(prochains, temps_reel=ok, avertissement=avertissement, passes=passes)

    # -- grille GTFS (cron rafraichir_grilles) et smoke test
    def dossier_grille(self) -> Path:
        return self.client.dossier / "gtfs"

    def rafraichir(self, forcer: bool = False, python: str | None = None, service: Path = SERVICE) -> dict[str, Any] | None:
        """Lance prepare.py dans un dossier temporaire puis installe la nouvelle grille (une fois par semaine)."""
        cible = self.dossier_grille()
        cible.mkdir(parents=True, exist_ok=True)
        ignore = cible / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# Grille GTFS Cartreize (≈ 25 Mo, régénérée chaque semaine) : jamais versionnée\n*\n",
                              encoding="utf-8")
        genere = lire_genere(cible)
        if genere is not None and not forcer and (interface.maintenant() - genere).days < 7:
            return None
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=cible) as tmp:
            travail = Path(tmp)
            for nom in ("prepare.py", "config.json"):
                shutil.copy2(service / nom, travail / nom)
            try:
                res = subprocess.run([python or sys.executable, "prepare.py"], cwd=travail, capture_output=True,
                                     text=True, timeout=DELAI_PREPARE_S, check=False)
            except subprocess.TimeoutExpired as exc:
                raise SourceIndisponible("prepare.py n'a pas fini à temps : grille inchangée") from exc
            if res.returncode != 0:
                fin = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["?"]
                raise SourceIndisponible(f"prepare.py a échoué (grille inchangée) : {fin[0][:200]}")
            produits = travail / "data"
            try:
                reseau = json.loads((produits / "reseau.json").read_text(encoding="utf-8"))
                nb_lignes = len(reseau["lignes"])
                taille = (produits / "horaires.json").stat().st_size
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SourceIndisponible("prepare.py n'a pas produit une grille lisible : grille inchangée") from exc
            if nb_lignes == 0 or taille < 1000:
                raise SourceIndisponible("grille produite vide : grille inchangée")
            for nom in FICHIERS_GRILLE:
                os.chmod(produits / nom, 0o664)
                os.replace(produits / nom, cible / nom)
        # Signal pour homeos-transports-cartreize-recharger.path (redémarrage du service), écrit en dernier.
        (cible / ".maj").write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n", encoding="utf-8")
        return {"lignes": nb_lignes, "horaires_octets": taille, "genere": reseau.get("genere")}

    def verifier(self) -> dict[str, Any]:
        import time

        debut = time.monotonic()
        try:
            diag = self.appeler("diagnostic", delai=3.0)
        except ErreurTransports:
            return {"ok": False, "message": f"Service Cartreize injoignable ({self.url}) : systemctl status homeos-transports-cartreize"}
        duree = time.monotonic() - debut
        if not isinstance(diag, dict):
            return {"ok": False, "message": "Service Cartreize : réponse de diagnostic inattendue"}
        if duree > 3.0:
            return {"ok": False, "message": f"Service Cartreize trop lent ({duree:.1f} s)"}
        morceaux = [f"Service Cartreize OK ({duree * 1000:.0f} ms)"]
        if diag.get("fluxEnPause"):
            morceaux.append("temps réel en pause : " + ", ".join(diag["fluxEnPause"]))
        erreurs = diag.get("dernieresErreurs") or []
        if erreurs:
            morceaux.append(f"dernière erreur SIRI : {str(erreurs[-1].get('erreur', ''))[:80]}")
        genere = lire_genere(self.dossier_grille())
        if genere is not None:
            age = (interface.maintenant() - genere).days
            if age > AGE_MAX_GRILLE_JOURS:
                return {"ok": False, "message": f"Grille horaires Cartreize vieille de {age} jours : le cron rafraichir_grilles échoue"}
            morceaux.append(f"grille de {age} j")
        return {"ok": True, "message": " ; ".join(morceaux)}
