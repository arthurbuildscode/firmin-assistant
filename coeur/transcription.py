"""Transcription locale des vocaux (Telegram, appli web) avec faster-whisper (modèle ``small``, CPU, int8).

Le modèle est chargé paresseusement au premier vocal (import lazy : faster-whisper n'est pas
nécessaire pour les tests). Les OGG/Opus sont convertis en WAV 16 kHz mono via ``ffmpeg``
si faster-whisper ne les lit pas directement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODELE = "small"
LANGUE = "fr"


class TranscriptionError(Exception):
    """Message utilisateur en français dans ``str(exc)``."""


class Transcripteur:
    def __init__(self, modele: str = MODELE, langue: str = LANGUE) -> None:
        self.modele_nom = modele
        self.langue = langue
        self._modele: Any = None
        self._lock = asyncio.Lock()

    def _charger(self) -> Any:
        if self._modele is None:
            try:
                from faster_whisper import WhisperModel  # import lazy
            except ImportError as exc:
                raise TranscriptionError(
                    "La transcription vocale n'est pas disponible (faster-whisper non installé)."
                ) from exc
            log.info("Chargement du modèle faster-whisper %s (CPU, int8)…", self.modele_nom)
            self._modele = WhisperModel(self.modele_nom, device="cpu", compute_type="int8")
        return self._modele

    def _transcrire_sync(self, chemin: Path) -> str:
        modele = self._charger()
        segments, _info = modele.transcribe(
            str(chemin), language=self.langue, beam_size=1, vad_filter=True, condition_on_previous_text=False
        )
        return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()

    async def transcrire(self, chemin_ogg: Path) -> str:
        """Transcrit un fichier audio (OGG/Opus) et renvoie le texte (peut être vide)."""
        chemin = Path(chemin_ogg)
        wav: Path | None = None
        try:
            try:
                async with self._lock:
                    return await asyncio.to_thread(self._transcrire_sync, chemin)
            except TranscriptionError:
                raise
            except Exception as exc:  # noqa: BLE001 — décodage audio : on retente via ffmpeg
                log.info("Lecture directe impossible (%s), conversion ffmpeg", exc)
                wav = await convertir_wav(chemin)
                async with self._lock:
                    return await asyncio.to_thread(self._transcrire_sync, wav)
        finally:
            if wav is not None:
                wav.unlink(missing_ok=True)


async def convertir_wav(source: Path) -> Path:
    """Convertit un fichier audio en WAV 16 kHz mono avec ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise TranscriptionError("ffmpeg n'est pas installé : impossible de convertir le vocal.")
    fd, nom = tempfile.mkstemp(suffix=".wav", prefix="homeos-voice-")
    os.close(fd)
    cible = Path(nom)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(cible),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except TimeoutError as exc:
        proc.kill()
        raise TranscriptionError("La conversion audio a pris trop de temps.") from exc
    if proc.returncode != 0:
        log.warning("ffmpeg a échoué : %s", err.decode("utf-8", "replace")[-300:])
        raise TranscriptionError("Je n'ai pas réussi à lire ce vocal.")
    return cible
