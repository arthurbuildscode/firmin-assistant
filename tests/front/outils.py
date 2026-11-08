"""Outils communs du test de bout en bout du front et des captures : backend de démonstration en sous-processus,
navigateur Chromium (Playwright) avec un authentificateur WebAuthn virtuel (passkey simulée, comme
l'authentificateur logiciel de ``tests/test_web.py``)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

RACINE = Path(__file__).resolve().parents[2]


def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serveur_demo(dossier: Path, *, sans_points_acces: bool = False) -> Iterator[dict[str, Any]]:
    """Lance ``python -m tests.front.demo`` et rend ``{origine, invitation}`` quand il écoute.

    ``sans_points_acces`` : sans les points d'accès directs des écrans (``/api/courses``…), comme le backend actuel :
    le front se replie sur les cartes du fil."""
    port = port_libre()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.front.demo", "--port", str(port), "--dossier", str(dossier)],
        cwd=RACINE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(RACINE), **({"HOMEOS_DEMO_SANS_POINTS_ACCES": "1"} if sans_points_acces else {})},
    )
    try:
        assert proc.stdout is not None
        for ligne in proc.stdout:
            if ligne.startswith("{"):
                infos = json.loads(ligne)
                if infos.get("prete"):
                    break
        else:
            raise RuntimeError("serveur de démonstration non démarré : " + (proc.stderr.read() if proc.stderr else ""))
        yield infos
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def executable_chromium() -> str | None:
    """Chromium de Playwright s'il est installé, sinon Chrome/Chromium du système."""
    if os.environ.get("HOMEOS_CHROMIUM"):
        return os.environ["HOMEOS_CHROMIUM"]
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            chemin = p.chromium.executable_path
            if chemin and Path(chemin).exists():
                return None  # Playwright sait le lancer lui-même
    except Exception as exc:  # noqa: BLE001 - Playwright sans navigateur installé : repli sur Chrome du système
        print(f"Chromium de Playwright indisponible ({exc.__class__.__name__})")
    for nom in ("google-chrome", "chromium", "chromium-browser"):
        trouve = shutil.which(nom)
        if trouve:
            return trouve
    raise FileNotFoundError("aucun Chromium disponible")


def passkey_virtuelle(contexte: Any, page: Any, *, prf: bool = False) -> Any:
    """Authentificateur de plateforme virtuel (CDP WebAuthn) : passkeys découvrables, vérification de l'utilisateur ;
    ``prf`` : extension PRF (documents sensibles protégés par la passkey), sinon repli sur la clé d'appareil."""
    cdp = contexte.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "automaticPresenceSimulation": True, **({"hasPrf": True} if prf else {}),
    }})
    return cdp


def png(largeur: int, hauteur: int, couleur: tuple[int, int, int]) -> bytes:
    """Petite image PNG unie (sans dépendance) pour les documents sensibles de démonstration."""
    import struct
    import zlib

    def bloc(type_: bytes, donnees: bytes) -> bytes:
        return struct.pack(">I", len(donnees)) + type_ + donnees + struct.pack(">I", zlib.crc32(type_ + donnees) & 0xFFFFFFFF)

    lignes = b"".join(b"\x00" + bytes(couleur) * largeur for _ in range(hauteur))
    return (b"\x89PNG\r\n\x1a\n" + bloc(b"IHDR", struct.pack(">IIBBBBB", largeur, hauteur, 8, 2, 0, 0, 0))
            + bloc(b"IDAT", zlib.compress(lignes)) + bloc(b"IEND", b""))


def semer_sensibles(data: Path, linux_user: str, phrase: str, documents: list[tuple[str, str, str, bytes]],
                    kid_hex: str | None = None) -> list[dict[str, Any]]:
    """Dépose des documents chiffrés par l'implémentation de référence Python (clé retrouvée depuis la phrase de
    secours affichée par le front, identifiant de clé lu dans le stockage) : ``(nom, categorie, nom_origine, contenu)``."""
    import os
    import tempfile

    from tests.sensibles_reference import chiffrer_document, cle_depuis_phrase
    from web.sensibles import CoffreDocuments

    coffre = CoffreDocuments(data / "fichiers" / "sensibles")
    cmd = cle_depuis_phrase(phrase)
    trousseau = coffre.trousseau(linux_user)
    kid = bytes.fromhex(kid_hex or (trousseau["cle"] if trousseau else coffre.index(linux_user)[0]["cle"]))
    deposes = []
    for nom, categorie, nom_origine, contenu in documents:
        type_ = "application/pdf" if nom_origine.endswith(".pdf") else "image/png"
        blob = chiffrer_document(cmd, kid, {"nom": nom_origine, "type": type_, "taille": len(contenu)}, contenu, os.urandom(12))
        fd, tmp = tempfile.mkstemp(dir=coffre.dossier(linux_user, creer=True), prefix=".depot-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        deposes.append(coffre.deposer(linux_user, Path(tmp), nom, categorie, kid.hex(), len(blob)))
    return deposes
