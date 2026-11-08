"""Atelier (lot 7b) : l'assistant fabrique lui-même une capacité à la demande du foyer.

Exemple cible : « ajoute Planity pour prendre les rendez-vous coiffeur ». Spécification et parcours : docs/atelier.md ;
contrat : CONTRACT.md §9 (« Atelier »).

Étapes (une demande à la fois) :

1. **Demande** (`preparer`, outil `atelier_creer`, propriétaires seulement) : questions de découverte faites en
   conversation (site, compte, action attendue), contrôle (jamais l'espace professionnel), reformulation et bouton
   Valider. La validation approuve le site (« Planity parlera à www.planity.com »).
2. **File** (`executer_creation`, après Valider) : `<etat>/atelier/_file/<nom>.json` ; le service
   `homeos-atelier.service` (déclenché par `homeos-atelier.path`) fabrique en arrière-plan.
3. **Fabrication** (`fabriquer`, dans le bac à sable de homeos-atelier : aucun secret ni donnée du foyer) :
   exploration publique du site (`tools.navigation.explorer`), puis un **sous-agent Claude Code** (`homeos-claude
   --homeos-atelier <nom>`, sous homeos, cwd `<etat>/atelier/<nom>`, prompt système `content/atelier/SYSTEM.md`,
   outils restreints) écrit la capacité ; contrôles statiques, `ruff`, tests de la capacité, smoke test (un tour de
   correction au plus). Résultat déposé dans `_resultats/<nom>.json`.
4. **Conclusion** (`conclure`, service `homeos-atelier-installer`, qui n'exécute jamais le code généré) : copie
   figée des seuls fichiers autorisés, contrôles statiques et `ruff` refaits, **relecteur** (second `claude -p` en
   lecture seule, grille `content/atelier/RELECTURE.md`), installation en **essai** dans
   `<etat>/capacites-atelier/<nom>/`, puis message Telegram (« Capacité … prête à l'essai : il me faut tes
   identifiants ») ou, en cas d'échec, « Ça n'a pas marché … je garde ce que j'ai appris pour l'atelier ».
5. **Première validation** : la première action avec effet est validée à la main par un propriétaire → actif
   (`tools.capacites`, `coeur.confirmations`).

Second type de fabrication (lot transports) : un **fournisseur de transports** (`fabrication="fournisseur_transports"`,
« ajoute le métro de Lyon ») — pas une capacité entière, seulement un réseau de la capacité `transports` : le dossier
de travail est amorcé avec le gabarit `content/atelier/gabarits/fournisseur_transports/` (`fournisseur.yaml`,
`source.py` qui implémente l'interface commune, `README.md`, `tests/`), contrôlé (`verifier_fournisseur`), testé, relu,
puis installé dans `<etat>/fournisseurs-atelier/<nom>/` (lecture seule : aucune action avec effet ; ses hôtes sont
approuvés pour la capacité transports à la validation de la demande).

CLI (sous homeos, cwd = dépôt) :

    python -m tools.atelier traiter            # fabrique puis conclut tout ce qui attend (mode sans systemd)
    python -m tools.atelier fabriquer          # homeos-atelier.service
    python -m tools.atelier conclure           # homeos-atelier-installer.service
    python -m tools.atelier etat [nom]         # progression (JSON)
    python -m tools.atelier smoke-candidat <dossier> --site URL --hote H [--fournisseur]   (sous-processus isolé)
    python -m tools.atelier retirer-fournisseur <nom>   (réseau de transports fabriqué : archivé, hôtes retirés)
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools import capacites
from tools.common import content_dir, data_dir, etat_dir, get_logger, iso_now, load_config, root_dir

log = get_logger("homeos.atelier")

NOM_CAPACITE = "atelier"
WRAPPER = "/usr/local/sbin/homeos-claude"
UNITE_PATH = Path("/etc/systemd/system/homeos-atelier.path")
TIMEOUT_GENERATION_S = 1500
TIMEOUT_RELECTURE_S = 600
TIMEOUT_TESTS_S = 300
TIMEOUT_SMOKE_S = 180
PASSES_MAX = 2  # génération + un tour de correction
TAILLE_MAX_FICHIER = 300_000
NOM_MAX = 30
NOMS_RESERVES = frozenset({"atelier", "administration", "admin", "capacites", "identifiants", "tools", "coeur", "bot",
                           "web", "test", "tests"})

# Outils du sous-agent (le wrapper homeos-claude impose les mêmes chaînes : tests/wrapper/test_homeos_claude.sh).
OUTILS_GENERER = (
    "Read,Glob,Grep,Edit(./**),Write(./**),Bash(python -m pytest:*),Bash(ruff check:*),Bash(python -m tools.navigation explorer:*)"
)
OUTILS_RELIRE = "Read,Glob,Grep"
MODES = {"generer": (OUTILS_GENERER, "SYSTEM.md", "60"), "relire": (OUTILS_RELIRE, "RELECTURE.md", "25")}

# Seuls ces fichiers sont installés (tout le reste du dossier de travail est ignoré).
FICHIERS_RACINE = ("capability.yaml", "tools.py", "connecteur.py", "smoke.py", "crons.py", "CLAUDE.md", "README.md",
                   "onboarding.md", "__init__.py")
FICHIERS_FOURNISSEUR = ("fournisseur.yaml", "source.py", "README.md", "__init__.py")
FABRICATIONS = {"capacite": "capacité", "fournisseur_transports": "réseau de transports"}
RE_FICHIER_TEST = re.compile(r"^tests/(?:[a-z0-9_]+\.py|fixtures/[a-z0-9_.-]+\.(?:html|json|txt))$")
IGNORES = ("exploration", "DEMANDE.md", "CORRECTIONS.md", "__pycache__", ".pytest_cache", ".ruff_cache")

ETAPES = {
    "en_attente": "en file d'attente",
    "exploration": "je visite le site",
    "generation": "j'écris la capacité",
    "verification": "je la teste",
    "correction": "je corrige ce qui ne va pas",
    "relecture": "je la fais relire",
    "installation": "je l'installe",
    "pret": "prête à l'essai",
    "echec": "pas abouti",
}

MOTS_PRO = re.compile(
    r"\b(espace\s+pro\w*|pro(?:fessionnel\w*)?\s+(?:mails?|messagerie|agenda|compte|outils?)|professionnel\w*|"
    r"(?:mails?|messagerie|agenda|compte|outils?|dossiers?|drive|slack|teams)\s+(?:du|de|au)\s+(?:travail|boulot|bureau)|"
    r"boulot|employeur|intranet|entreprise|travail|coll[eè]gues?)\b",
    re.IGNORECASE,
)
MOTS_PAIEMENT = re.compile(
    r"\b(pa(?:y|i)e[rz]?|paiements?|r[eé]gler|acheter|achats?|commander|commandes?|carte\s+bancaire|virements?|"
    r"abonne(?:r|ment))\b",
    re.IGNORECASE,
)
RE_ACTION_EFFET = re.compile(r"(reserv|annul|pay|paie|command|achat|achet|envoy|supprim|modifi|valid|inscri|resili)")
RE_ACTION_PAIEMENT = re.compile(r"(pay|paie|achat|achet|command|regl)")
RE_LITTERAL_SECRET = re.compile(
    r"(mot_de_passe|password|passwd|mdp|token|jeton|api_?key|cle_api|client_secret)\w*[\"']?\s*[=:]\s*[\"'][^\"'\s{}]{4,}[\"']",
    re.IGNORECASE,
)
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
CHEMINS_INTERDITS = ("/etc/", "/var/lib/homeos", "/srv/home-os", "/root/", "/home/", "/proc/", ".ssh", "coffre.json")

# Imports permis au code d'une capacité (hors tests) : la navigation passe par tools.navigation, rien d'autre du
# système n'est touché directement (pas de coffre, de base, de mémoire, de réseau brut ni de processus).
IMPORTS_PERMIS = frozenset({
    "__future__", "typing", "re", "json", "datetime", "dataclasses", "collections", "unicodedata", "html", "math",
    "itertools", "functools", "bs4", "urllib.parse", "zoneinfo", "decimal", "enum", "contextlib", "time", "logging",
    "tools.navigation", "string", "textwrap",
})
# Un fournisseur de transports importe en plus l'interface commune de la capacité transports (et rien d'autre d'elle).
IMPORTS_PERMIS_FOURNISSEUR = IMPORTS_PERMIS | {"capabilities.transports.interface"}
IMPORTS_INTERDITS_TESTS = frozenset({"subprocess", "socket", "ctypes", "requests", "httpx", "urllib.request",
                                     "http.client", "aiohttp", "pickle", "marshal", "shutil", "tools.coffre",
                                     "tools.identifiants", "tools.db", "tools.memoire"})
APPELS_INTERDITS = frozenset({"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "breakpoint",
                              "getattr", "setattr", "delattr"})
ATTRIBUTS_INTERDITS = frozenset({"__subclasses__", "__globals__", "__builtins__", "__code__", "__dict__", "__bases__",
                                 "__mro__", "__getattribute__", "__closure__", "__reduce__", "__loader__", "__spec__"})


class ErreurAtelier(capacites.ErreurCapacite):
    """Demande refusée ou fabrication impossible (message simple pour la personne)."""


class EchecFabrication(ErreurAtelier):
    """Échec d'une étape ; `detail` va dans les apprentissages de l'atelier, jamais dans la conversation."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


# --- chemins -----------------------------------------------------------------------------------------------------


def racine() -> Path:
    return etat_dir() / "atelier"


def _dossier(chemin: Path, mode: int = 0o700) -> Path:
    if not chemin.exists():
        chemin.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(chemin, mode)
    return chemin


def dossier_travail(nom: str) -> Path:
    """cwd du sous-agent : `<etat>/atelier/<nom>` (la capacité y est écrite directement)."""
    return racine() / nom


def dossier_suivi(nom: str) -> Path:
    return _dossier(racine() / "_suivi" / nom)


def dossier_file() -> Path:
    return _dossier(racine() / "_file")


def dossier_resultats() -> Path:
    return _dossier(racine() / "_resultats")


def dossier_relecture(nom: str) -> Path:
    """Copie figée relue puis installée : hors de l'arborescence où tourne le code généré."""
    return etat_dir() / "atelier-relecture" / nom


# --- demande -----------------------------------------------------------------------------------------------------


@dataclass
class Demande:
    nom: str
    titre: str
    demande: str
    site: str
    hote: str
    compte: str  # moi | foyer
    action: str
    demandeur: str
    prenom: str
    paiement: bool = False
    cree: str = field(default_factory=iso_now)
    fabrication: str = "capacite"   # capacite | fournisseur_transports
    modes: list[str] = field(default_factory=list)
    zone: str = ""

    @property
    def fournisseur(self) -> bool:
        return self.fabrication == "fournisseur_transports"

    @classmethod
    def depuis(cls, brut: dict[str, Any]) -> Demande:
        champs = {k: brut[k] for k in cls.__dataclass_fields__ if k in brut}
        return cls(**champs)


def _slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", t)).strip("_")


def normaliser_site(site: str) -> tuple[str, str]:
    """URL du site (https ajouté si absent) et hôte ; http seulement pour 127.0.0.1/localhost (démonstration)."""
    from tools.navigation import HOTES_LOCAUX

    brut = str(site or "").strip()
    if not brut:
        raise ErreurAtelier("Quel est le site à utiliser (son adresse) ?")
    if not re.match(r"^[a-z]+://", brut, re.IGNORECASE):
        brut = "https://" + brut
    parties = urlsplit(brut)
    hote = (parties.hostname or "").lower().rstrip(".")
    if parties.scheme not in ("http", "https") or not capacites.RE_HOTE.match(hote):
        raise ErreurAtelier("Je ne reconnais pas cette adresse de site : peux-tu me la redonner ?")
    if parties.scheme == "http" and hote not in HOTES_LOCAUX:
        brut = "https://" + brut.split("://", 1)[1]
    url = f"{urlsplit(brut).scheme}://{parties.netloc.lower()}{parties.path.rstrip('/')}"
    return url, hote


def controler_texte(*textes: str) -> bool:
    """Lève ActionRefusee si la demande vise un espace professionnel ; renvoie True si elle parle de paiement."""
    texte = " ".join(t for t in textes if t)
    if MOTS_PRO.search(texte):
        raise capacites.ActionRefusee(
            "Ça, je ne le fais pas : l'atelier ne touche jamais à l'espace professionnel de quelqu'un du foyer. "
            "Je peux fabriquer une capacité pour un service de la maison."
        )
    return bool(MOTS_PAIEMENT.search(texte))


def _en_cours() -> dict[str, dict[str, Any]]:
    suivis = racine() / "_suivi"
    resultat: dict[str, dict[str, Any]] = {}
    if suivis.is_dir():
        for d in suivis.iterdir():
            etat = lire_suivi(d.name)
            if etat.get("statut") == "en_cours":
                resultat[d.name] = etat
    return resultat


def preparer(
    demande: str,
    site: str,
    compte: str,
    action: str,
    titre: str = "",
    personne: str | None = None,
    config: dict[str, Any] | None = None,
    fabrication: str = "capacite",
    modes: Any = None,
    zone: str = "",
) -> tuple[str, dict[str, Any]]:
    """Vérifie une demande d'atelier et renvoie (reformulation, payload) pour le bouton Valider."""
    from tools import administration

    config = config if config is not None else load_config()
    prenom = administration.verifier_proprietaire(personne, config)
    fabrication = _slug(fabrication or "capacite") or "capacite"
    if fabrication in ("fournisseur", "reseau", "transports", "reseau_de_transports"):
        fabrication = "fournisseur_transports"
    if fabrication not in FABRICATIONS:
        raise ErreurAtelier("L'atelier fabrique une capacité ou un réseau de transports.")
    if fabrication == "fournisseur_transports":
        return preparer_fournisseur(demande, site, action, titre, personne, prenom, modes, zone)
    if not str(demande or "").strip():
        raise ErreurAtelier("Qu'est-ce que tu voudrais que je sache faire ?")
    if not str(action or "").strip():
        raise ErreurAtelier("Qu'est-ce que tu attends exactement sur ce site (voir des créneaux, réserver…) ?")
    compte_n = _slug(compte)
    if compte_n in ("moi", "perso", "personnel", "mon_compte", "le_mien", "a_moi"):
        compte_n = "moi"
    elif compte_n in ("foyer", "famille", "commun", "partage", "maison", "le_notre"):
        compte_n = "foyer"
    else:
        raise ErreurAtelier("C'est ton compte à toi, ou un compte commun du foyer ?")
    url, hote = normaliser_site(site)
    paiement = controler_texte(demande, action, titre, site)
    titre = (titre or "").strip() or hote.removeprefix("www.").split(".")[0].capitalize()
    nom = _slug(titre)[:NOM_MAX].strip("_")
    if not nom or not capacites.RE_NOM.fullmatch(nom) or nom in NOMS_RESERVES or nom[0].isdigit():
        raise ErreurAtelier(f"Je ne peux pas appeler cette capacité « {titre} » : propose-moi un autre nom.")
    try:
        existante: capacites.Capacite | None = capacites.trouver(nom)
    except capacites.ErreurCapacite:
        existante = None
    if existante is not None:
        raise ErreurAtelier(f"« {existante.titre} » existe déjà : je peux la régler, pas la refaire.")
    if nom in _en_cours():
        raise ErreurAtelier(f"L'atelier travaille déjà sur « {titre} » : je te préviens dès que c'est prêt.")
    d = Demande(nom=nom, titre=titre, demande=" ".join(str(demande).split())[:600], site=url, hote=hote,
                compte=compte_n, action=" ".join(str(action).split())[:300], demandeur=str(personne), prenom=prenom,
                paiement=paiement)
    return _resume_capacite(d, prenom, compte_n, titre, hote, paiement)


def _resume_capacite(d: Demande, prenom: str, compte_n: str, titre: str, hote: str,
                     paiement: bool) -> tuple[str, dict[str, Any]]:
    de = "d'" if prenom[:1].casefold() in "aeiouyéèêàâîôûh" else "de "
    compte_txt = f"compte {de}{prenom}" if compte_n == "moi" else "compte du foyer"
    resume = (
        f"Fabriquer la capacité « {titre} » : {d.action} sur {hote} ({compte_txt}). {titre} parlera à {hote}. "
        "Elle restera en essai jusqu'à sa première action validée à la main."
    )
    if paiement:
        resume += " Tout paiement attendra toujours ton bouton Valider."
    return resume, {"action": "atelier_creer", **asdict(d)}


def _transports() -> Any:
    """Modules de la capacité transports (fournisseurs, interface) ; ErreurAtelier si elle est absente."""
    try:
        from capabilities.transports import fournisseurs, interface
    except ImportError as exc:
        raise ErreurAtelier("La capacité Transports n'est pas installée : je ne peux pas y ajouter un réseau.") from exc
    return fournisseurs, interface


def preparer_fournisseur(demande: str, site: str, action: str, titre: str, personne: str | None, prenom: str,
                         modes: Any = None, zone: str = "") -> tuple[str, dict[str, Any]]:
    """« Ajoute le métro de Lyon » : un fournisseur de la capacité transports (lecture seule, compte inutile)."""
    fr, interface = _transports()
    if not str(demande or "").strip():
        raise ErreurAtelier("Quel réseau veux-tu que j'ajoute ?")
    url, hote = normaliser_site(site)
    controler_texte(demande, action, titre, site)
    liste_modes = []
    for m in (modes if isinstance(modes, (list, tuple)) else str(modes or "").replace(",", " ").split()):
        try:
            from capabilities.transports import tools as outils_transports

            liste_modes.append(outils_transports.lire_mode(m))
        except interface.ErreurTransports as exc:
            raise ErreurAtelier(str(exc)) from None
    if not liste_modes:
        raise ErreurAtelier("C'est quel type de transport : bus, métro, tram, train… ?")
    titre = (titre or "").strip() or hote.removeprefix("www.").split(".")[0].capitalize()
    nom = _slug(titre)[:NOM_MAX].strip("_")
    if not nom or not fr.RE_NOM.fullmatch(nom) or nom in NOMS_RESERVES:
        raise ErreurAtelier(f"Je ne peux pas appeler ce réseau « {titre} » : propose-moi un autre nom.")
    if any(f.nom == nom for f in fr.decouvrir(data_dir(), avec_modeles=True)):
        raise ErreurAtelier(f"Le réseau « {titre} » existe déjà : dis-moi plutôt ton trajet.")
    if nom in _en_cours():
        raise ErreurAtelier(f"L'atelier travaille déjà sur « {titre} » : je te préviens dès que c'est prêt.")
    action = " ".join(str(action or "lire les horaires, les retards et les perturbations").split())[:300]
    d = Demande(nom=nom, titre=titre, demande=" ".join(str(demande).split())[:600], site=url, hote=hote,
                compte="foyer", action=action, demandeur=str(personne), prenom=prenom,
                fabrication="fournisseur_transports", modes=sorted(set(liste_modes)), zone=" ".join(str(zone).split())[:80])
    lisibles = ", ".join(sorted(set(liste_modes)))
    resume = (f"Ajouter le réseau « {titre} » ({lisibles}) aux transports : {action} sur {hote}. {titre} parlera à "
              f"{hote}, en lecture seule. Ensuite, je pourrai noter vos trajets sur ce réseau.")
    return resume, {"action": "atelier_creer", **asdict(d)}


# --- suivi (progression lisible) -----------------------------------------------------------------------------------


def lire_suivi(nom: str) -> dict[str, Any]:
    try:
        etat = json.loads((racine() / "_suivi" / nom / "etat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return etat if isinstance(etat, dict) else {}


def ecrire_suivi(cle: str, **champs: Any) -> dict[str, Any]:
    etat = lire_suivi(cle)
    etat.update(champs)
    etat["maj"] = iso_now()
    if "etape" in champs:
        etat["libelle"] = ETAPES.get(str(champs["etape"]), str(champs["etape"]))
        etat.setdefault("historique", []).append({"etape": champs["etape"], "quand": etat["maj"]})
    chemin = dossier_suivi(cle) / "etat.json"
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, chemin)
    return etat


def apprendre(nom: str, titre: str, texte: str) -> None:
    """Ce que l'atelier garde d'un essai (erreurs de contrôle, incidents, sorties) : relu au prochain passage."""
    if not texte.strip():
        return
    with (dossier_suivi(nom) / "apprentissages.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {iso_now()} — {titre}\n\n{capacites.masquer_secrets(texte.strip())[:20_000]}\n")


def etat_public(nom: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    """Progression sans détail technique (outil atelier_etat)."""

    def propre(e: dict[str, Any]) -> dict[str, Any]:
        cles = ("nom", "titre", "statut", "etape", "libelle", "message", "debut", "maj", "fin", "reseau_en_attente")
        return {k: e[k] for k in cles if k in e}

    if nom:
        etat = lire_suivi(_slug(nom))
        if not etat:
            raise ErreurAtelier(f"Je n'ai pas de travail d'atelier pour « {nom} ».")
        return propre(etat)
    suivis = racine() / "_suivi"
    tous = [propre(lire_suivi(d.name)) for d in sorted(suivis.iterdir())] if suivis.is_dir() else []
    return [e for e in tous if e]


# --- lancement -------------------------------------------------------------------------------------------------------


def executer_creation(payload: dict[str, Any], donnees: Path | None = None) -> str:
    """Après Valider : approuve le site, met la demande en file et lance l'atelier en arrière-plan."""
    d = Demande.depuis(payload)
    if not capacites.RE_NOM.fullmatch(d.nom):
        raise ErreurAtelier("Demande d'atelier invalide.")
    if d.nom in _en_cours():
        return f"L'atelier travaille déjà sur « {d.titre} »."
    (dossier_suivi(d.nom) / "demande.json").write_text(json.dumps(asdict(d), ensure_ascii=False, indent=1),
                                                       encoding="utf-8")
    ecrire_suivi(d.nom, nom=d.nom, titre=d.titre, statut="en_cours", etape="en_attente", debut=iso_now(), fin=None,
                 demandeur=d.demandeur, message="")
    # Le site approuvé entre dans la liste blanche du proxy (homeos-appliquer-config suit etat.json).
    with capacites._modifier_etat(donnees) as etat:
        if d.fournisseur:  # réseau de transports : hôte approuvé pour la capacité transports
            entree = etat.setdefault("transports", {})
            entree.setdefault("fournisseurs_atelier", {})[d.nom] = {"en_cours": True, "demande_le": d.cree,
                                                                     "par": d.demandeur, "hote": d.hote}
        else:
            entree = etat.setdefault(d.nom, {})
            entree["atelier"] = {"en_cours": True, "demande_le": d.cree, "par": d.demandeur}
        entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | {d.hote})
    fichier = dossier_file() / f"{d.nom}.json"
    fichier.write_text(json.dumps(asdict(d), ensure_ascii=False), encoding="utf-8")
    lancer()
    if d.fournisseur:
        return (f"C'est parti : je prépare le réseau « {d.titre} » en arrière-plan. Je te préviens dès qu'il est prêt "
                "(en général quelques minutes).")
    return (f"C'est parti : je fabrique « {d.titre} » en arrière-plan. Je te préviens dès qu'elle est prête "
            "(en général quelques minutes).")


def lancer() -> None:
    """systemd (homeos-atelier.path surveille la file) ; sinon processus détaché ; `synchrone` pour les tests."""
    mode = os.environ.get("HOMEOS_ATELIER_LANCEMENT", "").strip().lower()
    if mode == "synchrone":
        traiter()
        return
    if mode == "systemd" or (not mode and UNITE_PATH.exists()):
        return
    subprocess.Popen(
        [sys.executable, "-m", "tools.atelier", "traiter"],
        cwd=str(root_dir()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@contextlib.contextmanager
def _verrou(nom: str) -> Iterator[None]:
    import fcntl

    with open(_dossier(racine()) / f".{nom}.lock", "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def traiter() -> int:
    """Fabrique puis conclut tout ce qui attend (mode sans systemd et tests)."""
    fabriquer_file()
    return conclure_tout()


# --- sous-agent ------------------------------------------------------------------------------------------------------


def commande_sous_agent(mode: str, nom: str, prompt: str, cwd: Path) -> list[str]:
    """Commande du sous-agent Claude Code. En production : le wrapper root `homeos-claude --homeos-atelier`, qui
    impose outils, prompt système, réglages, cwd, budget et clé API. Sans wrapper (développement, tests) :
    `HOMEOS_ATELIER_CLAUDE` (ou `claude`) avec les mêmes options."""
    if mode not in MODES:
        raise ValueError(mode)
    outils, systeme, tours = MODES[mode]
    binaire = os.environ.get("HOMEOS_ATELIER_CLAUDE", "").strip()
    if not binaire and os.access(WRAPPER, os.X_OK):
        return [WRAPPER, "--homeos-atelier", nom, "--homeos-atelier-mode", mode, "-p", prompt,
                "--output-format", "json"]
    binaire = binaire or shutil.which("claude") or "claude"
    return [binaire, "-p", prompt, "--output-format", "json", "--allowedTools", outils, "--permission-mode", "dontAsk",
            "--append-system-prompt-file", str(content_dir() / "atelier" / systeme), "--max-turns", tours,
            "--add-dir", str(cwd)]


def lancer_sous_agent(mode: str, nom: str, prompt: str, cwd: Path, delai_s: int) -> dict[str, Any]:
    """Lance le sous-agent ; renvoie le JSON de résultat de claude (`result`, `is_error`, coût…)."""
    cmd = commande_sous_agent(mode, nom, prompt, cwd)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM", "COFFRE", "HOMEOS_JETON"))}
    env.update({"HOMEOS_ATELIER_MODE": mode, "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "PYTHONPATH": str(root_dir()), "PYTHONSAFEPATH": "1"})
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=delai_s, check=False)
    except subprocess.TimeoutExpired:
        raise EchecFabrication("L'atelier a mis trop de temps.", f"sous-agent {mode} : délai de {delai_s} s dépassé") from None
    except OSError as exc:
        raise EchecFabrication("L'atelier n'a pas pu démarrer.", f"sous-agent {mode} : {exc}") from None
    sortie = proc.stdout.strip()
    resultat: dict[str, Any] | None = None
    for morceau in [sortie, *reversed(sortie.splitlines())]:
        with contextlib.suppress(ValueError):
            obj = json.loads(morceau)
            if isinstance(obj, dict) and ("result" in obj or obj.get("type") == "result"):
                resultat = obj
                break
    if proc.returncode != 0 or resultat is None or resultat.get("is_error"):
        detail = f"sous-agent {mode} : code {proc.returncode}\n{proc.stderr[-2000:]}\n{sortie[-2000:]}"
        raise EchecFabrication("L'atelier n'a pas pu écrire la capacité.", detail)
    return resultat


def prompt_generation(d: Demande, corrections: str = "") -> str:
    if corrections:
        return ("Lis CORRECTIONS.md : les contrôles de l'atelier ont refusé la capacité de ce dossier. Corrige chaque "
                "point sans rien casser d'autre, relance les tests, puis termine par un résumé d'une ligne.")
    return ("Lis DEMANDE.md puis le dossier exploration/, et fabrique dans ce dossier la capacité « "
            f"{d.titre} » (nom {d.nom}) en respectant le contrat de ton prompt système. Lance ses tests avec "
            "`python -m pytest -q tests` et `ruff check .`. Termine par un résumé d'une ligne.")


PROMPT_RELECTURE = (
    "Relis la capacité de ce dossier avec la grille de ton prompt système. Lis chaque fichier. Réponds uniquement par "
    'un objet JSON : {"verdict": "ok" | "refus", "problemes": ["…"], "resume": "une phrase"}.'
)


def ecrire_demande_md(d: Demande, dossier: Path, exploration: dict[str, Any] | None) -> None:
    compte = ("compte personnel de la personne qui parle : secret `par_personne: true`" if d.compte == "moi"
              else "compte commun du foyer : secret sans `par_personne`")
    if d.fournisseur:
        ecrire_demande_fournisseur(d, dossier, exploration)
        return
    lignes = [
        f"# Demande d'atelier : « {d.titre} »",
        "",
        f"- Demande (mots du foyer) : {d.demande}",
        f"- Action attendue : {d.action}",
        f"- Nom technique et préfixe des outils : `{d.nom}` (outils `{d.nom}_…`, dossier = ce dossier)",
        f"- Titre : {d.titre}",
        f"- Site : {d.site} — hôte approuvé par le foyer : `{d.hote}` (tout autre hôte devra être approuvé)",
        f"- Compte : {compte}",
        f"- Paiement évoqué : {'oui — action de paiement en confirmation_toujours, carte virtuelle seulement' if d.paiement else 'non'}",
        "- Paramètre obligatoire `site` (texte) : l'adresse ci-dessus y sera enregistrée à l'installation.",
        "",
        "## Exploration (pages publiques, déjà faite par l'atelier)",
        "",
    ]
    if exploration:
        lignes += [f"- Titre de la page : {exploration.get('titre', '')}",
                   f"- Hôtes tiers bloqués pendant la visite : {', '.join(exploration.get('hotes_bloques') or []) or 'aucun'}",
                   f"- Conditions d'utilisation lues : {', '.join(exploration.get('cgu') or []) or 'aucune page trouvée (cherche-les)'}",
                   ("- Fichiers : exploration/page.html, aria.txt, liens.json, cgu-<n>.txt (conditions d'utilisation), "
                    "reseau.json, robots.txt, capture.png")]
    else:
        lignes.append("- (pas d'exploration disponible)")
    apprentissages = dossier_suivi(d.nom) / "apprentissages.md"
    if apprentissages.exists():
        lignes += ["", "## Ce que l'atelier a appris lors d'un essai précédent", "",
                   apprentissages.read_text(encoding="utf-8")[-6000:]]
    (dossier / "DEMANDE.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")


def gabarit_fournisseur() -> Path:
    return content_dir() / "atelier" / "gabarits" / "fournisseur_transports"


def semer_gabarit(d: Demande, dossier: Path) -> None:
    """Amorce le dossier de travail avec le gabarit de fournisseur (champs {{…}} remplis)."""
    valeurs = {"{{nom}}": d.nom, "{{titre}}": d.titre, "{{modes}}": "[" + ", ".join(d.modes) + "]",
               "{{hote}}": d.hote, "{{site}}": d.site, "{{zone}}": d.zone or "à préciser", "{{demande}}": d.demande}
    source = gabarit_fournisseur()
    for f in sorted(source.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        cible = dossier / f.relative_to(source)
        cible.parent.mkdir(parents=True, exist_ok=True)
        texte = f.read_text(encoding="utf-8")
        for cle, valeur in valeurs.items():
            texte = texte.replace(cle, valeur)
        cible.write_text(texte, encoding="utf-8")


def ecrire_demande_fournisseur(d: Demande, dossier: Path, exploration: dict[str, Any] | None) -> None:
    lignes = [
        f"# Demande d'atelier : réseau de transports « {d.titre} »",
        "",
        ("**Type de fabrication : fournisseur de transports** (section « Second type : un fournisseur de transports » "
         "de ton prompt système) — pas une capacité entière. Le gabarit est déjà dans ce dossier : adapte-le."),
        "",
        f"- Demande (mots du foyer) : {d.demande}",
        f"- Ce qui est attendu : {d.action}",
        f"- Nom technique : `{d.nom}` (dossier = ce dossier) ; titre : {d.titre}",
        f"- Modes : {', '.join(d.modes)} ; zone : {d.zone or 'à préciser'}",
        f"- Site : {d.site} — hôte approuvé par le foyer : `{d.hote}` (tout autre hôte devra être approuvé)",
        "- Lecture seule : aucune action avec effet, aucun identifiant, aucune clé d'API.",
        "",
        "## Exploration (pages publiques, déjà faite par l'atelier)",
        "",
    ]
    if exploration:
        lignes += [f"- Titre de la page : {exploration.get('titre', '')}",
                   f"- Hôtes tiers bloqués pendant la visite : {', '.join(exploration.get('hotes_bloques') or []) or 'aucun'}",
                   f"- Conditions d'utilisation lues : {', '.join(exploration.get('cgu') or []) or 'aucune page trouvée (cherche-les)'}",
                   "- Fichiers : exploration/page.html, aria.txt, liens.json, cgu-<n>.txt, reseau.json, robots.txt, capture.png"]
    else:
        lignes.append("- (pas d'exploration disponible)")
    apprentissages = dossier_suivi(d.nom) / "apprentissages.md"
    if apprentissages.exists():
        lignes += ["", "## Ce que l'atelier a appris lors d'un essai précédent", "",
                   apprentissages.read_text(encoding="utf-8")[-6000:]]
    (dossier / "DEMANDE.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")


# --- contrôles ---------------------------------------------------------------------------------------------------


def fichiers_candidats(dossier: Path, racine: tuple[str, ...] = FICHIERS_RACINE) -> tuple[list[str], list[str]]:
    """(fichiers autorisés relatifs, erreurs) : liens refusés, taille plafonnée, le reste ignoré."""
    retenus: list[str] = []
    erreurs: list[str] = []
    for f in sorted(dossier.rglob("*")):
        rel = f.relative_to(dossier).as_posix()
        if any(part in IGNORES for part in Path(rel).parts):
            continue
        if f.is_symlink():
            erreurs.append(f"lien symbolique refusé : {rel}")
            continue
        if not f.is_file():
            continue
        if rel in racine or RE_FICHIER_TEST.fullmatch(rel):
            if f.stat().st_size > TAILLE_MAX_FICHIER:
                erreurs.append(f"fichier trop gros : {rel}")
                continue
            retenus.append(rel)
    return retenus, erreurs


def _module_import(noeud: ast.AST) -> list[str]:
    if isinstance(noeud, ast.Import):
        return [a.name for a in noeud.names]
    if isinstance(noeud, ast.ImportFrom):
        if noeud.level:  # import relatif (connecteur, tools du même paquet)
            return []
        base = noeud.module or ""
        if base in ("tools", "capabilities.transports"):
            return [f"{base}.{a.name}" for a in noeud.names]
        return [base]
    return []


def _permis(module: str, permis: frozenset[str]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in permis)


def controler_code(rel: str, source: str, permis: frozenset[str] = IMPORTS_PERMIS) -> list[str]:
    """Contrôles statiques d'un fichier Python de la capacité (imports, appels, chemins, secrets en dur)."""
    erreurs: list[str] = []
    try:
        arbre = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [f"{rel} : erreur de syntaxe ligne {exc.lineno}"]
    test = rel.startswith("tests/")
    docstrings = {id(n.value) for n in ast.walk(arbre) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    for noeud in ast.walk(arbre):
        for module in _module_import(noeud):
            if test:
                if _permis(module, IMPORTS_INTERDITS_TESTS):
                    erreurs.append(f"{rel} : import interdit « {module} »")
            elif not _permis(module, permis):
                erreurs.append(f"{rel} : import interdit « {module} » (navigation : tools.navigation seulement)")
        if isinstance(noeud, ast.Call):
            f = noeud.func
            nom = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if isinstance(f, ast.Name) and nom in APPELS_INTERDITS:
                erreurs.append(f"{rel} : appel interdit « {nom} »")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os":
                erreurs.append(f"{rel} : accès direct au système « os.{f.attr} »")
            if not test and nom == "open":
                erreurs.append(f"{rel} : pas de fichier ouvert directement (données : ctx.dossier, ctx.memoire_*)")
        if isinstance(noeud, ast.Attribute) and noeud.attr in ATTRIBUTS_INTERDITS:
            erreurs.append(f"{rel} : attribut spécial « {noeud.attr} » refusé")
        if not test and isinstance(noeud, ast.Constant) and isinstance(noeud.value, str) and id(noeud) not in docstrings:
            if any(c in noeud.value for c in CHEMINS_INTERDITS):
                erreurs.append(f"{rel} : chemin système en dur refusé")
            if RE_EMAIL.search(noeud.value):
                erreurs.append(f"{rel} : adresse e-mail en dur (identifiants : coffre ; réglages : parametres)")
    if not test and RE_LITTERAL_SECRET.search(source):
        erreurs.append(f"{rel} : valeur qui ressemble à un secret écrite en dur (les secrets viennent du coffre)")
    return sorted(set(erreurs))


def _outils_declares(source: str) -> dict[str, ast.FunctionDef]:
    """Fonctions décorées `@mcp.tool(...)` dans tools.py."""
    outils: dict[str, ast.FunctionDef] = {}
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, ast.FunctionDef):
            for deco in noeud.decorator_list:
                cible = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(cible, ast.Attribute) and cible.attr == "tool":
                    outils[noeud.name] = noeud
    return outils


def verifier_candidat(dossier: Path, d: Demande) -> tuple[capacites.Manifeste | None, list[str], list[str]]:
    """Contrôles déterministes d'une capacité candidate (aucun code exécuté). Renvoie (manifeste, erreurs,
    hôtes du manifeste encore à approuver)."""
    retenus, erreurs = fichiers_candidats(dossier)
    for requis in ("capability.yaml", "tools.py", "connecteur.py", "smoke.py", "CLAUDE.md", "README.md"):
        if requis not in retenus:
            erreurs.append(f"fichier manquant : {requis}")
    if not any(r.startswith("tests/test_") for r in retenus):
        erreurs.append("aucun test (tests/test_<nom>.py)")
    if erreurs:
        return None, erreurs, []
    if dossier.name != d.nom:
        erreurs.append(f"dossier « {dossier.name} » ≠ nom « {d.nom} »")
    cap = capacites.lire_capacite(dossier)
    if cap.manifeste is None:
        return None, erreurs + [f"manifeste : {e}" for e in cap.erreurs], []
    m = cap.manifeste
    erreurs += [f"manifeste : {a}" for a in cap.avertissements if "clé inconnue" in a or "type" in a]
    if m.nom != d.nom or m.prefixe != d.nom:
        erreurs.append(f"le nom et le préfixe doivent valoir « {d.nom} »")
    if m.origine != "assistant" or m.statut != "essai":
        erreurs.append("une capacité de l'atelier est `origine: assistant` et `statut: essai`")
    if m.personnes:
        erreurs.append("`personnes: []` (le foyer règle l'accès ensuite)")
    if controler_texte_sans_lever(m.titre, m.description):
        erreurs.append("la capacité vise un espace professionnel : refusé")
    # Actions avec effet : toutes en confirmation_toujours (règle de l'atelier) ; paiement explicitement.
    if set(m.confirmation_toujours) != set(m.actions_avec_effet):
        for a in sorted(set(m.actions_avec_effet) - set(m.confirmation_toujours)):
            if RE_ACTION_PAIEMENT.search(a) or any(s.type == "carte_bancaire" for s in m.secrets):
                erreurs.append(f"paiement sans confirmation_toujours : « {a} » doit toujours attendre le bouton Valider")
            else:
                erreurs.append(f"action avec effet « {a} » absente de confirmation_toujours")
    if (d.paiement or any(s.type == "carte_bancaire" for s in m.secrets)) and any(
        RE_ACTION_PAIEMENT.search(a) for a in m.actions_avec_effet
    ) and not m.confirmation_toujours:
        erreurs.append("paiement sans confirmation_toujours")
    source_tools = (dossier / "tools.py").read_text(encoding="utf-8")
    try:
        outils = _outils_declares(source_tools)
    except SyntaxError:
        outils = {}
    for nom_outil, fn in outils.items():
        if not nom_outil.startswith(f"{d.nom}_"):
            erreurs.append(f"outil « {nom_outil} » sans le préfixe {d.nom}_")
        if RE_ACTION_EFFET.search(nom_outil.removeprefix(d.nom + "_")) and nom_outil not in m.actions_avec_effet:
            erreurs.append(f"« {nom_outil} » ressemble à une action avec effet : la déclarer (et la confirmer toujours)")
        if nom_outil in m.actions_avec_effet:
            appels = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            if "demander_confirmation" not in appels:
                erreurs.append(f"« {nom_outil} » doit seulement préparer la validation (ctx.demander_confirmation)")
    if m.actions_avec_effet and not re.search(r"^def executer\(", source_tools, re.MULTILINE):
        erreurs.append("tools.py doit définir executer(action, payload, ctx) pour les actions validées")
    if not any(p.cle == "site" and p.obligatoire for p in m.parametres):
        erreurs.append("paramètre obligatoire `site` (adresse du site) manquant")
    for s in m.secrets:
        if s.base != d.nom:
            erreurs.append(f"secret « {s.cle} » : la clé commence par « {d.nom} »")
        if d.compte == "moi" and not s.par_personne and s.type == "identifiants":
            erreurs.append(f"secret « {s.cle} » : compte personnel → `par_personne: true`")
    if m.crons and not any("seuil" in p.cle for p in m.parametres):
        erreurs.append("une routine déclare son seuil « ça vaut le coup » dans parametres (cle seuil_…)")
    if not m.reseau:
        erreurs.append("`reseau` vide : déclarer l'hôte du site")
    readme = (dossier / "README.md").read_text(encoding="utf-8").casefold()
    if "robots" not in readme or not re.search(r"cgu|conditions", readme):
        erreurs.append("README.md : noter ce que disent robots.txt et les conditions d'utilisation du site")
    for rel in retenus:
        if rel.endswith(".py"):
            erreurs += controler_code(rel, (dossier / rel).read_text(encoding="utf-8"))
    en_plus = sorted({h.strip().lower() for h in m.reseau} - {d.hote})
    return (m if not erreurs else None), sorted(set(erreurs)), en_plus


def controler_texte_sans_lever(*textes: str) -> bool:
    return bool(MOTS_PRO.search(" ".join(textes)))


def verifier_fournisseur(dossier: Path, d: Demande) -> tuple[Any, list[str], list[str]]:
    """Contrôles déterministes d'un fournisseur de transports candidat (aucun code exécuté). Renvoie (manifeste,
    erreurs, hôtes encore à approuver)."""
    fr, _interface = _transports()
    retenus, erreurs = fichiers_candidats(dossier, FICHIERS_FOURNISSEUR)
    for requis in ("fournisseur.yaml", "source.py", "README.md"):
        if requis not in retenus:
            erreurs.append(f"fichier manquant : {requis}")
    if not any(r.startswith("tests/test_") for r in retenus):
        erreurs.append("aucun test (tests/test_<nom>.py)")
    if erreurs:
        return None, erreurs, []
    if dossier.name != d.nom:
        erreurs.append(f"dossier « {dossier.name} » ≠ nom « {d.nom} »")
    f = fr.lire_manifeste(dossier, "atelier")
    erreurs += [f"fournisseur.yaml : {e}" for e in f.erreurs]
    if f.nom != d.nom:
        erreurs.append(f"le nom doit valoir « {d.nom} »")
    if f.source not in ("api_ouverte", "page_web"):
        erreurs.append("`source` : api_ouverte ou page_web (un réseau sans source n'a pas besoin de l'atelier)")
    if f.secret is not None:
        erreurs.append("un fournisseur de l'atelier n'utilise ni identifiant ni clé d'API (données publiques seulement)")
    if d.hote not in f.reseau and not (f.provenance == "atelier" and f.parametres.get("url_grille")):
        erreurs.append(f"`reseau` : déclarer l'hôte approuvé « {d.hote} »")
    if d.modes and not set(f.modes) & set(d.modes):
        erreurs.append(f"`modes` : au moins un des modes demandés ({', '.join(d.modes)})")
    if controler_texte_sans_lever(f.titre, f.description):
        erreurs.append("le fournisseur vise un espace professionnel : refusé")
    source = (dossier / "source.py").read_text(encoding="utf-8")
    try:
        classes = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef) and n.name == "Source"]
    except SyntaxError:
        classes = []
    if not classes:
        erreurs.append("source.py doit définir `class Source(interface.Source)` (interface commune des transports)")
    readme = (dossier / "README.md").read_text(encoding="utf-8").casefold()
    if "robots" not in readme or not re.search(r"cgu|conditions|licence", readme):
        erreurs.append("README.md : noter ce que disent robots.txt et les conditions d'utilisation (ou la licence)")
    for rel in retenus:
        if rel.endswith(".py"):
            erreurs += controler_code(rel, (dossier / rel).read_text(encoding="utf-8"), IMPORTS_PERMIS_FOURNISSEUR)
    hotes = set(f.reseau) | set(fr.hotes_url(*[v for k, v in f.parametres.items() if k.startswith("url_")]))
    en_plus = sorted({h for h in hotes if h != d.hote and h not in fr.HOTES_LOCAUX})
    return (f if not erreurs else None), sorted(set(erreurs)), en_plus


def _ruff() -> list[str] | None:
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    with contextlib.suppress(Exception):
        subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, check=True, timeout=30)
        return [sys.executable, "-m", "ruff"]
    return None


def lancer_ruff(dossier: Path) -> list[str]:
    cmd = _ruff()
    if cmd is None:
        return ["ruff absent du serveur : installer les dépendances (tools/requirements.txt)"]
    config = root_dir() / "ruff.toml"
    args = [*cmd, "check", "--no-cache", "--quiet", *(["--config", str(config)] if config.exists() else []), str(dossier)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        return ["ruff : " + (proc.stdout or proc.stderr).strip()[-3000:]]
    return []


def _env_isole(donnees: Path) -> dict[str, str]:
    """Environnement du code généré : aucune donnée ni aucun secret du foyer, navigateur local ou isolé."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "PLAYWRIGHT_BROWSERS_PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                    "https_proxy", "http_proxy", "no_proxy", "HOMEOS_NAVIGATEUR", "HOMEOS_NAVIGATEUR_WS",
                    "HOMEOS_FICHIER_PROXY")}
    vide = donnees / "_absent"
    env.update({"PYTHONPATH": str(root_dir()), "HOMEOS_DATA": str(donnees), "HOMEOS_ROOT": str(root_dir()),
                "HOMEOS_SECRETS": str(vide), "HOMEOS_CAPABILITIES": str(vide), "PYTHONDONTWRITEBYTECODE": "1",
                # Le dossier d'une capacité contient un `tools.py` : il ne doit jamais masquer le paquet `tools`.
                "PYTHONSAFEPATH": "1",
                "HOMEOS_FICHIER_OUTILS": str(vide / "outils")})
    return env


def lancer_tests(dossier: Path) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="atelier-tests-") as tmp:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=importlib",
               "-o", "addopts=", "--rootdir", str(dossier), str(dossier / "tests")]
        try:
            proc = subprocess.run(cmd, cwd=str(dossier), env=_env_isole(Path(tmp)), capture_output=True, text=True,
                                  timeout=TIMEOUT_TESTS_S, check=False)
        except subprocess.TimeoutExpired:
            return ["tests : délai dépassé"]
    if proc.returncode != 0:
        return ["tests en échec :\n" + (proc.stdout + proc.stderr).strip()[-4000:]]
    return []


def lancer_smoke(dossier: Path, d: Demande) -> tuple[bool, str]:
    """Smoke test du candidat dans un sous-processus isolé (données temporaires, site approuvé seulement)."""
    with tempfile.TemporaryDirectory(prefix="atelier-smoke-") as tmp:
        cmd = [sys.executable, "-m", "tools.atelier", "smoke-candidat", str(dossier), "--site", d.site, "--hote", d.hote,
               *(["--fournisseur"] if d.fournisseur else [])]
        try:
            proc = subprocess.run(cmd, cwd=str(root_dir()), env=_env_isole(Path(tmp)), capture_output=True, text=True,
                                  timeout=TIMEOUT_SMOKE_S, check=False)
        except subprocess.TimeoutExpired:
            return False, "smoke : délai dépassé"
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        return bool(res.get("ok")), str(res.get("message") or "")
    except (ValueError, IndexError):
        return False, f"smoke illisible : {(proc.stdout + proc.stderr).strip()[-2000:]}"


def smoke_candidat(dossier: Path, site: str, hote: str, fournisseur: bool = False) -> dict[str, Any]:
    """(Sous-processus) smoke test d'un candidat avec ses paramètres de découverte et le site approuvé."""
    donnees = data_dir()
    if fournisseur:
        fr, _interface = _transports()
        with capacites._modifier_etat(donnees) as etat:
            etat["transports"] = {"reseau_approuve": [hote]}
        return fr.verifier_source(fr.lire_manifeste(dossier, "atelier"), donnees)
    cap = capacites.lire_capacite(dossier)
    if cap.manifeste is None:
        return {"ok": False, "message": "manifeste invalide : " + "; ".join(cap.erreurs)}
    with capacites._modifier_etat(donnees) as etat:
        etat[cap.nom] = {"reseau_approuve": [hote]}
    if any(p.cle == "site" for p in cap.manifeste.parametres):
        capacites.definir_parametre(cap.nom, "site", site, racine=dossier.parent, donnees=donnees)
    return capacites.smoke(cap, donnees, delai_s=TIMEOUT_SMOKE_S - 20)


# --- fabrication (bac à sable) ----------------------------------------------------------------------------------------


def _deposer_resultat(nom: str, **champs: Any) -> None:
    (dossier_resultats() / f"{nom}.json").write_text(json.dumps({"nom": nom, **champs}, ensure_ascii=False),
                                                       encoding="utf-8")


def explorer_site(d: Demande, travail: Path) -> dict[str, Any] | None:
    from tools import navigation

    try:
        return navigation.explorer(d.site, [d.hote], travail / "exploration", dossier=dossier_suivi(d.nom) / "navigation")
    except navigation.RobotsRefuse as exc:
        raise EchecFabrication("Le site demande aux robots de ne pas visiter ses pages : je n'insiste pas.",
                               str(exc)) from None
    except navigation.VerificationHumaine as exc:
        raise EchecFabrication("Le site demande de prouver qu'on est humain : je ne contourne pas ça.", str(exc)) from None
    except navigation.ErreurSite as exc:
        raise EchecFabrication("Je n'ai pas réussi à ouvrir le site.", str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise EchecFabrication("Je n'ai pas réussi à ouvrir le site.", f"{exc.__class__.__name__}: {exc}") from None


def fabriquer(d: Demande) -> None:
    """Exploration, sous-agent, contrôles, tests, smoke (avec un tour de correction) ; dépose le résultat."""
    travail = dossier_travail(d.nom)
    if travail.exists():
        shutil.rmtree(travail)
    _dossier(travail, 0o750)
    try:
        ecrire_suivi(d.nom, etape="exploration")
        exploration = explorer_site(d, travail)
        if d.fournisseur:
            semer_gabarit(d, travail)
        ecrire_demande_md(d, travail, exploration)
        corrections = ""
        for passe in range(1, PASSES_MAX + 1):
            ecrire_suivi(d.nom, etape="generation" if passe == 1 else "correction")
            if corrections:
                (travail / "CORRECTIONS.md").write_text(corrections, encoding="utf-8")
            sortie = lancer_sous_agent("generer", d.nom, prompt_generation(d, corrections), travail, TIMEOUT_GENERATION_S)
            ecrire_suivi(d.nom, etape="verification", cout_usd=sortie.get("total_cost_usd"))
            _m, erreurs, en_plus = verifier(travail, d)
            if not erreurs:
                erreurs += lancer_ruff(travail)
            if not erreurs:
                erreurs += lancer_tests(travail)
            if not erreurs:
                ok, message = lancer_smoke(travail, d)
                if not ok:
                    erreurs.append(f"smoke test en échec : {message}")
            if not erreurs:
                _deposer_resultat(d.nom, ok=True, reseau_en_plus=en_plus, passes=passe)
                return
            corrections = "# Corrections demandées par l'atelier\n\n" + "\n".join(f"- {e}" for e in erreurs) + "\n"
            apprendre(d.nom, f"contrôles, passe {passe}", corrections)
        raise EchecFabrication("La capacité ne passe pas les vérifications de l'atelier.", corrections)
    except EchecFabrication as exc:
        apprendre(d.nom, "échec de fabrication", exc.detail or str(exc))
        _deposer_resultat(d.nom, ok=False, message=str(exc))
    except Exception as exc:
        log.exception("atelier %s : erreur inattendue", d.nom)
        apprendre(d.nom, "erreur inattendue", f"{exc.__class__.__name__}: {exc}")
        _deposer_resultat(d.nom, ok=False, message="L'atelier a rencontré un problème.")


def fabriquer_file() -> int:
    """Traite la file (homeos-atelier.service). Renvoie le nombre de demandes traitées."""
    n = 0
    with _verrou("fabrication"):
        for fichier in sorted(dossier_file().glob("*.json")):
            try:
                d = Demande.depuis(json.loads(fichier.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                log.warning("demande d'atelier illisible : %s", fichier.name)
                fichier.unlink(missing_ok=True)
                continue
            fichier.unlink(missing_ok=True)
            fabriquer(d)
            n += 1
    return n


# --- conclusion (hors bac à sable : relecture, installation, message) -------------------------------------------------


def verifier(dossier: Path, d: Demande) -> tuple[Any, list[str], list[str]]:
    """Contrôles selon le type de fabrication (capacité ou fournisseur de transports)."""
    return verifier_fournisseur(dossier, d) if d.fournisseur else verifier_candidat(dossier, d)


def figer(source: Path, d: Demande) -> Path:
    """Copie des seuls fichiers autorisés dans `atelier-relecture/<nom>/` (le code relu = le code installé)."""
    cible = dossier_relecture(d.nom)
    if cible.exists():
        shutil.rmtree(cible)
    _dossier(cible.parent)
    retenus, erreurs = fichiers_candidats(source, FICHIERS_FOURNISSEUR if d.fournisseur else FICHIERS_RACINE)
    if erreurs:
        raise EchecFabrication("La capacité contient des fichiers refusés.", "\n".join(erreurs))
    for rel in retenus:
        (cible / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, cible / rel)
    return cible


def relire(dossier: Path, d: Demande) -> dict[str, Any]:
    sortie = lancer_sous_agent("relire", d.nom, PROMPT_RELECTURE, dossier, TIMEOUT_RELECTURE_S)
    texte = str(sortie.get("result") or "")
    verdict: dict[str, Any] | None = None
    for m in re.finditer(r"\{.*\}", texte, re.DOTALL):
        with contextlib.suppress(ValueError):
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "verdict" in obj:
                verdict = obj
    if verdict is None:
        raise EchecFabrication("La relecture de sécurité n'a pas abouti.", f"verdict illisible : {texte[-2000:]}")
    return verdict


def installer(dossier: Path, d: Demande, en_plus: list[str], donnees: Path | None = None) -> capacites.Capacite:
    """Installe la copie relue en essai dans `<etat>/capacites-atelier/<nom>/`."""
    racine_cap = capacites.racine_atelier()
    if racine_cap is None:
        raise EchecFabrication("Installation impossible.", "racine des capacités de l'atelier non définie")
    _dossier(racine_cap, 0o750)
    cible = racine_cap / d.nom
    if cible.exists():
        archives = _dossier(racine() / "retirees")
        shutil.move(str(cible), str(archives / f"{d.nom}-remplacee-{iso_now()[:19].replace(':', '')}"))
    shutil.copytree(dossier, cible)
    cap = capacites.lire_capacite(cible)
    if not cap.valide:
        shutil.rmtree(cible)
        raise EchecFabrication("La capacité installée est invalide.", "; ".join(cap.erreurs))
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.setdefault(d.nom, {})
        entree.update({"statut": "essai", "installee_le": iso_now(), "source": "atelier",
                       "atelier": {"en_cours": False, "demande_le": d.cree, "par": d.demandeur}})
        entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | {d.hote})
        entree["reseau_en_attente"] = [h for h in en_plus if h not in entree["reseau_approuve"]]
        entree.pop("validee_le", None)
    capacites.definir_parametre(d.nom, "site", d.site, donnees=donnees)
    return cap


def installer_fournisseur(dossier: Path, d: Demande, en_plus: list[str], donnees: Path | None = None) -> Any:
    """Installe la copie relue dans `<etat>/fournisseurs-atelier/<nom>/` (lecture seule, utilisable tout de suite)."""
    fr, _interface = _transports()
    racine_f = fr.racine_atelier()
    if racine_f is None:
        raise EchecFabrication("Installation impossible.", "racine des fournisseurs de l'atelier non définie")
    _dossier(racine_f, 0o750)
    cible = racine_f / d.nom
    if cible.exists():
        archives = _dossier(racine() / "retirees")
        shutil.move(str(cible), str(archives / f"{d.nom}-remplace-{iso_now()[:19].replace(':', '')}"))
    shutil.copytree(dossier, cible)
    f = fr.lire_manifeste(cible, "atelier")
    if not f.valide:
        shutil.rmtree(cible)
        raise EchecFabrication("Le réseau installé est invalide.", "; ".join(f.erreurs))
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.setdefault("transports", {})
        entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | {d.hote})
        entree.setdefault("fournisseurs_atelier", {})[d.nom] = {
            "en_cours": False, "installe_le": iso_now(), "demande_le": d.cree, "par": d.demandeur, "hote": d.hote,
            "reseau_en_attente": [h for h in en_plus if h not in entree["reseau_approuve"]]}
    return f


def message_pret_fournisseur(d: Demande, en_plus: list[str]) -> str:
    texte = f"Réseau « {d.titre} » prêt : dis-moi ton trajet (d'où, vers où, à quelle heure) et je le note."
    if en_plus:
        texte += f" Il voudrait aussi lire {', '.join(en_plus)} : dis-moi si tu es d'accord."
    return texte


def message_pret(cap: capacites.Capacite, d: Demande, en_plus: list[str]) -> str:
    m = cap.manifeste
    assert m is not None
    texte = f"Capacité « {m.titre} » prête à l'essai."
    if m.secrets:
        qui = "tes identifiants" if d.compte == "moi" else "les identifiants du foyer"
        texte += f" Il me faut {qui} {m.titre} : dis-moi « mes identifiants {m.titre} » et je t'ouvre la carte sécurisée."
    if en_plus:
        texte += f" Elle voudrait aussi parler à {', '.join(en_plus)} : dis-moi si tu es d'accord."
    texte += " Sa première action attendra ta validation."
    return texte


def conclure(nom: str, donnees: Path | None = None) -> str:
    """Résultat d'une fabrication → relecture et installation, ou échec ; prévient le demandeur. Renvoie le message."""
    fichier = dossier_resultats() / f"{nom}.json"
    resultat = json.loads(fichier.read_text(encoding="utf-8"))
    fichier.unlink(missing_ok=True)
    d = Demande.depuis(json.loads((dossier_suivi(nom) / "demande.json").read_text(encoding="utf-8")))
    try:
        if not resultat.get("ok"):
            raise EchecFabrication(str(resultat.get("message") or "La fabrication n'a pas abouti."))
        ecrire_suivi(nom, etape="relecture")
        fige = figer(dossier_travail(nom), d)
        _m, erreurs, en_plus = verifier(fige, d)
        erreurs += lancer_ruff(fige) if not erreurs else []
        if erreurs:
            raise EchecFabrication("La capacité ne passe pas les vérifications de l'atelier.", "\n".join(erreurs))
        verdict = relire(fige, d)
        (dossier_suivi(nom) / "relecture.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
        if str(verdict.get("verdict")).casefold() != "ok":
            raise EchecFabrication("La relecture de sécurité a refusé la capacité.",
                                   "\n".join(str(p) for p in verdict.get("problemes") or []))
        ecrire_suivi(nom, etape="installation")
        if d.fournisseur:
            installer_fournisseur(fige, d, en_plus, donnees)
            texte = message_pret_fournisseur(d, en_plus)
        else:
            cap = installer(fige, d, en_plus, donnees)
            texte = message_pret(cap, d, en_plus)
        ecrire_suivi(nom, statut="pret", etape="pret", message=texte, fin=iso_now(), reseau_en_attente=en_plus)
        _journaliser("atelier_pret", f"Atelier : « {d.titre} » installée en essai", d)
    except EchecFabrication as exc:
        apprendre(nom, "échec", exc.detail or str(exc))
        texte = f"Ça n'a pas marché pour « {d.titre} » : {exc} Je garde ce que j'ai appris pour l'atelier."
        ecrire_suivi(nom, statut="echec", etape="echec", message=texte, fin=iso_now())
        if d.fournisseur:
            _retirer_approbation_fournisseur(d, donnees)
        else:
            _retirer_approbation_si_absente(nom, donnees)
        _journaliser("atelier_echec", f"Atelier : « {d.titre} » non aboutie", d, "error")
    notifier(texte, d)
    return texte


def conclure_tout(donnees: Path | None = None) -> int:
    n = 0
    with _verrou("conclusion"):
        for fichier in sorted(dossier_resultats().glob("*.json")):
            try:
                conclure(fichier.stem, donnees)
            except Exception:
                log.exception("conclusion de l'atelier %s impossible", fichier.stem)
                fichier.unlink(missing_ok=True)
            n += 1
    return n


def _retirer_approbation_si_absente(nom: str, donnees: Path | None) -> None:
    """Échec sans capacité installée : le site sort de la liste blanche du proxy."""
    try:
        capacites.trouver(nom)
        installee = True
    except capacites.ErreurCapacite:
        installee = False
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.get(nom)
        if not isinstance(entree, dict):
            return
        if installee:
            (entree.get("atelier") or {})["en_cours"] = False
        else:
            etat.pop(nom, None)


def _hotes_encore_utiles(entree: dict[str, Any], sauf: str) -> set[str]:
    utiles: set[str] = set()
    for nom, f in (entree.get("fournisseurs_atelier") or {}).items():
        if nom != sauf and isinstance(f, dict) and f.get("hote"):
            utiles.add(str(f["hote"]))
    return utiles


def _retirer_approbation_fournisseur(d: Demande, donnees: Path | None) -> None:
    """Échec d'un réseau de transports non installé : son hôte sort des hôtes approuvés (s'il ne sert à rien d'autre)."""
    fr, _interface = _transports()
    installe = any(f.nom == d.nom for f in fr.decouvrir(None) if f.provenance == "atelier")
    if installe:
        return
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.get("transports")
        if not isinstance(entree, dict):
            return
        (entree.get("fournisseurs_atelier") or {}).pop(d.nom, None)
        utiles = _hotes_encore_utiles(entree, d.nom)
        utiles |= {h for r in fr.lire_instances(donnees or data_dir()) for h in fr.hotes_url(
            *[v for k, v in r.items() if str(k).startswith("url_")])}
        entree["reseau_approuve"] = [h for h in entree.get("reseau_approuve") or [] if h != d.hote or h in utiles]


def retirer_fournisseur(nom: str, donnees: Path | None = None) -> str:
    """Retire un réseau de transports fabriqué par l'atelier (archivé ; trajets conservés ; hôtes retirés)."""
    fr, _interface = _transports()
    f = next((x for x in fr.decouvrir(None) if x.provenance == "atelier" and x.nom == _slug(nom)), None)
    if f is None or f.dossier is None:
        raise ErreurAtelier(f"Je n'ai pas de réseau fabriqué par l'atelier qui s'appelle « {nom} ».")
    archives = _dossier(racine() / "retirees")
    shutil.move(str(f.dossier), str(archives / f"{f.nom}-{iso_now()[:19].replace(':', '')}"))
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.setdefault("transports", {})
        infos = (entree.get("fournisseurs_atelier") or {}).pop(f.nom, {}) or {}
        utiles = _hotes_encore_utiles(entree, f.nom)
        retires = set(f.reseau) | ({infos["hote"]} if infos.get("hote") else set())
        entree["reseau_approuve"] = [h for h in entree.get("reseau_approuve") or [] if h not in retires or h in utiles]
    return f"Réseau « {f.titre} » retiré (les trajets notés restent, sans suivi)."


def _journaliser(kind: str, resume: str, d: Demande, statut: str = "done") -> None:
    try:
        from tools import db

        db.log_action(kind, resume, {"nom": d.nom, "site": d.hote}, status=statut, acteur="atelier")
    except Exception:
        log.exception("journalisation de l'atelier impossible")


# --- notification ------------------------------------------------------------------------------------------------------


def telegram_id(linux_user: str, config: dict[str, Any] | None = None) -> str | None:
    config = config if config is not None else load_config()
    for p in config.get("personnes") or []:
        if isinstance(p, dict) and str(p.get("linux_user")) == linux_user and p.get("telegram_id"):
            return str(p["telegram_id"])
    return None


def envoyer_telegram(texte: str, chat_id: str | None) -> bool:
    """Message privé au demandeur (sinon dans le groupe) via scripts/telegram_send.py."""
    script = root_dir() / "scripts" / "telegram_send.py"
    cmd = [sys.executable, str(script), *(["--chat-id", chat_id] if chat_id else []), texte]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        log.warning("message Telegram de l'atelier non envoyé : %s", proc.stderr.strip()[-300:])
    return proc.returncode == 0


def notifier(texte: str, d: Demande) -> None:
    try:
        from tools import journal

        journal.ajouter(texte, "atelier")
    except Exception:
        log.debug("journal du foyer indisponible", exc_info=True)
    envoyer_telegram(texte, telegram_id(d.demandeur))


# --- réseau supplémentaire ------------------------------------------------------------------------------------------


def preparer_reseau(nom: str, personne: str | None, donnees: Path | None = None) -> tuple[str, dict[str, Any]]:
    """« Planity veut aussi parler à cdn.planity.com » : approbation des hôtes en attente (propriétaire)."""
    from tools import administration

    prenom = administration.verifier_proprietaire(personne)
    fournisseur = _fournisseur_atelier(nom)
    if fournisseur is not None:
        infos = ((capacites.lire_etat(donnees).get("transports") or {}).get("fournisseurs_atelier") or {}).get(fournisseur.nom) or {}
        attente = list(infos.get("reseau_en_attente") or [])
        if not attente:
            raise ErreurAtelier(f"Le réseau « {fournisseur.titre} » n'attend aucune autorisation de site.")
        resume = f"Le réseau « {fournisseur.titre} » veut aussi lire {', '.join(attente)}."
        return resume, {"action": "atelier_approuver_reseau", "nom": fournisseur.nom, "hotes": attente, "par": prenom,
                        "fournisseur": True}
    cap = administration.trouver_capacite(nom)
    if not capacites.de_l_atelier(cap):
        raise ErreurAtelier(f"« {cap.titre} » ne vient pas de l'atelier : rien à approuver.")
    attente = list((capacites.lire_etat(donnees).get(cap.nom) or {}).get("reseau_en_attente") or [])
    if not attente:
        raise ErreurAtelier(f"« {cap.titre} » n'attend aucune autorisation de site.")
    resume = f"« {cap.titre} » veut aussi parler à {', '.join(attente)}."
    return resume, {"action": "atelier_approuver_reseau", "nom": cap.nom, "hotes": attente, "par": prenom}


def _fournisseur_atelier(nom: str) -> Any:
    try:
        fr, _interface = _transports()
    except ErreurAtelier:
        return None
    cible = _slug(nom)
    return next((f for f in fr.decouvrir(None) if f.provenance == "atelier"
                 and (f.nom == cible or _slug(f.titre) == cible)), None)


def executer_reseau(payload: dict[str, Any], donnees: Path | None = None) -> str:
    nom = str(payload.get("nom") or "")
    hotes = [h for h in payload.get("hotes") or [] if isinstance(h, str) and capacites.RE_HOTE.match(h)]
    if payload.get("fournisseur"):
        with capacites._modifier_etat(donnees) as etat:
            entree = etat.setdefault("transports", {})
            entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | set(hotes))
            infos = (entree.get("fournisseurs_atelier") or {}).get(nom) or {}
            infos["reseau_en_attente"] = [h for h in infos.get("reseau_en_attente") or [] if h not in hotes]
        return f"C'est noté : le réseau peut maintenant lire {', '.join(hotes)}."
    cap = capacites.trouver(nom)
    with capacites._modifier_etat(donnees) as etat:
        entree = etat.setdefault(cap.nom, {})
        entree["reseau_approuve"] = sorted(set(entree.get("reseau_approuve") or []) | set(hotes))
        entree["reseau_en_attente"] = [h for h in entree.get("reseau_en_attente") or [] if h not in hotes]
    return f"C'est noté : « {cap.titre} » peut maintenant parler à {', '.join(hotes)}."


# --- CLI ---------------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    os.umask(0o027)
    parser = argparse.ArgumentParser(prog="python -m tools.atelier", description="Atelier des capacités (lot 7b)")
    sub = parser.add_subparsers(dest="commande", required=True)
    sub.add_parser("traiter", help="fabrique puis conclut ce qui attend")
    sub.add_parser("fabriquer", help="fabrique les demandes en file (homeos-atelier.service)")
    sub.add_parser("conclure", help="relit, installe et prévient (homeos-atelier-installer.service)")
    p_etat = sub.add_parser("etat", help="progression (JSON)")
    p_etat.add_argument("nom", nargs="?")
    p_smoke = sub.add_parser("smoke-candidat", help="(interne) smoke test isolé d'un candidat")
    p_smoke.add_argument("dossier", type=Path)
    p_smoke.add_argument("--site", required=True)
    p_smoke.add_argument("--hote", required=True)
    p_smoke.add_argument("--fournisseur", action="store_true", help="candidat fournisseur de transports")
    sub.add_parser("retirer-fournisseur", help="retire un réseau de transports fabriqué par l'atelier").add_argument("nom")
    args = parser.parse_args(argv)
    try:
        if args.commande == "traiter":
            traiter()
        elif args.commande == "fabriquer":
            fabriquer_file()
        elif args.commande == "conclure":
            conclure_tout()
        elif args.commande == "etat":
            sys.stdout.write(json.dumps(etat_public(args.nom), ensure_ascii=False, indent=1) + "\n")
        elif args.commande == "smoke-candidat":
            sys.stdout.write(json.dumps(smoke_candidat(args.dossier, args.site, args.hote, args.fournisseur),
                                        ensure_ascii=False) + "\n")
        elif args.commande == "retirer-fournisseur":
            sys.stdout.write(retirer_fournisseur(args.nom) + "\n")
    except ErreurAtelier as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
