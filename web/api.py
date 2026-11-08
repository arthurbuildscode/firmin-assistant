"""API de l'appli web (lot 4a, spec §3) : conversations en SSE, confirmations, fichiers, voix, brief, routines, coffre.

Toutes les routes exigent une session (``web.auth``) ; les méthodes qui écrivent exigent aussi l'en-tête CSRF
(``X-Requested-With: homeos``, contrôlé par ``web.app``) ; les actions sensibles exigent une session passkey (pas
de secours) et un step-up récent — à usage unique pour les écritures du coffre.
Chaque action est journalisée dans ``actions_log`` avec ``who`` = prénom ; jamais de valeur d'identifiant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

from bot import db as dbmod
from coeur import confirmations as coeur_confirmations
from coeur.conversation import Evenement, ligne_contexte
from coeur.fichiers import (
    TAILLE_MAX_FICHIER,
    chemin_entrant,
    construire_prompt_fichiers,
    convertir_heic,
    extension_document,
    taille_lisible,
)
from coeur.synthese import SyntheseIndisponible
from coeur.transcription import TranscriptionError
from web import config as configweb
from web import limites
from web.auth import (
    ACTIONS_STEP_UP_UNIQUE,
    Identite,
    consommer_step_up,
    contexte,
    corps_json,
    exiger_identite,
    exiger_step_up,
    journal,
    nom_cookie,
    refuser_secours,
    step_up_valide,
)
from web.contexte import ContexteWeb
from web.flux import EvenementBus

log = logging.getLogger(__name__)

routeur = APIRouter(prefix="/api")

INTERVALLE_FLUX_S = 5.0
DUREE_MAX_FLUX_S = 30 * 60
RE_CAPTURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(png|jpg|jpeg)$")
RE_ECRAN = re.compile(r"^[a-z0-9_-]{1,40}$")
FIN = object()
MESSAGE_TROP_D_ECHANGES = "Plusieurs demandes sont déjà en cours : attends leur réponse avant d'en envoyer une autre."
MESSAGE_TROP_DE_FLUX = "Trop d'onglets ouverts sur cette conversation : ferme-en un."


def _proprietaire(ident: Identite) -> None:
    if not ident.personne.proprietaire:
        raise HTTPException(403, "Réservé aux propriétaires du foyer.")


def _sse(type_: str, donnees: Any) -> dict[str, str]:
    return {"event": type_, "data": json.dumps(donnees, ensure_ascii=False, default=str)}


# --- conversations -----------------------------------------------------------------------------------------------


@routeur.get("/conversations")
def conversations(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    courante = ctx.historique.courante(ident.personne.linux_user)
    return {
        "courante": courante["id"] if courante else None,
        "conversations": ctx.historique.lister(ident.personne.linux_user),
    }


@routeur.post("/conversations")
def nouvelle_conversation(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    conv = ctx.coeur.nouvelle_conversation(ident.personne)
    journal(ctx, ident.personne.prenom, "commande", "nouvelle conversation (appli)")
    return {"conversation": conv}


def _conversation(ctx: ContexteWeb, ident: Identite, conversation_id: int) -> dict[str, Any]:
    conv = ctx.historique.conversation_de(ident.personne.linux_user, conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation inconnue.")
    return conv


@routeur.get("/conversations/{conversation_id}/messages")
def messages(
    request: Request, conversation_id: int, apres: int = 0, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    ctx = contexte(request)
    conv = _conversation(ctx, ident, conversation_id)
    return {"conversation": conv, "messages": ctx.historique.messages(conversation_id, apres_id=max(0, apres))}


def _ligne_ecran(contexte_ui: Any) -> str:
    """``[écran de l'appli : courses ; objet : …]`` (écrit par l'appli, texte court sur une ligne)."""
    if not isinstance(contexte_ui, dict):
        return ""
    ecran = str(contexte_ui.get("ecran") or "").strip().lower()
    # Sans crochets : l'objet ne peut pas fermer la ligne et en imiter une autre (« [contexte: … ] »).
    objet = " ".join(str(contexte_ui.get("objet") or "").replace("[", " ").replace("]", " ").split())[:200]
    if not RE_ECRAN.fullmatch(ecran):
        ecran = ""
    if not ecran and not objet:
        return ""
    morceaux = [f"écran de l'appli : {ecran}"] if ecran else []
    if objet:
        morceaux.append(f"objet : {objet}")
    return f"[{' ; '.join(morceaux)}]"


async def _enrichir(ctx: ContexteWeb, ev: Evenement) -> dict[str, Any]:
    """Confirmation : ``step_up`` vrai si la validation exigera une preuve d'identité récente."""
    donnees = dict(ev.donnees)
    if ev.type == "confirmation" and not donnees.get("auto"):
        action = await dbmod.run(ctx.db.action, int(donnees["id"]))
        donnees["step_up"] = bool(action is not None and coeur_confirmations.kind_sensible(ctx.cfg, str(action["kind"])))
    return donnees


@routeur.post("/conversations/{conversation_id}/messages")
async def envoyer_message(
    request: Request, conversation_id: int, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> EventSourceResponse:
    """Un message → flux SSE : ``attente``, ``debut``, ``carte``, ``etape``, ``texte``, ``confirmation``, ``fin``
    (``{texte, duree_ms, cout_usd, modele, niveau, etapes, confirmations, conversation_id, message_id}``) ou ``erreur``.

    L'échange tourne en tâche de fond : une déconnexion du navigateur ne l'interrompt pas (résultat dans l'historique et
    sur ``GET /api/conversations/{id}/flux``)."""
    ctx = contexte(request)
    personne = ident.personne
    _conversation(ctx, ident, conversation_id)
    corps = await corps_json(request)
    texte = str(corps.get("texte") or "").strip()
    ids = [str(i) for i in (corps.get("pieces_jointes") or []) if isinstance(i, str)][:10]
    pieces = ctx.auth.pieces(personne.linux_user, ids)
    if not texte and not pieces:
        raise HTTPException(400, "Message vide.")
    if len(texte) > configweb.LONGUEUR_MAX_MESSAGE:
        raise HTTPException(413, f"Message trop long ({configweb.LONGUEUR_MAX_MESSAGE} caractères au plus).")
    corps_prompt = construire_prompt_fichiers([Path(p["chemin"]) for p in pieces], texte) if pieces else texte
    lignes = [ligne_contexte(personne.prenom, canal="web")]
    ecran = _ligne_ecran(corps.get("contexte"))
    if ecran:
        lignes.append(ecran)
    prompt = "\n".join([*lignes, corps_prompt])
    meta_message: dict[str, Any] = {}
    if pieces:
        meta_message["pieces"] = [{"id": p["id"], "nom": p["nom"]} for p in pieces]
    texte_affiche = texte or ("📎 " + ", ".join(p["nom"] for p in pieces))
    if ctx.en_cours.get(personne.linux_user, 0) >= limites.MAX_ECHANGES_PAR_PERSONNE:
        # Les échanges d'une personne sont sérialisés par son verrou : au-delà de quelques demandes en file, on
        # refuse plutôt que d'empiler des `claude -p` (front emballé ou membre malveillant).
        raise HTTPException(429, MESSAGE_TROP_D_ECHANGES)

    file: asyncio.Queue[Any] = asyncio.Queue()
    ctx.echange_debute(personne.linux_user)

    async def echange() -> None:
        conv_id: int | None = conversation_id
        try:
            async for ev in ctx.coeur.echanger(
                personne,
                texte_affiche,
                canal="web",
                prompt=prompt,
                conversation_id=conversation_id,
                meta_journal={"canal": "web", "pieces": len(pieces)},
                meta_message=meta_message or None,
            ):
                if ev.type == "debut":
                    conv_id = ev.donnees.get("conversation_id") or conv_id
                donnees = await _enrichir(ctx, ev)
                await file.put((ev.type, donnees))
                ctx.bus.publier(personne.linux_user, EvenementBus(ev.type, donnees, conv_id))
        except Exception:
            log.exception("échange web en erreur")
            await file.put(("erreur", {"message": "Une erreur inattendue s'est produite. Réessaie dans un instant."}))
        finally:
            ctx.echange_termine(personne.linux_user)
            await file.put(FIN)

    ctx.lancer(echange())

    async def evenements() -> AsyncIterator[dict[str, str]]:
        while True:
            element = await file.get()
            if element is FIN:
                return
            type_, donnees = element
            yield _sse(type_, donnees)

    return EventSourceResponse(evenements(), ping=15)


@routeur.get("/conversations/{conversation_id}/flux")
async def flux(
    request: Request, conversation_id: int, apres: int | None = None, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> EventSourceResponse:
    """Flux long-vivant d'une conversation : événements des échanges en cours (autres onglets, appareils), résultats
    des confirmations, cartes des routines et messages arrivés par un autre canal (``message``)."""
    ctx = contexte(request)
    _conversation(ctx, ident, conversation_id)
    linux_user = ident.personne.linux_user
    if ctx.bus.nb_abonnes(linux_user) >= limites.MAX_FLUX_PAR_PERSONNE:
        raise HTTPException(429, MESSAGE_TROP_DE_FLUX)
    jeton_session = request.cookies.get(nom_cookie(ctx), "")
    session_id = int(ident.session["id"])
    file = ctx.bus.abonner(linux_user)
    # ?apres=<id> : messages postérieurs à cet id (reprise après « reconnexion ») ; sans paramètre : à partir de maintenant.
    dernier = max(apres, 0) if apres is not None else ctx.historique.dernier_id(conversation_id)

    def session_toujours_valide() -> bool:
        """La session a pu être révoquée (Réglages, `--revoquer`) pendant que le flux était ouvert."""
        session = ctx.auth.session(jeton_session)
        return session is not None and int(session["id"]) == session_id

    async def evenements() -> AsyncIterator[dict[str, str]]:
        nonlocal dernier
        debut = time.monotonic()
        try:
            yield _sse("pret", {"conversation_id": conversation_id, "dernier_message": dernier})
            while True:
                if not await asyncio.to_thread(session_toujours_valide):
                    yield _sse("deconnecte", {"message": "Session fermée."})
                    return
                for m in ctx.historique.messages(conversation_id, apres_id=dernier, limite=50):
                    dernier = m["id"]
                    yield _sse("message", m)
                if time.monotonic() - debut > DUREE_MAX_FLUX_S or await request.is_disconnected():
                    # Durée de vie bornée : le front se reconnecte avec ?apres=<dernier message reçu>.
                    yield _sse("reconnexion", {"apres": dernier})
                    return
                try:
                    ev = await asyncio.wait_for(file.get(), timeout=INTERVALLE_FLUX_S)
                except TimeoutError:
                    continue
                if ev.conversation_id in (None, conversation_id):
                    yield _sse(ev.type, ev.donnees)
        finally:
            ctx.bus.desabonner(linux_user, file)

    return EventSourceResponse(evenements(), ping=15)


# --- confirmations ------------------------------------------------------------------------------------------------


def _action_de(ctx: ContexteWeb, ident: Identite, action: Any) -> bool:
    """Vrai si l'action en attente appartient à la personne : rattachée à une de ses conversations, ou créée par
    elle hors conversation. Une action rattachée à la conversation de l'autre n'est jamais la sienne, même si
    ``who`` porte son prénom."""
    conv_id = ctx.historique.conversation_de_action(int(action["id"]))
    if conv_id is not None:
        return ctx.historique.conversation_de(ident.personne.linux_user, conv_id) is not None
    return action["who"] == ident.personne.prenom


@routeur.post("/confirmations/{action_id}")
async def confirmation(
    request: Request, action_id: int, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """``{decision: valider|annuler}`` : même chemin que les boutons Telegram (pause, rôle, déjà traitée). Les actions
    sensibles (administration, ``confirmation_toujours``, identifiants) exigent une session passkey et un step-up
    pour être validées ; celles qui écrivent dans le coffre (``ACTIONS_STEP_UP_UNIQUE``) consomment le jeton.

    Seules les actions de la personne connectée sont visibles ici : nées dans une de ses conversations, ou créées
    par elle (``who``) hors conversation (``PATCH /api/routines``). Celles de l'autre personne : 404, comme une
    conversation qui n'est pas la sienne (les identifiants d'action sont séquentiels, donc devinables)."""
    ctx = contexte(request)
    corps = await corps_json(request)
    decision = str(corps.get("decision") or "")
    if decision not in ("valider", "annuler"):
        raise HTTPException(400, "decision : valider ou annuler.")
    action = await dbmod.run(ctx.db.action, action_id)
    if action is None or not _action_de(ctx, ident, action):
        raise HTTPException(404, f"Action #{action_id} introuvable.")
    if decision == "valider" and coeur_confirmations.kind_sensible(ctx.cfg, str(action["kind"])):
        refuser_secours(ident)
        if not step_up_valide(request, ident):
            raise HTTPException(403, {"message": "Confirme ton identité pour valider cette action.", "step_up": True})
        if str(action["kind"]) in ACTIONS_STEP_UP_UNIQUE:
            consommer_step_up(request, ident)
    cartes: list[dict[str, Any]] = []
    texte = await ctx.decider(
        "confirm" if decision == "valider" else "cancel", action_id, ident.personne.prenom, ident.personne, cartes
    )
    conv_id = ctx.historique.conversation_de_action(action_id)
    if conv_id is not None:
        ctx.historique.ajouter_message(conv_id, "systeme", "web", texte, auteur=ident.personne.prenom,
                                       cartes=cartes or None, meta={"confirmation": action_id, "decision": decision})
    donnees = {"id": action_id, "decision": decision, "texte": texte, "cartes": cartes}
    ctx.bus.publier(ident.personne.linux_user, EvenementBus("resultat_confirmation", donnees, conv_id))
    return {"ok": True, **donnees}


@routeur.get("/captures/{nom}")
def capture(request: Request, nom: str, ident: Identite = Depends(exiger_identite)) -> FileResponse:  # noqa: B008
    ctx = contexte(request)
    if not RE_CAPTURE.fullmatch(nom):
        raise HTTPException(404, "Capture inconnue.")
    chemin = ctx.cfg.kd_captures_dir / nom
    if not coeur_confirmations.capture_autorisee(ctx.cfg, chemin):
        raise HTTPException(404, "Capture inconnue.")
    return FileResponse(chemin, headers={"Cache-Control": "private, max-age=3600"})


# --- fichiers -------------------------------------------------------------------------------------------------------


async def _copier_limite(fichier: UploadFile, cible: Path, limite: int) -> int:
    taille = 0
    with cible.open("wb") as fh:
        while True:
            morceau = await fichier.read(1024 * 1024)
            if not morceau:
                break
            taille += len(morceau)
            if taille > limite:
                raise HTTPException(413, f"Fichier trop gros ({taille_lisible(limite)} au plus).")
            fh.write(morceau)
    return taille


@routeur.post("/fichiers")
async def televerser(
    request: Request, fichier: UploadFile, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """Photo ou document (pdf, jpg, png, heic ; 20 Mo) rangé dans ``data/fichiers/entrants`` ; l'``id`` renvoyé est
    cité dans ``pieces_jointes`` du message suivant (« [fichier reçu: …] » pour l'assistant)."""
    ctx = contexte(request)
    personne = ident.personne
    extension = extension_document(fichier.filename, fichier.content_type)
    if extension is None:
        raise HTTPException(415, "Je lis les PDF et les images (JPG, PNG, HEIC). Ce format-là, pas encore.")
    dossier = ctx.cfg.data_dir.resolve() / "fichiers" / "entrants"
    dossier.mkdir(parents=True, exist_ok=True)
    nom_original = Path(fichier.filename or "document").name
    chemin = chemin_entrant(dossier, personne.prenom, nom_original, extension)
    try:
        taille = await _copier_limite(fichier, chemin, TAILLE_MAX_FICHIER)
    except BaseException:
        chemin.unlink(missing_ok=True)
        raise
    if extension == ".heic":
        try:
            chemin = await asyncio.to_thread(convertir_heic, chemin)
        except Exception:  # noqa: BLE001
            chemin.unlink(missing_ok=True)
            raise HTTPException(415, "Je ne sais pas lire les photos HEIC sur ce serveur : envoie-la en JPG.") from None
        taille = chemin.stat().st_size
    pid = ctx.auth.ajouter_piece(personne.linux_user, chemin, nom_original, taille)
    journal(ctx, personne.prenom, "fichier", f"reçu {chemin.name} ({taille_lisible(taille)}) par l'appli",
            {"chemin": str(chemin), "taille": taille})
    return {"id": pid, "nom": nom_original, "taille": taille}


# --- voix -------------------------------------------------------------------------------------------------------------


@routeur.post("/voix/transcrire")
async def transcrire(
    request: Request, audio: UploadFile, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """Audio (webm/ogg/mp4/wav, 10 Mo) → texte, avec faster-whisper (local)."""
    ctx = contexte(request)
    suffixe = Path(audio.filename or "vocal.webm").suffix.lower()
    if not re.fullmatch(r"\.(webm|ogg|oga|opus|mp4|m4a|wav|mp3)", suffixe):
        suffixe = ".webm"
    with tempfile.TemporaryDirectory(prefix="homeos-voix-") as tmp:
        chemin = Path(tmp) / f"vocal{suffixe}"
        await _copier_limite(audio, chemin, configweb.LIMITE_AUDIO)
        try:
            texte = await ctx.transcripteur.transcrire(chemin)
        except TranscriptionError as exc:
            journal(ctx, ident.personne.prenom, "vocal", "transcription impossible (appli)", None, dbmod.STATUS_ERROR)
            raise HTTPException(422, str(exc)) from None
    journal(ctx, ident.personne.prenom, "vocal", "transcrit (appli)")
    return {"texte": texte}


@routeur.post("/voix/synthese")
async def synthese(request: Request, ident: Identite = Depends(exiger_identite)) -> Response:  # noqa: B008
    """Texte → WAV (Piper, voix française, cache) ; 503 ``{repli: "navigateur"}`` si Piper est indisponible."""
    ctx = contexte(request)
    corps = await corps_json(request)
    if ctx.synthese is None:
        return JSONResponse({"message": "synthèse vocale indisponible", "repli": "navigateur"}, status_code=503)
    try:
        chemin = await ctx.synthese.wav(str(corps.get("texte") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except SyntheseIndisponible as exc:
        return JSONResponse({"message": str(exc), "repli": "navigateur"}, status_code=503)
    return FileResponse(chemin, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


# --- brief, état, routines ------------------------------------------------------------------------------------------


@routeur.get("/brief/jour")
async def brief_jour(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Cartes ``bonjour`` (date, météo, événements), ``brief`` (aujourd'hui) et ``a_noter`` d'un jour ; ``?date=``
    (AAAA-MM-JJ) de la veille à J+14."""
    from coeur.brief import cartes_du_jour

    ctx = contexte(request)
    aujourdhui = datetime.now().astimezone().date()
    brut = request.query_params.get("date")
    try:
        jour = date.fromisoformat(brut) if brut else aujourdhui
    except ValueError:
        raise HTTPException(400, "date : AAAA-MM-JJ.") from None
    if not aujourdhui - timedelta(days=1) <= jour <= aujourdhui + timedelta(days=14):
        raise HTTPException(400, "date hors de la période consultable (veille à J+14).")
    cartes = await asyncio.to_thread(cartes_du_jour, ctx.cfg, ident.personne, jour, aujourdhui=aujourdhui)
    return {"date": jour.isoformat(), "cartes": cartes}


@routeur.get("/etat")
async def etat(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """État du système (comme ``/admin``), propriétaires seulement."""
    from tools import administration

    _proprietaire(ident)
    ctx = contexte(request)
    return await asyncio.to_thread(administration.etat, ctx.cfg.capabilities_dir, ctx.cfg.data_dir)


@routeur.get("/routines")
async def routines(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Rendez-vous programmés : briefs du foyer et crons des capacités (heure, libellé, statut)."""
    from tools import administration

    ctx = contexte(request)
    e = await asyncio.to_thread(administration.etat, ctx.cfg.capabilities_dir, ctx.cfg.data_dir)
    liste = [{"capacite": "foyer", "nom": c["cron"], "titre": c["nom"], "quand": c["quand"], "lisible": c["lisible"],
              "statut": "actif"} for c in e.get("crons_foyer") or []]
    for cap in e.get("capacites") or []:
        for c in cap.get("crons") or []:
            liste.append({"capacite": cap["nom"], "nom": c["cron"], "titre": cap["titre"], "quand": c["quand"],
                          "lisible": c["lisible"], "statut": cap["statut"]})
    derniers = await asyncio.to_thread(_derniers_passages, ctx)
    for r in liste:
        kind = f"brief_{r['nom'].removeprefix('brief_')}" if r["capacite"] == "foyer" else f"cron_{r['capacite']}_{r['nom']}"
        r["dernier"] = derniers.get(kind)
    return {"routines": liste, "pause": e.get("pause"), "modifiable": ident.personne.proprietaire}


def _derniers_passages(ctx: ContexteWeb) -> dict[str, dict[str, Any]]:
    """Dernier passage de chaque routine (``actions_log`` : briefs ``brief_*``, crons ``cron_<capacité>_<cron>``) :
    ``{quand, statut}`` — jamais le texte produit."""
    try:
        with ctx.db.connect() as conn:
            lignes = conn.execute(
                "SELECT kind, MAX(ts) AS ts, status FROM actions_log WHERE (kind LIKE 'brief\\_%' ESCAPE '\\' "
                "OR kind LIKE 'cron\\_%' ESCAPE '\\') AND who = 'cron' GROUP BY kind"
            ).fetchall()
    except Exception:
        log.info("derniers passages des routines indisponibles", exc_info=True)
        return {}
    return {str(r[0]): {"quand": r[1], "statut": r[2]} for r in lignes}


@routeur.patch("/routines/{capacite}/{nom}")
async def modifier_routine(
    request: Request, capacite: str, nom: str, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """``{quand: "mercredi 12h"}`` → action ``admin_cron`` à valider (``POST /api/confirmations/{id}``, step-up),
    via ``tools.administration`` comme en conversation. ``capacite`` = ``foyer`` pour les briefs."""
    from tools import administration

    _proprietaire(ident)
    refuser_secours(ident)
    ctx = contexte(request)
    corps = await corps_json(request)
    quand = str(corps.get("quand") or "").strip()
    if not quand:
        raise HTTPException(400, "quand : nouvel horaire (« mercredi 12h », « Wed 18:00 »).")
    cible, cron = (nom, "") if capacite == "foyer" else (capacite, nom)
    try:
        resume, payload = await asyncio.to_thread(administration.preparer_cron, cible, cron, quand, ident.personne.prenom,
                                                  ctx.cfg.capabilities_dir)
    except administration.ErreurAdministration as exc:
        raise HTTPException(400, str(exc)) from None
    action_id = await dbmod.run(ctx.db.log, ident.personne.prenom, str(payload["action"]), resume, payload,
                                dbmod.STATUS_PENDING)
    return {"confirmation": {"id": action_id, "resume": resume, "auto": False, "step_up": True}}


# --- coffre (carte identifiants) --------------------------------------------------------------------------------------


def _secret_de(ctx: ContexteWeb, capacite: str, linux_user: str, base: str | None) -> tuple[str, tuple[str, ...], Any]:
    """(clé réelle, champs attendus, capacité) pour un identifiant déclaré par le manifeste de ``capacite``."""
    from tools import capacites

    cap = next((c for c in capacites.decouvrir(ctx.cfg.capabilities_dir) if c.nom == capacite), None)
    if cap is None or cap.manifeste is None or not cap.manifeste.secrets:
        raise HTTPException(404, "Cette capacité ne demande pas d'identifiants.")
    declares = cap.manifeste.secrets
    secret = next((s for s in declares if base in (s.base, s.cle)), None) if base else declares[0]
    if secret is None:
        raise HTTPException(400, "Identifiant non déclaré par cette capacité.")
    cle = f"{secret.base}/{linux_user}" if secret.par_personne else secret.cle
    return cle, tuple(secret.champs), cap


@routeur.get("/coffre/{capacite}")
def coffre_description(
    request: Request, capacite: str, ident: Identite = Depends(exiger_identite)  # noqa: B008
) -> dict[str, Any]:
    """Champs attendus et champs déjà renseignés (noms seulement, jamais de valeur) : formulaire de la carte."""
    from tools import coffre, identite

    ctx = contexte(request)
    cle, champs, cap = _secret_de(ctx, capacite, ident.personne.linux_user, None)
    renseignes: list[str] = []
    try:
        with identite.requete(ident.personne.linux_user):
            renseignes = next((d["champs"] for d in coffre.decrire() if d["cle"] == cle), [])
    except Exception:  # coffre inaccessible : le formulaire reste utilisable
        log.info("coffre illisible pour la description", exc_info=True)
    return {"capacite": capacite, "titre": cap.titre, "champs": list(champs), "renseignes": renseignes,
            "personnel": cle.endswith("/" + ident.personne.linux_user)}


@routeur.post("/coffre/{capacite}")
async def coffre_ecrire(
    request: Request, capacite: str, ident: Identite = Depends(exiger_step_up)  # noqa: B008
) -> dict[str, Any]:
    """``{champs: {identifiant: …, mot_de_passe: …}, cle?: <base>}`` → ``tools.coffre.ecrire`` (fusion), sous
    step-up à usage unique (consommé par l'écriture). Le modèle n'est jamais dans la boucle ; aucune valeur n'est
    journalisée ni renvoyée."""
    from tools import coffre, identite

    ctx = contexte(request)
    corps = await corps_json(request)
    valeurs = corps.get("champs")
    if not isinstance(valeurs, dict) or not valeurs:
        raise HTTPException(400, "champs : objet {nom: valeur}.")
    cle, attendus, _ = _secret_de(ctx, capacite, ident.personne.linux_user, str(corps.get("cle") or "") or None)
    inconnus = [k for k in valeurs if k not in attendus]
    if inconnus or not all(isinstance(v, str) and v and len(v) <= 4096 for v in valeurs.values()):
        raise HTTPException(400, f"Champs attendus : {', '.join(attendus)} (texte non vide).")
    consommer_step_up(request, ident)
    try:
        with identite.requete(ident.personne.linux_user):
            await asyncio.to_thread(coffre.ecrire, cle, {str(k): str(v) for k, v in valeurs.items()}, True)
    except Exception as exc:  # noqa: BLE001 - jamais de 500 (traceback) autour de valeurs d'identifiants
        # Seule la classe de l'erreur sort d'ici : ni message (qui pourrait citer une valeur), ni traceback.
        journal(ctx, ident.personne.prenom, "coffre", f"identifiants {capacite} non enregistrés", None,
                dbmod.STATUS_ERROR)
        log.warning("écriture du coffre impossible (%s)", exc.__class__.__name__)
        raise HTTPException(503, f"Coffre indisponible : {exc.__class__.__name__}") from None
    noms = sorted(valeurs)
    journal(ctx, ident.personne.prenom, "coffre", f"identifiants {capacite} enregistrés (champs : {', '.join(noms)})",
            {"cle": cle, "champs": noms})
    return {"ok": True, "champs": noms}


# --- coffre : carte identifiants du lot 5c (clés libres ou de capacité) --------------------------------------------


@routeur.post("/coffre")
async def coffre_saisie(request: Request, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    """Carte ``identifiants`` (``identifiants_preparer``, lot 5c) : ``{cle, champs: {nom: valeur}}`` sous step-up à
    usage unique (consommé par l'écriture, rendu si la saisie est refusée sans rien écrire) →
    ``tools.identifiants.enregistrer_saisie`` (droits de la personne sur l'entrée, validation par type : identifiants,
    carte bancaire virtuelle (Luhn, MM/AA), code, clé d'API). Le modèle n'est jamais dans la boucle ; aucune valeur
    n'est journalisée ni renvoyée (les messages d'erreur citent le champ, jamais la valeur)."""
    from tools import coffre, identifiants

    ctx = contexte(request)
    corps = await corps_json(request)
    cle = str(corps.get("cle") or "").strip()
    valeurs = corps.get("champs")
    if not cle or len(cle) > 200 or not isinstance(valeurs, dict) or not valeurs:
        raise HTTPException(400, "cle et champs : {cle, champs: {nom: valeur}}.")
    if not all(isinstance(k, str) and isinstance(v, str) and len(v) <= 4096 for k, v in valeurs.items()):
        raise HTTPException(400, "Champs : texte de 4096 caractères au plus.")
    consomme = consommer_step_up(request, ident)
    try:
        resultat = await asyncio.to_thread(identifiants.enregistrer_saisie, cle, dict(valeurs), ident.personne.linux_user)
    except identifiants.ErreurIdentifiants as exc:
        ctx.auth.restituer_step_up(int(ident.session["id"]), consomme)  # rien d'écrit : le jeton resservira
        raise HTTPException(403, str(exc)) from None
    except (coffre.CleCoffreAbsente, coffre.CoffreInaccessible, coffre.CoffreIllisible) as exc:
        journal(ctx, ident.personne.prenom, "coffre", "saisie d'identifiants impossible (coffre)", {"cle": cle},
                dbmod.STATUS_ERROR)
        raise HTTPException(503, f"Coffre indisponible : {exc.__class__.__name__}") from None
    except coffre.ErreurCoffre as exc:
        # Validation par type (« numéro de carte invalide (Luhn) ») ou coffre indisponible : jamais de valeur.
        journal(ctx, ident.personne.prenom, "coffre", "saisie d'identifiants refusée", {"cle": cle}, dbmod.STATUS_ERROR)
        ctx.auth.restituer_step_up(int(ident.session["id"]), consomme)
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "champs": list(resultat.get("champs") or [])}



# --- avis sur une réponse (barre d'outils sous chaque réponse de l'appli) -------------------------------------------


@routeur.post("/messages/{message_id}/avis")
async def avis_reponse(request: Request, message_id: int, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Pouce haut / pouce bas sur une réponse de l'assistant : ``{note: 1 | -1, commentaire?}`` → même table que
    l'outil ``feedback_noter`` (``tools.feedback``), sujet « réponse #<id> », note 5 (utile) ou 1 (pas utile).
    Sans appel au modèle ; 404 si le message n'est pas une réponse d'une conversation de la personne."""
    from tools import db
    from tools.common import iso_now

    ctx = contexte(request)
    corps = await corps_json(request)
    note = corps.get("note")
    commentaire = corps.get("commentaire")
    if note not in (1, -1) or isinstance(note, bool):
        raise HTTPException(400, "note : 1 ou -1.")
    if commentaire is not None and (not isinstance(commentaire, str) or len(commentaire) > 2000):
        raise HTTPException(400, "commentaire : texte de 2000 caractères au plus.")
    with ctx.historique.connexion() as c:
        ligne = c.execute(
            "SELECT m.id FROM messages m JOIN conversations v ON v.id = m.conversation_id"
            " WHERE m.id = ? AND v.linux_user = ? AND m.role != 'personne'",
            (message_id, ident.personne.linux_user),
        ).fetchone()
    if ligne is None:
        raise HTTPException(404, "Message inconnu.")

    def noter() -> dict[str, Any]:
        # Même table que feedback_noter, mais l'auteur est la personne de la session (jamais HOMEOS_WHO du processus).
        with db.cursor() as conn:
            cur = conn.execute(
                "INSERT INTO feedback (ts, who, sujet, note, commentaire) VALUES (?,?,?,?,?)",
                (iso_now(), ident.personne.prenom, f"réponse #{message_id}", 5 if note == 1 else 1,
                 (commentaire or "").strip() or None),
            )
            return {"id": cur.lastrowid}

    try:
        resultat = await asyncio.to_thread(noter)
    except Exception as exc:  # noqa: BLE001 - base des outils indisponible : jamais de 500 avec traceback
        log.warning("avis non enregistré (%s)", exc.__class__.__name__)
        raise HTTPException(503, "Avis non enregistré pour le moment.") from None
    journal(ctx, ident.personne.prenom, "feedback", f"avis {'+' if note == 1 else '-'} sur la réponse #{message_id}",
            {"message": message_id, "note": note, "commentaire": bool((commentaire or "").strip())})
    return {"ok": True, "id": resultat["id"]}
