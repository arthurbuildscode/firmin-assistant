"""Documents sensibles chiffrés côté client (CONTRACT §12, ``docs/documents-sensibles.md``).

Le serveur ne possède **jamais** la clé : le navigateur chiffre le document (AES-256-GCM, clé maîtresse de la personne
protégée par la passkey via l'extension WebAuthn PRF, sinon par une clé d'appareil dans IndexedDB et une phrase de
récupération affichée une fois) et dépose un **blob opaque**. Ici, on ne fait que ranger ce blob :

    data/fichiers/sensibles/<linux_user>/          700 homeos (l'appli tourne sous homeos)
        <id>.bin                                    blob chiffré, octet pour octet tel que reçu (600)
        index.json                                  métadonnées minimales : id, nom, categorie, date, taille, cle
        trousseau.json                              enveloppes PRF de la clé maîtresse (chiffrées, opaques)

- Format vérifié (sans rien déchiffrer) : ``HSB1`` | version 1 (1 octet) | identifiant de clé (16 octets) |
  IV (12 octets) | chiffré + étiquette GCM (16 octets au moins). Un fichier en clair (PDF, image…) est refusé.
- Toutes les routes exigent un **step-up** récent (``X-Homeos-Step-Up``) ; une personne ne voit que ses documents
  (ceux de l'autre : 404). Journal ``actions_log`` : identifiant, catégorie et taille, jamais le nom.
- Le modèle n'a **aucun** outil sur ce dossier : ``tools.fichiers`` ne le liste ni ne le range, la mémoire ne
  l'atteint pas, l'outil Read des sessions Claude est refusé (``tools.securite``), et le dossier est hors du dépôt
  git de data/ (``tools.memoire.GITIGNORE``). Il est sauvegardé par restic avec le reste de data/ (blobs chiffrés).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from web import config as configweb
from web.auth import Identite, contexte, corps_json, exiger_step_up, journal
from web.flux import EvenementBus

routeur = APIRouter(prefix="/api/sensibles")

MAGIC = b"HSB1"
VERSION = 1
LONGUEUR_ENTETE = 4 + 1 + 16 + 12  # magic, version, identifiant de clé, IV : données associées (AAD) du chiffrement
LONGUEUR_MIN = LONGUEUR_ENTETE + 16  # + étiquette GCM
TAILLE_MAX_BLOB = configweb.LIMITE_FICHIER + 64 * 1024
MAX_DOCUMENTS = 300
LONGUEUR_NOM = 80
TAILLE_MAX_TROUSSEAU = 16 * 1024
CATEGORIES = ("identite", "sante", "animal", "administratif", "banque", "logement", "voiture", "enfants", "autre")
RE_ID = re.compile(r"^s-[0-9a-f]{24}$")
RE_B64URL = re.compile(r"^[A-Za-z0-9_-]{1,2048}$")
CHAMPS_ENVELOPPE = {"type", "credential_id", "sel", "iv", "cle_chiffree", "cree"}
_VERROU = threading.Lock()


class BlobInvalide(ValueError):
    """Blob qui ne respecte pas l'enveloppe ``HSB1`` (fichier en clair, tronqué…)."""


def verifier_blob(debut: bytes, taille: int) -> str:
    """Contrôle l'en-tête d'un blob chiffré (sans le déchiffrer) ; renvoie l'identifiant de clé (hex)."""
    if taille < LONGUEUR_MIN or len(debut) < LONGUEUR_ENTETE:
        raise BlobInvalide("Document chiffré attendu (enveloppe trop courte).")
    if debut[:4] != MAGIC or debut[4] != VERSION:
        raise BlobInvalide("Document chiffré attendu : le serveur n'accepte jamais un document en clair.")
    return debut[5:21].hex()


# --- stockage ------------------------------------------------------------------------------------------------------


class CoffreDocuments:
    """Blobs chiffrés et métadonnées d'une personne, sous ``<data>/fichiers/sensibles/<linux_user>/``."""

    def __init__(self, racine: Path) -> None:
        self.racine = Path(racine)

    def dossier(self, linux_user: str, creer: bool = False) -> Path:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", linux_user):
            raise ValueError("personne invalide")
        d = self.racine / linux_user
        if creer:
            for chemin in (self.racine, d):
                if not chemin.exists():
                    chemin.mkdir(mode=0o700, parents=True, exist_ok=True)
                    try:
                        os.chmod(chemin, 0o700)
                    except OSError:
                        pass
        return d

    @staticmethod
    def _ecrire_json(chemin: Path, donnees: Any) -> None:
        fd, tmp = tempfile.mkstemp(dir=chemin.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(donnees, fh, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, chemin)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def index(self, linux_user: str) -> list[dict[str, Any]]:
        try:
            brut = json.loads((self.dossier(linux_user) / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in brut if isinstance(d, dict) and RE_ID.fullmatch(str(d.get("id") or ""))] \
            if isinstance(brut, list) else []

    def lister(self, linux_user: str) -> list[dict[str, Any]]:
        return sorted(self.index(linux_user), key=lambda d: str(d.get("date") or ""), reverse=True)

    def document(self, linux_user: str, doc_id: str) -> tuple[dict[str, Any], Path] | None:
        if not RE_ID.fullmatch(doc_id or ""):
            return None
        meta = next((d for d in self.index(linux_user) if d["id"] == doc_id), None)
        chemin = self.dossier(linux_user) / f"{doc_id}.bin"
        if meta is None or not chemin.is_file():
            return None
        return meta, chemin

    def deposer(self, linux_user: str, source: Path, nom: str, categorie: str, cle: str, taille: int) -> dict[str, Any]:
        with _VERROU:
            documents = self.index(linux_user)
            if len(documents) >= MAX_DOCUMENTS:
                raise ValueError(f"{MAX_DOCUMENTS} documents sensibles au plus : supprimes-en d'abord.")
            dossier = self.dossier(linux_user, creer=True)
            doc_id = f"s-{secrets.token_hex(12)}"
            cible = dossier / f"{doc_id}.bin"
            os.chmod(source, 0o600)
            os.replace(source, cible)
            meta = {"id": doc_id, "nom": nom, "categorie": categorie,
                    "date": datetime.now().astimezone().isoformat(timespec="seconds"), "taille": taille, "cle": cle}
            self._ecrire_json(dossier / "index.json", [*documents, meta])
            return meta

    def supprimer(self, linux_user: str, doc_id: str) -> bool:
        with _VERROU:
            trouve = self.document(linux_user, doc_id)
            if trouve is None:
                return False
            dossier = self.dossier(linux_user)
            self._ecrire_json(dossier / "index.json", [d for d in self.index(linux_user) if d["id"] != doc_id])
            trouve[1].unlink(missing_ok=True)
            return True

    def trousseau(self, linux_user: str) -> dict[str, Any] | None:
        try:
            brut = json.loads((self.dossier(linux_user) / "trousseau.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return brut if isinstance(brut, dict) else None

    def ecrire_trousseau(self, linux_user: str, trousseau: dict[str, Any]) -> None:
        with _VERROU:
            self._ecrire_json(self.dossier(linux_user, creer=True) / "trousseau.json", trousseau)


def valider_trousseau(corps: Any) -> dict[str, Any]:
    """``{version: 1, cle: <id hex>, enveloppes: [{type: "prf", credential_id, sel, iv, cle_chiffree, cree?}]}``.

    Seules les enveloppes PRF (clé maîtresse chiffrée par une clé dérivée de la passkey) sont gardées par le serveur :
    une enveloppe d'appareil reste dans le navigateur, une phrase de récupération n'est jamais transmise. Tout autre
    champ est refusé (rien d'autre ne peut être stocké ici par erreur)."""
    if not isinstance(corps, dict) or set(corps) - {"version", "cle", "enveloppes"}:
        raise ValueError("trousseau : {version, cle, enveloppes} attendu.")
    if corps.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(corps.get("cle") or "")):
        raise ValueError("trousseau : version 1 et cle (identifiant hexadécimal de 32 caractères) attendus.")
    enveloppes = corps.get("enveloppes")
    if not isinstance(enveloppes, list) or not 1 <= len(enveloppes) <= 10:
        raise ValueError("trousseau : 1 à 10 enveloppes.")
    for e in enveloppes:
        if not isinstance(e, dict) or set(e) - CHAMPS_ENVELOPPE or e.get("type") != "prf":
            raise ValueError("enveloppe : seules les enveloppes PRF {type, credential_id, sel, iv, cle_chiffree} "
                             "sont conservées par le serveur.")
        for champ in ("credential_id", "sel", "iv", "cle_chiffree"):
            if not RE_B64URL.fullmatch(str(e.get(champ) or "")):
                raise ValueError(f"enveloppe : {champ} en base64url attendu.")
    if len(json.dumps(corps)) > TAILLE_MAX_TROUSSEAU:
        raise ValueError("trousseau trop gros.")
    return {"version": 1, "cle": corps["cle"], "enveloppes": enveloppes,
            "maj": datetime.now().astimezone().isoformat(timespec="seconds")}


# --- routes ------------------------------------------------------------------------------------------------------------


def _coffre(request: Request) -> CoffreDocuments:
    ctx = contexte(request)
    return CoffreDocuments(ctx.cfg.data_dir / "fichiers" / "sensibles")


def _maj(request: Request, ident: Identite, action: str) -> None:
    contexte(request).bus.publier(ident.personne.linux_user, EvenementBus(
        "maj", {"domaine": "sensibles", "action": action, "par": ident.personne.prenom}))


@routeur.get("")
def lister(request: Request, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    """Documents de la personne (métadonnées seulement) et présence d'un trousseau PRF."""
    coffre = _coffre(request)
    linux_user = ident.personne.linux_user
    return {"documents": coffre.lister(linux_user), "trousseau": coffre.trousseau(linux_user) is not None,
            "categories": list(CATEGORIES)}


@routeur.get("/trousseau")
def lire_trousseau(request: Request, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    """Enveloppes PRF de la clé maîtresse (chiffrées : inutilisables sans la passkey de la personne)."""
    return {"trousseau": _coffre(request).trousseau(ident.personne.linux_user)}


@routeur.put("/trousseau")
async def ecrire_trousseau(request: Request, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    corps = await corps_json(request)
    try:
        trousseau = valider_trousseau(corps)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _coffre(request).ecrire_trousseau(ident.personne.linux_user, trousseau)
    journal(contexte(request), ident.personne.prenom, "sensible_trousseau",
            f"trousseau des documents sensibles enregistré ({len(trousseau['enveloppes'])} enveloppe(s))")
    return {"ok": True}


@routeur.post("")
async def deposer(
    request: Request,
    blob: UploadFile,
    nom: str = Form(...),
    categorie: str = Form("autre"),
    ident: Identite = Depends(exiger_step_up),  # noqa: B008
) -> dict[str, Any]:
    """Multipart ``blob`` (document **déjà chiffré** par le navigateur, enveloppe ``HSB1``), ``nom`` (libellé court
    choisi par la personne : le nom d'origine du fichier voyage chiffré), ``categorie``. Rangé tel quel."""
    nom = " ".join(str(nom or "").split())
    if not nom or len(nom) > LONGUEUR_NOM:
        raise HTTPException(400, f"nom : libellé de 1 à {LONGUEUR_NOM} caractères.")
    if categorie not in CATEGORIES:
        raise HTTPException(400, f"categorie : {', '.join(CATEGORIES)}.")
    coffre = _coffre(request)
    linux_user = ident.personne.linux_user
    dossier = coffre.dossier(linux_user, creer=True)
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix=".depot-", suffix=".bin")
    chemin = Path(tmp)
    taille, debut = 0, b""
    try:
        with os.fdopen(fd, "wb") as fh:
            while morceau := await blob.read(1024 * 1024):
                taille += len(morceau)
                if taille > TAILLE_MAX_BLOB:
                    raise HTTPException(413, "Document trop gros (20 Mo au plus).")
                if len(debut) < LONGUEUR_ENTETE:
                    debut += morceau[: LONGUEUR_ENTETE - len(debut)]
                fh.write(morceau)
        cle = verifier_blob(debut, taille)
        meta = coffre.deposer(linux_user, chemin, nom, categorie, cle, taille)
    except BlobInvalide as exc:
        raise HTTPException(415, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    finally:
        chemin.unlink(missing_ok=True)
    journal(contexte(request), ident.personne.prenom, "sensible_depot",
            f"document sensible déposé ({categorie}, {taille} octets)", {"id": meta["id"], "categorie": categorie})
    _maj(request, ident, "deposer")
    return {"document": meta}


@routeur.get("/{doc_id}")
def ouvrir(request: Request, doc_id: str, ident: Identite = Depends(exiger_step_up)) -> FileResponse:  # noqa: B008
    """Le blob chiffré, octet pour octet (le navigateur le déchiffre) ; 404 s'il n'est pas à la personne."""
    trouve = _coffre(request).document(ident.personne.linux_user, doc_id)
    if trouve is None:
        raise HTTPException(404, "Document introuvable.")
    meta, chemin = trouve
    journal(contexte(request), ident.personne.prenom, "sensible_ouverture", "document sensible ouvert",
            {"id": doc_id, "categorie": meta.get("categorie")})
    return FileResponse(chemin, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Homeos-Cle": str(meta.get("cle") or ""),
                                 "Content-Disposition": f'attachment; filename="{doc_id}.bin"'})


@routeur.delete("/{doc_id}")
def supprimer(request: Request, doc_id: str, ident: Identite = Depends(exiger_step_up)) -> dict[str, Any]:  # noqa: B008
    if not _coffre(request).supprimer(ident.personne.linux_user, doc_id):
        raise HTTPException(404, "Document introuvable.")
    journal(contexte(request), ident.personne.prenom, "sensible_suppression", "document sensible supprimé",
            {"id": doc_id})
    _maj(request, ident, "supprimer")
    return {"ok": True}
