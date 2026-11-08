"""Synthèse vocale locale (lot 4a) avec Piper (open source), voix française, cache des phrases.

Installé par ``install.sh`` dans ``/opt/piper`` : ``/opt/piper/venv/bin/piper`` (paquet ``piper-tts``, version
épinglée dans ``install/versions.env``) et ``/opt/piper/voix/<voix>.onnx`` (+ ``.onnx.json``), voix
``fr_FR-siwis-medium`` par défaut. Réglages (``/etc/default/homeos-web-app``, sans secret) : ``HOMEOS_PIPER``
(exécutable), ``HOMEOS_PIPER_VOIX`` (fichier .onnx).

Le texte est envoyé sur l'entrée standard (jamais en argument : rien dans ``ps``), le WAV produit est gardé dans
``<etat>/web/voix/<sha256(voix + texte)>.wav`` (phrases répétées : brief, cartes) ; le cache est borné.
Sans Piper : :class:`SyntheseIndisponible`, et le front utilise la synthèse du navigateur.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PIPER_DEFAUT = Path("/opt/piper/venv/bin/piper")
VOIX_DEFAUT = Path("/opt/piper/voix/fr_FR-siwis-medium.onnx")
LONGUEUR_MAX = 2000  # caractères par demande
ENTREES_MAX_CACHE = 500
DELAI_S = 60


class SyntheseIndisponible(RuntimeError):
    """Piper absent ou en échec : le front se replie sur la synthèse du navigateur."""


class Synthese:
    def __init__(self, cache: Path, piper: Path | None = None, voix: Path | None = None) -> None:
        self.cache = Path(cache)
        self.piper = Path(piper or os.environ.get("HOMEOS_PIPER") or PIPER_DEFAUT)
        self.voix = Path(voix or os.environ.get("HOMEOS_PIPER_VOIX") or VOIX_DEFAUT)
        self._verrou = asyncio.Lock()

    def disponible(self) -> bool:
        return os.access(self.piper, os.X_OK) and self.voix.is_file()

    def _cle(self, texte: str) -> str:
        return hashlib.sha256(f"{self.voix.name}\n{texte}".encode()).hexdigest()

    def _elaguer(self) -> None:
        try:
            fichiers = sorted(self.cache.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for vieux in fichiers[: max(0, len(fichiers) - ENTREES_MAX_CACHE)]:
            vieux.unlink(missing_ok=True)

    async def wav(self, texte: str) -> Path:
        """Chemin d'un WAV pour ce texte (du cache si possible). Lève SyntheseIndisponible ou ValueError."""
        texte = " ".join((texte or "").split())
        if not texte:
            raise ValueError("texte vide")
        if len(texte) > LONGUEUR_MAX:
            raise ValueError(f"texte trop long (plus de {LONGUEUR_MAX} caractères)")
        if not self.disponible():
            raise SyntheseIndisponible("synthèse vocale indisponible sur le serveur")
        self.cache.mkdir(parents=True, exist_ok=True)
        cible = self.cache / f"{self._cle(texte)}.wav"
        if cible.is_file() and cible.stat().st_size > 44:
            os.utime(cible)
            return cible
        async with self._verrou:  # un Piper à la fois (2 vCPU)
            if cible.is_file() and cible.stat().st_size > 44:
                return cible
            tmp = cible.with_name(f".{cible.stem}.{os.getpid()}.tmp.wav")
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(self.piper), "--model", str(self.voix), "--output_file", str(tmp),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise SyntheseIndisponible("synthèse vocale indisponible sur le serveur") from exc
            try:
                _, err = await asyncio.wait_for(proc.communicate(texte.encode("utf-8")), timeout=DELAI_S)
            except TimeoutError as exc:
                proc.kill()
                tmp.unlink(missing_ok=True)
                raise SyntheseIndisponible("la synthèse vocale a pris trop de temps") from exc
            if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:
                log.warning("Piper en échec (code %s) : %s", proc.returncode, err.decode("utf-8", "replace")[-300:])
                tmp.unlink(missing_ok=True)
                raise SyntheseIndisponible("la synthèse vocale a échoué")
            os.replace(tmp, cible)
        self._elaguer()
        return cible
