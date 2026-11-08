"""Validations côté Telegram : boutons Valider / Annuler et envoi des captures en photo.

La logique commune (motif ``CONFIRMATION_REQUISE``, exécution par ``tools.runner``, règles de pause et de rôle,
validation automatique) vit dans :mod:`coeur.confirmations` depuis le lot 4a ; ce module en réexporte les noms et
ajoute ce qui est propre à Telegram :

- :func:`clavier_confirmation` construit les boutons inline (``confirm:<id>`` / ``cancel:<id>``) ;
- :func:`traiter_callback` traite un clic (``confirm:<id>`` / ``cancel:<id>``) via :func:`coeur.confirmations.decider` ;
- les captures signalées par ``CAPTURE:<chemin>`` (ex. sélection Kitchen Daily) partent en photo, dans le groupe
  par défaut. Seuls les fichiers sous le dossier des captures Kitchen Daily (``/var/lib/homeos/kd/captures/``, ou
  ``data/.kd/captures/`` en développement) sont acceptés.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from bot import db as dbmod
from bot.config import Config, Personne
from coeur import confirmations as coeur_confirmations
from coeur.confirmations import (  # noqa: F401  (noms historiques importés par les tests et les commandes)
    ACTIONS_HORS_PAUSE,
    EXECUTE_TIMEOUT_S,
    MESSAGE_RESERVE,
    PREFIXE_ADMIN,
    Confirmation,
    capture_autorisee,
    executer_action,
    extraire_captures,
    extraire_confirmations,
    parser_callback,
    texte_confirmation,
)

log = logging.getLogger(__name__)

EnvoyerPhoto = Callable[[Path, str], Awaitable[None]]


def clavier_confirmation(confs: list[Confirmation]) -> InlineKeyboardMarkup:
    lignes = []
    for c in confs:
        lignes.append(
            [
                InlineKeyboardButton("✅ Valider", callback_data=f"confirm:{c.action_id}"),
                InlineKeyboardButton("❌ Annuler", callback_data=f"cancel:{c.action_id}"),
            ]
        )
    return InlineKeyboardMarkup(lignes)


async def envoyer_captures(
    cfg: Config, chemins: list[Path], envoyer_photo: EnvoyerPhoto | None = None, legende: str = ""
) -> int:
    """Envoie les captures autorisées ; par défaut dans le groupe du foyer. Renvoie le nombre envoyé."""
    valides = coeur_confirmations.captures_autorisees(cfg, chemins)
    if not valides:
        return 0
    if envoyer_photo is None:
        if cfg.groupe_telegram_id is None or not cfg.telegram_token:
            log.info("captures non envoyées : groupe ou token Telegram non configuré")
            return 0

        async def envoyer_photo(chemin: Path, legende_photo: str) -> None:
            async with Bot(cfg.telegram_token) as bot:
                with chemin.open("rb") as fh:
                    await bot.send_photo(cfg.groupe_telegram_id, photo=fh, caption=legende_photo or None)

    envoyees = 0
    for chemin in valides:
        try:
            await envoyer_photo(chemin, legende)
            envoyees += 1
        except (TelegramError, OSError):
            log.exception("envoi de la capture %s impossible", chemin.name)
    return envoyees


async def traiter_callback(
    cfg: Config,
    db: dbmod.Db,
    data: str,
    who: str,
    envoyer_photo: EnvoyerPhoto | None = None,
    personne: Personne | None = None,
) -> str:
    """Traite un callback ``confirm:<id>`` ou ``cancel:<id>`` et renvoie le texte à afficher.

    ``envoyer_photo`` (facultatif) envoie une capture dans la conversation d'origine ; à défaut, les
    captures signalées par ``CAPTURE:<chemin>`` partent dans le groupe du foyer. ``personne`` : qui a cliqué
    (une action d'administration ne peut être validée que par un propriétaire).
    """
    parsed = parser_callback(data)
    if parsed is None:
        return "Bouton inconnu."
    verbe, action_id = parsed

    async def executer(cfg_: Config, aid: int) -> tuple[bool, str]:
        return await executer_action(cfg_, aid)  # nom résolu à l'appel (remplaçable dans les tests)

    async def captures(chemins: list[Path], legende: str) -> int:
        return await envoyer_captures(cfg, chemins, envoyer_photo, legende)

    return await coeur_confirmations.decider(
        cfg, db, verbe, action_id, who, personne=personne, executer=executer, envoyer_captures=captures
    )


async def executer_automatiquement(
    cfg: Config, db: dbmod.Db, confs: list[Confirmation], who: str, envoyer_photo: EnvoyerPhoto | None = None
) -> tuple[list[Confirmation], list[str]]:
    """Exécute sans bouton les actions dont la capacité est en ``validation: automatique``
    (voir :func:`coeur.confirmations.executer_automatiquement`) ; captures envoyées vers le groupe."""

    async def traiter(data: str, qui: str) -> str:
        return await traiter_callback(cfg, db, data, qui, envoyer_photo)  # nom résolu à l'appel

    return await coeur_confirmations.executer_automatiquement(cfg, db, confs, who, traiter)
