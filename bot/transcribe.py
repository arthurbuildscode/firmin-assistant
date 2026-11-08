"""Compatibilité : la transcription locale (faster-whisper) vit dans :mod:`coeur.transcription` depuis le lot 4a."""

from __future__ import annotations

from coeur.transcription import (  # noqa: F401
    LANGUE,
    MODELE,
    Transcripteur,
    TranscriptionError,
    convertir_wav,
)
