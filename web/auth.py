"""Authentification de l'appli web (lot 4a, CONTRACT §12) : passkeys WebAuthn, sessions, step-up, code de secours.

- **Enrôlement** : lien à usage unique (``python -m web.invitation <personne>``, 10 min) → ``/inscription?jeton=…``
  → ``POST /auth/inscription/options`` puis ``/auth/inscription/verification`` (passkey créée, session ouverte).
  Plusieurs passkeys par personne : depuis une session, ``/auth/inscription/options`` sans jeton ajoute un
  appareil (preuve récente par une passkey existante exigée si la personne en a déjà une).
- **Connexion** : ``POST /auth/connexion/options`` (passkey « découvrable », aucun nom à taper) puis
  ``/auth/connexion/verification``. Repli : code à 6 chiffres envoyé par Telegram (``/auth/code/…``).
- **Session** : cookie ``__Host-homeos_session`` (HttpOnly, Secure, SameSite=Strict), table ``web_sessions``
  (empreinte du jeton seulement) ; révocation par appareil (``/api/appareils``). Passkey : 30 jours glissants.
  Code Telegram : niveau **secours**, 24 h non prolongées — converser, lire, enrôler une passkey ; les actions
  sensibles (coffre, documents sensibles, administration et routines, suppression de passkeys) sont refusées.
- **Identité Tailscale** : la session retient le login ``Tailscale-User-Login`` posé par ``tailscale serve`` ; toute
  requête qui arrive avec un autre login (cookie volé, rejoué depuis un autre appareil du tailnet) est refusée (401,
  journal). ``personnes[].tailscale_login`` (config.yaml) : connexion refusée depuis un autre compte. Sans aucun
  en-tête du proxy (accès local direct : tests, ``verifier.sh``), le contrôle est ignoré et journalisé une fois.
  L'en-tête est fiable parce que l'appli n'écoute que sur 127.0.0.1 : seul ``tailscale serve`` (qui retire les
  en-têtes ``Tailscale-User-*`` envoyés par le client avant de poser les siens) ou un processus local la joint.
- **Step-up** : ``POST /auth/step-up/options`` puis ``POST /auth/step-up`` : nouvelle assertion passkey avec
  ``userVerification: required`` (empreinte, visage, code de l'appareil) → jeton 5 min lié à la session, présenté dans
  l'en-tête ``X-Homeos-Step-Up`` par les actions sensibles (coffre, administration, actions toujours confirmées…).
  Écritures du coffre (``POST /api/coffre``, ``/api/coffre/{capacite}``, validation de ``identifiants_supprimer`` et
  ``identifiants_ranger_quarantaine``) : jeton **à usage unique**, consommé par la première écriture.

Rien de secret n'est journalisé : ni jeton, ni code, ni challenge ; ``actions_log`` reçoit qui, quoi, résultat.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import webauthn
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from bot import db as dbmod
from bot.config import Personne
from web import config as configweb
from web.contexte import ContexteWeb
from web.stockage import empreinte, maintenant, secours

log = logging.getLogger(__name__)

routeur = APIRouter()

MESSAGE_NON_CONNECTE = "Connexion nécessaire."
MESSAGE_STEP_UP = "Confirme ton identité (empreinte ou visage) pour cette action."
MESSAGE_SECOURS = ("Connecte-toi par empreinte pour ça : la connexion de secours (code Telegram) permet seulement de "
                   "converser, de lire et d'ajouter une passkey.")
MESSAGE_TAILSCALE = "Connexion refusée depuis ce compte Tailscale."
MESSAGE_AUTRE_TAILSCALE = "Session ouverte depuis un autre compte Tailscale : reconnecte-toi."
SIGNALEMENTS_MAX = 1000
# Actions validées par confirmation qui écrivent dans le coffre : step-up à usage unique, comme POST /api/coffre.
ACTIONS_STEP_UP_UNIQUE = ("identifiants_supprimer", "identifiants_ranger_quarantaine")


@dataclass
class Identite:
    personne: Personne
    session: dict[str, Any]

    @property
    def niveau(self) -> str:
        return configweb.NIVEAU_SECOURS if secours(self.session) else configweb.NIVEAU_PASSKEY

    @property
    def secours(self) -> bool:
        return secours(self.session)


def contexte(request: Request) -> ContexteWeb:
    ctx: ContexteWeb = request.app.state.ctx
    try:
        ctx.rafraichir_config()
    except Exception:
        log.warning("rechargement de la configuration impossible", exc_info=True)
    return ctx


def nom_cookie(ctx: ContexteWeb) -> str:
    return configweb.NOM_COOKIE if ctx.reglages.securise else configweb.NOM_COOKIE.removeprefix("__Host-")


def duree_session(methode: str) -> timedelta:
    if methode == configweb.METHODE_SECOURS:
        return timedelta(hours=configweb.DUREE_SESSION_SECOURS_H)
    return timedelta(days=configweb.DUREE_SESSION_JOURS)


def poser_cookie(reponse: Response, ctx: ContexteWeb, jeton: str, methode: str = "passkey") -> None:
    reponse.set_cookie(
        nom_cookie(ctx),
        jeton,
        max_age=int(duree_session(methode).total_seconds()),
        path="/",
        secure=ctx.reglages.securise,
        httponly=True,
        samesite="strict",
    )


def appareil(request: Request) -> str:
    """Libellé court de l'appareil (navigateur, système) pour la liste des appareils ; jamais l'adresse IP."""
    ua = request.headers.get("user-agent", "")
    systeme = next((s for s in ("Android", "iPhone", "iPad", "Windows", "Mac OS", "Linux") if s in ua), "appareil")
    navigateur = next((n for n in ("Edg", "Firefox", "Chrome", "Safari") if n in ua), "navigateur")
    navigateur = "Edge" if navigateur == "Edg" else navigateur
    return f"{navigateur} — {systeme.replace('Mac OS', 'Mac')}"


# --- identité Tailscale ----------------------------------------------------------------------------------------


def login_tailscale(request: Request) -> str | None:
    """Login Tailscale de la requête (minuscules) : en-tête ``Tailscale-User-Login`` posé par ``tailscale serve`` ;
    ``""`` si la requête est passée par le proxy sans identité (appareil « tagué ») ; None pour un accès local
    direct (ni identité ni ``X-Forwarded-For`` : tests, ``verifier.sh``).

    Fiable parce que l'appli n'écoute que sur 127.0.0.1 : seul ``tailscale serve`` la joint depuis le réseau, et il
    retire les en-têtes ``Tailscale-User-*`` envoyés par le client avant de poser les siens (un processus local
    pourrait les forger, mais il est déjà sur le serveur)."""
    brut = request.headers.get(configweb.ENTETE_TAILSCALE_LOGIN)
    if brut is not None:
        return brut.strip().lower()[:200]
    if request.headers.get(configweb.ENTETE_TRANSMIS):
        return ""
    return None


def _signaler_une_fois(ctx: ContexteWeb, cle: str) -> bool:
    """Vrai la première fois que ``cle`` est vue par ce processus (journaux sans répétition)."""
    if cle in ctx.signalements:
        return False
    if len(ctx.signalements) >= SIGNALEMENTS_MAX:
        ctx.signalements.clear()
    ctx.signalements.add(cle)
    return True


def _acces_local(ctx: ContexteWeb) -> None:
    if _signaler_une_fois(ctx, "acces-local"):
        log.info("requête sans en-tête de tailscale serve (accès local) : contrôle de l'identité Tailscale ignoré")


def tailscale_autorise(ctx: ContexteWeb, request: Request, personne: Personne) -> bool:
    """Connexion (passkey, code, enrôlement) : si ``tailscale_login`` est configuré pour la personne, l'en-tête
    doit lui correspondre. Refus journalisé."""
    vu = login_tailscale(request)
    if vu is None:
        _acces_local(ctx)
        return True
    if personne.tailscale_login and vu != personne.tailscale_login:
        log.warning("connexion de %s refusée : compte Tailscale inattendu", personne.prenom)
        journal(ctx, personne.prenom, "web_tailscale", "connexion refusée : compte Tailscale inattendu",
                {"vu": vu or "(aucun)", "attendu": personne.tailscale_login}, dbmod.STATUS_ERROR)
        return False
    return True


def _session_tailscale_valide(ctx: ContexteWeb, request: Request, personne: Personne, session: dict[str, Any]) -> bool:
    """Chaque requête : le login Tailscale doit être celui de la session (et celui configuré pour la personne).
    Une session sans login (ouverte en local, ou avant ce durcissement) est liée au premier login vu."""
    vu = login_tailscale(request)
    if vu is None:
        _acces_local(ctx)
        return True
    lie = session.get("tailscale_login")
    attendus = [a for a in (personne.tailscale_login, lie) if a]
    if any(vu != a for a in attendus):
        if _signaler_une_fois(ctx, f"tailscale:{session['id']}:{vu}"):
            log.warning("session #%s de %s refusée : compte Tailscale différent", session["id"], personne.prenom)
            journal(ctx, personne.prenom, "web_tailscale",
                    f"requête refusée : session #{session['id']} utilisée depuis un autre compte Tailscale",
                    {"session": session["id"], "vu": vu or "(aucun)", "attendu": attendus[0]}, dbmod.STATUS_ERROR)
        return False
    if not lie and vu:
        ctx.auth.lier_tailscale(int(session["id"]), vu)
        session["tailscale_login"] = vu
    return True


def identite_optionnelle(request: Request) -> Identite | None:
    ctx = contexte(request)
    jeton = request.cookies.get(nom_cookie(ctx), "")
    session = ctx.auth.session(jeton)
    if session is None:
        return None
    personne = ctx.cfg.personne_par_linux_user(str(session["linux_user"]))
    if personne is None:  # personne retirée de config.yaml : plus aucun accès
        ctx.auth.revoquer_tout(str(session["linux_user"]))
        return None
    if not _session_tailscale_valide(ctx, request, personne, session):
        request.state.refus_tailscale = True
        return None
    return Identite(personne, session)


def exiger_identite(request: Request) -> Identite:
    ident = identite_optionnelle(request)
    if ident is None:
        autre_compte = getattr(request.state, "refus_tailscale", False)
        raise HTTPException(401, MESSAGE_AUTRE_TAILSCALE if autre_compte else MESSAGE_NON_CONNECTE)
    return ident


def step_up_valide(request: Request, ident: Identite) -> bool:
    return contexte(request).auth.step_up_valide(ident.session, request.headers.get(configweb.ENTETE_STEP_UP))


def refuser_secours(ident: Identite) -> None:
    """Action sensible : jamais depuis une session de secours (code Telegram), même avec un step-up."""
    if ident.secours:
        raise HTTPException(403, {"message": MESSAGE_SECOURS, "secours": True})


def exiger_passkey(ident: Identite = Depends(exiger_identite)) -> Identite:  # noqa: B008
    """Session ouverte par passkey (pas par le code de secours)."""
    refuser_secours(ident)
    return ident


def exiger_step_up(request: Request, ident: Identite = Depends(exiger_identite)) -> Identite:  # noqa: B008
    """Action sensible : session passkey et step-up récent (fenêtre de 5 min, réutilisable)."""
    refuser_secours(ident)
    if not step_up_valide(request, ident):
        raise HTTPException(403, {"message": MESSAGE_STEP_UP, "step_up": True})
    return ident


def consommer_step_up(request: Request, ident: Identite) -> tuple[str, str]:
    """Step-up à usage unique (écritures du coffre) : le jeton est retiré de la session ; 403 ``step_up`` s'il a
    déjà servi (le front redemande l'empreinte). Renvoie de quoi le restituer si rien n'est écrit."""
    consomme = contexte(request).auth.consommer_step_up(
        int(ident.session["id"]), request.headers.get(configweb.ENTETE_STEP_UP)
    )
    if consomme is None:
        raise HTTPException(403, {"message": MESSAGE_STEP_UP, "step_up": True})
    return consomme


def journal(ctx: ContexteWeb, who: str | None, kind: str, resume: str, payload: dict[str, Any] | None = None,
            statut: str = dbmod.STATUS_OK) -> None:
    try:
        ctx.db.log(who, kind, resume, payload, statut)
    except Exception:
        log.warning("journal actions_log indisponible", exc_info=True)


async def corps_json(request: Request) -> dict[str, Any]:
    try:
        corps = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError : imbrication absurde
        raise HTTPException(400, "Corps JSON attendu.") from None
    if not isinstance(corps, dict):
        raise HTTPException(400, "Objet JSON attendu.")
    return corps


def _options_json(options: Any) -> dict[str, Any]:
    return json.loads(webauthn.options_to_json(options))


def _transports(valeurs: Any) -> list[AuthenticatorTransport]:
    sortie = []
    for v in str(valeurs or "").split(","):
        try:
            sortie.append(AuthenticatorTransport(v))
        except ValueError:
            continue
    return sortie


def _descripteurs(passkeys: list[dict[str, Any]]) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(id=bytes(p["credential_id"]), transports=_transports(p.get("transports")) or None)
        for p in passkeys
    ]


def ouvrir_session(ctx: ContexteWeb, request: Request, linux_user: str, methode: str) -> JSONResponse:
    personne = ctx.cfg.personne_par_linux_user(linux_user)
    assert personne is not None
    login = login_tailscale(request) or None
    jeton = ctx.auth.creer_session(linux_user, duree_session(methode), appareil(request), methode, login)
    niveau = configweb.NIVEAU_SECOURS if methode == configweb.METHODE_SECOURS else configweb.NIVEAU_PASSKEY
    journal(ctx, personne.prenom, "web_connexion", f"connexion à l'appli ({methode})",
            {"appareil": appareil(request), "niveau": niveau, "tailscale": login})
    reponse = JSONResponse({"ok": True, "personne": _personne_json(personne), "niveau": niveau})
    poser_cookie(reponse, ctx, jeton, methode)
    return reponse


def _personne_json(p: Personne) -> dict[str, Any]:
    return {"prenom": p.prenom, "linux_user": p.linux_user, "role": p.role}


# --- session ---------------------------------------------------------------------------------------------------


@routeur.get("/auth/session")
def session_courante(request: Request) -> Response:
    """État de la connexion ; renouvelle la session (30 jours glissants) et le cookie."""
    ctx = contexte(request)
    ident = identite_optionnelle(request)
    if ident is None:
        return JSONResponse({"connecte": False})
    ctx.auth.renouveler_session(int(ident.session["id"]), timedelta(days=configweb.DUREE_SESSION_JOURS))
    reponse = JSONResponse(
        {
            "connecte": True,
            "personne": _personne_json(ident.personne),
            "assistant": ctx.cfg.assistant_nom,
            "step_up": step_up_valide(request, ident),
            "passkeys": len(ctx.auth.passkeys(ident.personne.linux_user)),
            "niveau": ident.niveau,
            "tailscale": ident.session.get("tailscale_login"),
        }
    )
    if ident.secours:  # 24 h depuis l'ouverture, jamais prolongée
        reponse.set_cookie(nom_cookie(ctx), request.cookies.get(nom_cookie(ctx), ""),
                           max_age=max(0, _secondes_restantes(ident.session)), path="/",
                           secure=ctx.reglages.securise, httponly=True, samesite="strict")
    else:
        poser_cookie(reponse, ctx, request.cookies.get(nom_cookie(ctx), ""))
    return reponse


def _secondes_restantes(session: dict[str, Any]) -> int:
    try:
        cree = datetime.fromisoformat(str(session.get("cree")))
    except ValueError:
        return 0
    return int((cree + duree_session(configweb.METHODE_SECOURS) - maintenant()).total_seconds())


@routeur.post("/auth/deconnexion")
def deconnexion(request: Request, ident: Identite = Depends(exiger_identite)) -> Response:  # noqa: B008
    ctx = contexte(request)
    ctx.auth.revoquer_session(ident.personne.linux_user, int(ident.session["id"]))
    journal(ctx, ident.personne.prenom, "web_deconnexion", "déconnexion de l'appli")
    reponse = JSONResponse({"ok": True})
    reponse.delete_cookie(nom_cookie(ctx), path="/", secure=ctx.reglages.securise, httponly=True, samesite="strict")
    return reponse


# --- enrôlement ---------------------------------------------------------------------------------------------------


@routeur.post("/auth/inscription/options")
async def inscription_options(request: Request) -> dict[str, Any]:
    ctx = contexte(request)
    corps = await corps_json(request)
    jeton = str(corps.get("jeton") or "")
    invitation = None
    if jeton:
        linux_user = ctx.auth.invitation_valide(jeton)
        if linux_user is None:
            raise HTTPException(400, "Lien d'invitation invalide ou expiré : demande-en un nouveau.")
        invitation = jeton
    else:
        ident = identite_optionnelle(request)
        if ident is None:
            raise HTTPException(401, MESSAGE_NON_CONNECTE)
        linux_user = ident.personne.linux_user
        if ctx.auth.passkeys(linux_user) and not step_up_valide(request, ident):
            raise HTTPException(403, {"message": MESSAGE_STEP_UP, "step_up": True})
    personne = ctx.cfg.personne_par_linux_user(linux_user)
    if personne is None:
        raise HTTPException(400, "Cette personne ne fait plus partie du foyer.")
    options = webauthn.generate_registration_options(
        rp_id=ctx.reglages.rp_id,
        rp_name=ctx.cfg.assistant_nom,
        user_id=ctx.auth.handle(linux_user),
        user_name=linux_user,
        user_display_name=personne.prenom,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=_descripteurs(ctx.auth.passkeys(linux_user)),
    )
    ceremonie = ctx.auth.creer_ceremonie(
        "inscription", options.challenge, timedelta(seconds=configweb.DUREE_CEREMONIE_S), linux_user,
        invitation=None if invitation is None else empreinte(invitation),
    )
    return {"ceremonie": ceremonie, "options": _options_json(options)}


@routeur.post("/auth/inscription/verification")
async def inscription_verification(request: Request) -> Response:
    ctx = contexte(request)
    corps = await corps_json(request)
    ceremonie = ctx.auth.prendre_ceremonie(str(corps.get("ceremonie") or ""), "inscription")
    if ceremonie is None:
        raise HTTPException(400, "Demande expirée : recommence l'enrôlement.")
    linux_user = str(ceremonie["linux_user"])
    personne = ctx.cfg.personne_par_linux_user(linux_user)
    if personne is not None and not tailscale_autorise(ctx, request, personne):
        raise HTTPException(403, MESSAGE_TAILSCALE)
    try:
        verifie = webauthn.verify_registration_response(
            credential=corps.get("credential") or {},
            expected_challenge=bytes(ceremonie["challenge"]),
            expected_rp_id=ctx.reglages.rp_id,
            expected_origin=ctx.reglages.origine,
        )
    except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as exc:
        log.info("enrôlement refusé (%s)", exc.__class__.__name__)
        raise HTTPException(400, "Passkey refusée : recommence l'enrôlement.") from None
    # L'invitation n'est consommée (atomiquement, et seulement si elle n'a pas expiré) qu'une fois la passkey
    # acceptée : une passkey refusée ne brûle pas le lien ; deux enrôlements concurrents n'en gardent qu'un.
    if ceremonie.get("invitation") and not ctx.auth.consommer_invitation(str(ceremonie["invitation"])):
        raise HTTPException(400, "Ce lien d'invitation a déjà servi ou a expiré : demande-en un nouveau.")
    transports = [str(t) for t in ((corps.get("credential") or {}).get("response") or {}).get("transports") or []]
    nom = str(corps.get("nom") or appareil(request))[:60]
    ctx.auth.ajouter_passkey(
        linux_user, verifie.credential_id, verifie.credential_public_key, verifie.sign_count, transports, nom
    )
    journal(ctx, personne.prenom if personne else linux_user, "web_passkey", f"passkey enregistrée ({nom})")
    # Passkey ajoutée depuis une session de secours (c'est son but) : celle-ci est remplacée par la session passkey.
    courante = identite_optionnelle(request)
    if courante is not None and courante.secours and courante.personne.linux_user == linux_user:
        ctx.auth.revoquer_session(linux_user, int(courante.session["id"]))
    return ouvrir_session(ctx, request, linux_user, "passkey")


# --- connexion ------------------------------------------------------------------------------------------------------


@routeur.post("/auth/connexion/options")
def connexion_options(request: Request) -> dict[str, Any]:
    ctx = contexte(request)
    options = webauthn.generate_authentication_options(
        rp_id=ctx.reglages.rp_id, user_verification=UserVerificationRequirement.PREFERRED
    )
    ceremonie = ctx.auth.creer_ceremonie("connexion", options.challenge, timedelta(seconds=configweb.DUREE_CEREMONIE_S))
    return {"ceremonie": ceremonie, "options": _options_json(options)}


def _verifier_assertion(
    ctx: ContexteWeb, ceremonie: dict[str, Any], credential: Any, uv_requise: bool, linux_user: str | None = None
) -> dict[str, Any]:
    """Vérifie une assertion ; renvoie la passkey (compteur mis à jour). 400 sinon."""
    if not isinstance(credential, dict):
        raise HTTPException(400, "Réponse de la passkey attendue.")
    extensions = credential.get("clientExtensionResults")
    if isinstance(extensions, dict) and isinstance(extensions.get("prf"), dict) and extensions["prf"].get("results"):
        # Documents sensibles (docs/documents-sensibles.md) : la sortie PRF dérive la clé de chiffrement ; elle ne
        # quitte jamais le navigateur. Une assertion qui la contient est refusée (et n'est ni lue ni journalisée).
        raise HTTPException(400, "Le résultat PRF de la passkey ne doit jamais être envoyé au serveur.")
    credential = {**credential, "clientExtensionResults": {}}
    try:
        credential_id = base64url_to_bytes(str(credential.get("rawId") or credential.get("id") or ""))
    except (ValueError, TypeError):
        raise HTTPException(400, "Passkey inconnue.") from None
    passkey = ctx.auth.passkey_par_credential(credential_id)
    if passkey is None or (linux_user is not None and passkey["linux_user"] != linux_user):
        raise HTTPException(400, "Passkey inconnue sur ce serveur.")
    try:
        verifie = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=bytes(ceremonie["challenge"]),
            expected_rp_id=ctx.reglages.rp_id,
            expected_origin=ctx.reglages.origine,
            credential_public_key=bytes(passkey["cle_publique"]),
            credential_current_sign_count=int(passkey["compteur"]),
            require_user_verification=uv_requise,
        )
    except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError) as exc:
        log.info("assertion refusée (%s)", exc.__class__.__name__)
        raise HTTPException(400, "Passkey refusée.") from None
    ctx.auth.maj_compteur(int(passkey["id"]), verifie.new_sign_count)
    return passkey


@routeur.post("/auth/connexion/verification")
async def connexion_verification(request: Request) -> Response:
    ctx = contexte(request)
    corps = await corps_json(request)
    ceremonie = ctx.auth.prendre_ceremonie(str(corps.get("ceremonie") or ""), "connexion")
    if ceremonie is None:
        raise HTTPException(400, "Demande expirée : recommence.")
    passkey = _verifier_assertion(ctx, ceremonie, corps.get("credential"), uv_requise=False)
    personne = ctx.cfg.personne_par_linux_user(str(passkey["linux_user"]))
    if personne is None:
        raise HTTPException(403, "Cette personne ne fait plus partie du foyer.")
    if not tailscale_autorise(ctx, request, personne):
        raise HTTPException(403, MESSAGE_TAILSCALE)
    return ouvrir_session(ctx, request, str(passkey["linux_user"]), "passkey")


# --- step-up ----------------------------------------------------------------------------------------------------------


@routeur.post("/auth/step-up/options")
def step_up_options(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    passkeys = ctx.auth.passkeys(ident.personne.linux_user)
    if not passkeys:
        raise HTTPException(400, "Aucune passkey enregistrée : ajoute d'abord cet appareil.")
    options = webauthn.generate_authentication_options(
        rp_id=ctx.reglages.rp_id,
        allow_credentials=_descripteurs(passkeys),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    ceremonie = ctx.auth.creer_ceremonie(
        "step_up", options.challenge, timedelta(seconds=configweb.DUREE_CEREMONIE_S), ident.personne.linux_user
    )
    return {"ceremonie": ceremonie, "options": _options_json(options)}


@routeur.post("/auth/step-up")
async def step_up(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    corps = await corps_json(request)
    ceremonie = ctx.auth.prendre_ceremonie(str(corps.get("ceremonie") or ""), "step_up")
    if ceremonie is None or ceremonie["linux_user"] != ident.personne.linux_user:
        raise HTTPException(400, "Demande expirée : recommence.")
    _verifier_assertion(ctx, ceremonie, corps.get("credential"), uv_requise=True, linux_user=ident.personne.linux_user)
    jeton, expire = ctx.auth.accorder_step_up(int(ident.session["id"]), timedelta(seconds=configweb.DUREE_STEP_UP_S))
    journal(ctx, ident.personne.prenom, "web_step_up", "identité confirmée (step-up)")
    return {"jeton": jeton, "expire": expire, "entete": "X-Homeos-Step-Up"}


# --- code de secours par Telegram ---------------------------------------------------------------------------------


def _personne_par_nom(ctx: ContexteWeb, nom: str) -> Personne | None:
    nom = (nom or "").strip().lower()
    return next((p for p in ctx.cfg.personnes if nom in (p.linux_user.lower(), p.prenom.lower())), None)


@routeur.post("/auth/code/demande")
async def code_demande(request: Request) -> dict[str, Any]:
    """Envoie un code à 6 chiffres (10 min) en message privé Telegram. Réponse identique que la personne existe ou
    non (rien à deviner depuis le réseau)."""
    ctx = contexte(request)
    corps = await corps_json(request)
    personne = _personne_par_nom(ctx, str(corps.get("personne") or ""))
    if personne is not None and not tailscale_autorise(ctx, request, personne):
        personne = None  # compte Tailscale d'une autre personne : aucun code envoyé (réponse identique)
    if personne is not None:
        code = ctx.auth.creer_code(personne.linux_user, timedelta(minutes=configweb.DUREE_CODE_MIN))
        if code is not None and ctx.envoyer_telegram is not None:
            texte = (
                f"Code de connexion à l'appli {ctx.cfg.assistant_nom} : {code}\n"
                f"Valable {configweb.DUREE_CODE_MIN} minutes. Si tu n'as rien demandé, ignore ce message."
            )
            envoye = ctx.envoyer_telegram(personne.telegram_id, texte)
            journal(ctx, personne.prenom, "web_code", "code de connexion envoyé par Telegram" if envoye
                    else "code de connexion non envoyé", None, dbmod.STATUS_OK if envoye else dbmod.STATUS_ERROR)
    return {"ok": True, "message": "Si ce prénom est connu, un code vient d'être envoyé sur Telegram."}


@routeur.post("/auth/code/verification")
async def code_verification(request: Request) -> Response:
    ctx = contexte(request)
    corps = await corps_json(request)
    personne = _personne_par_nom(ctx, str(corps.get("personne") or ""))
    if personne is not None and not tailscale_autorise(ctx, request, personne):
        raise HTTPException(403, MESSAGE_TAILSCALE)
    if personne is None or not ctx.auth.verifier_code(personne.linux_user, str(corps.get("code") or ""),
                                                      configweb.ESSAIS_CODE):
        if personne is not None:
            journal(ctx, personne.prenom, "web_code", "code de connexion refusé", None, dbmod.STATUS_ERROR)
        raise HTTPException(400, "Code invalide ou expiré.")
    return ouvrir_session(ctx, request, personne.linux_user, configweb.METHODE_SECOURS)


# --- appareils (Réglages) -------------------------------------------------------------------------------------------


@routeur.get("/api/appareils")
def appareils(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    sessions = ctx.auth.sessions_de(ident.personne.linux_user)
    for s in sessions:
        s["courante"] = s["id"] == ident.session["id"]
        s["niveau"] = configweb.NIVEAU_SECOURS if secours(s) else configweb.NIVEAU_PASSKEY
    passkeys = [
        {"id": p["id"], "nom": p["nom"], "cree": p["cree"], "utilise": p["utilise"]}
        for p in ctx.auth.passkeys(ident.personne.linux_user)
    ]
    return {"sessions": sessions, "passkeys": passkeys, "tailscale_attendu": ident.personne.tailscale_login}


@routeur.delete("/api/appareils/sessions/{session_id}")
def revoquer_session(request: Request, session_id: int, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    ok = ctx.auth.revoquer_session(ident.personne.linux_user, session_id)
    if not ok:
        raise HTTPException(404, "Session inconnue.")
    journal(ctx, ident.personne.prenom, "web_session", f"session #{session_id} révoquée")
    return {"ok": True}


@routeur.delete("/api/appareils/passkeys/{passkey_id}")
def supprimer_passkey(request: Request, passkey_id: int, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    ctx = contexte(request)
    if not ctx.auth.supprimer_passkey(ident.personne.linux_user, passkey_id):
        raise HTTPException(404, "Passkey inconnue.")
    journal(ctx, ident.personne.prenom, "web_passkey", f"passkey #{passkey_id} supprimée")
    return {"ok": True}


def b64url(octets: bytes) -> str:
    return bytes_to_base64url(octets)
