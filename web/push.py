"""Notifications Web Push de l'appli (lot 4b) : abonnements des appareils et envoi des messages spontanés.

- **Clés VAPID** : générées par ``install.sh`` dans ``/etc/homeos/push.env`` (640 root:homeos, jamais dans le dépôt) :
  ``VAPID_CLE_PRIVEE`` (32 octets en base64url), ``VAPID_CLE_PUBLIQUE`` (point P-256 non compressé, base64url, donné
  au navigateur pour s'abonner) et ``VAPID_CONTACT`` (``mailto:`` exigé par les services de push). Sans fichier :
  notifications indisponibles (``GET /api/push/cle`` → 503), l'appli fonctionne sans.
- **Abonnements** : ``POST /api/push/abonnement`` ``{endpoint, keys: {p256dh, auth}}`` (session exigée, CSRF),
  ``DELETE /api/push/abonnement`` ``{endpoint}`` ; table ``web_push`` de ``<etat>/appli.sqlite`` (600 homeos). Un
  abonnement appartient à une personne ; un service de push qui répond 404/410 le fait supprimer.
- **Envoi** (``pywebpush``, chiffrement aes128gcm de bout en bout : le service de push ne voit que du chiffré) :
  le bus de l'appli (``web.flux.Bus``) signale chaque événement à :class:`Notifieur` ; un événement **spontané**
  (``spontane``, ``message`` d'un message spontané, ``carte`` sans conversation : routine, capture d'une action
  automatique) est notifié aux appareils de la personne **quand aucun onglet de l'appli n'est ouvert** (aucun abonné
  au flux) — sinon il s'affiche déjà dans la conversation.
- **Messages spontanés** : un message écrit dans ``appli.sqlite`` avec ``canal = 'routine'`` par
  ``coeur.spontane.publier`` (brief, rappel, routine d'une capacité, alerte sur seuil, depuis un cron) est repéré par
  :func:`surveiller_spontanes` (toutes les 20 s) et publié sur le bus de sa personne (événement ``message``), donc
  affiché en direct et notifié si l'appli est fermée — avec la ``meta.notification`` du message (texte court, sans
  donnée sensible), jamais son contenu ; ``meta.notifier = false`` (quota d'une notification par jour) : pas de
  notification.

Jamais de secret ni de contenu de message dans les journaux : seulement le nombre d'envois et les codes d'erreur.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from tools.common import _lire_env
from web.auth import Identite, contexte, corps_json, exiger_identite, journal
from web.flux import EvenementBus

log = logging.getLogger(__name__)

routeur = APIRouter(prefix="/api/push")

FICHIER_CLES = "push.env"
INTERVALLE_SURVEILLANCE_S = 20.0
LONGUEUR_CORPS = 160
TTL_S = 12 * 3600
MAX_ABONNEMENTS_PAR_PERSONNE = 10
TYPES_SPONTANES = ("spontane", "message", "carte")

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_push (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linux_user TEXT NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    appareil TEXT NOT NULL DEFAULT '',
    cree TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_push_personne ON web_push(linux_user);
"""

Envoyer = Callable[[dict[str, Any], str, str, dict[str, Any]], int]


@dataclass(frozen=True)
class ClesVapid:
    privee: str
    publique: str
    contact: str


def charger_cles(dossier: Path | None = None) -> ClesVapid | None:
    """Clés VAPID de ``<HOMEOS_SECRETS>/push.env`` (défaut ``/etc/homeos``) ; None si absentes ou incomplètes."""
    racine = Path(dossier or os.environ.get("HOMEOS_SECRETS") or "/etc/homeos")
    lus = _lire_env(racine / FICHIER_CLES)
    privee = (lus.get("VAPID_CLE_PRIVEE") or "").strip()
    publique = (lus.get("VAPID_CLE_PUBLIQUE") or "").strip()
    contact = (lus.get("VAPID_CONTACT") or "mailto:admin@localhost").strip()
    if not privee or not publique:
        return None
    return ClesVapid(privee, publique, contact)


def generer_cles() -> tuple[str, str]:
    """(clé privée brute, clé publique non compressée), en base64url sans remplissage — pour ``install.sh``."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    cle = ec.generate_private_key(ec.SECP256R1())
    brute = cle.private_numbers().private_value.to_bytes(32, "big")
    publique = cle.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(octets: bytes) -> str:
        return base64.urlsafe_b64encode(octets).rstrip(b"=").decode()

    return b64(brute), b64(publique)


# --- stockage --------------------------------------------------------------------------------------------------------


class Abonnements:
    def __init__(self, chemin: Path) -> None:
        self.chemin = Path(chemin)
        self._pret = False

    @contextmanager
    def connexion(self) -> Iterator[sqlite3.Connection]:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.chemin, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if not self._pret:
                conn.executescript(SCHEMA)
                self._pret = True
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ajouter(self, linux_user: str, endpoint: str, p256dh: str, auth: str, appareil: str) -> None:
        with self.connexion() as c:
            # Un même navigateur qui se réabonne (ou change de personne) remplace son abonnement.
            c.execute("DELETE FROM web_push WHERE endpoint = ?", (endpoint,))
            c.execute(
                "INSERT INTO web_push(linux_user, endpoint, p256dh, auth, appareil, cree) VALUES (?, ?, ?, ?, ?, ?)",
                (linux_user, endpoint, p256dh, auth, appareil[:60], datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM web_push WHERE linux_user = ? ORDER BY id DESC", (linux_user,))]
            for vieux in ids[MAX_ABONNEMENTS_PAR_PERSONNE:]:
                c.execute("DELETE FROM web_push WHERE id = ?", (vieux,))

    def retirer(self, linux_user: str, endpoint: str) -> bool:
        with self.connexion() as c:
            return c.execute("DELETE FROM web_push WHERE linux_user = ? AND endpoint = ?",
                             (linux_user, endpoint)).rowcount > 0

    def retirer_endpoint(self, endpoint: str) -> None:
        with self.connexion() as c:
            c.execute("DELETE FROM web_push WHERE endpoint = ?", (endpoint,))

    def de(self, linux_user: str) -> list[dict[str, Any]]:
        with self.connexion() as c:
            return [dict(r) for r in c.execute(
                "SELECT endpoint, p256dh, auth, appareil FROM web_push WHERE linux_user = ? ORDER BY id", (linux_user,))]

    def nombre(self, linux_user: str) -> int:
        with self.connexion() as c:
            return int(c.execute("SELECT COUNT(*) FROM web_push WHERE linux_user = ?", (linux_user,)).fetchone()[0])


# --- envoi -------------------------------------------------------------------------------------------------------------


def envoyer_pywebpush(abonnement: dict[str, Any], donnees: str, cle_privee: str, revendications: dict[str, Any]) -> int:
    """Envoi réel (``pywebpush``) ; renvoie le code HTTP du service de push (0 si injoignable)."""
    from pywebpush import WebPushException, webpush

    info = {"endpoint": abonnement["endpoint"], "keys": {"p256dh": abonnement["p256dh"], "auth": abonnement["auth"]}}
    try:
        reponse = webpush(info, data=donnees, vapid_private_key=cle_privee, vapid_claims=dict(revendications),
                          ttl=TTL_S, timeout=10)
        return int(getattr(reponse, "status_code", 201) or 201)
    except WebPushException as exc:
        return int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - réseau, proxy : jamais d'appli arrêtée par une notification
        log.info("notification non envoyée (%s)", exc.__class__.__name__)
        return 0


def _corps(texte: str) -> str:
    t = " ".join(str(texte or "").split())
    for marque in ("**", "__", "`"):
        t = t.replace(marque, "")
    return t if len(t) <= LONGUEUR_CORPS else t[: LONGUEUR_CORPS - 1].rstrip() + "…"


def _sure(titre: Any, texte: Any) -> dict[str, Any]:
    """Titre et texte passés par ``coeur.spontane.notification_sure`` : jamais de montant, de suite de chiffres ni
    de donnée sensible sur un écran verrouillé (texte générique à la place)."""
    from coeur.spontane import notification_sure

    sure = notification_sure({"titre": titre or "", "corps": _corps(str(texte or ""))}, "")
    return {"titre": sure["titre"] or None, "texte": sure["corps"]}


def resume_evenement(ev: EvenementBus) -> dict[str, Any] | None:
    """Notification ``{titre?, texte, url}`` d'un événement spontané du bus, ou None s'il ne se notifie pas.

    Un message spontané (``canal = 'routine'``, ``coeur.spontane``) n'est notifié que par sa
    ``meta.notification`` (texte court écrit par le cron) et seulement si ``meta.notifier`` n'est pas faux (quota
    d'une notification par jour) : jamais le texte du message ni ses cartes (brief, montants, événements)."""
    d = ev.donnees or {}
    if ev.type == "spontane":
        return {**_sure(d.get("titre"), d.get("texte")), "url": d.get("url") or "/"}
    if ev.type == "message" and d.get("canal") == "routine" and d.get("role") in ("assistant", "systeme"):
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        if meta.get("notifier") is False:
            return None
        notification = meta.get("notification") if isinstance(meta.get("notification"), dict) else {}
        return {**_sure(notification.get("titre"), notification.get("corps")), "url": "/"}
    if ev.type == "carte" and ev.conversation_id is None:
        titre = str(d.get("titre") or "")
        return {**_sure(None, titre or "Nouvelle information"), "url": "/"}
    return None


class Notifieur:
    """Observateur du bus : notifie les événements spontanés aux appareils d'une personne sans onglet ouvert."""

    def __init__(self, abonnements: Abonnements, cles: Callable[[], ClesVapid | None], nom_assistant: Callable[[], str],
                 envoyer: Envoyer = envoyer_pywebpush) -> None:
        self.abonnements = abonnements
        self.cles = cles
        self.nom_assistant = nom_assistant
        self.envoyer = envoyer
        self.taches: set[asyncio.Task[Any]] = set()

    def __call__(self, linux_user: str, ev: EvenementBus, nb_abonnes: int) -> None:
        if ev.type not in TYPES_SPONTANES or nb_abonnes > 0:
            return
        notification = resume_evenement(ev)
        if notification is None:
            return
        try:
            tache = asyncio.get_running_loop().create_task(self.notifier(linux_user, notification))
        except RuntimeError:  # hors boucle (tests synchrones, scripts) : rien à faire
            return
        self.taches.add(tache)
        tache.add_done_callback(self.taches.discard)

    async def notifier(self, linux_user: str, notification: dict[str, Any]) -> int:
        """Envoie à tous les appareils de la personne ; renvoie le nombre d'envois réussis."""
        cles = self.cles()
        if cles is None:
            return 0
        abonnes = await asyncio.to_thread(self.abonnements.de, linux_user)
        if not abonnes:
            return 0
        charge = json.dumps({"titre": notification.get("titre") or self.nom_assistant(),
                             "texte": notification.get("texte") or "", "url": notification.get("url") or "/"},
                            ensure_ascii=False)
        reussis = 0
        for abonnement in abonnes:
            origine = urlsplit(abonnement["endpoint"])
            revendications = {"sub": cles.contact, "aud": f"{origine.scheme}://{origine.netloc}"}
            code = await asyncio.to_thread(self.envoyer, abonnement, charge, cles.privee, revendications)
            if code in (404, 410):  # abonnement expiré ou révoqué par le navigateur
                await asyncio.to_thread(self.abonnements.retirer_endpoint, abonnement["endpoint"])
            elif 200 <= code < 300:
                reussis += 1
            else:
                log.info("notification refusée par le service de push (HTTP %s)", code)
        log.info("notification envoyée à %d appareil(s) sur %d", reussis, len(abonnes))
        return reussis


# --- messages spontanés écrits par d'autres processus ---------------------------------------------------------------


def _messages_spontanes(chemin: Path, apres_id: int) -> list[dict[str, Any]]:
    if not Path(chemin).exists():
        return []
    conn = sqlite3.connect(chemin, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        try:
            lignes = conn.execute(
                "SELECT m.id, m.conversation_id, m.ts, m.role, m.canal, m.auteur, m.texte, m.cartes, m.meta, "
                "c.linux_user FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.id > ? AND m.canal = 'routine' ORDER BY m.id LIMIT 100",
                (apres_id,),
            ).fetchall()
        except sqlite3.OperationalError:  # base pas encore créée
            return []
    finally:
        conn.close()
    sortie = []
    for r in lignes:
        m = dict(r)
        for cle in ("cartes", "meta"):
            try:
                m[cle] = json.loads(m[cle]) if m.get(cle) else ([] if cle == "cartes" else {})
            except ValueError:
                m[cle] = [] if cle == "cartes" else {}
        sortie.append(m)
    return sortie


def _dernier_id(chemin: Path) -> int:
    if not Path(chemin).exists():
        return 0
    conn = sqlite3.connect(chemin, timeout=5)
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


async def surveiller_spontanes(ctx: Any, intervalle_s: float = INTERVALLE_SURVEILLANCE_S) -> None:
    """Publie sur le bus de sa personne chaque nouveau message ``canal = 'routine'`` de ``appli.sqlite``."""
    chemin = ctx.historique.chemin
    dernier = await asyncio.to_thread(_dernier_id, chemin)
    while True:
        await asyncio.sleep(intervalle_s)
        try:
            for m in await asyncio.to_thread(_messages_spontanes, chemin, dernier):
                dernier = max(dernier, int(m["id"]))
                linux_user = str(m.pop("linux_user"))
                ctx.bus.publier(linux_user, EvenementBus("message", m, int(m["conversation_id"])))
        except Exception:  # la surveillance ne s'arrête jamais sur une erreur passagère
            log.warning("surveillance des messages spontanés en erreur", exc_info=True)


# --- routes ------------------------------------------------------------------------------------------------------------


def _abonnements(request: Request) -> Abonnements:
    return request.app.state.push_abonnements


def _cles(request: Request) -> ClesVapid | None:
    return request.app.state.push_cles()


@routeur.get("/cle")
def cle_publique(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """Clé publique VAPID (``applicationServerKey`` du navigateur) ; 503 si le serveur n'a pas de clés."""
    cles = _cles(request)
    if cles is None:
        raise HTTPException(503, "Notifications indisponibles sur ce serveur.")
    return {"cle": cles.publique, "abonnements": _abonnements(request).nombre(ident.personne.linux_user)}


def _url_push_valide(endpoint: str) -> bool:
    morceaux = urlsplit(endpoint)
    return morceaux.scheme == "https" and bool(morceaux.netloc) and len(endpoint) <= 1024


@routeur.post("/abonnement")
async def abonner(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    """``{endpoint, keys: {p256dh, auth}}`` (``PushSubscription.toJSON()``) : abonne cet appareil."""
    corps = await corps_json(request)
    endpoint = str(corps.get("endpoint") or "")
    cles = corps.get("keys") if isinstance(corps.get("keys"), dict) else {}
    p256dh, auth = str(cles.get("p256dh") or ""), str(cles.get("auth") or "")
    if not _url_push_valide(endpoint) or not (20 <= len(p256dh) <= 200) or not (8 <= len(auth) <= 100):
        raise HTTPException(400, "Abonnement invalide : {endpoint https, keys: {p256dh, auth}}.")
    from web.auth import appareil

    _abonnements(request).ajouter(ident.personne.linux_user, endpoint, p256dh, auth, appareil(request))
    journal(contexte(request), ident.personne.prenom, "web_push", "notifications activées sur un appareil",
            {"appareil": appareil(request)})
    return {"ok": True}


@routeur.delete("/abonnement")
async def desabonner(request: Request, ident: Identite = Depends(exiger_identite)) -> dict[str, Any]:  # noqa: B008
    corps = await corps_json(request)
    ok = _abonnements(request).retirer(ident.personne.linux_user, str(corps.get("endpoint") or ""))
    if ok:
        journal(contexte(request), ident.personne.prenom, "web_push", "notifications coupées sur un appareil")
    return {"ok": ok}
