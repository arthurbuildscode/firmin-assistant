"""Fournisseurs de la capacité transports : découverte, manifestes, accès réseau, chargement des sources.

Trois origines :

1. **standard** : `capabilities/transports/fournisseurs/<nom>/` (dépôt, `origine: humain`) ;
2. **atelier** : `<etat>/fournisseurs-atelier/<nom>/` (fabriqués par l'atelier, `origine: assistant`, lecture seule,
   hôtes approuvés par un propriétaire à la demande) — `HOMEOS_FOURNISSEURS_ATELIER` pour déplacer ce dossier ;
3. **instances d'un modèle** : `data/capacites/transports/reseaux.yaml` (ex. un réseau urbain qui publie du GTFS-RT :
   `{nom, titre, modele: gtfs_rt, modes, url_grille, url_temps_reel, url_alertes}`), ajoutées en conversation
   (`transports_reseau_ajouter`, validé par un propriétaire).

Manifeste `fournisseur.yaml` :

    nom: sncf                       # [a-z0-9_], identique au dossier
    titre: SNCF (TGV, Intercités, TER)
    description: …
    modes: [train]                  # interface.MODES
    source: api_ouverte             # api_ouverte | page_web | manuel
    formats: [gtfs, gtfs_rt]        # documentaire
    portee: nationale               # nationale (toujours proposé) | locale (proposé si le foyer l'a choisi)
    zone: France                    # couverture, en clair (découverte)
    origine: humain                 # humain | assistant
    reseau: [proxy.transport.data.gouv.fr]   # hôtes joints par la source (https ; « *.domaine » accepté)
    secret: {cle: transports/sncf, type: cle_api, facultatif: true}   # (facultatif) clé d'API déclarée dans le
                                    # manifeste de la capacité, lue dans le coffre, jamais en dur
    parametres: {url_grille: …}     # réglages par défaut passés à la source
    modele: false                   # true : modèle à instancier (gtfs_rt), jamais utilisé tel quel
    licence: ODbL                   # données utilisées, pour docs/transports.md

Accès réseau (:class:`Client`) : uniquement https (http seulement vers 127.0.0.1/localhost : services locaux, tests),
uniquement les hôtes du manifeste — et, pour un fournisseur de l'atelier ou une instance, seulement ceux approuvés
(`etat.json`, `transports.reseau_approuve`) ; agent utilisateur honnête, délais et tailles plafonnés, cache court ;
robots.txt respecté pour une source `page_web` (et la navigation passe par `tools.navigation`, qui s'arrête sur une
vérification humaine : aucun contournement anti-robot).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from . import interface
from .interface import ErreurTransports, SourceIndisponible

NOM_CAPACITE = "transports"
RACINE_STANDARD = Path(__file__).resolve().parent / "fournisseurs"
RE_NOM = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RE_HOTE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
HOTES_LOCAUX = ("127.0.0.1", "localhost", "::1")
CLES_MANIFESTE = ("nom", "titre", "description", "modes", "source", "formats", "portee", "zone", "origine", "reseau",
                  "secret", "parametres", "modele", "licence", "version")
AGENT = "FirminAssistant/1.0 (assistant domestique auto-heberge; lecture de donnees ouvertes de transport)"
DELAI_S = 12.0
TAILLE_MAX = 40_000_000


@dataclass
class Fournisseur:
    nom: str
    titre: str
    dossier: Path | None
    modes: list[str]
    source: str
    origine: str = "humain"
    portee: str = "locale"
    description: str = ""
    zone: str = ""
    reseau: list[str] = field(default_factory=list)
    secret: dict[str, Any] | None = None
    parametres: dict[str, Any] = field(default_factory=dict)
    modele: str | None = None       # instance : nom du modèle dont la source est reprise
    est_modele: bool = False
    licence: str = ""
    formats: list[str] = field(default_factory=list)
    provenance: str = "standard"   # standard | atelier | instance
    erreurs: list[str] = field(default_factory=list)

    @property
    def valide(self) -> bool:
        return not self.erreurs

    def resume(self) -> dict[str, Any]:
        return {"nom": self.nom, "titre": self.titre, "modes": self.modes, "source": self.source, "zone": self.zone,
                "portee": self.portee, "origine": self.origine, "provenance": self.provenance}


def _liste(valeur: Any) -> list[str]:
    if valeur is None:
        return []
    if isinstance(valeur, str):
        return [v.strip() for v in valeur.split(",") if v.strip()]
    return [str(v).strip() for v in valeur if str(v).strip()] if isinstance(valeur, (list, tuple)) else []


def valider_manifeste(brut: Any, nom_dossier: str | None = None, provenance: str = "standard",
                      dossier: Path | None = None) -> Fournisseur:
    """dict `fournisseur.yaml` → Fournisseur (avec `erreurs` si invalide : jamais chargé)."""
    erreurs: list[str] = []
    if not isinstance(brut, dict):
        return Fournisseur(nom_dossier or "?", nom_dossier or "?", dossier, [], "manuel", erreurs=["manifeste illisible"],
                           provenance=provenance)
    for cle in brut:
        if cle not in CLES_MANIFESTE:
            erreurs.append(f"clé inconnue « {cle} »")
    nom = str(brut.get("nom") or "")
    if not RE_NOM.fullmatch(nom):
        erreurs.append("`nom` : minuscules, chiffres et _ (commence par une lettre)")
    elif nom_dossier is not None and nom != nom_dossier:
        erreurs.append(f"`nom` ({nom}) ≠ dossier ({nom_dossier})")
    titre = str(brut.get("titre") or "").strip()
    if not titre:
        erreurs.append("`titre` obligatoire")
    modes = _liste(brut.get("modes"))
    if not modes or any(m not in interface.MODES for m in modes):
        erreurs.append(f"`modes` parmi {', '.join(interface.MODES)}")
    source = str(brut.get("source") or "")
    if source not in interface.TYPES_SOURCE:
        erreurs.append(f"`source` parmi {', '.join(interface.TYPES_SOURCE)}")
    origine = str(brut.get("origine") or "humain")
    if origine not in ("humain", "assistant"):
        erreurs.append("`origine` : humain ou assistant")
    if provenance == "atelier" and origine != "assistant":
        erreurs.append("un fournisseur de l'atelier est `origine: assistant`")
    portee = str(brut.get("portee") or "locale")
    if portee not in ("nationale", "locale"):
        erreurs.append("`portee` : nationale ou locale")
    reseau = [h.lower().rstrip(".") for h in _liste(brut.get("reseau"))]
    for h in reseau:
        if not RE_HOTE.fullmatch(h) and h not in HOTES_LOCAUX:
            erreurs.append(f"hôte réseau invalide « {h} »")
    if source != "manuel" and not reseau and not brut.get("modele"):
        erreurs.append("`reseau` : déclarer les hôtes joints par la source")
    secret = brut.get("secret")
    if secret is not None:
        if not isinstance(secret, dict) or not str(secret.get("cle") or "").startswith(f"{NOM_CAPACITE}/"):
            erreurs.append(f"`secret.cle` commence par « {NOM_CAPACITE}/ » (déclarée dans le manifeste de la capacité)")
        elif secret.get("type") not in ("cle_api", "identifiants", "code"):
            erreurs.append("`secret.type` : cle_api, identifiants ou code")
    parametres = brut.get("parametres") or {}
    if not isinstance(parametres, dict):
        erreurs.append("`parametres` : dictionnaire")
        parametres = {}
    if dossier is not None and source != "manuel" and not (dossier / "source.py").is_file():
        erreurs.append("source.py absent")
    return Fournisseur(
        nom=nom or (nom_dossier or "?"), titre=titre or nom, dossier=dossier, modes=modes, source=source,
        origine=origine, portee=portee, description=str(brut.get("description") or ""), zone=str(brut.get("zone") or ""),
        reseau=reseau, secret=secret if isinstance(secret, dict) else None, parametres=dict(parametres),
        est_modele=bool(brut.get("modele") is True), licence=str(brut.get("licence") or ""),
        formats=_liste(brut.get("formats")), provenance=provenance, erreurs=erreurs,
    )


def lire_manifeste(dossier: Path, provenance: str = "standard") -> Fournisseur:
    import yaml

    try:
        brut = yaml.safe_load((dossier / "fournisseur.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return Fournisseur(dossier.name, dossier.name, dossier, [], "manuel", provenance=provenance,
                           erreurs=[f"fournisseur.yaml illisible ({exc.__class__.__name__})"])
    return valider_manifeste(brut, dossier.name, provenance, dossier)


# --- racines et état -------------------------------------------------------------------------------------------------


def racine_atelier() -> Path | None:
    env = os.environ.get("HOMEOS_FOURNISSEURS_ATELIER", "").strip()
    if env:
        return Path(env)
    if os.environ.get("HOMEOS_CAPABILITIES"):  # tests : pas de fournisseur de l'atelier sauf demande explicite
        return None
    try:
        from tools.common import etat_dir
    except ImportError:
        return None
    return etat_dir() / "fournisseurs-atelier"


def dossier_donnees(donnees: Path) -> Path:
    return Path(donnees) / "capacites" / NOM_CAPACITE


def etat_capacite(donnees: Path) -> dict[str, Any]:
    try:
        etat = json.loads((Path(donnees) / "capacites" / "etat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entree = etat.get(NOM_CAPACITE) if isinstance(etat, dict) else None
    return entree if isinstance(entree, dict) else {}


def hotes_approuves(donnees: Path) -> set[str]:
    return {str(h).lower() for h in etat_capacite(donnees).get("reseau_approuve") or [] if isinstance(h, str)}


def lire_instances(donnees: Path) -> list[dict[str, Any]]:
    import yaml

    chemin = dossier_donnees(donnees) / "reseaux.yaml"
    try:
        brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    if isinstance(brut, dict):
        brut = brut.get("reseaux") or []
    return [r for r in brut if isinstance(r, dict)] if isinstance(brut, list) else []


def ecrire_instances(donnees: Path, instances: list[dict[str, Any]]) -> None:
    import yaml

    dossier = dossier_donnees(donnees)
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / "reseaux.yaml"
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text("# Réseaux ajoutés par le foyer (modèle gtfs_rt…), validés par un propriétaire.\n"
                   + yaml.safe_dump(instances, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(chemin)


def hotes_url(*urls: Any) -> list[str]:
    hotes = []
    for u in urls:
        if u:
            h = (urllib.parse.urlsplit(str(u)).hostname or "").lower()
            if h:
                hotes.append(h)
    return sorted(set(hotes))


# --- découverte --------------------------------------------------------------------------------------------------------


def _lire_racine(racine: Path | None, provenance: str) -> list[Fournisseur]:
    if racine is None or not racine.is_dir():
        return []
    return [lire_manifeste(d, provenance) for d in sorted(racine.iterdir())
            if d.is_dir() and not d.name.startswith((".", "_")) and (d / "fournisseur.yaml").is_file()]


def decouvrir(donnees: Path | None = None, avec_modeles: bool = False) -> list[Fournisseur]:
    """Tous les fournisseurs (valides ou non) : standard, atelier, instances de modèles du foyer."""
    standard = _lire_racine(RACINE_STANDARD, "standard")
    pris = {f.nom for f in standard}
    atelier = [f for f in _lire_racine(racine_atelier(), "atelier") if f.nom not in pris]
    pris |= {f.nom for f in atelier}
    modeles = {f.nom: f for f in standard if f.est_modele and f.valide}
    instances: list[Fournisseur] = []
    if donnees is not None:
        for r in lire_instances(donnees):
            nom = str(r.get("nom") or "")
            modele = modeles.get(str(r.get("modele") or ""))
            erreurs = []
            if not RE_NOM.fullmatch(nom) or nom in pris:
                erreurs.append(f"nom d'instance invalide ou déjà pris : « {nom} »")
            if modele is None:
                erreurs.append(f"modèle inconnu « {r.get('modele')} »")
            params = {k: v for k, v in r.items() if k.startswith("url_") or k in ("frequence_jours",)}
            instances.append(Fournisseur(
                nom=nom or "?", titre=str(r.get("titre") or nom), dossier=modele.dossier if modele else None,
                modes=_liste(r.get("modes")) or (modele.modes if modele else []), source="api_ouverte",
                origine="humain", portee="locale", zone=str(r.get("zone") or ""),
                reseau=hotes_url(*params.values()), parametres={**(modele.parametres if modele else {}), **params},
                modele=modele.nom if modele else None, provenance="instance", erreurs=erreurs,
                licence=str(r.get("licence") or ""),
            ))
            pris.add(nom)
    tous = standard + atelier + instances
    return tous if avec_modeles else [f for f in tous if not f.est_modele]


def trouver(nom: str, donnees: Path | None = None) -> Fournisseur:
    demande = interface.normaliser(nom).replace(" ", "_")
    tous = decouvrir(donnees)
    for f in tous:
        if f.nom == demande or interface.normaliser(f.titre) == interface.normaliser(nom):
            return f
    proches = [f for f in tous if demande and (demande in f.nom or interface.normaliser(nom) in interface.normaliser(f.titre))]
    if len(proches) == 1:
        return proches[0]
    raise ErreurTransports(f"Je ne connais pas le réseau « {nom} ». Réseaux connus : "
                           + ", ".join(f.titre for f in tous if f.valide) + ".")


def choisis_par_foyer(parametres: dict[str, Any]) -> set[str]:
    return {interface.slug(x, "_") for x in _liste(parametres.get("fournisseurs"))}


def disponibles(donnees: Path, parametres: dict[str, Any] | None = None, trajets_fournisseurs: set[str] | None = None) -> list[Fournisseur]:
    """Fournisseurs proposés au foyer : portée nationale, ceux choisis (paramètre `fournisseurs`), ceux de ses
    trajets, ses instances et ceux de l'atelier (approuvés)."""
    choisis = choisis_par_foyer(parametres or {}) | set(trajets_fournisseurs or ())
    return [f for f in decouvrir(donnees) if f.valide and (
        f.portee == "nationale" or f.nom in choisis or f.provenance in ("instance", "atelier"))]


# --- accès réseau ------------------------------------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, bytes]] = {}
_VERROU = threading.Lock()
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def hote_autorise(hote: str, hotes: list[str]) -> bool:
    hote = hote.lower().rstrip(".")
    for h in hotes:
        if h.startswith("*.") and (hote == h[2:] or hote.endswith(h[1:])):
            return True
        if hote == h:
            return True
    return False


class Client:
    """Accès réseau d'un fournisseur, limité à ses hôtes (voir docstring du module)."""

    def __init__(self, fournisseur: Fournisseur, hotes: list[str], dossier: Path, ctx: Any = None,
                 robots: bool | None = None) -> None:
        self.fournisseur = fournisseur
        self.hotes = [h.lower() for h in hotes]
        self._dossier = Path(dossier)
        self.ctx = ctx
        self.robots = fournisseur.source == "page_web" if robots is None else robots
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    @property
    def dossier(self) -> Path:
        """`data/capacites/transports/<fournisseur>/` : grilles, caches (créé à la demande)."""
        if not self._dossier.exists():
            self._dossier.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self._dossier, 0o2770)
        ignore = self._dossier / ".gitignore"
        if not ignore.exists():  # grilles et caches régénérés : jamais dans le git de data/
            with contextlib.suppress(OSError):
                ignore.write_text("# Grilles et caches du réseau (régénérés) : jamais versionnés\n*\n", encoding="utf-8")
        return self._dossier

    # -- contrôle
    def verifier_url(self, url: str) -> str:
        parties = urllib.parse.urlsplit(str(url))
        hote = (parties.hostname or "").lower()
        local = hote in HOTES_LOCAUX
        if parties.scheme != "https" and not (local and parties.scheme == "http"):
            raise ErreurTransports(f"{self.fournisseur.titre} : adresse refusée (https seulement).")
        if not hote_autorise(hote, self.hotes):
            raise ErreurTransports(f"{self.fournisseur.titre} : le site {hote} n'est pas autorisé pour ce réseau "
                                   "(un propriétaire du foyer doit l'approuver).")
        return hote

    def _robots_autorise(self, url: str) -> bool:
        parties = urllib.parse.urlsplit(url)
        base = f"{parties.scheme}://{parties.netloc}"
        if base not in self._robots:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                contenu = self._lire(base + "/robots.txt", max_octets=500_000, delai=DELAI_S, verifier=False)
                rp.parse(contenu.decode("utf-8", "replace").splitlines())  # type: ignore[union-attr]
            except (ErreurTransports, urllib.error.HTTPError):
                rp = None  # pas de robots.txt : autorisé
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(AGENT, url)

    def _lire(self, url: str, *, max_octets: int, delai: float, entetes: dict[str, str] | None = None,
              verifier: bool = True) -> bytes:
        hote = self.verifier_url(url) if verifier else (urllib.parse.urlsplit(url).hostname or "")
        requete = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "*/*", **(entetes or {})})
        ouvreur = _DIRECT if hote in HOTES_LOCAUX else urllib.request.build_opener()
        try:
            with ouvreur.open(requete, timeout=delai) as rep:
                contenu = rep.read(max_octets + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SourceIndisponible(f"{self.fournisseur.titre} refuse l'accès (clé manquante ou invalide ?).") from None
            raise SourceIndisponible(f"{self.fournisseur.titre} ne répond pas correctement (erreur {exc.code}).") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SourceIndisponible(f"{self.fournisseur.titre} ne répond pas : réessaie dans quelques minutes.") from None
        if len(contenu) > max_octets:
            raise SourceIndisponible(f"{self.fournisseur.titre} : réponse trop grosse.")
        return contenu

    # -- API des sources
    def octets(self, url: str, params: dict[str, Any] | None = None, *, entetes: dict[str, str] | None = None,
               cache_s: float = 0, max_octets: int = TAILLE_MAX, delai: float = DELAI_S) -> bytes:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        self.verifier_url(url)
        if self.robots and not self._robots_autorise(url):
            raise ErreurTransports(f"{self.fournisseur.titre} demande aux robots de ne pas lire cette page : je n'insiste pas.")
        cle = f"{url}|{json.dumps(entetes or {}, sort_keys=True)}"
        if cache_s:
            with _VERROU:
                trouve = _CACHE.get(cle)
            if trouve and time.monotonic() - trouve[0] < cache_s:
                return trouve[1]
        contenu = self._lire(url, max_octets=max_octets, delai=delai, entetes=entetes)
        if cache_s:
            with _VERROU:
                if len(_CACHE) > 64:
                    _CACHE.clear()
                _CACHE[cle] = (time.monotonic(), contenu)
        return contenu

    def json(self, url: str, params: dict[str, Any] | None = None, **options: Any) -> Any:
        contenu = self.octets(url, params, **options)
        try:
            return json.loads(contenu.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SourceIndisponible(f"{self.fournisseur.titre} : réponse illisible.") from None

    def texte(self, url: str, params: dict[str, Any] | None = None, **options: Any) -> str:
        return self.octets(url, params, **options).decode("utf-8", "replace")

    def telecharger(self, url: str, chemin: Path, *, max_octets: int = 300_000_000, delai: float = 120.0) -> Path:
        """Télécharge un gros fichier (grille GTFS) dans le dossier du fournisseur, par morceaux."""
        self.verifier_url(url)
        chemin = Path(chemin)
        if self.dossier.resolve() not in chemin.resolve().parents:
            raise ErreurTransports("Téléchargement hors du dossier du réseau refusé.")
        hote = urllib.parse.urlsplit(url).hostname or ""
        requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
        ouvreur = _DIRECT if hote in HOTES_LOCAUX else urllib.request.build_opener()
        total = 0
        try:
            with ouvreur.open(requete, timeout=delai) as rep, chemin.open("wb") as fh:
                while morceau := rep.read(1 << 20):
                    total += len(morceau)
                    if total > max_octets:
                        raise SourceIndisponible(f"{self.fournisseur.titre} : grille trop grosse.")
                    fh.write(morceau)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            chemin.unlink(missing_ok=True)
            raise SourceIndisponible(f"{self.fournisseur.titre} : téléchargement impossible ({exc.__class__.__name__}).") from None
        return chemin

    def lire_json(self, nom: str, defaut: Any = None) -> Any:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", nom):
            raise ErreurTransports("nom de fichier invalide")
        try:
            return json.loads((self.dossier / f"{nom}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaut

    def ecrire_json(self, nom: str, valeur: Any) -> None:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", nom):
            raise ErreurTransports("nom de fichier invalide")
        chemin = self.dossier / f"{nom}.json"
        tmp = chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(valeur, ensure_ascii=False), encoding="utf-8")
        tmp.replace(chemin)

    def cle_api(self) -> str | None:
        """Clé d'API du fournisseur (coffre, via la capacité), None si elle n'est pas renseignée et facultative."""
        secret = self.fournisseur.secret
        if not secret or self.ctx is None or not hasattr(self.ctx, "secret"):
            return None
        try:
            valeurs = self.ctx.secret(str(secret["cle"]))
        except Exception as exc:  # noqa: BLE001 - SecretManquant du coffre, coffre indisponible…
            if secret.get("facultatif"):
                return None
            raise ErreurTransports(str(exc)) from None
        return str(valeurs.get("cle") or "") or None

    def navigation(self, **options: Any) -> Any:
        """Session de navigateur (page web) limitée aux hôtes du fournisseur : robots.txt, pas de contournement."""
        from tools import navigation

        return navigation.Session(f"{NOM_CAPACITE}_{self.fournisseur.nom}", self.hotes, personne=None, **options)


def hotes_effectifs(f: Fournisseur, donnees: Path) -> list[str]:
    """Hôtes joignables : manifeste (dépôt) ; manifeste ∩ approuvés (atelier, instances)."""
    if f.provenance == "standard" and f.origine == "humain":
        return list(f.reseau)
    approuves = hotes_approuves(donnees)
    return [h for h in f.reseau if h in approuves or h in HOTES_LOCAUX]


# --- chargement des sources -------------------------------------------------------------------------------------------

_MODULES: dict[str, tuple[float, ModuleType]] = {}


def _module_source(f: Fournisseur) -> ModuleType:
    if f.dossier is None:
        raise ErreurTransports(f"{f.titre} : pas de source.")
    chemin = f.dossier / "source.py"
    cle = str(chemin.resolve())
    mtime = chemin.stat().st_mtime
    if cle in _MODULES and _MODULES[cle][0] == mtime:
        return _MODULES[cle][1]
    base = __name__.rsplit(".", 1)[0]  # capabilities.transports
    nom = f"{base}.fournisseurs_charges.{f.provenance}_{f.dossier.name}"
    spec = importlib.util.spec_from_file_location(nom, chemin)
    if spec is None or spec.loader is None:
        raise ErreurTransports(f"{f.titre} : source illisible.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[nom] = module
    spec.loader.exec_module(module)
    _MODULES[cle] = (mtime, module)
    return module


class SourceManuelle(interface.Source):
    """Réseau sans source ouverte : seules les heures habituelles sont connues (aucun temps réel)."""

    def prochains_departs(self, trajet: dict[str, Any], horizon: int = 60) -> dict[str, Any]:
        heure = trajet.get("heure")
        liste = [interface.depart(heure)] if heure else []
        liste = [d for d in liste if d["dans_min"] >= -30]
        return interface.departs(liste, temps_reel=False,
                                 avertissement="Pas de source en temps réel pour ce trajet : heure habituelle seulement.")


def charger(f: Fournisseur, donnees: Path, ctx: Any = None) -> interface.Source:
    """Instancie la source d'un fournisseur avec son client réseau et ses paramètres."""
    if not f.valide:
        raise ErreurTransports(f"Le réseau « {f.titre} » est mal décrit : {'; '.join(f.erreurs)}.")
    client = Client(f, hotes_effectifs(f, donnees), dossier_donnees(donnees) / f.nom, ctx)
    if f.source == "manuel" and (f.dossier is None or not (f.dossier / "source.py").is_file()):
        return SourceManuelle(client, f.parametres, f)
    module = _module_source(f)
    classe = getattr(module, "Source", None)
    if classe is None:
        raise ErreurTransports(f"{f.titre} : source.py ne définit pas de classe Source.")
    return classe(client, dict(f.parametres), f)


def verifier_source(f: Fournisseur, donnees: Path, ctx: Any = None) -> dict[str, Any]:
    """Smoke test d'un fournisseur (jamais d'exception)."""
    try:
        res = charger(f, donnees, ctx).verifier()
    except ErreurTransports as exc:
        return {"ok": False, "message": f"{f.titre} : {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{f.titre} : erreur {exc.__class__.__name__}"}
    if not isinstance(res, dict):
        return {"ok": False, "message": f"{f.titre} : vérification illisible"}
    return {"ok": bool(res.get("ok")), "message": str(res.get("message") or "")}
