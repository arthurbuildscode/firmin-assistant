"""Implémentation de référence (Python) du chiffrement des documents sensibles (``docs/documents-sensibles.md``).

Mêmes constantes que le front (``web/front/src/lib/sensibles/crypto.ts``) : enveloppe ``HSB1`` des documents, enveloppe
PRF de la clé maîtresse (HKDF-SHA-256 puis AES-GCM), enveloppe d'appareil, phrase de récupération en base32 Crockford.
Sert aux tests d'interopérabilité : vecteurs communs (``tests/fixtures/sensibles-vecteurs.json``, relus par Vitest)
et test de bout en bout dans Chromium (``tests/front/test_front_sensibles.py``)."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"HSB1"
VERSION = 1
INFO_KEK = b"homeos-sensibles-v1/kek"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def b64u(o: bytes) -> str:
    return base64.urlsafe_b64encode(o).rstrip(b"=").decode()


def d64u(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def kek_prf(prf: bytes, sel: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt=sel, info=INFO_KEK).derive(prf)


def aad_enveloppe(kid_hex: str, destinataire: str) -> bytes:
    return f"homeos-sensibles-v1|{kid_hex}|{destinataire}".encode()


def envelopper_prf(cmd: bytes, kid_hex: str, prf: bytes, sel: bytes, credential_id: str, iv: bytes) -> dict[str, str]:
    chiffre = AESGCM(kek_prf(prf, sel)).encrypt(iv, cmd, aad_enveloppe(kid_hex, credential_id))
    return {"type": "prf", "credential_id": credential_id, "sel": b64u(sel), "iv": b64u(iv), "cle_chiffree": b64u(chiffre)}


def ouvrir_enveloppe_prf(enveloppe: dict[str, Any], kid_hex: str, prf: bytes) -> bytes:
    kek = kek_prf(prf, d64u(enveloppe["sel"]))
    return AESGCM(kek).decrypt(d64u(enveloppe["iv"]), d64u(enveloppe["cle_chiffree"]),
                               aad_enveloppe(kid_hex, enveloppe["credential_id"]))


def envelopper_appareil(cmd: bytes, kid_hex: str, kek: bytes, iv: bytes) -> dict[str, str]:
    chiffre = AESGCM(kek).encrypt(iv, cmd, aad_enveloppe(kid_hex, "appareil"))
    return {"type": "appareil", "kid": kid_hex, "iv": b64u(iv), "cle_chiffree": b64u(chiffre)}


def phrase_depuis_cle(cmd: bytes) -> str:
    brut = cmd + hashlib.sha256(cmd).digest()[:2]
    bits = "".join(f"{o:08b}" for o in brut)
    bits += "0" * (-len(bits) % 5)
    texte = "".join(ALPHABET[int(bits[i:i + 5], 2)] for i in range(0, len(bits), 5))
    return "-".join(texte[i:i + 5] for i in range(0, len(texte), 5))


def cle_depuis_phrase(phrase: str) -> bytes:
    texte = phrase.upper().replace("-", "").replace(" ", "").replace("O", "0").replace("I", "1").replace("L", "1")
    if len(texte) != 55:
        raise ValueError("phrase : 55 caractères attendus")
    bits = "".join(f"{ALPHABET.index(c):05b}" for c in texte)
    brut = bytes(int(bits[i:i + 8], 2) for i in range(0, 272, 8))
    cmd, somme = brut[:32], brut[32:]
    if hashlib.sha256(cmd).digest()[:2] != somme:
        raise ValueError("phrase : somme de contrôle invalide")
    return cmd


def chiffrer_document(cmd: bytes, kid: bytes, meta: dict[str, Any], contenu: bytes, iv: bytes) -> bytes:
    """Enveloppe HSB1 : en-tête (33 octets) en données associées, texte clair = longueur JSON || JSON || contenu."""
    entete = MAGIC + bytes([VERSION]) + kid + iv
    json_meta = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode()
    return entete + AESGCM(cmd).encrypt(iv, struct.pack(">I", len(json_meta)) + json_meta + contenu, entete)


def dechiffrer_document(cmd: bytes, blob: bytes) -> tuple[dict[str, Any], bytes]:
    if blob[:4] != MAGIC or blob[4] != VERSION:
        raise ValueError("enveloppe HSB1 attendue")
    clair = AESGCM(cmd).decrypt(blob[21:33], blob[33:], blob[:33])
    longueur = struct.unpack(">I", clair[:4])[0]
    return json.loads(clair[4:4 + longueur]), clair[4 + longueur:]


def vecteurs() -> dict[str, Any]:
    """Vecteurs déterministes (entrées fixes) partagés avec les tests Vitest du front."""
    cmd = bytes(range(32))
    kid = bytes(range(100, 116))
    prf = bytes((7 * i + 3) % 256 for i in range(32))
    sel = bytes((11 * i + 5) % 256 for i in range(32))
    kek_appareil = bytes((13 * i + 1) % 256 for i in range(32))
    credential_id = b64u(bytes(range(200, 216)))
    iv_doc, iv_env, iv_app = bytes(range(12)), bytes(range(12, 24)), bytes(range(24, 36))
    contenu = b"%PDF-1.7 passeport de test \xe9\x00\xff"
    meta = {"nom": "passeport-é.pdf", "type": "application/pdf", "taille": len(contenu)}
    return {
        "cmd": cmd.hex(), "kid": kid.hex(), "prf": prf.hex(), "sel": sel.hex(), "kek_appareil": kek_appareil.hex(),
        "credential_id": credential_id, "iv_document": iv_doc.hex(), "iv_enveloppe": iv_env.hex(),
        "iv_appareil": iv_app.hex(), "meta": meta, "contenu": contenu.hex(),
        "kek_prf": kek_prf(prf, sel).hex(),
        "document": chiffrer_document(cmd, kid, meta, contenu, iv_doc).hex(),
        "enveloppe_prf": envelopper_prf(cmd, kid.hex(), prf, sel, credential_id, iv_env),
        "enveloppe_appareil": envelopper_appareil(cmd, kid.hex(), kek_appareil, iv_app),
        "phrase": phrase_depuis_cle(cmd),
    }
