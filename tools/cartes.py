"""Cartes génératives côté outils (lot 4a, CONTRACT §14) : une ligne ``CARTE:<json>`` dans le résultat d'un outil.

Le canal de la conversation arrive au serveur d'outils par l'en-tête ``X-Homeos-Canal`` (wrapper ``homeos-claude
--homeos-canal web``, ou configuration MCP du bot pour une personne en clé API), placé ici dans une ``ContextVar`` le
temps de la requête (comme l'identité, ``tools.identite``) ; en stdio, la variable ``HOMEOS_CANAL`` en tient lieu.

- Hors canal web (Telegram, crons, CLI) : rien ne change, aucun outil ne renvoie de carte.
- Canal web : après un appel d'outil réussi, le constructeur enregistré pour cet outil (``CONSTRUCTEURS``) produit
  zéro, une ou plusieurs cartes. En HTTP, elles sont gardées pour la requête et ajoutées par
  ``tools.serveur_http`` comme bloc texte supplémentaire du résultat (le résultat de l'outil et son schéma de
  sortie restent intacts) ; en stdio, elles sont ajoutées à la fin d'un résultat textuel.
- Le cœur (``coeur.cartes``) extrait ces lignes du flux ``stream-json`` (résultats d'outils) et les diffuse à
  l'appli ; le modèle les voit aussi, et ses consignes lui disent de ne pas les recopier.

Une capacité fournit ses cartes dans ``tools.py`` avec ``@ctx.carte_pour("<outil>")`` (constructeur
``(resultat, arguments) -> carte | [cartes] | None``) ou, depuis un outil, ``ctx.ajouter_carte(type, donnees,
actions=…)``. Un constructeur en erreur n'empêche jamais l'outil de répondre.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import os
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from tools.common import get_logger

log = get_logger("homeos.cartes")

PREFIXE = "CARTE:"
ENTETE_CANAL = "x-homeos-canal"
CANAUX = ("telegram", "web")
TAILLE_MAX_CARTE = 64 * 1024  # une carte plus grosse est ignorée (journalisé)

_CANAL: contextvars.ContextVar[str | None] = contextvars.ContextVar("homeos_canal", default=None)
# Cartes produites pendant la requête HTTP en cours (None hors requête : mode stdio, tests, CLI).
_CARTES_REQUETE: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "homeos_cartes", default=None
)

Constructeur = Callable[[Any, dict[str, Any]], "dict[str, Any] | list[dict[str, Any]] | None"]
CONSTRUCTEURS: dict[str, Constructeur] = {}

_RE_CONFIRMATION = re.compile(r"CONFIRMATION_REQUISE:(\d+):([^\n`]*)")


# --- canal ---------------------------------------------------------------------------------------------------------


def canal_courant() -> str:
    """« web » ou « telegram » : en-tête de la requête HTTP, sinon ``HOMEOS_CANAL``, sinon « telegram »."""
    valeur = _CANAL.get() or os.environ.get("HOMEOS_CANAL", "") or "telegram"
    valeur = valeur.strip().lower()
    return valeur if valeur in CANAUX else "telegram"


@contextmanager
def requete(canal: str | None) -> Iterator[list[dict[str, Any]]]:
    """Le temps d'une requête HTTP : canal annoncé et liste des cartes produites (lue par le serveur HTTP)."""
    valeur = (canal or "").strip().lower()
    jeton_canal = _CANAL.set(valeur if valeur in CANAUX else None)
    cartes: list[dict[str, Any]] = []
    jeton_cartes = _CARTES_REQUETE.set(cartes)
    try:
        yield cartes
    finally:
        _CARTES_REQUETE.reset(jeton_cartes)
        _CANAL.reset(jeton_canal)


# --- construction ----------------------------------------------------------------------------------------------------


def carte(
    type_: str,
    donnees: dict[str, Any],
    *,
    titre: str | None = None,
    actions: list[dict[str, Any]] | None = None,
    id_: str | None = None,
) -> dict[str, Any]:
    """Carte au format du contrat : ``{type, id, titre?, donnees, actions}``."""
    sortie: dict[str, Any] = {"type": type_, "id": id_ or f"c-{secrets.token_hex(6)}", "donnees": donnees,
                              "actions": list(actions or [])}
    if titre:
        sortie["titre"] = titre
    return sortie


def action(libelle: str, nom: str, args: dict[str, Any] | None = None, confirmation: int | None = None) -> dict[str, Any]:
    """Bouton d'une carte : ``{libelle, action, args, confirmation?}`` (``confirmation`` = id de l'action en attente)."""
    sortie: dict[str, Any] = {"libelle": libelle, "action": nom, "args": dict(args or {})}
    if confirmation is not None:
        sortie["confirmation"] = confirmation
    return sortie


def boutons_confirmation(action_id: int) -> list[dict[str, Any]]:
    return [
        action("Valider", "confirmer", {"decision": "valider"}, action_id),
        action("Modifier", "message", {"texte": "Je voudrais modifier cette proposition."}),
        action("Annuler", "confirmer", {"decision": "annuler"}, action_id),
    ]


def ligne_carte(c: dict[str, Any]) -> str:
    """``CARTE:<json compact sur une ligne>``."""
    return PREFIXE + json.dumps(c, ensure_ascii=False, separators=(",", ":"), default=str)


def confirmation_dans(resultat: Any) -> tuple[int, str] | None:
    """(id, résumé) d'un résultat ``CONFIRMATION_REQUISE:<id>:<résumé>``, sinon None."""
    if not isinstance(resultat, str):
        return None
    m = _RE_CONFIRMATION.search(resultat)
    return (int(m.group(1)), m.group(2).strip()) if m else None


def _normaliser(sortie: Any) -> list[dict[str, Any]]:
    if sortie is None:
        return []
    cartes = sortie if isinstance(sortie, list) else [sortie]
    propres: list[dict[str, Any]] = []
    for c in cartes:
        if not isinstance(c, dict) or not c.get("type") or not isinstance(c.get("donnees"), dict):
            log.warning("carte ignorée : format invalide")
            continue
        c.setdefault("id", f"c-{secrets.token_hex(6)}")
        c.setdefault("actions", [])
        if len(ligne_carte(c)) > TAILLE_MAX_CARTE:
            log.warning("carte %s ignorée : trop grosse", c.get("type"))
            continue
        propres.append(c)
    return propres


def ajouter(cartes: list[dict[str, Any]]) -> None:
    """Garde des cartes pour la requête HTTP en cours (ignoré hors canal web ou hors requête)."""
    if canal_courant() != "web":
        return
    liste = _CARTES_REQUETE.get()
    if liste is not None:
        liste.extend(_normaliser(cartes))


def produire(nom_outil: str, resultat: Any, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Cartes du constructeur de `nom_outil` pour ce résultat ([] hors canal web, sans constructeur ou en erreur)."""
    if canal_courant() != "web":
        return []
    constructeur = CONSTRUCTEURS.get(nom_outil)
    if constructeur is None:
        return []
    try:
        return _normaliser(constructeur(resultat, dict(arguments)))
    except Exception:  # une carte ne doit jamais faire échouer l'outil
        log.warning("carte de %s non produite", nom_outil, exc_info=True)
        return []


def pour(nom_outil: str) -> Callable[[Constructeur], Constructeur]:
    """Décorateur : enregistre le constructeur de cartes d'un outil (remplace un constructeur existant)."""

    def deco(fn: Constructeur) -> Constructeur:
        CONSTRUCTEURS[nom_outil] = fn
        return fn

    return deco


def sans_sortie_structuree(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Options d'enregistrement d'un outil MCP : sans sortie structurée (`structured_output=False`), sauf choix
    explicite. Avec un `structuredContent`, Claude Code ne transmet au modèle que ce JSON et ignore les blocs texte du
    résultat, dont les lignes `CARTE:` du canal web : la carte n'arrivait jamais à l'appli (retour d'usage,
    écran Courses : liste rendue en texte)."""
    return {"structured_output": False, **kwargs}


def avec_cartes(fn: Callable[..., Any], nom_outil: str) -> Callable[..., Any]:
    """Enveloppe un outil (synchrone ou asynchrone) : après un appel réussi en canal web, ses cartes sont gardées
    pour la requête HTTP (ou, en stdio, ajoutées à la fin d'un résultat textuel). Signature conservée."""

    def finir(resultat: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if canal_courant() != "web":
            return resultat
        arguments = dict(kwargs)
        if args:
            try:
                arguments = dict(inspect.signature(fn).bind_partial(*args, **kwargs).arguments)
            except TypeError:
                pass
        produites = produire(nom_outil, resultat, arguments)
        if not produites:
            return resultat
        liste = _CARTES_REQUETE.get()
        if liste is not None:
            liste.extend(produites)
            return resultat
        # stdio : la ligne voyage dans le contenu du résultat (outils enregistrés sans sortie structurée, voir
        # tools.server.outil) : à la fin d'un texte, sinon comme bloc texte de plus après l'objet ou la liste.
        lignes = [ligne_carte(c) for c in produites]
        if isinstance(resultat, str):
            return resultat.rstrip("\n") + "\n" + "\n".join(lignes)
        if resultat is None:
            return "\n".join(lignes)
        if isinstance(resultat, list):
            return [*resultat, *lignes]
        return [resultat, *lignes]

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def asynchrone(*args: Any, **kwargs: Any) -> Any:
            return finir(await fn(*args, **kwargs), args, kwargs)

        return asynchrone

    @functools.wraps(fn)
    def synchrone(*args: Any, **kwargs: Any) -> Any:
        return finir(fn(*args, **kwargs), args, kwargs)

    return synchrone


def injecter_dans_reponse(corps: bytes, cartes: list[dict[str, Any]]) -> bytes:
    """Réponse JSON-RPC d'un ``tools/call`` : ajoute un bloc texte ``CARTE:…`` par carte au ``result.content``.

    Toute autre réponse (erreur, liste d'outils, format inattendu) est rendue telle quelle."""
    if not cartes:
        return corps
    try:
        obj = json.loads(corps)
    except (ValueError, UnicodeDecodeError):
        return corps
    if not isinstance(obj, dict) or not isinstance(obj.get("result"), dict):
        return corps
    contenu = obj["result"].get("content")
    if not isinstance(contenu, list) or obj["result"].get("isError"):
        return corps
    contenu.append({"type": "text", "text": "\n".join(ligne_carte(c) for c in cartes)})
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- constructeurs des outils de base ------------------------------------------------------------------------------


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def carte_courses(articles: list[dict[str, Any]], titre: str = "Liste de courses", **extra: Any) -> dict[str, Any]:
    """Carte ``courses`` (rayons dans l'ordre de la liste, cochés compris) ; aussi l'état de l'écran Courses."""
    return _carte_courses(articles, titre, **extra)


def _carte_courses(articles: list[dict[str, Any]], titre: str = "Liste de courses", **extra: Any) -> dict[str, Any]:
    rayons: dict[str, list[dict[str, Any]]] = {}
    for a in articles:
        rayons.setdefault(_str(a.get("rayon")) or "autre", []).append(
            {"id": a.get("id"), "article": a.get("article"), "coche": bool(a.get("coche")),
             "ajoute_par": a.get("ajoute_par"), "ts": a.get("ts")}
        )
    restants = sum(1 for a in articles if not a.get("coche"))
    donnees = {"rayons": [{"rayon": r, "articles": lst} for r, lst in rayons.items()], "restants": restants,
               "total": len(articles), **extra}
    return carte("courses", donnees, titre=titre, actions=[action("Ouvrir la liste", "ouvrir", {"ecran": "courses"})])


@pour("courses_lister")
def _courses_lister(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    return _carte_courses(resultat) if isinstance(resultat, list) else None


def _courses_actuelles() -> list[dict[str, Any]]:
    from tools import courses

    return courses.lister()


@pour("courses_ajouter")
def _courses_ajouter(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    ajoutes = [str(a) for a in arguments.get("articles") or []]
    return _carte_courses(_courses_actuelles(), "Liste de courses mise à jour", ajoutes=ajoutes)


@pour("courses_cocher")
def _courses_cocher(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    coches = list((resultat or {}).get("coches") or []) if isinstance(resultat, dict) else []
    return _carte_courses(_courses_actuelles(), "Liste de courses mise à jour", coches=coches)


def _evenements(resultat: Any) -> list[dict[str, Any]]:
    champs = ("agenda", "titre", "debut", "fin", "lieu", "journee_entiere")
    return [{k: e.get(k) for k in champs} for e in resultat or [] if isinstance(e, dict)]


@pour("agenda_lire")
def _agenda_lire(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(resultat, list):
        return None
    debut, fin = _str(arguments.get("debut")), _str(arguments.get("fin"))
    donnees: dict[str, Any] = {"date": debut, "evenements": _evenements(resultat)}
    if fin and fin != debut:
        donnees["jusqu_au"] = fin
    return carte("agenda_jour", donnees, actions=[action("Ouvrir l'agenda", "ouvrir", {"ecran": "agenda", "date": debut})])


@pour("agenda_ajouter")
def _agenda_ajouter(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    donnees = {k: arguments.get(k) for k in ("agenda", "titre", "debut", "fin", "lieu")}
    conf = confirmation_dans(resultat)
    if conf is not None:
        donnees["statut"] = "a_valider"
        donnees["resume"] = conf[1]
        return carte("evenement", donnees, titre="Proposition d'événement", actions=boutons_confirmation(conf[0]))
    donnees["statut"] = "ajout_manuel"
    donnees["texte_a_copier"] = _str(resultat)
    return carte("evenement", donnees, titre="Événement à ajouter à la main")


def elements_echeances(lst: Any) -> list[dict[str, Any]]:
    return [
        {"type": "echeance", "id": e.get("id"), "titre": e.get("titre"), "date": e.get("prochaine") or e.get("date"),
         "dans_jours": e.get("dans_jours"), "en_retard": bool(e.get("en_retard")), "notes": e.get("notes")}
        for e in lst or [] if isinstance(e, dict)
    ]


def elements_anniversaires(lst: Any) -> list[dict[str, Any]]:
    return [
        {"type": "anniversaire", "titre": f"Anniversaire de {a.get('prenom')}", "date": a.get("prochaine"),
         "dans_jours": a.get("dans_jours"), "age": a.get("age")}
        for a in lst or [] if isinstance(a, dict)
    ]


def carte_a_noter(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """« À noter » : éléments datés triés, comptes du jour et de la semaine (aussi utilisée par le brief du jour)."""
    elements = sorted(elements, key=lambda x: (x.get("dans_jours") if isinstance(x.get("dans_jours"), int) else 999,
                                               _str(x.get("titre"))))
    aujourdhui = sum(1 for x in elements if isinstance(x.get("dans_jours"), int) and x["dans_jours"] <= 0)
    semaine = sum(1 for x in elements if isinstance(x.get("dans_jours"), int) and 0 < x["dans_jours"] <= 7)
    return carte("a_noter", {"aujourdhui": aujourdhui, "semaine": semaine, "elements": elements}, titre="À noter")


@pour("echeances_lister")
def _echeances_lister(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    return carte_a_noter(elements_echeances(resultat)) if isinstance(resultat, list) else None


@pour("anniversaires_lister")
def _anniversaires_lister(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    return carte_a_noter(elements_anniversaires(resultat)) if isinstance(resultat, list) else None


@pour("rappels_du_jour")
def _rappels(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(resultat, dict):
        return None
    elements = elements_echeances(resultat.get("echeances")) + elements_anniversaires(resultat.get("anniversaires"))
    return carte_a_noter(elements)


@pour("echeances_ajouter")
def _echeances_ajouter(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(resultat, dict):
        return None
    champs = ("id", "titre", "date", "prochaine", "rappels_jours", "recurrence", "notes")
    return carte("echeance", {k: resultat.get(k) for k in champs if k in resultat}, titre="Échéance enregistrée",
                 actions=[action("Voir les échéances", "ouvrir", {"ecran": "echeances"})])


@pour("fichiers_ranger")
def _fichiers_ranger(resultat: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(resultat, dict):
        return None
    donnees = {"nom": resultat.get("nom"), "categorie": resultat.get("categorie"), "chemin": resultat.get("chemin"),
               "description": arguments.get("description"), "fiche": resultat.get("fiche")}
    return carte("fichier_classe", donnees, titre="Document rangé",
                 actions=[action("Voir les documents", "ouvrir", {"ecran": "fichiers", "categorie": donnees["categorie"]})])
