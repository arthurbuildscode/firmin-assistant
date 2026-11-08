"""Système de capacités (lot 5a) : une capacité = un plugin déclaratif dans `capabilities/<nom>/`.

Contrat (CONTRACT.md §9, docs/spec-lot5-capacites.md §A) :

    capabilities/<nom>/
      capability.yaml   manifeste (validé ici, voir `valider_manifeste`)
      tools.py          def enregistrer(mcp, ctx) -> None        (+ optionnel : def executer(action, payload, ctx) -> str)
      CLAUDE.md         fragment ajouté au prompt système quand la capacité est chargée
      onboarding.md     (optionnel) module de découverte
      smoke.py          def verifier(ctx) -> {"ok": bool, "message": str}
      crons.py          (optionnel) def executer(nom_cron, ctx) -> None | str | dict
                        (un texte renvoyé est posté dans le groupe et, avec `cron --publier`, remis dans le fil de
                        l'appli de chaque adulte ; un dict `{texte, carte?, notification?, seuil?}` précise la carte et
                        la notification ; `ctx.publier(personne, …)` remet un message à une seule personne)
      tests/            pytest, lancés par la suite globale

Portabilité : aucune donnée personnelle dans `capabilities/<nom>/`. Ce qui dépend du foyer est déclaré dans
`parametres:` du manifeste ; les valeurs vivent dans `data/capacites/<nom>/parametres.yaml` (remplies par
l'onboarding via l'outil `capacites_parametrer`, lues par `ctx.parametre(cle)`).

CLI (cwd = racine du dépôt, HOMEOS_DATA défini) :

    python -m tools.capacites list [--json]
    python -m tools.capacites statut <nom>
    python -m tools.capacites enable <nom>
    python -m tools.capacites disable <nom>
    python -m tools.capacites remove <nom> [--donnees] [--oui]
    python -m tools.capacites smoke [nom] [--tsv] [--cache MINUTES]
    python -m tools.capacites new <nom> --titre "…" [--description "…"] [--origine humain|assistant]
    python -m tools.capacites parametre <nom> [<cle> <valeur>]   (affiche ou renseigne un paramètre)
    python -m tools.capacites export <nom> [--sortie DOSSIER]     (archive .tar.gz + fichier .sha256)
    python -m tools.capacites install <archive.tar.gz> [--oui]     (installe en essai après confirmation)
    python -m tools.capacites cron <nom> <cron> [--publier]   (timers homeos-cap-<nom>-<cron>, scripts/capacite-cron.sh)
    python -m tools.capacites timers                      (pour install.sh : nom, cron, OnCalendar)
    python -m tools.capacites migrer                      (état des capacités remplacées, `remplace:` ; install.sh)
    python -m tools.capacites prompt                      (fragments CLAUDE.md des capacités chargées)

Statut effectif d'une capacité : la valeur du manifeste, remplacée par `enable`/`disable` (état gardé dans
`data/capacites/etat.json`, le dépôt n'est jamais modifié) ; une capacité `origine: assistant` reste en
`essai` tant qu'une personne du foyer ne l'a pas activée (`enable`, qui enregistre la validation humaine) ou, lot 7b,
tant qu'un propriétaire n'a pas validé à la main sa première action avec effet (voir `proposer_premiere_validation`).
Capacités fabriquées par l'atelier (lot 7b) : `<etat>/capacites-atelier/<nom>/` (`racine_atelier`), découvertes après
celles du dépôt ; leurs hôtes réseau ne comptent qu'une fois approuvés (`hotes_approuves`).
`stdout` est réservé au protocole MCP quand ce module est chargé par `tools.server` : tout log va vers stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import functools
import getpass
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import threading
import types
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import coffre
from tools.coffre import SecretManquant  # réexporté : `capacites.SecretManquant` est celui du coffre (lot 5b)
from tools.common import data_dir, get_logger, iso_now, load_config, now

log = get_logger("homeos.capacites")

STATUTS = ("actif", "inactif", "essai")
STATUTS_CHARGES = ("actif", "essai")  # outils exposés, fragment de prompt injecté, smoke test exécuté
ORIGINES = ("humain", "assistant")
JOURS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RE_NOM = re.compile(r"^[a-z0-9_]+$")
RE_COMMANDE = re.compile(r"^[a-z0-9_]{1,32}$")
RE_QUAND = re.compile(
    r"^(?:(?P<jours>[A-Za-z]{3}(?:(?:,|\.\.)[A-Za-z]{3})*)\s+)?(?P<h>\d{1,2}):(?P<m>\d{2})"
    r"(?:\.\.(?P<h2>\d{1,2}):00/(?P<pas>\d{1,2}))?$"
)
# Crons périodiques (« Mon..Fri 06:00..20:00/10 » : toutes les 10 min de 6 h à 20 h) : pas permis, en minutes.
PAS_PERIODIQUES = (5, 10, 15, 20, 30)
CLES_MANIFESTE = (
    "nom", "titre", "description", "version", "statut", "origine", "personnes", "secrets", "crons",
    "actions_avec_effet", "confirmation_toujours", "reseau", "commandes_telegram", "parametres", "prefixe",
    "seuils", "remplace",
)
TYPES_PARAMETRE = ("texte", "nombre", "choix", "booleen")
# Commandes du bot qu'une capacité ne peut pas remplacer.
COMMANDES_RESERVEES = frozenset(
    {"start", "nouveau", "profil", "journal", "courses", "brief", "statut", "stop", "reprendre", "aide", "help",
     "capacites", "identifiants", "admin"}
)

MASQUE = coffre.MASQUE  # « ••• »
LONGUEUR_MIN_MASQUE = coffre.LONGUEUR_MIN_MASQUAGE  # en dessous, une « valeur de secret » (ex. « 1 ») masquerait n'importe quoi
TIMEOUT_SMOKE_S = 90
PREFIXE_CONFIRMATION = "CONFIRMATION_REQUISE:"
MODE_DOSSIER = 0o2770

MESSAGE_ESSAI = (
    "« {titre} » est en essai : je peux consulter, mais pas encore agir. "
    "Une personne du foyer doit d'abord l'activer."
)


# --- erreurs ------------------------------------------------------------------


class ErreurCapacite(RuntimeError):
    """Erreur du système de capacités, message en français destiné à l'utilisateur."""


class ManifesteInvalide(ErreurCapacite):
    pass


# SecretManquant (secret absent ou incomplet) vient de `tools.coffre` (importé plus haut) : un seul type
# d'erreur pour le coffre et les capacités, avec `cle`, `capacite` et `champs_manquants`.


class ActionRefusee(ErreurCapacite):
    """Capacité désactivée, en essai pour une action avec effet, ou réservée à d'autres personnes."""


class ParametreManquant(ErreurCapacite):
    """Paramètre obligatoire non renseigné (l'onboarding de la capacité le demande)."""


@functools.cache
def _classe_erreur_outil() -> type[Exception]:
    """Erreur transmise au modèle avec son message (ToolError du SDK mcp), aussi `ValueError` comme `tools.server`."""
    try:
        from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[import-not-found]
    except ImportError:
        try:
            from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[import-not-found,no-redef]
        except ImportError:
            ToolError = RuntimeError  # type: ignore[misc,assignment]
    return type("ErreurOutilCapacite", (ToolError, ValueError), {})


def erreur_outil(message: str) -> Exception:
    return _classe_erreur_outil()(message)


# --- manifeste ------------------------------------------------------------------


@dataclass(frozen=True)
class SecretRequis:
    cle: str
    champs: tuple[str, ...]
    par_personne: bool = False
    # Type de l'entrée du coffre (lot 5c) : identifiants | carte_bancaire | code | cle_api | autre.
    type: str = coffre.TYPE_DEFAUT

    @property
    def base(self) -> str:
        """`planity/alex` + par_personne → `planity` (les clés réelles sont `planity/<personne>`)."""
        if self.par_personne and "/" in self.cle:
            return self.cle.rsplit("/", 1)[0]
        return self.cle


@dataclass(frozen=True)
class CronDeclare:
    nom: str
    quand: str


@dataclass(frozen=True)
class SeuilDeclare:
    """Seuil au-delà duquel la capacité peut parler hors du brief (skill ux-capacites §2, `coeur.spontane`)."""

    nom: str
    description: str


@dataclass(frozen=True)
class CommandeTelegram:
    nom: str
    aide: str


@dataclass(frozen=True)
class ParametreDeclare:
    cle: str
    type: str
    question: str
    obligatoire: bool = False
    choix: tuple[str, ...] = ()
    defaut: Any = None


@dataclass
class Manifeste:
    nom: str
    titre: str
    description: str
    version: int
    statut: str
    origine: str
    personnes: list[str] = field(default_factory=list)
    secrets: list[SecretRequis] = field(default_factory=list)
    crons: list[CronDeclare] = field(default_factory=list)
    actions_avec_effet: list[str] = field(default_factory=list)
    # Actions jamais exécutées sans bouton, même si la capacité est en `validation: automatique` (config.yaml).
    confirmation_toujours: list[str] = field(default_factory=list)
    reseau: list[str] = field(default_factory=list)
    commandes_telegram: list[CommandeTelegram] = field(default_factory=list)
    parametres: list[ParametreDeclare] = field(default_factory=list)
    # Préfixe des outils et des actions (`<prefixe>_…`) ; par défaut le nom (lot 7a : `administration` → `admin`).
    prefixe: str = ""
    # Seuils déclarés : un message spontané qui en franchit un est notifié même au-delà d'un par jour.
    seuils: list[SeuilDeclare] = field(default_factory=list)
    # Anciennes capacités reprises (lot transports : `remplace: [bus]`) : leur état (etat.json) et leurs réglages
    # (config.yaml `capacites.<ancien>`) valent pour celle-ci tant qu'elle n'a pas les siens.
    remplace: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.prefixe:
            self.prefixe = self.nom


def accepte_nom(prefixe: str, nom_outil: str) -> bool:
    """Un outil ou un kind d'action appartient à la capacité si c'est `<prefixe>` ou `<prefixe>_…`."""
    return nom_outil == prefixe or nom_outil.startswith(f"{prefixe}_")


@dataclass
class Capacite:
    dossier: Path
    manifeste: Manifeste | None
    erreurs: list[str] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)

    @property
    def nom(self) -> str:
        return self.manifeste.nom if self.manifeste else self.dossier.name

    @property
    def titre(self) -> str:
        return self.manifeste.titre if self.manifeste else self.dossier.name

    @property
    def prefixe(self) -> str:
        return self.manifeste.prefixe if self.manifeste else self.nom

    @property
    def valide(self) -> bool:
        return self.manifeste is not None and not self.erreurs


def oncalendar(quand: str) -> str:
    """« Wed 18:00 » → « Wed *-*-* 18:00:00 » ; « 07:15 » → « *-*-* 07:15:00 » ; « Mon..Fri 07:00 » accepté."""
    m = RE_QUAND.fullmatch(str(quand).strip())
    if not m:
        raise ValueError(f"horaire invalide « {quand} » (attendu « HH:MM » ou « Jour HH:MM », jours Mon..Sun)")
    h, mn = int(m.group("h")), int(m.group("m"))
    if h > 23 or mn > 59:
        raise ValueError(f"horaire invalide « {quand} »")
    heures, minutes = f"{h:02d}", f"{mn:02d}"
    if m.group("h2") is not None:
        # Périodique : « 06:00..20:00/10 » → heures 06..19, minutes 00/10 (la borne de fin est exclue).
        h2, pas = int(m.group("h2")), int(m.group("pas"))
        if mn != 0 or not h < h2 <= 24 or pas not in PAS_PERIODIQUES:
            raise ValueError(
                f"horaire périodique invalide « {quand} » (attendu « HH:00..HH:00/N », N parmi "
                f"{', '.join(map(str, PAS_PERIODIQUES))} minutes)"
            )
        heures, minutes = f"{h:02d}..{h2 - 1:02d}", f"00/{pas:02d}"
    jours = m.group("jours")
    prefixe = ""
    if jours:
        morceaux = re.split(r"(,|\.\.)", jours)
        for i in range(0, len(morceaux), 2):
            morceaux[i] = morceaux[i].capitalize()
            if morceaux[i] not in JOURS:
                raise ValueError(f"jour inconnu « {morceaux[i]} » dans « {quand} » (Mon..Sun)")
        prefixe = "".join(morceaux) + " "
    return f"{prefixe}*-*-* {heures}:{minutes}:00"


def _liste_de_textes(valeur: Any, champ: str, erreurs: list[str]) -> list[str]:
    if valeur is None:
        return []
    if not isinstance(valeur, list) or not all(isinstance(v, str) and v.strip() for v in valeur):
        erreurs.append(f"`{champ}` doit être une liste de textes")
        return []
    return [v.strip() for v in valeur]


def valider_manifeste(brut: Any, nom_dossier: str | None = None) -> tuple[Manifeste | None, list[str], list[str]]:
    """Valide un manifeste chargé (dict YAML). Renvoie (manifeste ou None, erreurs, avertissements)."""
    erreurs: list[str] = []
    avertissements: list[str] = []
    if not isinstance(brut, dict):
        return None, ["le manifeste doit être un dictionnaire YAML"], []

    for cle in brut:
        if cle not in CLES_MANIFESTE:
            avertissements.append(f"clé inconnue `{cle}` ignorée")

    nom = brut.get("nom")
    if not isinstance(nom, str) or not RE_NOM.fullmatch(nom):
        erreurs.append("`nom` obligatoire : minuscules, chiffres et _ uniquement (ex. kitchen_daily)")
        nom = nom_dossier or "?"
    elif nom_dossier is not None and nom != nom_dossier:
        erreurs.append(f"`nom` ({nom}) doit être identique au nom du dossier ({nom_dossier})")

    titre = brut.get("titre")
    if not isinstance(titre, str) or not titre.strip():
        erreurs.append("`titre` obligatoire (texte)")
        titre = str(nom)
    description = brut.get("description")
    if not isinstance(description, str) or not description.strip():
        erreurs.append("`description` obligatoire (texte)")
        description = ""

    version = brut.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        erreurs.append("`version` doit être un entier ≥ 1")
        version = 1

    statut = brut.get("statut")
    if statut not in STATUTS:
        erreurs.append(f"`statut` doit valoir {', '.join(STATUTS)}")
        statut = "inactif"
    origine = brut.get("origine")
    if origine not in ORIGINES:
        erreurs.append(f"`origine` doit valoir {' ou '.join(ORIGINES)}")
        origine = "assistant"

    personnes = [_slug(p) for p in _liste_de_textes(brut.get("personnes"), "personnes", erreurs)]

    prefixe = brut.get("prefixe", nom)
    if not isinstance(prefixe, str) or not RE_NOM.fullmatch(prefixe):
        erreurs.append("`prefixe` : minuscules, chiffres et _ uniquement (préfixe des outils, ex. admin)")
        prefixe = str(nom)

    secrets: list[SecretRequis] = []
    for i, s in enumerate(brut.get("secrets") or []):
        if not isinstance(s, dict) or not isinstance(s.get("cle"), str) or not s["cle"].strip():
            erreurs.append(f"`secrets[{i}]` : `cle` obligatoire")
            continue
        cle = s["cle"].strip().removeprefix("secret://")
        if not re.fullmatch(r"[a-z0-9_]+(/[a-z0-9_.-]+)*", cle):
            erreurs.append(f"`secrets[{i}].cle` invalide « {cle} » (ex. planity/alex)")
            continue
        type_secret = coffre.TYPE_DEFAUT
        if s.get("type") is None:
            avertissements.append(f"`secrets[{i}].type` absent : « identifiants » supposé (déclarer le type)")
        else:
            try:
                type_secret = coffre.normaliser_type(s["type"])
            except coffre.ErreurCoffre:
                erreurs.append(f"`secrets[{i}].type` doit valoir {', '.join(coffre.TYPES)}")
                continue
        champs = s.get("champs")
        if champs is None and coffre.champs_du_type(type_secret)[0]:
            champs = list(coffre.champs_du_type(type_secret)[0])
        if not isinstance(champs, list) or not champs or not all(isinstance(c, str) and c.strip() for c in champs):
            erreurs.append(f"`secrets[{i}].champs` : liste non vide de noms de champs attendue")
            continue
        if type_secret in coffre.TYPES_FERMES:
            permis = set(coffre.champs_du_type(type_secret)[0]) | set(coffre.champs_du_type(type_secret)[1])
            hors = sorted({c.strip() for c in champs} - permis)
            if hors:
                erreurs.append(f"`secrets[{i}].champs` : {', '.join(hors)} hors du type {type_secret}")
                continue
        par_personne = s.get("par_personne", False)
        if not isinstance(par_personne, bool):
            erreurs.append(f"`secrets[{i}].par_personne` doit être true ou false")
            continue
        if any(k not in ("cle", "champs", "par_personne", "type") for k in s):
            avertissements.append(f"`secrets[{i}]` : clés inconnues ignorées (jamais de valeur de secret ici)")
        secrets.append(SecretRequis(cle, tuple(c.strip() for c in champs), par_personne, type_secret))
    if brut.get("secrets") is not None and not isinstance(brut.get("secrets"), list):
        erreurs.append("`secrets` doit être une liste")

    crons: list[CronDeclare] = []
    vus: set[str] = set()
    if brut.get("crons") is not None and not isinstance(brut.get("crons"), list):
        erreurs.append("`crons` doit être une liste")
    for i, c in enumerate(brut.get("crons") or [] if isinstance(brut.get("crons"), list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("nom"), str) or not RE_NOM.fullmatch(c["nom"]):
            erreurs.append(f"`crons[{i}].nom` obligatoire : minuscules, chiffres et _")
            continue
        if c["nom"] in vus:
            erreurs.append(f"cron « {c['nom']} » déclaré deux fois")
            continue
        try:
            oncalendar(str(c.get("quand", "")))
        except ValueError as exc:
            erreurs.append(f"`crons[{i}].quand` : {exc}")
            continue
        vus.add(c["nom"])
        crons.append(CronDeclare(c["nom"], str(c["quand"]).strip()))

    actions = _liste_de_textes(brut.get("actions_avec_effet"), "actions_avec_effet", erreurs)
    for a in actions:
        if not accepte_nom(str(prefixe), a):
            erreurs.append(f"action avec effet « {a} » : les outils doivent être préfixés par « {prefixe}_ »")

    toujours = _liste_de_textes(brut.get("confirmation_toujours"), "confirmation_toujours", erreurs)
    for a in toujours:
        if a not in actions:
            erreurs.append(f"confirmation_toujours « {a} » : doit aussi figurer dans actions_avec_effet")
    if any(s.type == "carte_bancaire" for s in secrets) and not toujours:
        erreurs.append(
            "une carte bancaire ne se lit que pendant une action validée par un humain : "
            "déclarer l'action de paiement dans `confirmation_toujours`"
        )

    reseau = _liste_de_textes(brut.get("reseau"), "reseau", erreurs)

    commandes: list[CommandeTelegram] = []
    if brut.get("commandes_telegram") is not None and not isinstance(brut.get("commandes_telegram"), list):
        erreurs.append("`commandes_telegram` doit être une liste")
    for i, c in enumerate(brut.get("commandes_telegram") or [] if isinstance(brut.get("commandes_telegram"), list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("nom"), str):
            erreurs.append(f"`commandes_telegram[{i}].nom` obligatoire")
            continue
        nom_cmd = c["nom"].strip().lstrip("/")
        if not RE_COMMANDE.fullmatch(nom_cmd):
            erreurs.append(f"commande Telegram « {nom_cmd} » invalide (minuscules, chiffres, _ ; 32 caractères max)")
            continue
        if nom_cmd in COMMANDES_RESERVEES:
            erreurs.append(f"commande Telegram « /{nom_cmd} » réservée au bot")
            continue
        aide = c.get("aide")
        if not isinstance(aide, str) or not aide.strip():
            erreurs.append(f"`commandes_telegram[{i}].aide` obligatoire (texte court)")
            continue
        commandes.append(CommandeTelegram(nom_cmd, aide.strip()))

    parametres: list[ParametreDeclare] = []
    brut_params = brut.get("parametres")
    if brut_params is not None and not isinstance(brut_params, list):
        erreurs.append("`parametres` doit être une liste")
        brut_params = []
    cles_vues: set[str] = set()
    for i, p in enumerate(brut_params or []):
        if not isinstance(p, dict) or not isinstance(p.get("cle"), str) or not RE_NOM.fullmatch(p["cle"]):
            erreurs.append(f"`parametres[{i}].cle` obligatoire : minuscules, chiffres et _")
            continue
        if p["cle"] in cles_vues:
            erreurs.append(f"paramètre « {p['cle']} » déclaré deux fois")
            continue
        type_p = p.get("type", "texte")
        if type_p not in TYPES_PARAMETRE:
            erreurs.append(f"`parametres[{i}].type` doit valoir {', '.join(TYPES_PARAMETRE)}")
            continue
        question = p.get("question")
        if not isinstance(question, str) or not question.strip():
            erreurs.append(f"`parametres[{i}].question` obligatoire (posée pendant l'onboarding)")
            continue
        obligatoire = p.get("obligatoire", False)
        if not isinstance(obligatoire, bool):
            erreurs.append(f"`parametres[{i}].obligatoire` doit être true ou false")
            continue
        choix: tuple[str, ...] = ()
        if type_p == "choix":
            brut_choix = p.get("choix")
            if not isinstance(brut_choix, list) or not brut_choix:
                erreurs.append(f"`parametres[{i}].choix` : liste non vide attendue pour le type choix")
                continue
            choix = tuple(str(c) for c in brut_choix)
        declare = ParametreDeclare(p["cle"], type_p, question.strip(), obligatoire, choix)
        if p.get("defaut") is not None:
            try:
                declare = ParametreDeclare(declare.cle, type_p, declare.question, obligatoire, choix,
                                           convertir_parametre(declare, p["defaut"]))
            except ValueError as exc:
                erreurs.append(f"`parametres[{i}].defaut` : {exc}")
                continue
        cles_vues.add(p["cle"])
        parametres.append(declare)

    seuils: list[SeuilDeclare] = []
    brut_seuils = brut.get("seuils")
    if brut_seuils is not None and not isinstance(brut_seuils, list):
        erreurs.append("`seuils` doit être une liste")
        brut_seuils = []
    for i, sd in enumerate(brut_seuils or []):
        if not isinstance(sd, dict) or not isinstance(sd.get("nom"), str) or not RE_NOM.fullmatch(sd["nom"]):
            erreurs.append(f"`seuils[{i}].nom` obligatoire : minuscules, chiffres et _")
            continue
        description_s = sd.get("description")
        if not isinstance(description_s, str) or not description_s.strip():
            erreurs.append(f"`seuils[{i}].description` obligatoire (ce qui justifie de parler hors du brief)")
            continue
        if any(x.nom == sd["nom"] for x in seuils):
            erreurs.append(f"seuil « {sd['nom']} » déclaré deux fois")
            continue
        seuils.append(SeuilDeclare(sd["nom"], description_s.strip()))

    remplace: list[str] = []
    brut_remplace = brut.get("remplace")
    if brut_remplace is not None:
        if not isinstance(brut_remplace, list) or not all(isinstance(x, str) and RE_NOM.fullmatch(x) for x in brut_remplace):
            erreurs.append("`remplace` : liste de noms de capacités ([a-z0-9_])")
        elif nom in brut_remplace:
            erreurs.append("`remplace` ne peut pas contenir le nom de la capacité elle-même")
        else:
            remplace = list(brut_remplace)

    if erreurs:
        return None, erreurs, avertissements
    return (
        Manifeste(
            nom=str(nom),
            titre=str(titre).strip(),
            description=str(description).strip(),
            version=int(version),
            statut=str(statut),
            origine=str(origine),
            personnes=personnes,
            secrets=secrets,
            crons=crons,
            actions_avec_effet=actions,
            confirmation_toujours=toujours,
            reseau=reseau,
            commandes_telegram=commandes,
            parametres=parametres,
            prefixe=str(prefixe),
            seuils=seuils,
            remplace=remplace,
        ),
        erreurs,
        avertissements,
    )


def lire_capacite(dossier: Path) -> Capacite:
    """Lit et valide `capabilities/<nom>/` (manifeste + présence des fichiers attendus)."""
    dossier = Path(dossier)
    fichier = dossier / "capability.yaml"
    if not fichier.exists():
        return Capacite(dossier, None, ["capability.yaml absent"])
    try:
        import yaml

        brut = yaml.safe_load(fichier.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return Capacite(dossier, None, [f"capability.yaml illisible ({exc.__class__.__name__})"])
    manifeste, erreurs, avertissements = valider_manifeste(brut, dossier.name)
    if not (dossier / "tools.py").exists():
        erreurs.append("tools.py absent (def enregistrer(mcp, ctx))")
    if not (dossier / "CLAUDE.md").exists():
        avertissements.append("CLAUDE.md absent : l'assistant ne saura pas quand utiliser la capacité")
    if not (dossier / "smoke.py").exists():
        avertissements.append("smoke.py absent : pas de vérification par le healthcheck")
    if manifeste is not None and manifeste.crons and not (dossier / "crons.py").exists():
        erreurs.append("crons déclarés mais crons.py absent")
    if erreurs:
        manifeste = None
    return Capacite(dossier, manifeste, erreurs, avertissements)


def racine_capacites() -> Path:
    """`capabilities/` à côté du code (le dépôt), ou HOMEOS_CAPABILITIES (tests)."""
    env = os.environ.get("HOMEOS_CAPABILITIES")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "capabilities"


def racine_atelier() -> Path | None:
    """Capacités fabriquées par l'atelier (lot 7b) : `<etat>/capacites-atelier` (`/var/lib/homeos`, réservé à
    homeos : le code exécuté par le serveur d'outils n'est jamais modifiable par les personnes du foyer).

    `HOMEOS_CAPABILITIES_ATELIER` la remplace ; quand `HOMEOS_CAPABILITIES` est fixé (tests) sans elle : aucune."""
    env = os.environ.get("HOMEOS_CAPABILITIES_ATELIER", "").strip()
    if env:
        return Path(env)
    if os.environ.get("HOMEOS_CAPABILITIES"):
        return None
    from tools.common import etat_dir

    return etat_dir() / "capacites-atelier"


def _lire_racine(racine: Path) -> list[Capacite]:
    if not racine.is_dir():
        return []
    return [lire_capacite(d) for d in sorted(racine.iterdir()) if d.is_dir() and not d.name.startswith((".", "_"))]


def decouvrir(racine: Path | None = None) -> list[Capacite]:
    """Toutes les capacités présentes (valides ou non), triées par nom.

    Sans `racine` : celles du dépôt puis celles de l'atelier (`racine_atelier`) ; un nom déjà pris dans le dépôt
    masque celui de l'atelier (l'atelier refuse de toute façon un nom existant)."""
    if racine is not None:
        return _lire_racine(Path(racine))
    capacites = _lire_racine(racine_capacites())
    atelier = racine_atelier()
    if atelier is not None:
        pris = {c.nom for c in capacites} | {c.dossier.name for c in capacites}
        capacites += [c for c in _lire_racine(atelier) if c.nom not in pris and c.dossier.name not in pris]
    return sorted(capacites, key=lambda c: c.nom)


def de_l_atelier(cap: Capacite) -> bool:
    """Vrai pour une capacité installée par l'atelier (dossier sous `racine_atelier()`)."""
    atelier = racine_atelier()
    if atelier is None:
        return False
    try:
        return cap.dossier.resolve().parent == atelier.resolve()
    except OSError:
        return False


def trouver(nom: str, racine: Path | None = None) -> Capacite:
    nom = nom.strip()
    caps = decouvrir(racine)
    for cap in caps:
        if cap.valide and (cap.nom == nom or cap.dossier.name == nom):
            return cap
    for cap in caps:  # ancien nom d'une capacité qui l'a remplacée (« bus » → transports)
        if cap.manifeste is not None and nom in cap.manifeste.remplace:
            return cap
    for cap in caps:  # capacité invalide : trouvée quand même (pour lister ses erreurs)
        if cap.nom == nom or cap.dossier.name == nom:
            return cap
    raise ErreurCapacite(f"Capacité « {nom} » introuvable.")


# --- état (data/capacites/etat.json) -------------------------------------------


def _donnees(donnees: Path | None) -> Path:
    return Path(donnees) if donnees is not None else data_dir()


def _creer_dossier(chemin: Path) -> Path:
    if not chemin.exists():
        chemin.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(chemin, MODE_DOSSIER)
    return chemin


def chemin_etat(donnees: Path | None = None) -> Path:
    return _donnees(donnees) / "capacites" / "etat.json"


def lire_etat(donnees: Path | None = None) -> dict[str, Any]:
    """État persistant (statut forcé, validation humaine, dernier smoke test). Lecture seule, jamais d'écriture."""
    chemin = chemin_etat(donnees)
    try:
        etat = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return etat if isinstance(etat, dict) else {}


@contextlib.contextmanager
def _modifier_etat(donnees: Path | None = None) -> Iterator[dict[str, Any]]:
    """Lecture-modification-écriture verrouillée (flock) et atomique de etat.json."""
    import fcntl

    chemin = chemin_etat(donnees)
    _creer_dossier(chemin.parent)
    with open(chemin.parent / ".etat.lock", "a", encoding="utf-8") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        etat = lire_etat(donnees)
        yield etat
        tmp = chemin.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(etat, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o664)
        os.replace(tmp, chemin)


def entree_etat(cap: Capacite, etat: dict[str, Any] | None) -> dict[str, Any]:
    """Entrée de etat.json de la capacité ; à défaut, celle d'une capacité qu'elle remplace (`remplace:`)."""
    etat = etat or {}
    entree = etat.get(cap.nom)
    if isinstance(entree, dict) and entree:
        return entree
    for ancien in cap.manifeste.remplace if cap.manifeste is not None else []:
        if isinstance(etat.get(ancien), dict) and etat[ancien]:
            return etat[ancien]
    return {}


def bloc_config(cap: Capacite, config: dict[str, Any]) -> dict[str, Any]:
    """`capacites.<nom>` de config.yaml ; à défaut, celui d'une capacité remplacée."""
    blocs = config.get("capacites") if isinstance(config.get("capacites"), dict) else {}
    for nom in [cap.nom, *(cap.manifeste.remplace if cap.manifeste is not None else [])]:
        if isinstance(blocs.get(nom), dict):
            return blocs[nom]
    return {}


def migrer_remplacees(racine: Path | None = None, donnees: Path | None = None) -> list[str]:
    """Écrit dans etat.json l'entrée d'une capacité à partir de celle qu'elle remplace (idempotent ; l'ancienne
    entrée est retirée). Renvoie les migrations faites (« bus → transports »)."""
    faites: list[str] = []
    caps = [c for c in decouvrir(racine) if c.manifeste is not None and c.manifeste.remplace]
    if not caps:
        return faites
    with _modifier_etat(donnees) as etat:
        for cap in caps:
            for ancien in cap.manifeste.remplace:  # type: ignore[union-attr]
                if ancien not in etat:
                    continue
                vieille = etat.pop(ancien)
                if not (isinstance(etat.get(cap.nom), dict) and etat[cap.nom]) and isinstance(vieille, dict):
                    etat[cap.nom] = vieille
                faites.append(f"{ancien} → {cap.nom}")
    return faites


def statut_effectif(cap: Capacite, etat: dict[str, Any] | None = None) -> str:
    """actif | essai | inactif | invalide (voir docstring du module)."""
    if not cap.valide or cap.manifeste is None:
        return "invalide"
    entree = entree_etat(cap, etat)
    statut = entree.get("statut") if entree.get("statut") in STATUTS else cap.manifeste.statut
    if statut == "actif" and cap.manifeste.origine == "assistant" and not entree.get("validee_le"):
        return "essai"
    return str(statut)


def _statut_courant(cap: Capacite, donnees: Path | None) -> str:
    return statut_effectif(cap, lire_etat(donnees))


def quand_cron(cap: Capacite, cron: CronDeclare, config: dict[str, Any] | None = None) -> str:
    """Horaire du cron : `capacites.<nom>.crons.<cron>` dans config.yaml, sinon `crons.<cron>_<nom>`
    (compatibilité lot 2 : `crons.rappel_kitchen_daily`), sinon le manifeste."""
    config = config if config is not None else load_config()
    surcharge = (bloc_config(cap, config).get("crons") or {}).get(cron.nom)
    if surcharge is None:
        surcharge = (config.get("crons") or {}).get(f"{cron.nom}_{cap.nom}")
    if surcharge is not None:
        try:
            oncalendar(str(surcharge))
            return str(surcharge).strip()
        except ValueError:
            log.warning("horaire « %s » invalide pour %s/%s dans config.yaml : manifeste utilisé", surcharge, cap.nom, cron.nom)
    return cron.quand


# --- paramètres (data/capacites/<nom>/parametres.yaml) -------------------------------

_VRAI = {"oui", "o", "true", "vrai", "1", "yes", "y"}
_FAUX = {"non", "n", "false", "faux", "0", "no"}


def convertir_parametre(p: ParametreDeclare, valeur: Any) -> Any:
    """Convertit/valide une valeur selon le type déclaré (texte, nombre, choix, booleen)."""
    if p.type == "booleen":
        if isinstance(valeur, bool):
            return valeur
        texte = str(valeur).strip().casefold()
        if texte in _VRAI:
            return True
        if texte in _FAUX:
            return False
        raise ValueError(f"oui ou non attendu pour « {p.cle} »")
    if p.type == "nombre":
        if isinstance(valeur, bool):
            raise ValueError(f"nombre attendu pour « {p.cle} »")
        try:
            n = float(str(valeur).strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"nombre attendu pour « {p.cle} »") from None
        return int(n) if n.is_integer() else n
    texte = str(valeur).strip()
    if not texte:
        raise ValueError(f"valeur vide pour « {p.cle} »")
    if p.type == "choix":
        for c in p.choix:
            if _slug(c) == _slug(texte):
                return c
        raise ValueError(f"« {texte} » n'est pas un choix possible pour « {p.cle} » ({', '.join(p.choix)})")
    return texte


def chemin_parametres(nom: str, donnees: Path | None = None) -> Path:
    return _donnees(donnees) / "capacites" / nom / "parametres.yaml"


def lire_parametres(nom: str, donnees: Path | None = None) -> dict[str, Any]:
    try:
        import yaml

        valeurs = yaml.safe_load(chemin_parametres(nom, donnees).read_text(encoding="utf-8"))
    except (OSError, ImportError):
        return {}
    except Exception:  # noqa: BLE001 - YAML corrompu : on repart de zéro plutôt que de planter un outil
        log.warning("parametres.yaml illisible pour %s", nom)
        return {}
    return valeurs if isinstance(valeurs, dict) else {}


def definir_parametre(nom: str, cle: str, valeur: Any, racine: Path | None = None, donnees: Path | None = None) -> dict[str, Any]:
    """Valide et enregistre un paramètre déclaré ; renvoie tous les paramètres de la capacité."""
    import yaml

    cap = trouver(nom, racine)
    if cap.manifeste is None:
        raise ManifesteInvalide(f"« {cap.nom} » invalide : {'; '.join(cap.erreurs)}")
    declare = next((p for p in cap.manifeste.parametres if p.cle == cle), None)
    if declare is None:
        cles = ", ".join(p.cle for p in cap.manifeste.parametres) or "aucun"
        raise ErreurCapacite(f"Paramètre « {cle} » non déclaré par « {cap.titre} » (déclarés : {cles}).")
    try:
        propre = convertir_parametre(declare, valeur)
    except ValueError as exc:
        raise ErreurCapacite(str(exc)) from None
    chemin = chemin_parametres(cap.nom, donnees)
    _creer_dossier(_creer_dossier(chemin.parent.parent) / cap.nom)
    valeurs = lire_parametres(cap.nom, donnees)
    valeurs[cle] = propre
    tmp = chemin.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        f"# Paramètres de la capacité « {cap.titre} » (remplis par l'onboarding)\n"
        + yaml.safe_dump(valeurs, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o664)
    os.replace(tmp, chemin)
    return valeurs


def retirer_parametre(nom: str, cle: str, donnees: Path | None = None) -> dict[str, Any]:
    """Efface la valeur d'un paramètre (retour arrière de l'administration) ; renvoie les paramètres restants."""
    import yaml

    chemin = chemin_parametres(nom, donnees)
    valeurs = lire_parametres(nom, donnees)
    if cle not in valeurs:
        return valeurs
    del valeurs[cle]
    tmp = chemin.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        f"# Paramètres de la capacité « {nom} » (remplis par l'onboarding)\n"
        + yaml.safe_dump(valeurs, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o664)
    os.replace(tmp, chemin)
    return valeurs


def remplacer_etat(nom: str, entree: dict[str, Any] | None, donnees: Path | None = None) -> None:
    """Remet l'entrée de etat.json d'une capacité à une valeur antérieure (retour arrière de l'administration)."""
    with _modifier_etat(donnees) as etat:
        if entree:
            etat[nom] = dict(entree)
        else:
            etat.pop(nom, None)


def parametres_manquants(cap: Capacite, donnees: Path | None = None) -> list[ParametreDeclare]:
    if cap.manifeste is None:
        return []
    valeurs = lire_parametres(cap.nom, donnees)
    return [p for p in cap.manifeste.parametres if p.obligatoire and p.defaut is None and valeurs.get(p.cle) is None]


# --- personnes ------------------------------------------------------------------


def _slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9_]+", "", t)


def personne_courante(config: dict[str, Any] | None = None) -> str | None:
    """Slug de la personne courante : utilisateur de la requête (jeton du serveur d'outils HTTP, lot 7), sinon
    HOMEOS_PERSONNE, sinon l'utilisateur Linux s'il est une personne du foyer (outils stdio sous alex/camille),
    sinon None (crons, jeton homeos, bot en clé API sans personne)."""
    from tools import identite

    if identite.en_requete():
        config = config if config is not None else load_config()
        utilisateur = identite.utilisateur_requete()
        return _slug(utilisateur) if identite.personne_de(utilisateur, config) is not None else None
    env = os.environ.get("HOMEOS_PERSONNE", "").strip()
    if env:
        return _slug(env)
    config = config if config is not None else load_config()
    try:
        utilisateur = getpass.getuser()
    except Exception:  # noqa: BLE001
        return None
    for p in config.get("personnes") or []:
        if isinstance(p, dict) and p.get("linux_user") == utilisateur:
            return _slug(utilisateur)
    return None


def _alias_personne(personne: str, config: dict[str, Any]) -> set[str]:
    """{linux_user, prénom} en slugs, pour comparer avec `personnes:` du manifeste."""
    alias = {_slug(personne)}
    for p in config.get("personnes") or []:
        if not isinstance(p, dict):
            continue
        noms = {_slug(str(p.get("linux_user") or "")), _slug(str(p.get("prenom") or ""))} - {""}
        if alias & noms:
            alias |= noms
    return alias


def personne_autorisee(manifeste: Manifeste, personne: str | None, config: dict[str, Any] | None = None) -> bool:
    if not manifeste.personnes or personne is None:
        return True
    config = config if config is not None else load_config()
    return bool(_alias_personne(personne, config) & set(manifeste.personnes))


# --- masquage des secrets ---------------------------------------------------------

_SECRETS_LUS: set[str] = set()
_VERROU_SECRETS = threading.Lock()


def _memoriser_secrets(valeurs: Iterable[Any]) -> None:
    with _VERROU_SECRETS:
        for v in valeurs:
            if isinstance(v, str) and len(v) >= LONGUEUR_MIN_MASQUE:
                _SECRETS_LUS.add(v)


_CACHE_COFFRE: dict[str, Any] = {"quand": -1e9, "valeurs": frozenset()}
DUREE_CACHE_COFFRE_S = 5.0  # un appel d'outil masque plusieurs fois (sortie, journal) : une seule lecture du coffre


def _valeurs_coffre() -> frozenset[str]:
    import time

    if time.monotonic() - _CACHE_COFFRE["quand"] < DUREE_CACHE_COFFRE_S:
        return _CACHE_COFFRE["valeurs"]
    valeurs: frozenset[str] = frozenset()
    try:
        valeurs = frozenset(coffre.valeurs_connues())  # ne lève pas pour un coffre absent ou illisible
    except Exception as exc:  # noqa: BLE001 - le masquage ne doit jamais faire échouer un outil
        log.debug("valeurs du coffre indisponibles pour le masquage (%s)", exc.__class__.__name__)
    _CACHE_COFFRE.update(quand=time.monotonic(), valeurs=valeurs)
    return valeurs


def valeurs_secretes() -> set[str]:
    """Valeurs à masquer : secrets lus dans ce processus + `tools.coffre.valeurs_connues()` (cache de 5 s)."""
    with _VERROU_SECRETS:
        valeurs = set(_SECRETS_LUS)
    valeurs |= _valeurs_coffre()
    return {v for v in valeurs if len(v) >= LONGUEUR_MIN_MASQUE}


def masquer_secrets(valeur: Any, secrets: Iterable[str] | None = None) -> Any:
    """Remplace toute valeur de secret connue par `•••` (textes, dictionnaires, listes, récursivement)."""
    connus = sorted(set(secrets) if secrets is not None else valeurs_secretes(), key=len, reverse=True)
    connus = [s for s in connus if len(s) >= LONGUEUR_MIN_MASQUE]
    if not connus:
        return valeur

    ensemble = set(connus)

    def masquer(v: Any) -> Any:
        if isinstance(v, str):
            return coffre.masquer(v, ensemble)
        if isinstance(v, dict):
            return {masquer(k): masquer(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [masquer(x) for x in v]
        return v

    return masquer(valeur)


# --- contexte ---------------------------------------------------------------------

# Kind de l'action validée par bouton en cours d'exécution (`executeur_pour`, runner) ; None ailleurs (outils
# appelés par le modèle, crons, smoke tests). Une carte bancaire ne se lit que pendant une action de
# `confirmation_toujours` (jamais exécutée sans clic humain, même en validation automatique).
_ACTION_VALIDEE: contextvars.ContextVar[str | None] = contextvars.ContextVar("homeos_action_validee", default=None)

MESSAGE_CARTE_HORS_ACTION = (
    "Une carte bancaire ne se lit que pendant un paiement validé par une personne du foyer (bouton Valider) : "
    "je prépare la proposition, tu valides, puis tu confirmes le paiement (3-D Secure) toi-même."
)


def action_validee() -> str | None:
    """Kind de l'action validée en cours d'exécution, s'il y en a une."""
    return _ACTION_VALIDEE.get()


class Contexte:
    """Contexte passé aux capacités (`enregistrer`, `verifier`, `executer`).

    - `donnees` : Path de `data/` ; `dossier` : `data/capacites/<nom>/` (créé à la demande) ;
    - `config` : config.yaml chargé ; `personne` : slug de la personne courante, None pour les crons ;
    - `secret(cle)`, `journal(texte)`, `memoire_lire(chemin)`, `memoire_ecrire(chemin, texte)` ;
    - `demander_confirmation(action, resume, payload)` : action à valider par bouton (`CONFIRMATION_REQUISE:…`).
    """

    def __init__(
        self,
        donnees: Path | None = None,
        config: dict[str, Any] | None = None,
        personne: str | None = None,
        capacite: Capacite | None = None,
    ) -> None:
        self.donnees = _donnees(donnees)
        self._config = config
        self._config_lue: tuple[tuple[int, int] | None, dict[str, Any]] | None = None
        self._personne = personne
        self.capacite = capacite
        if config is None:
            self.config  # noqa: B018 - première lecture, comme avant le lot 7

    @property
    def personne(self) -> str | None:
        """Personne qui parle. Serveur d'outils HTTP (lot 7) : celle de la requête en cours (jeton), car un seul
        contexte sert tout le foyer ; sinon la personne fixée au chargement (stdio, crons : None)."""
        from tools import identite

        if identite.en_requete():
            return personne_courante(self.config)
        return self._personne

    @personne.setter
    def personne(self, valeur: str | None) -> None:
        self._personne = valeur

    @property
    def config(self) -> dict[str, Any]:
        """config.yaml : celle passée au constructeur, sinon relue quand le fichier change (le serveur d'outils
        persistant voit les réglages faits en conversation sans redémarrer)."""
        if self._config is not None:
            return self._config
        try:
            st = (data_dir() / "config.yaml").stat()
            empreinte: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            empreinte = None
        if self._config_lue is None or self._config_lue[0] != empreinte:
            self._config_lue = (empreinte, load_config())
        return self._config_lue[1]

    @config.setter
    def config(self, valeur: dict[str, Any] | None) -> None:
        self._config = valeur

    @property
    def nom(self) -> str | None:
        return self.capacite.nom if self.capacite else None

    @property
    def manifeste(self) -> Manifeste | None:
        return self.capacite.manifeste if self.capacite else None

    @property
    def acteur(self) -> str | None:
        """Auteur à inscrire dans actions_log : HOMEOS_WHO, sinon le prénom de la personne courante, sinon None."""
        env = os.environ.get("HOMEOS_WHO", "").strip()
        if env:
            return env
        if not self.personne:
            return None
        for p in self.config.get("personnes") or []:
            if isinstance(p, dict) and _slug(str(p.get("linux_user") or "")) == _slug(self.personne):
                return str(p.get("prenom") or p.get("linux_user"))
        return self.personne

    @property
    def dossier(self) -> Path:
        if not self.nom:
            raise ErreurCapacite("Contexte sans capacité : pas de dossier de données dédié.")
        return _creer_dossier(_creer_dossier(self.donnees / "capacites") / self.nom)

    # -- secrets
    def _message_secret(self, detail: str = "") -> str:
        nom = self.nom or "?"
        titre = self.capacite.titre if self.capacite else nom
        return f"Identifiants {titre} à renseigner{detail} : /identifiants {nom}"

    def _secret_manquant(self, cle: str, detail: str = "", champs_manquants: list[str] | None = None) -> SecretManquant:
        """`SecretManquant` du coffre, avec le titre lisible du manifeste (« Kitchen Daily » plutôt que « Kitchen daily »)."""
        exc = SecretManquant(cle, champs_manquants)
        exc.args = (self._message_secret(detail),)
        return exc

    def _resoudre_cle(self, cle: str) -> tuple[str, tuple[str, ...]]:
        """Clé complète et champs attendus ; refuse une clé non déclarée ou celle d'une autre personne."""
        if self.manifeste is None:
            return cle, ()
        for s in self.manifeste.secrets:
            if not s.par_personne:
                if cle == s.cle:
                    return cle, s.champs
                continue
            if cle == s.base:
                if not self.personne:
                    raise self._secret_manquant(s.base, " (personne inconnue pour un identifiant personnel)")
                return f"{s.base}/{self.personne}", s.champs
            if cle.startswith(s.base + "/"):
                proprietaire = cle[len(s.base) + 1 :]
                if self.personne and _slug(proprietaire) != _slug(self.personne):
                    raise ActionRefusee("Ces identifiants appartiennent à une autre personne du foyer.")
                return cle, s.champs
        raise ErreurCapacite(f"Secret « {cle} » non déclaré dans le manifeste de « {self.nom} ».")

    def _declaration(self, cle: str) -> SecretRequis | None:
        if self.manifeste is None:
            return None
        for s in self.manifeste.secrets:
            if cle == s.cle or cle == s.base or (s.par_personne and cle.startswith(s.base + "/")):
                return s
        return None

    def _verifier_type(self, cle_complete: str, declare: SecretRequis | None) -> None:
        """Lot 5c : le type de l'entrée doit être celui déclaré ; une carte bancaire n'est lue que pendant une
        action de `confirmation_toujours` validée par un humain, et seulement si elle est virtuelle. Contrôlé AVANT
        toute lecture de valeur."""
        try:
            infos = coffre.meta(cle_complete)
        except (coffre.ErreurCoffre, NotImplementedError):
            return  # coffre indisponible : `secret()` le dira juste après, sans valeur
        type_declare = declare.type if declare is not None else coffre.TYPE_DEFAUT
        type_entree = (infos or {}).get("type")
        if type_declare != "carte_bancaire" and type_entree != "carte_bancaire":
            if infos is not None and infos.get("champs") and type_entree not in (type_declare, None):
                # Entrée écrite sans type (coffre du lot 5b, API) : tolérée, seuls les champs comptent.
                log.info("%s : entrée de type %s pour un secret déclaré %s", cle_complete, type_entree, type_declare)
            return
        if type_declare != type_entree and infos is not None:
            raise ActionRefusee("Une carte bancaire ne peut servir qu'à une capacité qui déclare une carte bancaire.")
        manifeste = self.manifeste
        action = action_validee()
        if manifeste is None or not manifeste.confirmation_toujours or action not in manifeste.confirmation_toujours:
            raise ActionRefusee(MESSAGE_CARTE_HORS_ACTION)
        if infos is not None and infos.get("virtuelle") is not True:
            raise ActionRefusee("Seule une carte virtuelle à plafond peut servir : cette carte n'est pas marquée virtuelle.")

    def secret(self, cle: str) -> dict[str, Any]:
        """Champs du secret `cle` (référence `secret://…`), lus dans le coffre. Lève `SecretManquant`.

        Une carte bancaire n'est rendue que pendant une action validée par bouton listée dans
        `confirmation_toujours` (sinon `ActionRefusee`)."""
        cle_complete, champs = self._resoudre_cle(str(cle).strip().removeprefix(coffre.PREFIXE_REFERENCE))
        self._verifier_type(cle_complete, self._declaration(cle_complete))
        try:
            valeurs = coffre.lire(cle_complete)
        except SecretManquant:
            raise self._secret_manquant(cle_complete) from None
        except NotImplementedError as exc:  # backend vaultwarden pas encore implémenté
            exc_m = self._secret_manquant(cle_complete)
            exc_m.args = (f"Coffre indisponible : {exc}",)
            raise exc_m from None
        except coffre.ErreurCoffre as exc:  # COFFRE_CLE absente, coffre illisible : jamais de valeur dans le message
            log.warning("coffre indisponible pour %s : %s", cle_complete, exc)
            exc_m = self._secret_manquant(cle_complete)
            exc_m.args = ("Coffre indisponible pour l'instant : prévenir l'administrateur (détail dans le journal du serveur).",)
            raise exc_m from None
        _memoriser_secrets(valeurs.values())
        manquants = [c for c in champs if not valeurs.get(c)]
        if manquants:
            raise self._secret_manquant(cle_complete, f" (champs manquants : {', '.join(manquants)})", manquants)
        return dict(valeurs)

    # -- paramètres
    def parametre(self, cle: str, defaut: Any = None) -> Any:
        """Valeur du paramètre `cle` (parametres.yaml, sinon `defaut` du manifeste, sinon `defaut`).

        Lève `ParametreManquant` pour un paramètre obligatoire non renseigné."""
        declare = next((p for p in self.manifeste.parametres if p.cle == cle), None) if self.manifeste else None
        if self.manifeste is not None and declare is None:
            raise ErreurCapacite(f"Paramètre « {cle} » non déclaré dans le manifeste de « {self.nom} ».")
        valeur = lire_parametres(self.nom, self.donnees).get(cle) if self.nom else None
        if valeur is not None:
            return valeur
        if declare is not None and declare.defaut is not None:
            return declare.defaut
        if declare is not None and declare.obligatoire and defaut is None:
            titre = self.capacite.titre if self.capacite else cle
            raise ParametreManquant(
                f"Réglage « {titre} » à compléter : {declare.question} (je peux te le demander maintenant)."
            )
        return defaut

    # -- journal et mémoire
    def journal(self, texte: str) -> str:
        from tools import journal

        return journal.ajouter(str(masquer_secrets(texte)))

    def memoire_lire(self, chemin: str) -> str:
        from tools import memoire

        return memoire.lire(chemin)

    def memoire_ecrire(self, chemin: str, texte: str) -> str:
        from tools import memoire

        return memoire.ecrire(chemin, str(masquer_secrets(texte)))

    # -- confirmation
    def demander_confirmation(self, action: str, resume: str, payload: dict[str, Any] | None = None) -> str:
        """Crée l'action `pending` (kind `<nom>_<action>`) et renvoie `CONFIRMATION_REQUISE:<id>:<résumé>`.

        Après « Valider », `tools.runner execute <id>` appelle `executer(kind, payload, ctx)` du tools.py de la capacité.
        """
        from tools import db

        prefixe = self.capacite.prefixe if self.capacite else ""
        kind = action if not prefixe or accepte_nom(prefixe, action) else f"{prefixe}_{action}"
        resume = str(masquer_secrets(resume))[:300]
        aid = db.log_action(kind, resume, masquer_secrets(payload or {}), status="pending", acteur=self.acteur)
        return f"{PREFIXE_CONFIRMATION}{aid}:{resume}"

    # -- messages spontanés (coeur.spontane)
    def publier(
        self,
        personne: str,
        texte: str,
        carte: dict[str, Any] | list[dict[str, Any]] | None = None,
        notification: dict[str, Any] | None = None,
        seuil: str | None = None,
        motif: str = "routine",
    ) -> Any:
        """Depuis un cron : remet un message à une seule personne (fil de l'appli, notification courte sans donnée
        sensible ; un par jour, sauf `seuil` déclaré dans `seuils:` du manifeste). Rien n'est posté dans le groupe.
        Renvoie la publication (``coeur.spontane.Publication``), ou None si l'appli est injoignable."""
        from coeur import spontane

        if self.manifeste is not None and not personne_autorisee(self.manifeste, personne, self.config):
            raise ActionRefusee(f"« {self.capacite.titre if self.capacite else '?'} » n'est pas ouverte à {personne}.")
        try:
            return spontane.publier(personne, str(masquer_secrets(texte)), masquer_secrets(carte),
                                    notification=notification, motif=motif, seuil=seuil, capacite=self.nom,
                                    config=self.config)
        except Exception as exc:  # noqa: BLE001 - un cron ne s'arrête jamais sur la publication dans l'appli
            log.warning("message spontané non publié (%s)", exc.__class__.__name__)
            return None

    # --- cartes génératives (lot 4a, CONTRACT §14) ------------------------------------------------

    def carte_pour(self, nom_outil: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Décorateur : constructeur des cartes d'un outil de la capacité, ``(resultat, arguments) -> carte | [cartes]
        | None``, appelé seulement en canal web (appli). Les valeurs du coffre sont masquées dans la carte."""
        from tools import cartes

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            def constructeur(resultat: Any, arguments: dict[str, Any]) -> Any:
                return masquer_secrets(fn(resultat, arguments))

            cartes.pour(nom_outil)(constructeur)
            return fn

        return deco

    def ajouter_carte(
        self, type_: str, donnees: dict[str, Any], *, titre: str | None = None, actions: list[dict[str, Any]] | None = None
    ) -> None:
        """Depuis un outil : ajoute une carte au résultat (canal web seulement ; sans effet sinon)."""
        from tools import cartes

        cartes.ajouter([masquer_secrets(cartes.carte(type_, donnees, titre=titre, actions=actions))])


# --- import des modules d'une capacité ------------------------------------------


def _paquet(cap: Capacite) -> str:
    """Charge `capabilities.<nom>` comme paquet pointant sur le dossier de la capacité (imports relatifs possibles)."""
    if "capabilities" not in sys.modules:
        try:  # paquet-espace de noms `capabilities/` du dépôt (racine du dépôt dans sys.path)
            importlib.import_module("capabilities")
        except ImportError:
            racine_mod = types.ModuleType("capabilities")
            racine_mod.__path__ = [str(racine_capacites())]  # type: ignore[attr-defined]
            sys.modules["capabilities"] = racine_mod
    nom_paquet = f"capabilities.{cap.dossier.name}"
    existant = sys.modules.get(nom_paquet)
    chemins = [Path(c).resolve() for c in (getattr(existant, "__path__", None) or [])]
    if existant is None or cap.dossier.resolve() not in chemins:
        for nom_mod in [m for m in sys.modules if m == nom_paquet or m.startswith(nom_paquet + ".")]:
            del sys.modules[nom_mod]
        init = cap.dossier / "__init__.py"
        if init.exists():
            spec = importlib.util.spec_from_file_location(
                nom_paquet, init, submodule_search_locations=[str(cap.dossier)]
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[nom_paquet] = module
            spec.loader.exec_module(module)
        else:
            module = types.ModuleType(nom_paquet)
            module.__path__ = [str(cap.dossier)]  # type: ignore[attr-defined]
            sys.modules[nom_paquet] = module
    return nom_paquet


def importer_module(cap: Capacite, fichier: str, recharger: bool = True) -> types.ModuleType | None:
    """Importe `capabilities/<nom>/<fichier>.py` (None s'il est absent)."""
    if not (cap.dossier / f"{fichier}.py").exists():
        return None
    nom_module = f"{_paquet(cap)}.{fichier}"
    if recharger and nom_module in sys.modules:
        del sys.modules[nom_module]
    importlib.invalidate_caches()
    return importlib.import_module(nom_module)


# --- enregistrement des outils MCP --------------------------------------------------


def _apercu(valeur: Any, limite: int = 300) -> str:
    return str(valeur)[:limite]


def _args_journal(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v if isinstance(v, (int, float, bool, type(None))) else _apercu(v))
        for k, v in masquer_secrets(dict(kwargs)).items()
    }


def _journaliser(
    kind: str, resume: str, kwargs: dict[str, Any], statut: str, resultat: str, acteur: str | None = None
) -> None:
    try:
        from tools import db

        db.log_action(
            kind, resume, {"args": _args_journal(kwargs)}, statut, str(masquer_secrets(resultat))[:500], acteur=acteur
        )
    except Exception:
        log.exception("journalisation impossible")


# --- première validation humaine d'une capacité écrite par l'assistant (lot 7b) -----------------------------------
#
# Une capacité `origine: assistant` reste en essai jusqu'à la première validation humaine. Ses actions avec effet ne
# sont alors jamais exécutées par l'outil : le cadre crée lui-même l'action à valider (payload = arguments de l'outil,
# convention des capacités de l'atelier), seul un propriétaire du foyer peut la valider (coeur.confirmations), et sa
# réussite active la capacité (`validee_par` = ce propriétaire). Un échec la laisse en essai.

MESSAGE_PREMIERE = "première utilisation, à valider par un propriétaire du foyer"


def _libelle_outil(fn: Callable[..., Any], nom_outil: str) -> str:
    doc = (inspect.getdoc(fn) or "").strip().splitlines()
    if not doc or not doc[0].strip():
        return nom_outil
    phrase = re.split(r"(?<=[.;!?])\s", doc[0].strip(), maxsplit=1)[0].rstrip(".;")
    return phrase if len(phrase) <= 100 else phrase[:99].rstrip() + "…"


def proposer_premiere_validation(
    fn: Callable[..., Any], nom_outil: str, cap: Capacite, ctx: Contexte, kwargs: dict[str, Any]
) -> str:
    """Action avec effet d'une capacité de l'assistant en essai : `CONFIRMATION_REQUISE` créée par le cadre, sans
    appeler l'outil (aucun effet possible avant la validation humaine)."""
    assert cap.manifeste is not None
    arguments = ", ".join(f"{k} : {_apercu(v, 60)}" for k, v in kwargs.items())
    resume = f"{cap.manifeste.titre} ({MESSAGE_PREMIERE}) — {_libelle_outil(fn, nom_outil)}"
    if arguments:
        resume += f" ({arguments})"
    payload = {k: (v if isinstance(v, (int, float, bool, str, type(None), list, dict)) else str(v)) for k, v in kwargs.items()}
    return ctx.demander_confirmation(nom_outil, resume, payload)


def attend_premiere_validation(cap: Capacite | None, etat: dict[str, Any] | None = None) -> bool:
    """Capacité écrite par l'assistant, en essai, jamais validée par un humain."""
    if cap is None or cap.manifeste is None or cap.manifeste.origine != "assistant":
        return False
    etat = etat if etat is not None else lire_etat()
    return statut_effectif(cap, etat) == "essai" and not (etat.get(cap.nom) or {}).get("validee_le")


def premiere_validation_pour(kind: str, racine: Path | None = None, donnees: Path | None = None) -> Capacite | None:
    """Capacité en attente de première validation dont le préfixe couvre ce kind d'action (sinon None)."""
    cap = capacite_pour_kind(kind, racine)
    if cap is None or cap.manifeste is None or kind not in cap.manifeste.actions_avec_effet:
        return None
    return cap if attend_premiere_validation(cap, lire_etat(donnees)) else None


def autoriser_premiere_validation(
    kind: str, action_id: int, par: str, racine: Path | None = None, donnees: Path | None = None
) -> bool:
    """Appelé au clic « Valider » d'un propriétaire : autorise l'exécution de CETTE action en essai (etat.json).

    Renvoie False si le kind n'appartient pas à une capacité en attente de première validation."""
    cap = premiere_validation_pour(kind, racine, donnees)
    if cap is None:
        return False
    with _modifier_etat(donnees) as etat:
        etat.setdefault(cap.nom, {})["premiere_validation"] = {"action": int(action_id), "par": par, "quand": iso_now()}
    return True


def envelopper_outil(fn: Callable[..., Any], nom_outil: str, cap: Capacite, ctx: Contexte) -> Callable[..., Any]:
    """Ajoute à un outil de capacité : statut (inactif/essai), personnes autorisées, masquage des secrets,
    journalisation dans actions_log et conversion des erreurs en message pour le modèle."""
    assert cap.manifeste is not None
    manifeste = cap.manifeste
    avec_effet = nom_outil in manifeste.actions_avec_effet
    resume = f"{manifeste.titre} : {nom_outil}"

    def verifier() -> bool:
        """Contrôles d'accès ; True = action avec effet d'une capacité de l'assistant en essai, à proposer pour
        la première validation humaine sans exécuter le corps de l'outil."""
        statut = _statut_courant(cap, ctx.donnees)
        if statut not in STATUTS_CHARGES:
            raise ActionRefusee(f"« {manifeste.titre} » est désactivée.")
        if not personne_autorisee(manifeste, ctx.personne, ctx.config):
            raise ActionRefusee(f"« {manifeste.titre} » n'est pas ouverte à cette personne du foyer.")
        if avec_effet and statut == "essai":
            if manifeste.origine == "assistant":
                return True
            raise ActionRefusee(MESSAGE_ESSAI.format(titre=manifeste.titre))
        return False

    def proposer_premiere(kwargs: dict[str, Any]) -> str:
        return proposer_premiere_validation(fn, nom_outil, cap, ctx, kwargs)

    def echec(exc: BaseException, kwargs: dict[str, Any]) -> Exception:
        message = str(masquer_secrets(str(exc) or exc.__class__.__name__))
        log.warning("%s : %s", nom_outil, message)
        _journaliser(nom_outil, resume, kwargs, "error", message, ctx.acteur)
        return erreur_outil(message)

    def apres(resultat: Any, kwargs: dict[str, Any]) -> Any:
        resultat = masquer_secrets(resultat)
        confirmation = isinstance(resultat, str) and resultat.startswith(PREFIXE_CONFIRMATION)
        if avec_effet and not confirmation:
            log.warning("%s est une action avec effet mais n'a pas renvoyé CONFIRMATION_REQUISE", nom_outil)
        if not confirmation:  # une action à confirmer est déjà journalisée en `pending`
            _journaliser(nom_outil, resume, kwargs, "done", _apercu(resultat), ctx.acteur)
        return resultat

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def asynchrone(*args: Any, **kwargs: Any) -> Any:
            try:
                resultat = proposer_premiere(kwargs) if verifier() else await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                raise echec(exc, kwargs) from None
            return apres(resultat, kwargs)

        return asynchrone

    @functools.wraps(fn)
    def synchrone(*args: Any, **kwargs: Any) -> Any:
        try:
            resultat = proposer_premiere(kwargs) if verifier() else fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise echec(exc, kwargs) from None
        return apres(resultat, kwargs)

    return synchrone


class MCPCapacite:
    """Mandataire de l'objet FastMCP passé à `enregistrer(mcp, ctx)` : `@mcp.tool()` impose le préfixe de la
    capacité et enveloppe l'outil (`envelopper_outil`) avant de l'enregistrer sur le vrai serveur."""

    def __init__(self, mcp: Any, cap: Capacite, ctx: Contexte) -> None:
        self._mcp = mcp
        self._cap = cap
        self._ctx = ctx
        self.outils: list[str] = []

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if args and callable(args[0]) and not kwargs and len(args) == 1:  # @mcp.tool sans parenthèses
            return self.tool()(args[0])

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            nom_outil = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else None) or fn.__name__
            nom, prefixe = self._cap.nom, self._cap.prefixe
            if not accepte_nom(prefixe, nom_outil):
                raise ManifesteInvalide(
                    f"Outil « {nom_outil} » : les outils de « {nom} » doivent commencer par « {prefixe}_ »."
                )
            enveloppe = envelopper_outil(fn, nom_outil, self._cap, self._ctx)
            if getattr(self._mcp, "outils_synchrones", False):
                self._mcp.tool(*args, **kwargs)(enveloppe)
            else:  # vrai serveur MCP : outil synchrone exécuté hors de la boucle, identité de la requête copiée
                from tools import cartes, identite

                # Lot 4a : cartes génératives de l'outil en canal web (constructeurs déclarés par la capacité).
                self._mcp.tool(*args, **cartes.sans_sortie_structuree(kwargs))(
                    identite.en_thread(cartes.avec_cartes(enveloppe, nom_outil))
                )
            self.outils.append(nom_outil)
            return enveloppe

        return deco

    def __getattr__(self, nom: str) -> Any:
        return getattr(self._mcp, nom)


class MCPFactice:
    """Faux serveur MCP pour les tests de capacités : `outils[nom]` = fonction enregistrée (déjà enveloppée)."""

    outils_synchrones = True  # les fonctions sont appelées directement par les tests

    def __init__(self) -> None:
        self.outils: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if args and callable(args[0]) and not kwargs and len(args) == 1:
            return self.tool()(args[0])

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            nom = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else None) or fn.__name__
            self.outils[nom] = fn
            return fn

        return deco


def charger_capacite(
    cap: Capacite,
    mcp: Any,
    donnees: Path | None = None,
    config: dict[str, Any] | None = None,
    personne: str | None = None,
) -> list[str]:
    """Importe tools.py et appelle `enregistrer(mandataire, ctx)`. Renvoie les noms d'outils enregistrés."""
    if not cap.valide:
        raise ManifesteInvalide(f"Capacité « {cap.nom} » invalide : {'; '.join(cap.erreurs)}")
    module = importer_module(cap, "tools")
    enregistrer = getattr(module, "enregistrer", None)
    if not callable(enregistrer):
        raise ManifesteInvalide(f"Capacité « {cap.nom} » : tools.py doit définir enregistrer(mcp, ctx).")
    ctx = Contexte(donnees, config, personne, cap)
    mandataire = MCPCapacite(mcp, cap, ctx)
    enregistrer(mandataire, ctx)
    assert cap.manifeste is not None
    absentes = set(cap.manifeste.actions_avec_effet) - set(mandataire.outils)
    if absentes:
        log.warning("%s : actions avec effet sans outil correspondant : %s", cap.nom, ", ".join(sorted(absentes)))
    return mandataire.outils


def charger_actives(mcp: Any, racine: Path | None = None, donnees: Path | None = None) -> dict[str, list[str]]:
    """Appelé par `tools.server` après les outils de base : enregistre les outils des capacités actives ou en essai
    ouvertes à la personne courante. Une capacité en erreur est journalisée et ignorée (le serveur démarre)."""
    config = load_config()
    etat = lire_etat(donnees)
    personne = personne_courante(config)
    charges: dict[str, list[str]] = {}
    for cap in decouvrir(racine):
        if not cap.valide:
            log.warning("capacité %s ignorée : %s", cap.nom, "; ".join(cap.erreurs))
            continue
        assert cap.manifeste is not None
        if statut_effectif(cap, etat) not in STATUTS_CHARGES:
            continue
        if not personne_autorisee(cap.manifeste, personne, config):
            continue
        try:
            # config=None : le contexte relit config.yaml quand il change (serveur d'outils persistant, lot 7).
            charges[cap.nom] = charger_capacite(cap, mcp, donnees, None, personne)
        except Exception:
            log.exception("capacité %s non chargée", cap.nom)
    return charges


def decharger(mcp: Any, charges: dict[str, list[str]]) -> int:
    """Retire du serveur MCP les outils enregistrés par `charger_actives` (rechargement à chaud, lot 7).

    Renvoie le nombre d'outils retirés. SDK mcp 1.x et 2.x : `remove_tool`, sinon le registre interne."""
    retires = 0
    for noms in charges.values():
        for nom in noms:
            try:
                if callable(getattr(mcp, "remove_tool", None)):
                    mcp.remove_tool(nom)
                else:  # pragma: no cover - très anciennes versions du SDK
                    mcp._tool_manager._tools.pop(nom)
                retires += 1
            except Exception:  # noqa: BLE001 - outil déjà absent
                log.debug("outil %s déjà retiré", nom)
    return retires


# --- prompt ---------------------------------------------------------------------------


def capacites_chargees(racine: Path | None = None, donnees: Path | None = None) -> list[tuple[Capacite, str]]:
    etat = lire_etat(donnees)
    return [(c, s) for c in decouvrir(racine) if (s := statut_effectif(c, etat)) in STATUTS_CHARGES]


def fragments_prompt(racine: Path | None = None, donnees: Path | None = None) -> str:
    """Fragments CLAUDE.md des capacités actives ou en essai, précédés d'un sommaire (commandes, statut).

    Le bot l'ajoute au prompt système (`content/CLAUDE.md` rendu + fragments) ; `{{assistant.nom}}` y est
    substitué par le bot comme pour le reste du prompt."""
    chargees = capacites_chargees(racine, donnees)
    if not chargees:
        return ""
    lignes = [
        "# Capacités actives",
        "",
        (
            "Chaque capacité ajoute des outils préfixés par son nom. Une capacité « en essai » est en lecture seule : "
            "ses actions avec effet sont refusées tant qu'une personne du foyer ne l'a pas activée."
        ),
        "",
    ]
    for cap, statut in chargees:
        m = cap.manifeste
        assert m is not None
        details = [f"outils `{m.prefixe}_*`"]
        if statut == "essai":
            details.append("en essai")
        if m.personnes:
            details.append("réservée à " + ", ".join(m.personnes))
        lignes.append(f"- **{m.titre}** — {m.description} ({' ; '.join(details)})")
        for c in m.commandes_telegram:
            lignes.append(
                f"  - Commande Telegram `/{c.nom}` ({c.aide}) : un message qui commence par « /{c.nom} » "
                "se traite avec les outils de cette capacité."
            )
        if (cap.dossier / "onboarding.md").exists():
            lignes.append(f"  - Module de découverte : lis `{cap.dossier / 'onboarding.md'}` avec Read avant de le mener.")
        manquants = parametres_manquants(cap, donnees)
        if manquants:
            questions = " ; ".join(f"`{p.cle}` : {p.question}" for p in manquants)
            lignes.append(
                f"  - Réglages à compléter (pose les questions puis `capacites_parametrer(\"{m.nom}\", cle, valeur)`) : {questions}"
            )
    morceaux = ["\n".join(lignes)]
    for cap, _statut in chargees:
        fichier = cap.dossier / "CLAUDE.md"
        try:
            texte = fichier.read_text(encoding="utf-8").strip() if fichier.exists() else ""
        except OSError:
            texte = ""
        if texte:
            morceaux.append(texte)
    return "\n\n".join(morceaux) + "\n"


# --- smoke tests --------------------------------------------------------------------


def _avec_delai(fn: Callable[[], Any], delai_s: float) -> Any:
    """Exécute `fn` dans un thread démon (un smoke test bloqué n'empêche pas le processus de se terminer)."""
    resultat: dict[str, Any] = {}

    def cible() -> None:
        try:
            resultat["valeur"] = fn()
        except BaseException as exc:  # noqa: BLE001
            resultat["erreur"] = exc

    t = threading.Thread(target=cible, daemon=True, name="smoke")
    t.start()
    t.join(delai_s)
    if t.is_alive():
        raise TimeoutError(f"pas de réponse en {int(delai_s)} s")
    if "erreur" in resultat:
        raise resultat["erreur"]
    return resultat.get("valeur")


def smoke(cap: Capacite, donnees: Path | None = None, delai_s: float = TIMEOUT_SMOKE_S) -> dict[str, Any]:
    """Lance `smoke.verifier(ctx)` ; renvoie {"ok", "message"} (jamais d'exception)."""
    if not cap.valide:
        return {"ok": False, "message": "manifeste invalide : " + "; ".join(cap.erreurs)}
    try:
        module = importer_module(cap, "smoke")
        if module is None or not callable(getattr(module, "verifier", None)):
            return {"ok": True, "message": "aucun smoke test (smoke.py absent)"}
        ctx = Contexte(donnees, None, None, cap)
        brut = _avec_delai(lambda: module.verifier(ctx), delai_s)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(masquer_secrets(str(exc) or exc.__class__.__name__))[:300]}
    if isinstance(brut, dict):
        ok, message = bool(brut.get("ok")), str(brut.get("message") or "")
    else:
        ok, message = bool(brut), ""
    return {"ok": ok, "message": str(masquer_secrets(message))[:300] or ("OK" if ok else "échec")}


def smoke_tous(
    nom: str | None = None,
    racine: Path | None = None,
    donnees: Path | None = None,
    cache_minutes: float = 0,
) -> list[dict[str, Any]]:
    """Smoke test des capacités chargées (ou de `nom`, quel que soit son statut) ; mémorise le résultat dans etat.json.

    `cache_minutes` > 0 : un résultat plus récent est réutilisé (le healthcheck tourne toutes les 10 min,
    certains smoke tests lancent un navigateur)."""
    etat = lire_etat(donnees)
    if nom:
        cibles = [trouver(nom, racine)]
    else:
        cibles = [c for c in decouvrir(racine) if statut_effectif(c, etat) in STATUTS_CHARGES]
    resultats: list[dict[str, Any]] = []
    for cap in cibles:
        precedent = (etat.get(cap.nom) or {}).get("smoke") or {}
        res: dict[str, Any] | None = None
        if cache_minutes > 0 and precedent.get("quand"):
            try:
                age = (now() - datetime.fromisoformat(precedent["quand"])).total_seconds()
            except (TypeError, ValueError):
                age = None
            if age is not None and 0 <= age < cache_minutes * 60:
                res = {"ok": bool(precedent.get("ok")), "message": str(precedent.get("message") or ""),
                       "quand": precedent["quand"], "cache": True}
        if res is None:
            res = {**smoke(cap, donnees), "quand": iso_now(), "cache": False}
            try:
                with _modifier_etat(donnees) as e:
                    e.setdefault(cap.nom, {})["smoke"] = {"ok": res["ok"], "message": res["message"], "quand": res["quand"]}
            except OSError as exc:
                log.warning("résultat du smoke test non enregistré (%s)", exc.__class__.__name__)
        resultats.append({"nom": cap.nom, "titre": cap.titre, "statut": statut_effectif(cap, etat), **res})
    return resultats


# --- crons et actions confirmées ---------------------------------------------------------


@dataclass
class ResultatCron:
    """Ce qu'un cron renvoie : texte à poster dans le groupe (et à remettre dans l'appli avec `--publier`), carte,
    notification courte et seuil éventuels (`crons.executer` peut renvoyer un texte ou un dict de ces clés)."""

    texte: str | None
    carte: dict[str, Any] | None = None
    notification: dict[str, Any] | None = None
    seuil: str | None = None


def _resultat_cron(sortie: Any) -> ResultatCron | None:
    if isinstance(sortie, str):
        texte = str(masquer_secrets(sortie)).strip()
        return ResultatCron(texte) if texte else None
    if isinstance(sortie, dict):
        texte = str(masquer_secrets(str(sortie.get("texte") or ""))).strip() or None
        carte = sortie.get("carte") if isinstance(sortie.get("carte"), dict) else None
        notification = sortie.get("notification") if isinstance(sortie.get("notification"), dict) else None
        seuil = str(sortie["seuil"]) if sortie.get("seuil") else None
        if texte is None and carte is None:
            return None
        return ResultatCron(texte, masquer_secrets(carte) if carte else None, notification, seuil)
    return None


def executer_cron_detaille(
    nom_capacite: str, nom_cron: str, racine: Path | None = None, donnees: Path | None = None
) -> ResultatCron | None:
    """Exécute `crons.executer(nom_cron, ctx)` d'une capacité active ; renvoie son résultat (ou None)."""
    cap = trouver(nom_capacite, racine)
    statut = _statut_courant(cap, donnees)
    if statut != "actif":
        raise ActionRefusee(f"« {cap.titre} » n'est pas active (statut : {statut}) : cron « {nom_cron} » ignoré.")
    assert cap.manifeste is not None
    if nom_cron not in {c.nom for c in cap.manifeste.crons}:
        raise ErreurCapacite(f"Cron « {nom_cron} » non déclaré par « {cap.nom} ».")
    from tools import db

    kind = f"cron_{cap.nom}_{nom_cron}"
    if (_donnees(donnees) / "PAUSE").exists():
        db.log_action(kind, f"{cap.titre} : {nom_cron} suspendu (PAUSE)", status="skipped", acteur="cron")
        return None
    module = importer_module(cap, "crons")
    executer = getattr(module, "executer", None) if module else None
    if not callable(executer):
        raise ErreurCapacite(f"« {cap.nom} » : crons.py doit définir executer(nom_cron, ctx).")
    ctx = Contexte(donnees, None, None, cap)
    try:
        sortie = executer(nom_cron, ctx)
    except Exception as exc:  # noqa: BLE001
        message = str(masquer_secrets(str(exc) or exc.__class__.__name__))
        db.log_action(kind, f"{cap.titre} : {nom_cron} en échec", status="error", result=message[:500], acteur="cron")
        raise ErreurCapacite(message) from None
    resultat = _resultat_cron(sortie)
    db.log_action(kind, f"{cap.titre} : {nom_cron}", status="done",
                  result=((resultat.texte if resultat else None) or "")[:300], acteur="cron")
    return resultat


def executer_cron(nom_capacite: str, nom_cron: str, racine: Path | None = None, donnees: Path | None = None) -> str | None:
    """Exécute `crons.executer(nom_cron, ctx)` d'une capacité active ; renvoie le texte à poster (ou None)."""
    resultat = executer_cron_detaille(nom_capacite, nom_cron, racine, donnees)
    return resultat.texte if resultat else None


def publier_resultat_cron(nom_capacite: str, nom_cron: str, resultat: ResultatCron, racine: Path | None = None) -> int:
    """Remet le résultat d'un cron dans le fil de l'appli des personnes concernées (``coeur.spontane``) : carte du
    cron, sinon ``routine_resultat`` ; notification fournie, sinon « <titre> : nouveau résultat ». Renvoie le nombre
    de messages écrits ; jamais d'exception (l'envoi Telegram ne dépend pas de l'appli)."""
    try:
        from coeur import spontane

        cap = trouver(nom_capacite, racine)
        texte = resultat.texte or ""
        carte = resultat.carte or spontane.carte_routine(texte, cap.titre, cap.nom, nom_cron)
        notification = resultat.notification or {"titre": cap.titre, "corps": "Nouveau résultat de ta routine."}
        return len(spontane.publier_foyer(texte, carte, notification, capacite=cap.nom, seuil=resultat.seuil,
                                          motif="resultat"))
    except Exception as exc:  # noqa: BLE001
        log.warning("résultat du cron %s/%s non publié dans l'appli (%s)", nom_capacite, nom_cron,
                    exc.__class__.__name__)
        return 0


def capacite_pour_kind(kind: str, racine: Path | None = None) -> Capacite | None:
    """Capacité (valide) dont le préfixe couvre ce kind d'action ou ce nom d'outil (préfixe le plus long)."""
    candidates = [c for c in decouvrir(racine) if c.manifeste and accepte_nom(c.prefixe, kind)]
    return max(candidates, key=lambda c: len(c.prefixe)) if candidates else None


def _demandeur_action() -> str | None:
    """Personne qui a demandé l'action en cours d'exécution (`HOMEOS_ACTION_ID`, posé par `tools.runner`) : son
    identifiant personnel sert à l'action (ex. `planity/<personne>`), quel que soit le propriétaire qui valide."""
    action_id = os.environ.get("HOMEOS_ACTION_ID", "")
    if not action_id.isdigit():
        return None
    try:
        from tools import db

        action = db.get_action(int(action_id))
    except Exception:  # noqa: BLE001
        return None
    qui = _slug(str((action or {}).get("who") or ""))
    if not qui:
        return None
    for p in load_config().get("personnes") or []:
        if isinstance(p, dict) and qui in {_slug(str(p.get("linux_user") or "")), _slug(str(p.get("prenom") or ""))}:
            return _slug(str(p.get("linux_user") or "")) or None
    return None


def executeur_pour(kind: str, racine: Path | None = None, donnees: Path | None = None) -> Callable[[dict[str, Any]], str] | None:
    """Exécuteur d'une action confirmée dont le kind commence par `<nom>_` d'une capacité (préfixe le plus long).

    Utilisé par `tools.runner execute` : appelle `executer(kind, payload, ctx)` du tools.py de la capacité,
    seulement si elle est active (une capacité en essai ou désactivée n'agit pas)."""
    cap = capacite_pour_kind(kind, racine)
    if cap is None:
        return None

    def executeur(payload: dict[str, Any]) -> str:
        etat = lire_etat(donnees)
        statut = statut_effectif(cap, etat)
        premiere: dict[str, Any] | None = None
        if statut == "essai" and attend_premiere_validation(cap, etat):
            # Lot 7b : la première action avec effet d'une capacité de l'assistant, validée par un propriétaire.
            autorisation = (etat.get(cap.nom) or {}).get("premiere_validation") or {}
            action_id = os.environ.get("HOMEOS_ACTION_ID", "")
            if action_id.isdigit() and autorisation.get("action") == int(action_id):
                premiere = autorisation
        if statut == "essai" and premiere is None and attend_premiere_validation(cap, etat):
            raise ActionRefusee(f"« {cap.titre} » vient de l'atelier : sa première action doit être validée par un "
                                "propriétaire du foyer (bouton Valider).")
        if statut != "actif" and premiere is None:
            raise ActionRefusee(
                MESSAGE_ESSAI.format(titre=cap.titre) if statut == "essai" else f"« {cap.titre} » est désactivée."
            )
        module = importer_module(cap, "tools")
        executer = getattr(module, "executer", None)
        if not callable(executer):
            raise ErreurCapacite(f"« {cap.nom} » ne sait pas exécuter l'action « {kind} ».")
        jeton = _ACTION_VALIDEE.set(kind)
        try:
            resultat = executer(kind, payload, Contexte(donnees, None, _demandeur_action(), cap))
        except BaseException:
            if premiere is not None:  # échec : la capacité reste en essai, l'autorisation est consommée
                with _modifier_etat(donnees) as e:
                    (e.get(cap.nom) or {}).pop("premiere_validation", None)
            raise
        finally:
            _ACTION_VALIDEE.reset(jeton)
        texte = str(masquer_secrets(resultat))
        if premiere is not None:
            activer(cap.nom, racine, donnees, par=str(premiere.get("par") or "?"))
            with _modifier_etat(donnees) as e:
                (e.get(cap.nom) or {}).pop("premiere_validation", None)
            try:
                from tools import db

                db.log_action(f"capacite_validee_{cap.nom}", f"{cap.titre} : première validation humaine réussie",
                              {"action": premiere.get("action")}, status="done", acteur=str(premiere.get("par") or "?"))
            except Exception:
                log.exception("journalisation de la première validation impossible")
            texte += f"\n« {cap.titre} » est maintenant active : première action validée à la main."
        return texte

    return executeur


# --- informations (outils MCP de base, /capacites) ----------------------------------------


def _infos(cap: Capacite, etat: dict[str, Any]) -> dict[str, Any]:
    entree = etat.get(cap.nom) or {}
    m = cap.manifeste
    infos: dict[str, Any] = {
        "nom": cap.nom,
        "titre": cap.titre,
        "description": m.description if m else "",
        "statut": statut_effectif(cap, etat),
        "origine": m.origine if m else None,
        "personnes": list(m.personnes) if m else [],
        "commandes": [f"/{c.nom}" for c in m.commandes_telegram] if m else [],
        "dernier_smoke": entree.get("smoke"),
        "atelier": de_l_atelier(cap),
    }
    if not cap.valide:
        infos["erreurs"] = list(cap.erreurs)
    return infos


def _infos_parametres(cap: Capacite, donnees: Path | None) -> list[dict[str, Any]]:
    if cap.manifeste is None:
        return []
    valeurs = lire_parametres(cap.nom, donnees)
    return [
        {
            "cle": p.cle,
            "type": p.type,
            "question": p.question,
            "obligatoire": p.obligatoire,
            **({"choix": list(p.choix)} if p.choix else {}),
            "valeur": valeurs.get(p.cle, p.defaut),
            "renseigne": valeurs.get(p.cle) is not None,
        }
        for p in cap.manifeste.parametres
    ]


def lister(racine: Path | None = None, donnees: Path | None = None) -> list[dict[str, Any]]:
    etat = lire_etat(donnees)
    return [_infos(c, etat) for c in decouvrir(racine)]


def statut_detaille(nom: str, racine: Path | None = None, donnees: Path | None = None) -> dict[str, Any]:
    cap = trouver(nom, racine)
    etat = lire_etat(donnees)
    infos = _infos(cap, etat)
    entree = etat.get(cap.nom) or {}
    m = cap.manifeste
    infos.update(
        {
            "version": m.version if m else None,
            "statut_manifeste": m.statut if m else None,
            "validee_le": entree.get("validee_le"),
            "crons": [{"nom": c.nom, "quand": quand_cron(cap, c)} for c in m.crons] if m else [],
            "actions_avec_effet": list(m.actions_avec_effet) if m else [],
            "confirmation_toujours": list(m.confirmation_toujours) if m else [],
            "secrets": [
                {"cle": f"secret://{s.base}" + ("/<personne>" if s.par_personne else ""), "champs": list(s.champs),
                 "type": s.type}
                for s in m.secrets
            ] if m else [],
            "reseau": list(m.reseau) if m else [],
            "avertissements": list(cap.avertissements),
            "onboarding": (cap.dossier / "onboarding.md").exists(),
            "parametres": _infos_parametres(cap, donnees),
        }
    )
    return infos


def commandes_actives(racine: Path | None = None, donnees: Path | None = None) -> list[tuple[CommandeTelegram, Capacite]]:
    """Commandes Telegram déclarées par les capacités chargées (premier déclarant gagnant en cas de doublon)."""
    vues: set[str] = set()
    commandes: list[tuple[CommandeTelegram, Capacite]] = []
    for cap, _statut in capacites_chargees(racine, donnees):
        assert cap.manifeste is not None
        for c in cap.manifeste.commandes_telegram:
            if c.nom not in vues:
                vues.add(c.nom)
                commandes.append((c, cap))
    return commandes


def timers(racine: Path | None = None, donnees: Path | None = None) -> list[tuple[str, str, str]]:
    """(capacité, cron, OnCalendar) des crons des capacités actives, pour install.sh."""
    etat = lire_etat(donnees)
    config = load_config()
    lignes: list[tuple[str, str, str]] = []
    for cap in decouvrir(racine):
        if statut_effectif(cap, etat) != "actif" or cap.manifeste is None:
            continue
        for c in cap.manifeste.crons:
            lignes.append((cap.nom, c.nom, oncalendar(quand_cron(cap, c, config))))
    return lignes


RE_HOTE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def reseau(racine: Path | None = None, donnees: Path | None = None) -> list[str]:
    """Hôtes déclarés (`reseau:`) par les capacités chargées (actives ou en essai), triés, sans doublon.

    Lu par install.sh pour l'allowlist du proxy sortant (`--filtrer-sortant`). Un hôte invalide (schéma, port,
    chemin, joker ailleurs qu'en tête) est ignoré avec un avertissement : seul `*.domaine` est accepté.
    """
    hotes: set[str] = set()
    etat = lire_etat(donnees)
    for cap, _statut in capacites_chargees(racine, donnees):
        if cap.manifeste is None:
            continue
        # Lot 7b : les hôtes d'une capacité écrite par l'assistant ne sortent qu'une fois approuvés par un propriétaire.
        approuves = set(hotes_approuves(cap.nom, donnees)) if cap.manifeste.origine == "assistant" else None
        # Capacité du dépôt : hôtes approuvés en plus par un propriétaire (réseau ajouté à la demande, lot transports).
        supplementaires = [] if approuves is not None else hotes_approuves(cap.nom, donnees)
        for brut in [*cap.manifeste.reseau, *supplementaires]:
            hote = str(brut).strip().lower().rstrip(".")
            if not RE_HOTE.match(hote):
                log.warning("capacité %s : hôte réseau ignoré « %s » (attendu : nom d'hôte ou *.domaine)", cap.nom, brut)
            elif approuves is None or hote in approuves:
                hotes.add(hote)
    # Atelier en cours : le site approuvé à la demande sert à l'exploration et au smoke test avant l'installation.
    for entree in etat.values():
        if isinstance(entree, dict) and (entree.get("atelier") or {}).get("en_cours"):
            hotes.update(h for h in entree.get("reseau_approuve") or [] if isinstance(h, str) and RE_HOTE.match(h))
    return sorted(hotes)


def hotes_approuves(nom: str, donnees: Path | None = None) -> list[str]:
    """Hôtes réseau approuvés par un propriétaire pour une capacité de l'atelier (etat.json, `reseau_approuve`)."""
    entree = lire_etat(donnees).get(nom) or {}
    return [h for h in entree.get("reseau_approuve") or [] if isinstance(h, str)]


def hotes_autorises(cap: Capacite, donnees: Path | None = None) -> list[str]:
    """Hôtes que la capacité peut joindre : son manifeste, restreint aux hôtes approuvés si l'assistant l'a écrite."""
    if cap.manifeste is None:
        return []
    declares = [str(h).strip().lower().rstrip(".") for h in cap.manifeste.reseau]
    if cap.manifeste.origine != "assistant":
        return declares + [h for h in hotes_approuves(cap.nom, donnees) if h not in declares]
    approuves = set(hotes_approuves(cap.nom, donnees))
    return [h for h in declares if h in approuves]


# --- cycle de vie (CLI) -------------------------------------------------------------------


def _qui() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("HOMEOS_WHO") or getpass.getuser()


def activer(nom: str, racine: Path | None = None, donnees: Path | None = None, par: str | None = None) -> dict[str, Any]:
    """`enable` : statut actif + validation humaine (lève l'essai d'une capacité écrite par l'assistant).

    `par` : auteur de la validation (administration par la conversation) ; défaut : utilisateur courant."""
    cap = trouver(nom, racine)
    if not cap.valide:
        raise ManifesteInvalide(f"« {cap.nom} » ne peut pas être activée : {'; '.join(cap.erreurs)}")
    with _modifier_etat(donnees) as etat:
        entree = etat.setdefault(cap.nom, {})
        entree.update({"statut": "actif", "validee_le": iso_now(), "validee_par": par or _qui()})
    _creer_dossier(_creer_dossier(_donnees(donnees) / "capacites") / cap.nom)
    return statut_detaille(cap.nom, racine, donnees)


def desactiver(nom: str, racine: Path | None = None, donnees: Path | None = None) -> dict[str, Any]:
    """`disable` : statut inactif ; les données (`data/capacites/<nom>/`) sont conservées."""
    cap = trouver(nom, racine)
    with _modifier_etat(donnees) as etat:
        etat.setdefault(cap.nom, {})["statut"] = "inactif"
    return statut_detaille(cap.nom, racine, donnees)


def retirer(nom: str, par: str | None = None, donnees: Path | None = None) -> dict[str, Any]:
    """Retire une capacité fabriquée par l'atelier (administration, lot 7b) : son code est archivé hors des capacités
    (`<etat>/atelier/retirees/<nom>-<horodatage>/`), ses données sont conservées. Refusé pour une capacité du dépôt
    (la désactiver à la place). Renvoie {nom, titre, archive, avant} (avant = entrée de etat.json)."""
    from tools.common import etat_dir

    cap = trouver(nom)
    if not de_l_atelier(cap):
        raise ActionRefusee(f"« {cap.titre} » fait partie du système : je peux la désactiver, pas la retirer.")
    archives = etat_dir() / "atelier" / "retirees"
    archives.mkdir(parents=True, exist_ok=True)
    archive = archives / f"{cap.dossier.name}-{now().strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(cap.dossier), str(archive))
    with _modifier_etat(donnees) as etat:
        avant = etat.pop(cap.nom, None)
    log.info("capacité %s retirée par %s (archive %s)", cap.nom, par or _qui(), archive)
    return {"nom": cap.nom, "titre": cap.titre, "archive": str(archive), "avant": avant}


def supprimer(nom: str, avec_donnees: bool = False, racine: Path | None = None, donnees: Path | None = None) -> list[str]:
    """`remove` : supprime le dossier de la capacité (et `data/capacites/<nom>/` avec `--donnees`)."""
    cap = trouver(nom, racine)
    supprimes = [str(cap.dossier)]
    shutil.rmtree(cap.dossier)
    with _modifier_etat(donnees) as etat:
        etat.pop(cap.nom, None)
    if avec_donnees:
        dossier = _donnees(donnees) / "capacites" / cap.nom
        if dossier.exists():
            shutil.rmtree(dossier)
            supprimes.append(str(dossier))
    return supprimes


# --- squelette (`new`) ------------------------------------------------------------------------

_GABARIT_MANIFESTE = """\
# Manifeste de la capacité « {titre} » (contrat : CONTRACT.md §9)
nom: {nom}
titre: {titre_yaml}
description: {description_yaml}
version: 1
statut: essai             # actif | inactif | essai (essai = lecture seule, actions avec effet refusées)
origine: {origine}            # humain | assistant
personnes: []             # [] = tout le foyer ; sinon ex. [alex]
secrets: []               # ex. - {{cle: {nom}/alex, champs: [identifiant, mot_de_passe], par_personne: true}}
crons: []                 # ex. - {{nom: rafraichir, quand: "Mon 04:00"}}  (+ crons.py)
actions_avec_effet: []    # outils qui renvoient CONFIRMATION_REQUISE (ctx.demander_confirmation)
confirmation_toujours: [] # parmi eux, ceux jamais exécutés sans bouton, même en validation automatique
reseau: []                # hôtes contactés (documentaire)
commandes_telegram: []    # ex. - {{nom: {nom}, aide: Texte court}}
parametres: []            # ce qui dépend du foyer (jamais en dur ici), ex. - {{cle: arret, type: texte, question: "Quel arrêt ?", obligatoire: true}}
"""

_GABARIT_TOOLS = '''\
"""Capacité « {titre} » : outils MCP (préfixe obligatoire : `{nom}_`)."""

from __future__ import annotations

from typing import Any


def enregistrer(mcp: Any, ctx: Any) -> None:
    """Enregistre les outils de la capacité. `ctx` : donnees, config, personne, secret(cle), journal(texte)…"""

    @mcp.tool()
    def {nom}_exemple(texte: str) -> str:
        """Outil d'exemple de la capacité « {titre} » : renvoie le texte reçu. À remplacer."""
        return f"{titre} a bien reçu : {{texte}}"


def executer(action: str, payload: dict[str, Any], ctx: Any) -> str:
    """Exécute une action validée par bouton (créée par ctx.demander_confirmation). Optionnel."""
    raise ValueError(f"Action inconnue : {{action}}")
'''

_GABARIT_SMOKE = '''\
"""Smoke test de la capacité « {titre} » (lancé par le healthcheck)."""

from __future__ import annotations

from typing import Any


def verifier(ctx: Any) -> dict[str, Any]:
    return {{"ok": True, "message": "squelette : rien à vérifier"}}
'''

_GABARIT_CLAUDE = """\
## {titre}

{description}

- **`{nom}_exemple`** — outil d'exemple, à remplacer : dis ici quand l'utiliser, avec des phrases d'exemple.
"""

_GABARIT_TEST = '''\
"""Tests de la capacité « {titre} » (générés par `python -m tools.capacites new`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import capacites

DOSSIER = Path(__file__).resolve().parent.parent


def test_manifeste_valide() -> None:
    cap = capacites.lire_capacite(DOSSIER)
    assert cap.valide, cap.erreurs


def test_outil_exemple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path))
    mcp = capacites.MCPFactice()
    outils = capacites.charger_capacite(capacites.lire_capacite(DOSSIER), mcp, donnees=tmp_path)
    assert outils == ["{nom}_exemple"]
    assert "bonjour" in mcp.outils["{nom}_exemple"](texte="bonjour")


def test_smoke() -> None:
    assert capacites.smoke(capacites.lire_capacite(DOSSIER))["ok"]
'''

_GABARIT_README = """\
# Capacité « {titre} »

{description}

Fichiers : `capability.yaml` (manifeste), `tools.py` (outils MCP), `CLAUDE.md` (consignes pour l'assistant),
`smoke.py` (vérification du healthcheck), `tests/`. Optionnels : `onboarding.md`, `crons.py`.

Cycle de vie : créée en `essai` (lecture seule) ; `python -m tools.capacites enable {nom}` pour l'activer.
Notes d'exploration du site, décisions : ici.
"""


def creer(
    nom: str,
    titre: str,
    description: str = "",
    origine: str = "humain",
    racine: Path | None = None,
) -> Path:
    """`new` : génère le squelette complet d'une capacité (statut essai, un outil d'exemple, un test)."""
    import yaml

    if not RE_NOM.fullmatch(nom or ""):
        raise ErreurCapacite("Nom invalide : minuscules, chiffres et _ uniquement (ex. planity).")
    if origine not in ORIGINES:
        raise ErreurCapacite(f"Origine invalide : {' ou '.join(ORIGINES)}.")
    if not titre.strip():
        raise ErreurCapacite("Titre obligatoire (--titre).")
    racine = Path(racine) if racine is not None else racine_capacites()
    dossier = racine / nom
    if dossier.exists():
        raise ErreurCapacite(f"La capacité « {nom} » existe déjà ({dossier}).")
    description = description.strip() or f"{titre.strip()} (à décrire)."
    valeurs = {
        "nom": nom,
        "titre": titre.strip(),
        "description": description,
        "origine": origine,
        "titre_yaml": yaml.safe_dump(titre.strip(), allow_unicode=True, default_style='"').strip(),
        "description_yaml": yaml.safe_dump(description, allow_unicode=True, default_style='"', width=10_000).strip(),
    }
    fichiers = {
        "capability.yaml": _GABARIT_MANIFESTE,
        "tools.py": _GABARIT_TOOLS,
        "smoke.py": _GABARIT_SMOKE,
        "CLAUDE.md": _GABARIT_CLAUDE,
        "README.md": _GABARIT_README,
        f"tests/test_{nom}.py": _GABARIT_TEST,
    }
    (dossier / "tests").mkdir(parents=True)
    for chemin, gabarit in fichiers.items():
        (dossier / chemin).write_text(gabarit.format(**valeurs), encoding="utf-8")
    cap = lire_capacite(dossier)
    if not cap.valide:  # garde-fou : le squelette doit toujours être valide
        shutil.rmtree(dossier)
        raise ErreurCapacite("Squelette invalide : " + "; ".join(cap.erreurs))
    return dossier


# --- partage : export / install --------------------------------------------------------------

# Jamais exportés : données runtime, caches, fichiers cachés, environnements, secrets éventuels.
EXCLUS_EXPORT = frozenset({"__pycache__", "data", "node_modules", ".git", ".pytest_cache", ".ruff_cache", ".venv"})
SUFFIXES_EXCLUS = (".pyc", ".pyo", ".env", ".tmp", ".log", ".sqlite", ".key", ".pem")


def _exportable(rel: Path) -> bool:
    if any(part in EXCLUS_EXPORT or part.startswith(".") for part in rel.parts):
        return False
    nom = rel.name.casefold()
    return not (nom.endswith(SUFFIXES_EXCLUS) or nom.startswith("secret") or nom in ("parametres.yaml", "etat.json"))


def fichiers_exportables(cap: Capacite) -> list[Path]:
    """Chemins relatifs exportés : manifeste, code, tests, README, CLAUDE.md, onboarding… (jamais data/ ni secrets)."""
    return sorted(
        f.relative_to(cap.dossier)
        for f in cap.dossier.rglob("*")
        if f.is_file() and not f.is_symlink() and _exportable(f.relative_to(cap.dossier))
    )


def _sha256(chemin: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with chemin.open("rb") as fh:
        for bloc in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def exporter(nom: str, sortie: Path | None = None, racine: Path | None = None) -> tuple[Path, Path]:
    """Archive `<nom>-v<version>.tar.gz` (dossier racine `<nom>/`) + `<archive>.sha256` (format sha256sum).

    Pas de signature cryptographique pour l'instant (à venir) : l'empreinte détecte une archive abîmée ou
    modifiée en route, pas l'identité de l'auteur."""
    import tarfile

    cap = trouver(nom, racine)
    if not cap.valide or cap.manifeste is None:
        raise ManifesteInvalide(f"« {cap.nom} » invalide, export impossible : {'; '.join(cap.erreurs)}")
    sortie = Path(sortie) if sortie is not None else Path.cwd()
    sortie.mkdir(parents=True, exist_ok=True)
    archive = sortie / f"{cap.nom}-v{cap.manifeste.version}.tar.gz"

    def anonymiser(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(archive, "w:gz") as tar:
        for rel in fichiers_exportables(cap):
            tar.add(cap.dossier / rel, arcname=f"{cap.nom}/{rel.as_posix()}", recursive=False, filter=anonymiser)
    empreinte = archive.with_name(archive.name + ".sha256")
    empreinte.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive, empreinte


def resume_permissions(cap: Capacite) -> str:
    """Ce que la capacité demande, à montrer avant installation."""
    m = cap.manifeste
    assert m is not None
    def liste(valeurs: Iterable[str]) -> str:
        return ", ".join(valeurs) or "aucun"
    secrets = [f"{s.base}{'/<personne>' if s.par_personne else ''} ({', '.join(s.champs)})" for s in m.secrets]
    lignes = [
        f"Capacité « {m.titre} » ({m.nom}), version {m.version}, écrite par : {m.origine}",
        f"  {m.description}",
        f"  Identifiants demandés (coffre) : {liste(secrets)}",
        f"  Accès réseau : {liste(m.reseau)}",
        f"  Actions avec effet (toujours à valider par bouton) : {liste(m.actions_avec_effet)}",
        f"  Tâches programmées : {liste(f'{c.nom} ({c.quand})' for c in m.crons)}",
        f"  Commandes Telegram : {liste('/' + c.nom for c in m.commandes_telegram)}",
        f"  Réservée à : {liste(m.personnes) if m.personnes else 'tout le foyer'}",
        f"  Réglages demandés à l'onboarding : {liste(p.question for p in m.parametres)}",
        "  Installée en essai (lecture seule) ; signature de l'auteur : non vérifiée (à venir).",
    ]
    return "\n".join(lignes)


def _verifier_empreinte(archive: Path) -> bool | None:
    """True/False si un fichier `.sha256` accompagne l'archive, None sinon."""
    empreinte = archive.with_name(archive.name + ".sha256")
    if not empreinte.exists():
        return None
    attendu = empreinte.read_text(encoding="utf-8").split()[:1]
    return bool(attendu) and attendu[0].lower() == _sha256(archive)


def installer(
    archive: Path,
    confirmer: Callable[[str], bool] | None = None,
    racine: Path | None = None,
    donnees: Path | None = None,
) -> Capacite:
    """Installe une archive exportée en statut `essai`, après `confirmer(résumé des permissions)`."""
    import tarfile
    import tempfile

    archive = Path(archive)
    if not archive.is_file():
        raise ErreurCapacite(f"Archive introuvable : {archive}")
    empreinte_ok = _verifier_empreinte(archive)
    if empreinte_ok is False:
        raise ErreurCapacite("Empreinte SHA256 différente : archive abîmée ou modifiée, installation refusée.")
    racine = Path(racine) if racine is not None else racine_capacites()
    with tempfile.TemporaryDirectory(prefix="capacite-") as tmp:
        try:
            with tarfile.open(archive, "r:gz") as tar:
                membres = tar.getmembers()
                tetes = {Path(m.name).parts[0] for m in membres if Path(m.name).parts}
                if len(tetes) != 1 or not RE_NOM.fullmatch(next(iter(tetes))):
                    raise ErreurCapacite("Archive invalide : un seul dossier <nom>/ attendu à la racine.")
                if any(not (m.isfile() or m.isdir()) for m in membres):
                    raise ErreurCapacite("Archive invalide : liens et fichiers spéciaux refusés.")
                tar.extractall(tmp, filter="data")  # refuse chemins absolus et « .. »
        except (tarfile.TarError, OSError) as exc:
            raise ErreurCapacite(f"Archive illisible ({exc.__class__.__name__}).") from None
        nom = next(iter(tetes))
        cap = lire_capacite(Path(tmp) / nom)
        if not cap.valide:
            raise ManifesteInvalide(f"Capacité invalide : {'; '.join(cap.erreurs)}")
        cible = racine / nom
        if cible.exists():
            raise ErreurCapacite(f"La capacité « {nom} » est déjà installée ({cible}) : `remove` d'abord.")
        texte = resume_permissions(cap)
        if empreinte_ok is None:
            texte += "\n  Attention : aucun fichier .sha256 fourni, intégrité non vérifiée."
        if confirmer is not None and not confirmer(texte):
            raise ErreurCapacite("Installation annulée.")
        racine.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(tmp) / nom, cible)
    with _modifier_etat(donnees) as etat:
        etat[nom] = {"statut": "essai", "installee_le": iso_now(), "source": archive.name}
    return lire_capacite(cible)


# --- CLI ---------------------------------------------------------------------------------


def _imprimer_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def _fmt_smoke(smoke_info: dict[str, Any] | None) -> str:
    if not smoke_info:
        return "smoke —"
    q = str(smoke_info.get("quand") or "")
    quand = f"{q[8:10]}/{q[5:7]} {q[11:16]}" if len(q) >= 16 else ""
    return f"smoke {'OK' if smoke_info.get('ok') else 'ÉCHEC'} {quand}".strip()


def _une_ligne(texte: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(texte)).strip()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o002)
    parser = argparse.ArgumentParser(prog="python -m tools.capacites", description="Capacités du foyer (plugins)")
    sub = parser.add_subparsers(dest="commande", required=True)
    p_list = sub.add_parser("list", help="capacités présentes et leur statut")
    p_list.add_argument("--json", action="store_true")
    p_statut = sub.add_parser("statut", help="détail d'une capacité (JSON)")
    p_statut.add_argument("nom")
    sub.add_parser("enable", help="active (et valide) une capacité").add_argument("nom")
    sub.add_parser("disable", help="désactive une capacité (données conservées)").add_argument("nom")
    p_rm = sub.add_parser("remove", help="supprime une capacité")
    p_rm.add_argument("nom")
    p_rm.add_argument("--donnees", action="store_true", help="supprime aussi data/capacites/<nom>/")
    p_rm.add_argument("--oui", action="store_true", help="pas de confirmation interactive")
    p_smoke = sub.add_parser("smoke", help="smoke tests (code 1 si un échec)")
    p_smoke.add_argument("nom", nargs="?")
    p_smoke.add_argument("--tsv", action="store_true", help="sortie nom<TAB>ok<TAB>titre<TAB>message (healthcheck)")
    p_smoke.add_argument("--cache", type=float, default=0, metavar="MINUTES", help="réutilise un résultat plus récent")
    p_new = sub.add_parser("new", help="crée le squelette d'une capacité")
    p_new.add_argument("nom")
    p_new.add_argument("--titre", required=True)
    p_new.add_argument("--description", default="")
    p_new.add_argument("--origine", choices=ORIGINES, default="humain")
    p_cron = sub.add_parser("cron", help="exécute un cron (timers homeos-cap-<nom>-<cron>)")
    p_cron.add_argument("nom")
    p_cron.add_argument("cron")
    p_cron.add_argument("--publier", action="store_true", help="remet aussi le résultat dans le fil de l'appli")
    sub.add_parser("timers", help="crons des capacités actives : nom<TAB>cron<TAB>OnCalendar (install.sh)")
    sub.add_parser("migrer", help="reprend dans etat.json l'état des capacités remplacées (`remplace:`, install.sh)")
    sub.add_parser("prompt", help="fragments de prompt des capacités chargées")
    sub.add_parser("reseau", help="hôtes réseau déclarés par les capacités chargées, un par ligne (install.sh)")
    p_param = sub.add_parser("parametre", help="affiche ou renseigne un paramètre (data/capacites/<nom>/parametres.yaml)")
    p_param.add_argument("nom")
    p_param.add_argument("cle", nargs="?")
    p_param.add_argument("valeur", nargs="?")
    p_export = sub.add_parser("export", help="archive .tar.gz partageable (+ .sha256), sans data/ ni secrets")
    p_export.add_argument("nom")
    p_export.add_argument("--sortie", type=Path, default=None, help="dossier de sortie (défaut : dossier courant)")
    p_install = sub.add_parser("install", help="installe une archive en statut essai (après confirmation)")
    p_install.add_argument("archive", type=Path)
    p_install.add_argument("--oui", action="store_true", help="pas de confirmation interactive")
    args = parser.parse_args(argv)

    try:
        if args.commande == "list":
            infos = lister()
            if args.json:
                _imprimer_json(infos)
                return 0
            if not infos:
                sys.stdout.write(f"Aucune capacité dans {racine_capacites()}.\n")
                return 0
            for i in infos:
                personnes = ", ".join(i["personnes"]) or "tout le foyer"
                commandes = " ".join(i["commandes"])
                sys.stdout.write(
                    f"{i['nom']:<18} {i['statut']:<9} {i['origine'] or '—'!s:<9} {_fmt_smoke(i['dernier_smoke']):<24} "
                    f"{i['titre']} — {personnes}{(' — ' + commandes) if commandes else ''}\n"
                )
                for e in i.get("erreurs", []):
                    sys.stdout.write(f"{'':<18} ! {e}\n")
            return 0
        if args.commande == "statut":
            _imprimer_json(statut_detaille(args.nom))
            return 0
        if args.commande == "migrer":
            for ligne in migrer_remplacees():
                sys.stdout.write(f"{ligne}\n")
            return 0
        if args.commande == "enable":
            info = activer(args.nom)
            sys.stdout.write(f"« {info['titre']} » activée.\n")
            if info["crons"]:
                sys.stdout.write(
                    "Crons déclarés : timers créés par homeos-appliquer-config.path "
                    "(sinon : sudo homeos-appliquer-config).\n"
                )
            return 0
        if args.commande == "disable":
            info = desactiver(args.nom)
            sys.stdout.write(f"« {info['titre']} » désactivée (données conservées).\n")
            if info["crons"]:
                sys.stdout.write(
                    "Crons déclarés : timers retirés par homeos-appliquer-config.path "
                    "(sinon : sudo homeos-appliquer-config).\n"
                )
            return 0
        if args.commande == "remove":
            cap = trouver(args.nom)
            if not args.oui:
                cible = f"{cap.dossier}" + (" et ses données" if args.donnees else "")
                reponse = input(f"Supprimer définitivement {cible} ? Tape OUI : ")
                if reponse.strip() != "OUI":
                    sys.stderr.write("Abandon.\n")
                    return 1
            for chemin in supprimer(args.nom, args.donnees):
                sys.stdout.write(f"supprimé : {chemin}\n")
            return 0
        if args.commande == "smoke":
            resultats = smoke_tous(args.nom, cache_minutes=args.cache)
            for r in resultats:
                if args.tsv:
                    sys.stdout.write(f"{r['nom']}\t{1 if r['ok'] else 0}\t{_une_ligne(r['titre'])}\t{_une_ligne(r['message'])}\n")
                else:
                    sys.stdout.write(f"{'OK   ' if r['ok'] else 'ÉCHEC'} {r['nom']} — {r['message']}{' (cache)' if r['cache'] else ''}\n")
            return 0 if all(r["ok"] for r in resultats) else 1
        if args.commande == "new":
            dossier = creer(args.nom, args.titre, args.description, args.origine)
            sys.stdout.write(
                f"Capacité créée dans {dossier} (statut essai).\n"
                f"Tests : pytest {dossier / 'tests'} ; activation : python -m tools.capacites enable {args.nom}\n"
            )
            return 0
        if args.commande == "cron":
            resultat = executer_cron_detaille(args.nom, args.cron)
            if resultat is not None and args.publier:
                publier_resultat_cron(args.nom, args.cron, resultat)
            if resultat is not None and resultat.texte:
                sys.stdout.write(resultat.texte + "\n")
            return 0
        if args.commande == "timers":
            for nom, cron, cal in timers():
                sys.stdout.write(f"{nom}\t{cron}\t{cal}\n")
            return 0
        if args.commande == "reseau":
            for hote in reseau():
                sys.stdout.write(hote + "\n")
            return 0
        if args.commande == "prompt":
            sys.stdout.write(fragments_prompt())
            return 0
        if args.commande == "parametre":
            if args.cle is not None and args.valeur is None:
                parser.error("parametre : valeur manquante (parametre <nom> <cle> <valeur>)")
            if args.cle is not None:
                definir_parametre(args.nom, args.cle, args.valeur)
            _imprimer_json(statut_detaille(args.nom)["parametres"])
            return 0
        if args.commande == "export":
            archive, empreinte = exporter(args.nom, args.sortie)
            sys.stdout.write(f"{archive}\n{empreinte}\n")
            return 0
        if args.commande == "install":

            def confirmer(texte: str) -> bool:
                sys.stdout.write(texte + "\n")
                if args.oui:
                    return True
                try:
                    return input("Installer en essai ? [o/N] ").strip().casefold() in ("o", "oui")
                except EOFError:
                    return False

            cap = installer(args.archive, confirmer)
            sys.stdout.write(
                f"« {cap.titre} » installée en essai dans {cap.dossier}. "
                f"Activation : python -m tools.capacites enable {cap.nom}\n"
            )
            return 0
    except ErreurCapacite as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    parser.error("commande inconnue")
    return 2


if __name__ == "__main__":
    sys.exit(main())
