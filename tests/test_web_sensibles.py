"""Documents sensibles (web/sensibles.py) : blobs chiffrés par le navigateur stockés tels quels, step-up partout,
cloisonnement entre personnes, aucune clé ni nom dans les journaux, trousseau limité aux enveloppes PRF, résultat
PRF refusé par le serveur, et aucun accès pour le modèle (outils fichiers, mémoire, lecture des sessions)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.test_web import Env, enroler, step_up


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


KID = bytes(range(16))


def blob(contenu: bytes = b"\x8a\x13" * 40) -> bytes:
    """Enveloppe HSB1 : magic, version 1, identifiant de clé, IV, « chiffré » (opaque pour le serveur)."""
    return b"HSB1" + bytes([1]) + KID + os.urandom(12) + contenu


def connecte(env: Env, linux_user: str) -> tuple[Any, dict[str, str]]:
    client = env.client()
    cle = enroler(env, client, linux_user)
    r = step_up(client, cle)
    assert r.status_code == 200, r.text
    return client, {"X-Homeos-Step-Up": r.json()["jeton"]}


def deposer(client: Any, entete: dict[str, str], donnees: bytes, nom: str = "Passeport", categorie: str = "identite") -> Any:
    return client.post("/api/sensibles", data={"nom": nom, "categorie": categorie},
                       files={"blob": ("document.bin", donnees, "application/octet-stream")}, headers=entete)


def test_step_up_exige_partout(env: Env) -> None:
    client = env.client()
    enroler(env, client)
    for methode, url in (("get", "/api/sensibles"), ("get", "/api/sensibles/s-" + "0" * 24),
                         ("delete", "/api/sensibles/s-" + "0" * 24), ("get", "/api/sensibles/trousseau")):
        r = getattr(client, methode)(url)
        assert r.status_code == 403 and r.json()["step_up"] is True, url
    r = deposer(client, {}, blob())
    assert r.status_code == 403 and r.json()["step_up"] is True


def test_blob_opaque_stocke_tel_quel_et_cloisonne(env: Env) -> None:
    alice, sa = connecte(env, "alice")
    bruno, sb = connecte(env, "bruno")
    # Un document en clair (PDF, image) est refusé : le serveur ne reçoit que du chiffré.
    assert deposer(alice, sa, b"%PDF-1.4 mon passeport en clair").status_code == 415
    assert deposer(alice, sa, b"HSB1" + bytes([1]) + KID).status_code == 415  # tronqué
    assert deposer(alice, sa, blob(), categorie="nimporte").status_code == 400
    donnees = blob()
    r = deposer(alice, sa, donnees, nom="Passeport de Nina")
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert set(doc) == {"id", "nom", "categorie", "date", "taille", "cle"}
    assert doc["cle"] == KID.hex() and doc["taille"] == len(donnees)
    dossier = env.data / "fichiers" / "sensibles" / "alice"
    assert (dossier / f"{doc['id']}.bin").read_bytes() == donnees  # octet pour octet, rien de déchiffré
    assert stat.S_IMODE(dossier.stat().st_mode) == 0o700
    assert stat.S_IMODE((dossier / f"{doc['id']}.bin").stat().st_mode) == 0o600
    assert stat.S_IMODE((dossier / "index.json").stat().st_mode) == 0o600
    # Lecture : le blob identique, sans cache.
    r = alice.get(f"/api/sensibles/{doc['id']}", headers=sa)
    assert r.status_code == 200 and r.content == donnees
    assert r.headers["cache-control"] == "no-store" and r.headers["x-homeos-cle"] == KID.hex()
    assert [d["id"] for d in alice.get("/api/sensibles", headers=sa).json()["documents"]] == [doc["id"]]
    # Bruno : ne voit rien, n'ouvre rien, ne supprime rien.
    assert bruno.get("/api/sensibles", headers=sb).json()["documents"] == []
    assert bruno.get(f"/api/sensibles/{doc['id']}", headers=sb).status_code == 404
    assert bruno.delete(f"/api/sensibles/{doc['id']}", headers=sb).status_code == 404
    assert alice.get("/api/sensibles/..%2Findex.json", headers=sa).status_code == 404
    # Journal : identifiant et catégorie, jamais le nom du document.
    lignes = [l for l in env.journal() if l["kind"].startswith("sensible_")]
    assert [l["kind"] for l in lignes] == ["sensible_depot", "sensible_ouverture"]
    assert all(l["who"] == "Alice" for l in lignes)
    assert "Nina" not in json.dumps(env.journal(), ensure_ascii=False)
    # Suppression.
    assert alice.delete(f"/api/sensibles/{doc['id']}", headers=sa).json() == {"ok": True}
    assert not (dossier / f"{doc['id']}.bin").exists()
    assert alice.get("/api/sensibles", headers=sa).json()["documents"] == []


def test_trousseau_enveloppes_prf_seulement(env: Env) -> None:
    alice, sa = connecte(env, "alice")
    bruno, sb = connecte(env, "bruno")
    enveloppe = {"type": "prf", "credential_id": "Y3JlZA", "sel": "c2Vs" * 8, "iv": "aXZpdml2aXZpdml2",
                 "cle_chiffree": "Y2hpZmZyZQ" * 4}
    trousseau = {"version": 1, "cle": KID.hex(), "enveloppes": [enveloppe]}
    assert alice.get("/api/sensibles/trousseau", headers=sa).json() == {"trousseau": None}
    assert alice.put("/api/sensibles/trousseau", json=trousseau, headers=sa).json() == {"ok": True}
    lu = alice.get("/api/sensibles/trousseau", headers=sa).json()["trousseau"]
    assert lu["enveloppes"] == [enveloppe] and lu["cle"] == KID.hex()
    assert alice.get("/api/sensibles", headers=sa).json()["trousseau"] is True
    assert bruno.get("/api/sensibles/trousseau", headers=sb).json() == {"trousseau": None}
    # Ni enveloppe d'appareil, ni phrase de récupération, ni champ inconnu : rien d'autre ne peut être stocké.
    for mauvais in (
        {**trousseau, "phrase": "abcd-efgh"},
        {**trousseau, "enveloppes": [{**enveloppe, "type": "appareil"}]},
        {**trousseau, "enveloppes": [{**enveloppe, "cle_brute": "AAAA"}]},
        {**trousseau, "enveloppes": [{**enveloppe, "sel": "pas du base64url !"}]},
        {**trousseau, "cle": "court"},
    ):
        assert alice.put("/api/sensibles/trousseau", json=mauvais, headers=sa).status_code == 400


def test_resultat_prf_jamais_accepte_par_le_serveur(env: Env) -> None:
    client = env.client()
    cle = enroler(env, client)
    r = client.post("/auth/step-up/options", json={})
    credential = cle.assertion(r.json()["options"])
    credential["clientExtensionResults"] = {"prf": {"results": {"first": "c2VjcmV0LWRlcml2ZQ"}}}
    r = client.post("/auth/step-up", json={"ceremonie": r.json()["ceremonie"], "credential": credential})
    assert r.status_code == 400 and "PRF" in r.json()["detail"]
    # Sans résultat PRF (extension simplement annoncée) : accepté.
    r = client.post("/auth/step-up/options", json={})
    credential = cle.assertion(r.json()["options"])
    credential["clientExtensionResults"] = {"prf": {"enabled": True}}
    assert client.post("/auth/step-up", json={"ceremonie": r.json()["ceremonie"],
                                               "credential": credential}).status_code == 200


def test_le_modele_n_a_aucun_acces(env: Env) -> None:
    from tools import fichiers, memoire, securite

    alice, sa = connecte(env, "alice")
    doc = deposer(alice, sa, blob()).json()["document"]
    chemin = env.data / "fichiers" / "sensibles" / "alice" / f"{doc['id']}.bin"
    # fichiers_lister / fichiers_entrants (mêmes fonctions que les outils) ne les voient pas.
    vus = json.dumps(fichiers.lister() + fichiers.entrants(), ensure_ascii=False)
    assert "sensibles" not in vus and doc["id"] not in vus
    for categorie in ("sensibles", "identite"):
        with pytest.raises(fichiers.FichierInvalide):
            fichiers.lister(categorie)
    # fichiers_ranger et photos_journal_ajouter refusent de les déplacer ou de les lire.
    for source in (str(chemin), f"fichiers/sensibles/alice/{doc['id']}.bin", "fichiers/sensibles/alice/index.json"):
        with pytest.raises(fichiers.FichierInvalide):
            fichiers.resoudre_source(source)
        with pytest.raises(fichiers.FichierInvalide):
            fichiers.ranger(source, "autre", "x", "y")
    assert chemin.exists()
    # Mémoire : hors des dossiers autorisés.
    with pytest.raises(memoire.CheminInterdit):
        memoire.resoudre("fichiers/sensibles/alice/index.md")
    # Lecture directe par la session Claude (outil Read) : refusée par les réglages, hors du dépôt git de data/.
    refus = securite.regles_refus(env.data, env.data, Path("/etc/homeos"))
    assert securite.regle_lecture(env.data, "fichiers/sensibles/**") in refus
    assert "fichiers/sensibles/" in memoire.GITIGNORE
    # La purge des fichiers temporaires ne les touche pas.
    fichiers.purger()
    assert chemin.exists()


def test_wrapper_refuse_la_lecture_par_defaut() -> None:
    texte = (Path(__file__).resolve().parent.parent / "install" / "bin" / "homeos-claude").read_text(encoding="utf-8")
    assert '"Read(/%s/fichiers/sensibles/**)"' in texte


def test_schema_de_reference_aller_retour(env: Env) -> None:
    """Implémentation de référence du schéma (docs/documents-sensibles.md) : enveloppe PRF de la clé maîtresse, blob
    HSB1 chiffré en AES-256-GCM avec l'en-tête en données associées ; dépôt, relecture, déchiffrement côté « client »."""
    import base64
    import hashlib
    import struct

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    def b64u(o: bytes) -> str:
        return base64.urlsafe_b64encode(o).rstrip(b"=").decode()

    def d64u(t: str) -> bytes:
        return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))

    # Navigateur : clé maîtresse, identifiant de clé, sortie PRF de la passkey (simulée), clé d'enveloppe HKDF.
    cmd, kid, prf, sel, credential_id = os.urandom(32), os.urandom(16), os.urandom(32), os.urandom(32), os.urandom(16)
    kek = HKDF(hashes.SHA256(), 32, salt=sel, info=b"homeos-sensibles-v1/kek").derive(prf)
    aad_env = f"homeos-sensibles-v1|{kid.hex()}|{b64u(credential_id)}".encode()
    iv_env = os.urandom(12)
    enveloppe = {"type": "prf", "credential_id": b64u(credential_id), "sel": b64u(sel), "iv": b64u(iv_env),
                 "cle_chiffree": b64u(AESGCM(kek).encrypt(iv_env, cmd, aad_env))}
    # Phrase de récupération : clé maîtresse + 2 octets de contrôle, Crockford base32, groupes de 5.
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    brut = cmd + hashlib.sha256(cmd).digest()[:2]
    bits = "".join(f"{o:08b}" for o in brut)
    bits += "0" * (-len(bits) % 5)
    phrase = "".join(alphabet[int(bits[i:i + 5], 2)] for i in range(0, len(bits), 5))
    assert len(phrase) == 55 and len("-".join(phrase[i:i + 5] for i in range(0, 55, 5))) == 65
    # Document : longueur du JSON (4 octets), JSON des métadonnées, contenu ; en-tête en données associées.
    contenu = b"%PDF-1.7 vrai passeport"
    meta = json.dumps({"nom": "passeport-nina.pdf", "type": "application/pdf", "taille": len(contenu)}).encode()
    iv = os.urandom(12)
    entete = b"HSB1" + bytes([1]) + kid + iv
    donnees = entete + AESGCM(cmd).encrypt(iv, struct.pack(">I", len(meta)) + meta + contenu, entete)

    alice, sa = connecte(env, "alice")
    assert alice.put("/api/sensibles/trousseau", json={"version": 1, "cle": kid.hex(), "enveloppes": [enveloppe]},
                     headers=sa).status_code == 200
    doc = deposer(alice, sa, donnees, nom="Passeport").json()["document"]
    assert doc["cle"] == kid.hex()
    stocke = (env.data / "fichiers" / "sensibles" / "alice" / f"{doc['id']}.bin").read_bytes()
    assert b"passeport" not in stocke and b"%PDF" not in stocke  # rien de lisible sur le serveur

    # Autre appareil : la même passkey (synchronisée) redonne la sortie PRF → clé maîtresse → document.
    env_lu = alice.get("/api/sensibles/trousseau", headers=sa).json()["trousseau"]["enveloppes"][0]
    kek2 = HKDF(hashes.SHA256(), 32, salt=d64u(env_lu["sel"]), info=b"homeos-sensibles-v1/kek").derive(prf)
    cmd2 = AESGCM(kek2).decrypt(d64u(env_lu["iv"]), d64u(env_lu["cle_chiffree"]),
                                f"homeos-sensibles-v1|{kid.hex()}|{env_lu['credential_id']}".encode())
    recu = alice.get(f"/api/sensibles/{doc['id']}", headers=sa).content
    assert recu[:4] == b"HSB1" and recu[5:21].hex() == doc["cle"]
    clair = AESGCM(cmd2).decrypt(recu[21:33], recu[33:], recu[:33])
    longueur = struct.unpack(">I", clair[:4])[0]
    assert json.loads(clair[4:4 + longueur])["nom"] == "passeport-nina.pdf" and clair[4 + longueur:] == contenu


def test_vecteurs_communs_avec_le_front() -> None:
    """Les vecteurs relus par Vitest (web/front/src/lib/sensibles/crypto.test.ts) sont ceux de la référence Python :
    le front chiffre et déchiffre octet pour octet comme elle (documents, enveloppes PRF et d'appareil, phrase)."""
    from tests.sensibles_reference import cle_depuis_phrase, dechiffrer_document, ouvrir_enveloppe_prf, vecteurs

    fichier = Path(__file__).resolve().parent / "fixtures" / "sensibles-vecteurs.json"
    v = vecteurs()
    assert json.loads(fichier.read_text(encoding="utf-8")) == v
    cmd = bytes.fromhex(v["cmd"])
    assert cle_depuis_phrase(v["phrase"]) == cmd
    assert ouvrir_enveloppe_prf(v["enveloppe_prf"], v["kid"], bytes.fromhex(v["prf"])) == cmd
    meta, contenu = dechiffrer_document(cmd, bytes.fromhex(v["document"]))
    assert meta == v["meta"] and contenu.hex() == v["contenu"]


def test_categories_de_la_maquette(env: Env) -> None:
    """Pièces d'identité, carnets de santé, carnet de l'animal, autres (écran Fichiers du front)."""
    alice, sa = connecte(env, "alice")
    assert {"identite", "sante", "animal", "autre"} <= set(alice.get("/api/sensibles", headers=sa).json()["categories"])
    assert deposer(alice, sa, blob(), nom="Carnet de santé de Rex", categorie="animal").status_code == 200
    assert deposer(alice, sa, blob(), categorie="chien").status_code == 400
