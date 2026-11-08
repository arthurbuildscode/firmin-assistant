"""Points d'accès directs des écrans de l'appli, sans modèle : courses, échéances, journal, fichiers, mémoire, routines.

Chaque route réutilise les fonctions des outils (``tools/courses.py``, ``echeances.py``, ``journal.py``,
``fichiers.py``, ``memoire.py``) — aucune logique métier dupliquée — exécutées sous l'identité de la personne
connectée (``tools.identite.requete`` : ``ajoute_par``, ``cree_par``, auteur du journal = son prénom).

Droits (mêmes niveaux que la conversation, skill ux-capacites §4) :

- **Famille** (tous les adultes) : courses, échéances, journal, fichiers rangés, fiches Famille de la mémoire ;
- **personnel** : ses fiches (``personnes/<moi>.md``, ``alimentation/<moi>.md``), ses fichiers entrants (non
  rangés) ; jamais ceux de l'autre (404 ou 403) ;
- **privé** : dossiers ``prive/`` et documents sensibles (``web/sensibles.py``) : jamais par ces routes ;
- un invité (``role: invite``) lit, mais n'écrit pas (403) ; une session de secours (code Telegram) non plus.

Chaque écriture est journalisée dans ``actions_log`` (``who`` = prénom, payload ``{"canal": "appli"}``) et diffusée
sur le bus (événement ``maj {domaine, action, par}``, sans contenu) à la personne et, pour une donnée Famille, aux
autres adultes : leurs écrans ouverts se rafraîchissent (nouvelle lecture). Les écritures exigent l'en-tête CSRF
(``web.app.Garde``) et une session.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from web.auth import Identite, contexte, corps_json, exiger_identite, journal, refuser_secours
from web.contexte import ContexteWeb
from web.flux import EvenementBus

routeur = APIRouter(prefix="/api")

T = TypeVar("T")
ROLES_ECRITURE = ("proprietaire", "membre")
RE_PHOTO = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9_-]{1,60}(-[0-9]{1,3})?\.jpg$")
MAX_ARTICLES = 50
LONGUEUR_ARTICLE = 120
LONGUEUR_FICHE = 200 * 1024


# --- utilitaires -------------------------------------------------------------------------------------------------


async def en_personne(ident: Identite, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Exécute un outil dans un fil, sous l'identité de la personne connectée (auteur = son prénom)."""
    from tools import identite

    def appel() -> T:
        with identite.requete(ident.personne.linux_user):
            return fn(*args, **kwargs)

    return await asyncio.to_thread(appel)


def exiger_ecriture(ident: Identite) -> None:
    """Écriture : adulte du foyer, session ouverte par passkey (la connexion de secours par code Telegram lit)."""
    if ident.personne.role not in ROLES_ECRITURE:
        raise HTTPException(403, "Lecture seule pour les invités.")
    refuser_secours(ident)


def diffuser_maj(ctx: ContexteWeb, ident: Identite, domaine: str, action: str, famille: bool = True,
                 **details: Any) -> None:
    """Événement ``maj`` (sans contenu) : à la personne, et aux autres adultes pour une donnée Famille."""
    donnees = {"domaine": domaine, "action": action, "par": ident.personne.prenom, **details}
    destinataires = {ident.personne.linux_user}
    if famille:
        destinataires |= {p.linux_user for p in ctx.cfg.personnes if p.role in ROLES_ECRITURE}
    for linux_user in sorted(destinataires):
        ctx.bus.publier(linux_user, EvenementBus("maj", dict(donnees)))


def noter(ctx: ContexteWeb, ident: Identite, kind: str, resume: str, **payload: Any) -> None:
    journal(ctx, ident.personne.prenom, kind, resume[:300], {"canal": "appli", **payload})


def _texte(corps: dict[str, Any], cle: str, longueur: int, obligatoire: bool = True) -> str:
    valeur = " ".join(str(corps.get(cle) or "").split())
    if obligatoire and not valeur:
        raise HTTPException(400, f"{cle} : texte attendu.")
    if len(valeur) > longueur:
        raise HTTPException(413, f"{cle} : {longueur} caractères au plus.")
    return valeur


# --- courses (Famille) -------------------------------------------------------------------------------------------


async def _etat_courses(ident: Identite) -> dict[str, Any]:
    from tools import cartes, courses

    articles = await en_personne(ident, courses.lister)
    donnees = cartes.carte_courses(articles)["donnees"]
    return {"articles": articles, "rayons": donnees["rayons"], "restants": donnees["restants"],
            "total": donnees["total"]}


@routeur.get("/courses")
async def courses_lister(ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Liste partagée : ``articles`` (ordre stable : rayon puis nom, cochés compris), ``rayons``, ``restants``."""
    return await _etat_courses(ident)


@routeur.post("/courses")
async def courses_ajouter(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{articles: ["lait", …], rayon?}`` (ou ``{article}``) → ``tools.courses.ajouter`` (doublons ignorés, article
    coché réactivé). Renvoie ``ajoutes`` et la liste à jour."""
    from tools import courses

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    brut = corps.get("articles") if isinstance(corps.get("articles"), list) else [corps.get("article")]
    articles = [" ".join(str(a or "").split()) for a in brut if isinstance(a, str)]
    articles = [a for a in articles if a]
    if not articles or len(articles) > MAX_ARTICLES or any(len(a) > LONGUEUR_ARTICLE for a in articles):
        raise HTTPException(400, f"articles : 1 à {MAX_ARTICLES} noms de {LONGUEUR_ARTICLE} caractères au plus.")
    rayon = corps.get("rayon") if isinstance(corps.get("rayon"), str) else None
    ajoutes = await en_personne(ident, courses.ajouter, articles, rayon)
    noter(ctx, ident, "courses_ajouter", f"ajout {', '.join(articles)[:120]} (appli)")
    diffuser_maj(ctx, ident, "courses", "ajouter")
    return {"ajoutes": ajoutes, **await _etat_courses(ident)}


@routeur.patch("/courses/{article_id}")
async def courses_marquer(
    request: Request, article_id: int, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """``{coche: true|false}`` : coche ou décoche sur place (la liste ne se réordonne pas)."""
    from tools import courses

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    if not isinstance(corps.get("coche"), bool):
        raise HTTPException(400, "coche : true ou false.")
    article = await en_personne(ident, courses.marquer, article_id, corps["coche"])
    if article is None:
        raise HTTPException(404, "Article introuvable.")
    noter(ctx, ident, "courses_cocher", f"{'coche' if corps['coche'] else 'décoche'} {article['article']} (appli)")
    diffuser_maj(ctx, ident, "courses", "cocher", id=article_id)
    return {"article": article}


@routeur.delete("/courses/coches")
async def courses_vider_coches(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Retire les articles cochés (action explicite et séparée)."""
    from tools import courses

    exiger_ecriture(ident)
    ctx = contexte(request)
    supprimes = await en_personne(ident, courses.vider_coches)
    noter(ctx, ident, "courses_vider_coches", f"vide les articles cochés ({supprimes}) (appli)")
    diffuser_maj(ctx, ident, "courses", "vider_coches")
    return {"supprimes": supprimes, **await _etat_courses(ident)}


# --- échéances (Famille) ---------------------------------------------------------------------------------------------


@routeur.get("/echeances")
async def echeances_lister(horizon: int = 365, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Échéances dont la prochaine occurrence tombe dans ``horizon`` jours (1 à 3650), retards compris."""
    from tools import echeances

    if not 1 <= horizon <= 3650:
        raise HTTPException(400, "horizon : 1 à 3650 jours.")
    return {"echeances": await en_personne(ident, echeances.lister, horizon)}


@routeur.post("/echeances")
async def echeances_ajouter(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{titre, date (AAAA-MM-JJ), rappels_jours?: [30, 7, 1], recurrence?, notes?}``."""
    from tools import echeances

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    titre = _texte(corps, "titre", 200)
    notes = _texte(corps, "notes", 1000, obligatoire=False) or None
    rappels = corps.get("rappels_jours")
    if rappels is not None and (not isinstance(rappels, list) or len(rappels) > 10
                                or not all(isinstance(r, int) and not isinstance(r, bool) and 0 <= r <= 366
                                           for r in rappels)):
        raise HTTPException(400, "rappels_jours : liste d'entiers de 0 à 366.")
    recurrence = corps.get("recurrence") if isinstance(corps.get("recurrence"), str) else None
    try:
        echeance = await en_personne(ident, echeances.ajouter, titre, str(corps.get("date") or ""), rappels,
                                     recurrence, notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    noter(ctx, ident, "echeances_ajouter", f"échéance « {titre} » le {echeance['date']} (appli)")
    diffuser_maj(ctx, ident, "echeances", "ajouter", id=echeance["id"])
    return {"echeance": echeance}


@routeur.delete("/echeances/{echeance_id}")
async def echeances_supprimer(
    request: Request, echeance_id: int, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    from tools import echeances

    exiger_ecriture(ident)
    ctx = contexte(request)
    if not await en_personne(ident, echeances.supprimer, echeance_id):
        raise HTTPException(404, "Échéance introuvable.")
    noter(ctx, ident, "echeances_supprimer", f"supprime échéance #{echeance_id} (appli)")
    diffuser_maj(ctx, ident, "echeances", "supprimer", id=echeance_id)
    return {"ok": True}


# --- journal (Famille) ---------------------------------------------------------------------------------------------


def _avec_url_photo(entree: dict[str, Any]) -> dict[str, Any]:
    photo = entree.get("photo")
    if not photo:
        return entree
    return {**{k: v for k, v in entree.items() if k != "photo"}, "url": f"/api/{photo}"}


@routeur.get("/journal")
async def journal_lister(request: Request, mois: str | None = None,
                         ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Entrées d'un mois (``?mois=AAAA-MM``, défaut : mois courant), dans l'ordre chronologique ; une photo porte
    ``url`` (``/api/photos/…``). ``mois_disponibles`` : mois qui ont un journal, du plus récent au plus ancien."""
    from tools import journal as journal_outil

    mois = mois or date.today().strftime("%Y-%m")
    try:
        entrees = await en_personne(ident, journal_outil.entrees_du_mois, mois)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"mois": mois, "entrees": [_avec_url_photo(e) for e in entrees],
            "mois_disponibles": await en_personne(ident, journal_outil.mois_disponibles)}


@routeur.post("/journal")
async def journal_ajouter(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{texte}`` : une ligne datée (``tools.journal.ajouter``, auteur = prénom) ; ``{texte, piece, date?}`` : photo
    souvenir (``piece`` = id renvoyé par ``POST /api/fichiers``, une image de la personne ; réduite dans ``photos/``,
    ligne 📷 au journal du mois de ``date``, ``tools.fichiers.photos_journal_ajouter``)."""
    from tools import fichiers
    from tools import journal as journal_outil

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    texte = _texte(corps, "texte", 500)
    piece_id = corps.get("piece")
    if piece_id:
        pieces = ctx.auth.pieces(ident.personne.linux_user, [str(piece_id)])
        if not pieces:
            raise HTTPException(404, "Pièce introuvable.")
        quand = str(corps.get("date") or "") or None
        try:
            resultat = await en_personne(ident, fichiers.photos_journal_ajouter, pieces[0]["chemin"], texte, quand)
        except (fichiers.FichierInvalide, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        noter(ctx, ident, "photos_journal_ajouter", f"photo : {texte[:80]} (appli)")
        diffuser_maj(ctx, ident, "journal", "photo")
        return {"ok": True, "url": "/api/" + resultat["photo"], "mois": resultat["journal"][-10:-3]}
    try:
        await en_personne(ident, journal_outil.ajouter, texte, ident.personne.prenom)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    noter(ctx, ident, "journal_ajouter", f"journal : {texte[:80]} (appli)")
    diffuser_maj(ctx, ident, "journal", "ajouter")
    return {"ok": True, "mois": date.today().strftime("%Y-%m")}


@routeur.get("/photos/{annee}/{mois}/{nom}")
def photo(request: Request, annee: str, mois: str, nom: str,
          ident: Identite = Depends(exiger_identite)) -> FileResponse:  # noqa: B008
    """Photo souvenir du journal (``data/photos/AAAA/MM/…jpg``)."""
    ctx = contexte(request)
    if not (re.fullmatch(r"\d{4}", annee) and re.fullmatch(r"0[1-9]|1[0-2]", mois) and RE_PHOTO.fullmatch(nom)):
        raise HTTPException(404, "Photo introuvable.")
    racine = (ctx.cfg.data_dir / "photos").resolve()
    chemin = (racine / annee / mois / nom).resolve()
    if racine not in chemin.parents or not chemin.is_file():
        raise HTTPException(404, "Photo introuvable.")
    return FileResponse(chemin, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# --- fichiers (rangés : Famille ; entrants : personnel) --------------------------------------------------------------


@routeur.get("/fichiers")
async def fichiers_lister(categorie: str | None = None, limite: int = 50,
                          ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Fichiers rangés (index des catégories), du plus récent au plus ancien ; ``url`` pour les ouvrir. Jamais un
    chemin du serveur ; jamais les documents sensibles (``/api/sensibles``)."""
    from tools import fichiers

    try:
        liste = await en_personne(ident, fichiers.lister, categorie or None, max(1, min(limite, 200)))
    except fichiers.FichierInvalide as exc:
        raise HTTPException(400, str(exc)) from None
    sortie = []
    for f in liste:
        item = {k: v for k, v in f.items() if k != "chemin"}
        if f.get("existe"):
            item["url"] = f"/api/fichiers/{f['categorie']}/{f['date'][:4]}/{f['nom']}"
        sortie.append(item)
    return {"categories": list(fichiers.CATEGORIES), "fichiers": sortie}


@routeur.get("/fichiers/entrants")
async def fichiers_entrants(ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Fichiers reçus de la personne, pas encore rangés (jamais ceux de l'autre) ; ``url`` pour les ouvrir."""
    from tools import fichiers

    liste = await en_personne(ident, fichiers.entrants_de, ident.personne.prenom)
    return {"entrants": [{**f, "url": f"/api/fichiers/entrants/{f['nom']}"} for f in liste]}


@routeur.get("/fichiers/entrants/{nom}")
async def fichier_entrant(nom: str, ident: Identite = Depends(exiger_identite)) -> FileResponse:  # noqa: B008
    from tools import fichiers

    try:
        chemin = await en_personne(ident, fichiers.chemin_entrant_de, ident.personne.prenom, nom)
    except fichiers.FichierInvalide:
        raise HTTPException(404, "Fichier introuvable.") from None
    return FileResponse(chemin, headers={"Cache-Control": "private, no-store"})


@routeur.get("/fichiers/{categorie}/{annee}/{nom}")
async def fichier_range(categorie: str, annee: str, nom: str,
                        ident: Identite = Depends(exiger_identite)) -> FileResponse:  # noqa: B008
    from tools import fichiers

    try:
        chemin = await en_personne(ident, fichiers.chemin_range, categorie, annee, nom)
    except fichiers.FichierInvalide:
        raise HTTPException(404, "Fichier introuvable.") from None
    return FileResponse(chemin, headers={"Cache-Control": "private, max-age=3600"})


# --- mémoire (Famille + personnel) -----------------------------------------------------------------------------------


def _version(contenu: str) -> str:
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()[:16]


async def _niveau(ident: Identite, ctx: ContexteWeb, chemin: str) -> str:
    """Niveau de la fiche pour la personne ; 400 hors mémoire, 403 fiche d'une autre personne ou dossier privé."""
    from tools import memoire

    try:
        niveau = await en_personne(ident, memoire.niveau, chemin, ident.personne.linux_user, ctx.cfg.raw)
    except memoire.CheminInterdit as exc:
        raise HTTPException(400, str(exc)) from None
    if niveau is None:
        raise HTTPException(403, "Cette fiche n'est pas la tienne.")
    return niveau


@routeur.get("/memoire")
async def memoire_lister(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Fiches lisibles par la personne : ``{chemin, titre, niveau (famille | personnel), taille, maj}``."""
    from tools import memoire

    ctx = contexte(request)
    liste = await en_personne(ident, memoire.fiches, ident.personne.linux_user, ctx.cfg.raw)
    return {"fiches": liste, "modifiable": ident.personne.role in ROLES_ECRITURE and not ident.secours}


@routeur.get("/memoire/fiche")
async def memoire_lire(request: Request, chemin: str, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``?chemin=foyer/contacts.md`` → ``{chemin, contenu, niveau, version}`` (``version`` : à renvoyer à l'écriture)."""
    from tools import memoire

    ctx = contexte(request)
    niveau = await _niveau(ident, ctx, chemin)
    cible = await en_personne(ident, memoire.resoudre, chemin)
    if not cible.is_file():
        raise HTTPException(404, "Fiche introuvable.")
    contenu = await asyncio.to_thread(cible.read_text, encoding="utf-8")
    return {"chemin": chemin, "contenu": contenu, "niveau": niveau, "version": _version(contenu),
            "modifiable": ident.personne.role in ROLES_ECRITURE and not ident.secours}


@routeur.put("/memoire/fiche")
async def memoire_ecrire(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{chemin, contenu, version?}`` → ``tools.memoire.ecrire`` (commit git) ; 409 si la fiche a changé depuis la
    lecture (``version`` différente : relire avant d'écraser)."""
    from tools import memoire

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    chemin, contenu = str(corps.get("chemin") or ""), corps.get("contenu")
    if not isinstance(contenu, str) or len(contenu.encode("utf-8")) > LONGUEUR_FICHE:
        raise HTTPException(400, "contenu : texte de 200 Ko au plus.")
    niveau = await _niveau(ident, ctx, chemin)
    cible = await en_personne(ident, memoire.resoudre, chemin)
    if corps.get("version") and cible.is_file():
        actuel = await asyncio.to_thread(cible.read_text, encoding="utf-8")
        if _version(actuel) != str(corps["version"]):
            raise HTTPException(409, "La fiche a changé entre-temps : relis-la avant de l'enregistrer.")
    await en_personne(ident, memoire.ecrire, chemin, contenu)
    noter(ctx, ident, "memoire_ecrire", f"écrit {chemin} (appli)")
    diffuser_maj(ctx, ident, "memoire", "ecrire", famille=niveau == memoire.NIVEAU_FAMILLE, chemin=chemin)
    nouveau = await asyncio.to_thread(cible.read_text, encoding="utf-8")
    return {"ok": True, "chemin": chemin, "version": _version(nouveau)}


@routeur.post("/memoire/fiche/ligne")
async def memoire_ajouter(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{chemin, ligne}`` → ``tools.memoire.ajouter`` (fiche créée avec son en-tête si besoin)."""
    from tools import memoire

    exiger_ecriture(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    chemin = str(corps.get("chemin") or "")
    ligne = _texte(corps, "ligne", 1000)
    niveau = await _niveau(ident, ctx, chemin)
    try:
        await en_personne(ident, memoire.ajouter, chemin, ligne)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    noter(ctx, ident, "memoire_ajouter", f"ajoute à {chemin} (appli)")
    diffuser_maj(ctx, ident, "memoire", "ajouter", famille=niveau == memoire.NIVEAU_FAMILLE, chemin=chemin)
    return {"ok": True, "chemin": chemin}
