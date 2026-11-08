"""Appli web conversationnelle (lot 4a) : application FastAPI.

    python -m web            # service homeos-web-app (127.0.0.1:8095, publié par `tailscale serve --https=8443`)

- ``web.auth`` : passkeys, sessions, step-up, code de secours ;
- ``web.api`` : conversations (SSE), confirmations, fichiers, voix, brief, routines, coffre ;
- ``web.push`` : abonnements Web Push et notification des messages spontanés (lot 4b) ;
- ``web.ecrans`` : points d'accès directs des écrans, sans modèle (courses, échéances, journal, fichiers, mémoire) ;
- ``web.sensibles`` : documents sensibles chiffrés dans le navigateur (blobs opaques, step-up) ;
- ``GET /`` (et ``/inscription``, ``/<écran>``) : l'appli (lot 4b, build statique de ``web/front`` dans
  ``web/static/app``, servi tel quel : aucun npm au déploiement) ; ``/assets/…`` (fichiers à empreinte, cache long),
  ``/sw.js`` (service worker, portée ``/``), ``/manifest.webmanifest`` (nom de l'assistant lu dans la configuration),
  ``/icones/…`` ; ``GET /demo`` : page de démonstration du lot 4a (``web/static/index.html``, sans framework) ;
  ``GET /api/sante`` : sonde sans authentification ni information.

Garde-fous communs (:class:`Garde`, ASGI pur pour ne jamais retenir un flux SSE) : CSRF (en-tête
``X-Requested-With: homeos`` sur toute écriture, quel que soit le chemin ; origine vérifiée si le navigateur
l'envoie ; cookie SameSite=Strict), taille des corps (``Content-Length`` obligatoire et borné par route), limite de
débit sur les routes d'authentification non authentifiées (``web.limites``), en-têtes de sécurité (CSP stricte avec
l'empreinte du script et du style de la page, pas d'iframe, pas de référent, HSTS derrière TLS) — y compris sur les
réponses de refus. L'appli a sa propre CSP : scripts et feuilles de style du même site seulement, plus un
**nonce par requête** pour les feuilles que les composants injectent (fenêtres modales de Radix) — jamais
``unsafe-inline``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html as htmlmod
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from web import api, auth, ecrans, limites, push, sensibles
from web import config as configweb
from web.contexte import ContexteWeb, contexte_par_defaut

log = logging.getLogger(__name__)

STATIQUE = Path(__file__).resolve().parent / "static"
APPLI = STATIQUE / "app"
METHODES_LECTURE = {"GET", "HEAD", "OPTIONS"}
# Routes joignables sans session (cérémonies WebAuthn, codes Telegram, invitations) : débit limité.
PREFIXES_AUTH_LIMITES = ("/auth/connexion/", "/auth/code/", "/auth/inscription/")
HSTS = b"max-age=31536000"
# Écrans de l'appli (routage côté navigateur) : chacun sert la même page.
ECRANS = ("conversation", "semaine", "courses", "repas", "echeances", "journal", "fichiers", "memoire", "routines",
          "reglages", "plus", "inscription")
RE_FICHIER_STATIQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
NONCE = "__CSP_NONCE__"
ASSISTANT = "__ASSISTANT__"


def limite_pour(chemin: str) -> int:
    if chemin in ("/api/fichiers", "/api/sensibles"):
        return configweb.LIMITE_FICHIER + 128 * 1024
    if chemin == "/api/voix/transcrire":
        return configweb.LIMITE_AUDIO + 64 * 1024
    return configweb.LIMITE_JSON


def _empreintes_csp(html: str) -> tuple[list[str], list[str]]:
    def hacher(contenu: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(contenu.encode("utf-8")).digest()).decode() + "'"

    scripts = [hacher(m) for m in re.findall(r"<script>(.*?)</script>", html, re.DOTALL)]
    styles = [hacher(m) for m in re.findall(r"<style>(.*?)</style>", html, re.DOTALL)]
    return scripts, styles


def politique_csp(html: str = "") -> str:
    scripts, styles = _empreintes_csp(html)
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' " + " ".join(scripts),
            "style-src 'self' " + " ".join(styles),
            "img-src 'self' data: blob: https:",  # images réelles des plats (site Kitchen Daily)
            "media-src 'self' blob:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ]
    )


class Garde:
    """CSRF, taille des corps, limite de débit et en-têtes de sécurité, sans toucher aux flux (ASGI pur)."""

    def __init__(self, app: ASGIApp, origine: str, csp: str, hsts: bool = False) -> None:
        self.app = app
        self.origine = origine.rstrip("/")
        self.csp = csp.encode("latin-1")
        self.hsts = hsts
        self.limite_client = limites.Limiteur(limites.MAX_AUTH_PAR_MINUTE)
        self.limite_globale = limites.Limiteur(limites.MAX_AUTH_GLOBAL_PAR_MINUTE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        methode = scope["method"].upper()
        chemin = scope["path"]
        entetes = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}

        async def envoyer(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = list(message.get("headers") or [])
                h += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(self), microphone=(self), geolocation=()"),
                ]
                if self.hsts:
                    h.append((b"strict-transport-security", HSTS))
                if not any(k.lower() == b"content-security-policy" for k, _ in h):
                    h.append((b"content-security-policy", self.csp))
                if chemin.startswith(("/api/", "/auth/")) and not any(k.lower() == b"cache-control" for k, _ in h):
                    h.append((b"cache-control", b"no-store"))
                message = {**message, "headers": h}
            await send(message)

        if methode not in METHODES_LECTURE:
            # Toute écriture, quel que soit le chemin : une route oubliée n'est jamais sans protection.
            if entetes.get(configweb.ENTETE_CSRF) != configweb.VALEUR_CSRF:
                await self._refus(envoyer, 403, "Requête refusée (en-tête X-Requested-With manquant).")
                return
            origine = entetes.get("origin")
            if origine and origine.rstrip("/") != self.origine:
                await self._refus(envoyer, 403, "Requête refusée (origine).")
                return
            longueur = entetes.get("content-length")
            if longueur is None and methode in ("POST", "PUT", "PATCH") and "transfer-encoding" in entetes:
                await self._refus(envoyer, 411, "Longueur du corps requise.")
                return
            try:
                if longueur is not None and int(longueur) > limite_pour(chemin):
                    await self._refus(envoyer, 413, "Corps de requête trop gros.")
                    return
            except ValueError:
                await self._refus(envoyer, 400, "Content-Length invalide.")
                return
            if chemin.startswith(PREFIXES_AUTH_LIMITES):
                cle = limites.cle_client(entetes, scope.get("client"))
                if not self.limite_client.autoriser(cle) or not self.limite_globale.autoriser("*"):
                    log.info("limite de débit atteinte sur %s", chemin)
                    await self._refus(envoyer, 429, "Trop de tentatives : réessaie dans une minute.")
                    return

        await self.app(scope, receive, envoyer)

    @staticmethod
    async def _refus(send: Send, statut: int, message: str) -> None:
        corps = JSONResponse({"detail": message}, status_code=statut)
        await corps({"type": "http"}, _recevoir_rien, send)


async def _recevoir_rien() -> Message:
    return {"type": "http.disconnect"}


def politique_csp_appli(nonce: str) -> str:
    """CSP de l'appli : rien d'inline sauf les feuilles de style portant le nonce de la requête."""
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            f"style-src 'self' 'nonce-{nonce}'",
            "img-src 'self' data: blob: https:",  # images réelles des plats (site Kitchen Daily)
            "media-src 'self' blob:",
            "frame-src blob:",  # PDF d'un document sensible déchiffré en mémoire (URL blob: créée par l'appli)
            "font-src 'self'",
            "connect-src 'self'",
            "worker-src 'self'",
            "manifest-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ]
    )


def page_appli() -> str | None:
    try:
        return (APPLI / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None


def _fichier_appli(*morceaux: str) -> Path:
    if not all(RE_FICHIER_STATIQUE.fullmatch(m) for m in morceaux):
        raise HTTPException(404, "Introuvable.")
    chemin = APPLI.joinpath(*morceaux)
    if not chemin.is_file():
        raise HTTPException(404, "Introuvable.")
    return chemin


def page_demo() -> str:
    try:
        return (STATIQUE / "index.html").read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Appli</title><p>Front absent (web/static/index.html).</p>"


def creer_app(ctx: ContexteWeb | None = None, surveiller: bool = True) -> FastAPI:
    ctx = ctx or contexte_par_defaut()

    @contextlib.asynccontextmanager
    async def cycle(app: FastAPI) -> AsyncIterator[None]:
        tache = asyncio.create_task(push.surveiller_spontanes(ctx)) if surveiller else None
        try:
            yield
        finally:
            if tache is not None:
                tache.cancel()

    app = FastAPI(title="Appli du foyer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=cycle)
    app.state.ctx = ctx
    html = page_demo()
    csp_page = politique_csp(html)
    appli = page_appli()
    # Notifications Web Push : abonnements dans appli.sqlite, clés VAPID lues une fois (install.sh les crée).
    cles_push = push.charger_cles()
    app.state.push_cles = lambda: cles_push
    app.state.push_abonnements = push.Abonnements(ctx.historique.chemin)
    app.state.push_notifieur = push.Notifieur(app.state.push_abonnements, lambda: app.state.push_cles(),
                                              lambda: ctx.cfg.assistant_nom)
    ctx.bus.observateurs.append(app.state.push_notifieur)

    @app.exception_handler(HTTPException)
    async def erreur_http(request: Request, exc: HTTPException) -> Response:
        if isinstance(exc.detail, dict):  # {"message", "step_up"} : rendu tel quel pour le front
            return JSONResponse({"detail": exc.detail.get("message"), **exc.detail}, status_code=exc.status_code)
        return await http_exception_handler(request, exc)

    @app.get("/api/sante")
    def sante() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/demo", response_class=HTMLResponse)
    def demo() -> HTMLResponse:
        return HTMLResponse(html, headers={"content-security-policy": csp_page, "cache-control": "no-store"})

    def servir_appli() -> HTMLResponse:
        if appli is None:  # build absent : la démonstration du lot 4a tient lieu d'appli
            return demo()
        nonce = secrets.token_urlsafe(18)
        page = appli.replace(NONCE, nonce).replace(ASSISTANT, htmlmod.escape(ctx.cfg.assistant_nom))
        return HTMLResponse(page, headers={"content-security-policy": politique_csp_appli(nonce),
                                           "cache-control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    def accueil() -> HTMLResponse:
        return servir_appli()

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        return FileResponse(_fichier_appli("sw.js"), media_type="text/javascript",
                            headers={"cache-control": "no-cache", "service-worker-allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifeste() -> JSONResponse:
        try:
            donnees = json.loads(_fichier_appli("manifest.webmanifest").read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(404, "Introuvable.") from None
        donnees["name"] = donnees["short_name"] = ctx.cfg.assistant_nom
        return JSONResponse(donnees, media_type="application/manifest+json", headers={"cache-control": "no-cache"})

    @app.get("/assets/{nom}")
    def ressource(nom: str) -> FileResponse:
        # Noms à empreinte (build Vite) : immuables.
        return FileResponse(_fichier_appli("assets", nom),
                            headers={"cache-control": "public, max-age=31536000, immutable"})

    @app.get("/icones/{nom}")
    def icone(nom: str) -> FileResponse:
        return FileResponse(_fichier_appli("icones", nom), headers={"cache-control": "public, max-age=86400"})

    @app.get("/{ecran}", response_class=HTMLResponse)
    def ecran(ecran: str) -> HTMLResponse:
        if ecran not in ECRANS:
            raise HTTPException(404, "Introuvable.")
        return servir_appli()

    app.include_router(auth.routeur)
    app.include_router(api.routeur)
    app.include_router(ecrans.routeur)
    app.include_router(sensibles.routeur)
    app.include_router(push.routeur)
    app.add_middleware(Garde, origine=ctx.reglages.origine, csp=politique_csp(), hsts=ctx.reglages.securise)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Les journaux d'accès d'uvicorn contiendraient les chemins (jetons d'invitation en paramètre) : coupés.
    reglages = configweb.charger()
    if reglages.hote not in configweb.HOTES_LOCAUX:
        log.warning("HOMEOS_WEB_HOTE=%s : l'appli doit écouter en local (tailscale serve devant)", reglages.hote)
    uvicorn.run(creer_app(), host=reglages.hote, port=reglages.port, access_log=False, proxy_headers=False,
                log_level="info")
