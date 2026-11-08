"""Serveur d'outils MCP persistant (lot 7) : transport Streamable HTTP, un jeton par personne.

    python -m tools.server --transport http [--hote 127.0.0.1] [--port 8090]   (homeos-outils.service, sous homeos)
    python -m tools.serveur_http sonde [--url URL] [--utilisateur homeos] [--appel OUTIL]   (healthcheck, verifier.sh)

Principes (docs/spec-lot7-serveur-outils.md, CONTRACT.md §6 et §12) :

- le serveur écoute en local seulement (127.0.0.1) ; chaque requête porte `Authorization: Bearer <jeton>` ;
- les jetons sont dans `/etc/homeos/outils-jetons.env` (`JETON_<linux_user>=…`, 640 root:homeos), générés par
  `scripts/jetons-outils.sh` (install.sh) ; la personne a une copie du sien dans `~/.config/homeos/jeton` (600) ;
- `jeton → linux_user` est résolu à temps constant (empreintes SHA-256 comparées avec `hmac.compare_digest`, toutes
  les entrées parcourues) ; jeton absent ou inconnu → 401, la requête n'atteint jamais le SDK ;
- l'utilisateur résolu est placé dans la `ContextVar` de `tools.identite` le temps de la requête : `actions_log.who`,
  `ctx.personne`, `ctx.secret()`, les agendas privés et les dossiers `prive/` suivent le jeton, pas l'uid ;
- mode sans état du SDK (`stateless_http`) : chaque requête POST est autonome, la `ContextVar` suit la requête
  jusqu'à l'outil (aucune session MCP partagée entre deux jetons) ; réponses JSON (pas de flux SSE) ;
- canal (lot 4a) : l'en-tête `X-Homeos-Canal: web` (appli web) fait ajouter au résultat d'un `tools/call` un bloc
  texte `CARTE:<json>` par carte générative produite par l'outil (`tools.cartes`) ; sans l'en-tête, rien ne change ;
- `SIGHUP` (`systemctl reload homeos-outils`) relit les jetons et recharge les capacités (après enable/disable,
  install, appliqué automatiquement par homeos-appliquer-config) sans couper le service.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import signal
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from tools import cartes, identite
from tools.common import FICHIER_JETONS, get_logger, parse_env, reglages_outils, secrets_dir

log = get_logger("homeos.serveur_http")

HOTE_DEFAUT = "127.0.0.1"
PORT_DEFAUT = 8090
CHEMIN_MCP = "/mcp"
CHEMIN_SANTE = "/sante"
PREFIXE_JETON = "JETON_"
LONGUEUR_MIN_JETON = 32

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


# --- jetons ----------------------------------------------------------------------------------------------------


def fichier_jetons() -> Path:
    """`HOMEOS_JETONS` (tests), sinon `<HOMEOS_SECRETS>/outils-jetons.env`."""
    env = os.environ.get("HOMEOS_JETONS")
    return Path(env) if env else secrets_dir() / FICHIER_JETONS


def _empreinte(jeton: str) -> bytes:
    return hashlib.sha256(jeton.encode("utf-8")).digest()


class Jetons:
    """Table `jeton → linux_user`, relue sur demande (SIGHUP). Seules les empreintes restent en mémoire."""

    def __init__(self, chemin: Path | None = None) -> None:
        self.chemin = chemin or fichier_jetons()
        self._table: list[tuple[bytes, str]] = []
        self._verrou = threading.Lock()

    def charger(self) -> int:
        """Relit le fichier ; renvoie le nombre de jetons. Un fichier illisible garde la table précédente."""
        try:
            brut = parse_env(self.chemin.read_text(encoding="utf-8"))
        except OSError as exc:
            log.error("jetons illisibles (%s : %s) : table précédente conservée", self.chemin, exc.__class__.__name__)
            return len(self._table)
        table: list[tuple[bytes, str]] = []
        for cle, valeur in brut.items():
            if not cle.startswith(PREFIXE_JETON):
                continue
            utilisateur = cle[len(PREFIXE_JETON) :]
            if not identite.RE_UTILISATEUR.match(utilisateur):
                log.warning("jeton ignoré : nom d'utilisateur invalide (%r)", utilisateur[:40])
                continue
            if len(valeur) < LONGUEUR_MIN_JETON:
                log.warning("jeton de %s ignoré : trop court (relancer scripts/jetons-outils.sh)", utilisateur)
                continue
            table.append((_empreinte(valeur), utilisateur))
        with self._verrou:
            self._table = table
        log.info("%d jeton(s) chargé(s) : %s", len(table), ", ".join(sorted(u for _, u in table)) or "aucun")
        return len(table)

    def ajouter(self, utilisateur: str, jeton: str) -> None:
        """Tests : ajoute une entrée sans fichier."""
        with self._verrou:
            self._table.append((_empreinte(jeton), utilisateur))

    def utilisateurs(self) -> list[str]:
        with self._verrou:
            return sorted(u for _, u in self._table)

    def resoudre(self, jeton: str | None) -> str | None:
        """Utilisateur du jeton, None s'il est absent ou inconnu.

        Temps constant : l'empreinte du jeton présenté (longueur fixe) est comparée à TOUTES les entrées avec
        `hmac.compare_digest`, sans sortie anticipée ; le résultat ne dépend ni de la position ni du préfixe commun."""
        if not jeton:
            return None
        candidat = _empreinte(jeton)
        trouve: str | None = None
        with self._verrou:
            table = list(self._table)
        for empreinte, utilisateur in table:
            if hmac.compare_digest(candidat, empreinte) and trouve is None:
                trouve = utilisateur
        return trouve


def jeton_bearer(scope: Scope) -> str | None:
    """Valeur de `Authorization: Bearer <jeton>` (insensible à la casse du schéma), None sinon."""
    for nom, valeur in scope.get("headers") or []:
        if nom.lower() == b"authorization":
            texte = valeur.decode("latin-1").strip()
            schema, _, reste = texte.partition(" ")
            if schema.lower() == "bearer" and reste.strip():
                return reste.strip()
            return None
    return None


# --- application ASGI --------------------------------------------------------------------------------------------


async def _repondre_json(send: Send, statut: int, corps: dict[str, Any], entetes: list[tuple[bytes, bytes]] = ()) -> None:  # type: ignore[assignment]
    donnees = json.dumps(corps, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": statut,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(donnees)).encode()),
                (b"cache-control", b"no-store"),
                *entetes,
            ],
        }
    )
    await send({"type": "http.response.body", "body": donnees})


class AuthentificationJeton:
    """Intergiciel ASGI : `/sante` sans authentification ; tout le reste exige un jeton connu, sinon 401.

    L'utilisateur du jeton est placé dans `tools.identite` le temps de la requête (copié dans les tâches et fils
    lancés pendant celle-ci)."""

    def __init__(self, app: App, jetons: Jetons) -> None:
        self.app = app
        self.jetons = jetons

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # websocket : jamais
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("path") == CHEMIN_SANTE and scope.get("method") in ("GET", "HEAD"):
            await _repondre_json(send, 200, {"ok": True, "service": "homeos-outils"})
            return
        utilisateur = self.jetons.resoudre(jeton_bearer(scope))
        if utilisateur is None:
            client = scope.get("client") or ("?", 0)
            log.warning("requête refusée (jeton absent ou inconnu) depuis %s %s %s", client[0], scope.get("method"), scope.get("path"))
            await _repondre_json(
                send,
                401,
                {"erreur": "jeton absent ou inconnu"},
                [(b"www-authenticate", b'Bearer realm="homeos-outils"')],
            )
            return
        canal = entete(scope, cartes.ENTETE_CANAL)
        with identite.requete(utilisateur), cartes.requete(canal) as cartes_requete:
            if cartes.canal_courant() != "web":
                await self.app(scope, receive, send)
                return
            # Canal web (lot 4a) : la réponse JSON est retenue pour y ajouter les cartes produites par l'outil.
            await self._avec_cartes(scope, receive, send, cartes_requete)

    async def _avec_cartes(self, scope: Scope, receive: Receive, send: Send, cartes_requete: list[dict[str, Any]]) -> None:
        debut: dict[str, Any] | None = None
        morceaux: list[bytes] = []

        async def retenir(message: dict[str, Any]) -> None:
            nonlocal debut
            if message["type"] == "http.response.start":
                debut = message
                return
            if message["type"] == "http.response.body":
                morceaux.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                corps = b"".join(morceaux)
                entetes = list((debut or {}).get("headers") or [])
                type_contenu = next((v for k, v in entetes if k.lower() == b"content-type"), b"")
                if cartes_requete and type_contenu.startswith(b"application/json"):
                    corps = cartes.injecter_dans_reponse(corps, cartes_requete)
                    entetes = [(k, v) for k, v in entetes if k.lower() != b"content-length"]
                    entetes.append((b"content-length", str(len(corps)).encode()))
                await send({**(debut or {"type": "http.response.start", "status": 200}), "headers": entetes})
                await send({"type": "http.response.body", "body": corps})
                return
            await send(message)

        await self.app(scope, receive, retenir)


def entete(scope: Scope, nom: str) -> str | None:
    """Valeur d'un en-tête (nom en minuscules), None s'il est absent."""
    cle = nom.lower().encode("latin-1")
    for k, valeur in scope.get("headers") or []:
        if k.lower() == cle:
            return valeur.decode("latin-1").strip()
    return None


def application_sdk(mcp: Any, hote: str = HOTE_DEFAUT) -> App:
    """Application Streamable HTTP du SDK, sans état et en JSON (SDK mcp 2.x, sinon 1.x)."""
    try:  # SDK mcp 2.x : options passées à l'appel
        return mcp.streamable_http_app(stateless_http=True, json_response=True, host=hote)  # type: ignore[no-any-return]
    except TypeError:  # SDK mcp 1.x : options dans settings
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
        mcp.settings.streamable_http_path = CHEMIN_MCP
        return mcp.streamable_http_app()  # type: ignore[no-any-return]


def application(mcp: Any, jetons: Jetons, hote: str = HOTE_DEFAUT) -> App:
    return AuthentificationJeton(application_sdk(mcp, hote), jetons)


# --- service --------------------------------------------------------------------------------------------------------


async def servir(
    mcp: Any,
    jetons: Jetons,
    hote: str = HOTE_DEFAUT,
    port: int = PORT_DEFAUT,
    recharger: Callable[[], None] | None = None,
    pret: Callable[[int], None] | None = None,
) -> None:
    """Lance uvicorn ; `SIGHUP` → `recharger()` (jetons + capacités) dans la boucle, sans couper le service."""
    import uvicorn

    if hote not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(f"refus d'écouter sur {hote} : le serveur d'outils reste local (127.0.0.1)")
    config = uvicorn.Config(
        application(mcp, jetons, hote),
        host=hote,
        port=port,
        log_level=os.environ.get("HOMEOS_LOG_UVICORN", "warning").lower(),
        access_log=False,
        lifespan="on",
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )
    serveur = uvicorn.Server(config)
    boucle = asyncio.get_running_loop()

    def sur_sighup() -> None:
        log.info("SIGHUP : rechargement des jetons et des capacités")
        try:
            if recharger is not None:
                recharger()
            else:
                jetons.charger()
        except Exception:
            log.exception("rechargement en échec : configuration précédente conservée")

    with contextlib.suppress(NotImplementedError, RuntimeError):
        boucle.add_signal_handler(signal.SIGHUP, sur_sighup)

    async def annoncer() -> None:
        while not serveur.started:
            await asyncio.sleep(0.05)
        port_reel = port
        with contextlib.suppress(Exception):
            port_reel = serveur.servers[0].sockets[0].getsockname()[1]
        log.info("serveur d'outils MCP homeos à l'écoute sur http://%s:%s%s", hote, port_reel, CHEMIN_MCP)
        if pret is not None:
            pret(port_reel)

    tache = asyncio.create_task(annoncer())
    try:
        await serveur.serve()
    finally:
        tache.cancel()


# --- client (sonde du healthcheck, tests) ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def client(url: str, jeton: str, canal: str | None = None) -> AsyncIterator[Any]:
    """Session MCP cliente authentifiée (SDK mcp 1.x récent et 2.x : `streamable_http_client`, sinon l'ancien
    `streamablehttp_client`). `canal` : en-tête `X-Homeos-Canal` (lot 4a, « web » pour recevoir les cartes)."""
    from mcp import ClientSession  # type: ignore[import-not-found]

    entetes = {"Authorization": f"Bearer {jeton}"}
    if canal:
        entetes["X-Homeos-Canal"] = canal
    try:
        from mcp.client.streamable_http import streamable_http_client  # type: ignore[import-not-found]
        from mcp.shared._httpx_utils import create_mcp_http_client  # type: ignore[import-not-found]

        transport = streamable_http_client(url, http_client=create_mcp_http_client(headers=entetes))
    except ImportError:  # pragma: no cover - SDK mcp 1.8 à 1.2x
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import-not-found,attr-defined]

        transport = streamablehttp_client(url, headers=entetes)
    async with transport as flux, ClientSession(flux[0], flux[1]) as session:
        await session.initialize()
        yield session


def est_erreur(resultat: Any) -> bool:
    """Résultat d'appel d'outil en erreur (`isError` en SDK 1.x, `is_error` en 2.x)."""
    return bool(getattr(resultat, "isError", None) or getattr(resultat, "is_error", None))


def texte_resultat(resultat: Any) -> str:
    """Texte d'un résultat d'appel d'outil (contenu textuel concaténé)."""
    morceaux = []
    for bloc in getattr(resultat, "content", None) or []:
        texte = getattr(bloc, "text", None)
        if texte:
            morceaux.append(texte)
    return "\n".join(morceaux)


def lire_jeton(utilisateur: str, fichier: Path | None = None) -> str:
    """Jeton de `utilisateur` dans outils-jetons.env (sonde lancée sous homeos ou root)."""
    brut = parse_env((fichier or fichier_jetons()).read_text(encoding="utf-8"))
    jeton = brut.get(PREFIXE_JETON + utilisateur, "")
    if not jeton:
        raise SystemExit(f"aucun jeton pour {utilisateur} dans {fichier or fichier_jetons()} (scripts/jetons-outils.sh)")
    return jeton


def code_sans_jeton(url: str) -> int:
    """Code HTTP d'un POST sans jeton (attendu : 401)."""
    import urllib.error
    import urllib.request

    requete = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=10) as reponse:
            return int(reponse.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


async def sonder(url: str, jeton: str, appel: str | None = None) -> dict[str, Any]:
    """`initialize` + `tools/list` avec le jeton, et un appel d'outil sans argument si demandé."""
    rapport: dict[str, Any] = {"url": url}
    async with client(url, jeton) as session:
        outils = await session.list_tools()
        rapport["outils"] = len(outils.tools)
        if appel:
            resultat = await session.call_tool(appel, {})
            rapport["appel"] = appel
            rapport["appel_erreur"] = est_erreur(resultat)
            rapport["appel_apercu"] = texte_resultat(resultat)[:200]
    return rapport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.serveur_http", description="Serveur d'outils homeos (lot 7)")
    sub = parser.add_subparsers(dest="commande", required=True)
    p_sonde = sub.add_parser("sonde", help="vérifie le service avec le jeton d'un utilisateur (défaut : homeos)")
    p_sonde.add_argument("--url", default=None)
    p_sonde.add_argument("--utilisateur", default="homeos")
    p_sonde.add_argument("--appel", default=None, help="outil à appeler sans argument (ex. capacites_lister)")
    args = parser.parse_args(argv)

    if args.commande == "sonde":
        url = args.url or reglages_outils()["url"]
        jeton = lire_jeton(args.utilisateur)
        try:
            sans_jeton = code_sans_jeton(url)
            rapport = asyncio.run(sonder(url, jeton, args.appel))
            rapport["sans_jeton"] = sans_jeton
        except Exception as exc:  # noqa: BLE001 - ExceptionGroup d'anyio compris
            print(json.dumps({"ok": False, "url": url, "erreur": f"{exc.__class__.__name__}: {exc}"[:300]}, ensure_ascii=False))
            return 1
        ok = rapport.get("sans_jeton") == 401 and rapport.get("outils", 0) > 0 and not rapport.get("appel_erreur")
        rapport["ok"] = ok
        print(json.dumps(rapport, ensure_ascii=False))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
