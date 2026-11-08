"""Handlers Telegram : messages texte, vocaux, boutons de validation, allowlist.

Flux d'un message :
  allowlist → ligne de contexte → cœur de la conversation (``coeur.conversation``, partagé avec l'appli web :
  verrou, session, ``claude -p``, confirmations, historique, journal) → rendu Telegram : streaming (édition
  ≤ 1 édition / 1,5 s), Markdown → HTML, découpage, boutons Valider/Annuler si besoin.

Photos et documents (lot 3) : téléchargés dans ``data/fichiers/entrants/`` puis confiés à Claude
avec une ligne ``[fichier reçu: <chemin absolu>]`` ; un album (media group) donne un seul prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telegram import Message, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from bot import confirmations, formatting
from bot import db as dbmod
from bot.config import Personne
from bot.context import BotContext, get_ctx
from bot.transcribe import TranscriptionError
from coeur import filtre_sensible
from coeur.cartes import rendu_texte
from coeur.conversation import ligne_contexte as coeur_ligne_contexte

log = logging.getLogger(__name__)

INTERVALLE_EDITION_S = 1.5
PLACEHOLDER = "…"
TRONCATURE_TRANSCRIPTION = 200


# ---------------------------------------------------------------------------
# Allowlist et contexte
# ---------------------------------------------------------------------------
def verifier_acces(ctx: BotContext, update: Update) -> Personne | None:
    """Renvoie la personne autorisée, ou ``None`` (et journalise l'intrus)."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return None
    personne = ctx.cfg.personne_par_telegram(user.id)
    if personne is None:
        log.warning("Message ignoré : telegram_id %s (%s) hors allowlist, chat %s", user.id, user.username, chat.id)
        try:
            ctx.db.log(
                str(user.id),
                "acces_refuse",
                f"telegram_id {user.id} hors allowlist (chat {chat.id})",
                {"username": user.username or "", "chat_id": chat.id},
                "ignored",
            )
        except Exception:
            log.exception("Impossible de journaliser l'accès refusé")
        return None
    return personne


def est_groupe(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def ligne_contexte(prenom: str, groupe: bool, quand: datetime | None = None) -> str:
    return coeur_ligne_contexte(prenom, groupe, quand, canal="telegram")


def texte_masquage(nom_assistant: str, texte: str, limite: int = 600) -> str:
    """« 🔒 Transmis à Firmin : … [carte bancaire ••1234] » (lot 5c) : ce que l'assistant a réellement reçu."""
    t = " ".join(texte.split())
    t = t if len(t) <= limite else t[: limite - 1] + "…"
    return f"🔒 Transmis à {nom_assistant} : {t}"


def nettoyer_mention(texte: str, bot_username: str | None) -> str:
    if bot_username:
        texte = re.sub(rf"@{re.escape(bot_username)}\b", "", texte, flags=re.IGNORECASE)
    return texte.strip()


# ---------------------------------------------------------------------------
# Envoi / édition sûrs
# ---------------------------------------------------------------------------
async def _editer(msg: Message, texte: str, html: bool, reply_markup=None) -> Message:
    """Édite un message ; en cas d'HTML refusé, retombe en texte brut."""
    try:
        if html:
            return await msg.edit_text(texte, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return await msg.edit_text(texte, reply_markup=reply_markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return msg
        if html and "parse" in str(exc).lower():
            log.info("HTML refusé par Telegram, envoi en texte brut (%s)", exc)
            return await msg.edit_text(_strip_html(texte), reply_markup=reply_markup)
        raise


async def _envoyer(update: Update, texte: str, html: bool = True, reply_markup=None) -> Message:
    msg = update.effective_message
    assert msg is not None
    try:
        if html:
            return await msg.reply_text(texte, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return await msg.reply_text(texte, reply_markup=reply_markup)
    except BadRequest as exc:
        if html and "parse" in str(exc).lower():
            return await msg.reply_text(_strip_html(texte), reply_markup=reply_markup)
        raise


def _strip_html(texte: str) -> str:
    import html as _html

    return _html.unescape(re.sub(r"<[^>]+>", "", texte))


async def envoyer_long(update: Update, texte_html: str, reply_markup=None) -> None:
    """Envoie un texte HTML en plusieurs messages si nécessaire (boutons sur le dernier)."""
    morceaux = formatting.decouper(texte_html) or ["(réponse vide)"]
    for i, m in enumerate(morceaux):
        await _envoyer(update, m, html=True, reply_markup=reply_markup if i == len(morceaux) - 1 else None)


# ---------------------------------------------------------------------------
# Cœur : envoyer un prompt à Claude et poster la réponse
# ---------------------------------------------------------------------------
async def repondre_via_claude(
    update: Update, context: ContextTypes.DEFAULT_TYPE, personne: Personne, texte: str
) -> None:
    """Un échange avec Claude via le cœur (``coeur.conversation``), rendu en messages Telegram : message d'attente,
    « … » édité au fil du texte, puis réponse finale (Markdown → HTML, découpage, boutons Valider/Annuler)."""
    ctx = get_ctx(context)
    groupe = est_groupe(update)
    who = personne.prenom
    msg_in = update.effective_message
    assert msg_in is not None

    if groupe and update.effective_chat is not None:
        # Utile à l'installation : l'ID du groupe (à reporter dans config.yaml) est visible dans journalctl.
        log.info(
            "Message de %s dans le groupe « %s » (chat_id=%s)",
            who,
            update.effective_chat.title,
            update.effective_chat.id,
        )

    prompt = f"{ligne_contexte(personne.prenom, groupe)}\n{texte}"
    placeholder: Message | None = None
    accum: list[str] = []
    dernier_edit = time.monotonic()
    confs: list[confirmations.Confirmation] = []
    cartes: list[dict] = []
    texte_final = ""
    async for ev in ctx.coeur.echanger(
        personne,
        texte,
        canal="telegram",
        groupe=groupe,
        prompt=prompt,
        meta_journal={"groupe": groupe, "chat_id": update.effective_chat.id if update.effective_chat else None},
        meta_message={"groupe": groupe} if groupe else None,
    ):
        if ev.type == "attente":
            await _envoyer(update, ev.donnees["message"], html=False)
        elif ev.type == "debut":
            placeholder = await _envoyer(update, PLACEHOLDER, html=False)
            dernier_edit = time.monotonic()
            try:
                await msg_in.chat.send_action(ChatAction.TYPING)
            except TelegramError:
                pass
        elif ev.type == "texte":
            accum.append(ev.donnees["delta"])
            maintenant = time.monotonic()
            if placeholder is not None and maintenant - dernier_edit >= INTERVALLE_EDITION_S:
                dernier_edit = maintenant
                apercu = "".join(accum)
                if len(apercu) > formatting.LIMITE_TELEGRAM - 10:
                    apercu = apercu[-(formatting.LIMITE_TELEGRAM - 10) :]
                try:
                    await _editer(placeholder, apercu + " ▍", html=False)
                except RetryAfter as exc:
                    dernier_edit = maintenant + float(exc.retry_after)
                except TelegramError as exc:
                    log.debug("Édition streaming ignorée : %s", exc)
        elif ev.type == "masquage":
            # Lot 5c : preuve visible de ce que l'assistant a reçu (données sensibles masquées avant transmission).
            await _envoyer(update, texte_masquage(ctx.cfg.assistant_nom, str(ev.donnees.get("texte") or "")), html=False)
        elif ev.type == "carte":
            cartes.append(ev.donnees)
        elif ev.type == "confirmation" and not ev.donnees.get("auto"):
            confs.append(confirmations.Confirmation(int(ev.donnees["id"]), str(ev.donnees.get("resume") or "")))
        elif ev.type == "erreur":
            message = f"⚠️ {ev.donnees['message']}"
            if placeholder is not None:
                await _editer(placeholder, message, html=False)
            else:
                await _envoyer(update, message, html=False)
            return
        elif ev.type == "fin":
            texte_final = str(ev.donnees.get("texte") or "")

    if placeholder is None:
        return
    # Cartes (rares sur Telegram : produites pour l'appli) : rendu texte dégradé, ajouté à la réponse.
    rendus = [r for r in (rendu_texte(c) for c in cartes) if r]
    if rendus:
        texte_final = "\n\n".join([texte_final, *rendus]) if texte_final else "\n\n".join(rendus)
    html_final = formatting.md_vers_html(texte_final) if texte_final else ""
    markup = confirmations.clavier_confirmation(confs) if confs else None

    morceaux = formatting.decouper(html_final) if html_final else []
    if not morceaux:
        morceaux = [formatting.md_vers_html(confirmations.texte_confirmation(confs))]
    elif confs:
        morceaux[-1] = morceaux[-1] + "\n\n" + formatting.md_vers_html(confirmations.texte_confirmation(confs))
        if len(morceaux[-1]) > formatting.LIMITE_TELEGRAM:
            morceaux.append(formatting.md_vers_html(confirmations.texte_confirmation(confs)))

    try:
        await _editer(placeholder, morceaux[0], html=True, reply_markup=markup if len(morceaux) == 1 else None)
        for i, m in enumerate(morceaux[1:], start=1):
            await _envoyer(update, m, html=True, reply_markup=markup if i == len(morceaux) - 1 else None)
    except TelegramError:
        log.exception("Envoi de la réponse impossible")


# ---------------------------------------------------------------------------
# Handlers Telegram
# ---------------------------------------------------------------------------
async def on_texte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    personne = verifier_acces(ctx, update)
    msg = update.effective_message
    if personne is None or msg is None or not msg.text:
        return
    texte = nettoyer_mention(msg.text, context.bot.username)
    if not texte:
        return
    await repondre_via_claude(update, context, personne, texte)


async def on_vocal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    personne = verifier_acces(ctx, update)
    msg = update.effective_message
    if personne is None or msg is None:
        return
    voice = msg.voice or msg.audio
    if voice is None:
        return
    if (voice.duration or 0) > 300:
        await _envoyer(update, "Ce vocal est trop long (5 min max).", html=False)
        return

    with tempfile.TemporaryDirectory(prefix="homeos-voice-") as tmp:
        chemin = Path(tmp) / "vocal.ogg"
        try:
            fichier = await context.bot.get_file(voice.file_id)
            await fichier.download_to_drive(custom_path=str(chemin))
        except TelegramError as exc:
            log.warning("Téléchargement du vocal impossible : %s", exc)
            await _envoyer(update, "Je n'ai pas réussi à récupérer ce vocal.", html=False)
            return
        try:
            await msg.chat.send_action(ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            texte = await ctx.transcripteur.transcrire(chemin)
        except TranscriptionError as exc:
            await dbmod.run(
                ctx.db.log, personne.prenom, "vocal", "transcription impossible", None, dbmod.STATUS_ERROR, str(exc)
            )
            await _envoyer(update, f"⚠️ {exc}", html=False)
            return
        except Exception:
            log.exception("Transcription : erreur inattendue")
            await _envoyer(update, "⚠️ La transcription a échoué.", html=False)
            return

    if not texte:
        await _envoyer(update, "🎙 Je n'ai rien compris dans ce vocal, peux-tu réessayer ?", html=False)
        return

    apercu = filtre_sensible.masquer(texte)  # lot 5c : jamais un numéro de carte ou un code renvoyé en clair
    apercu = apercu if len(apercu) <= TRONCATURE_TRANSCRIPTION else apercu[: TRONCATURE_TRANSCRIPTION - 1] + "…"
    await _envoyer(update, f"🎙 transcrit : {apercu}", html=False)
    await dbmod.run(ctx.db.log, personne.prenom, "vocal", f"transcrit ({voice.duration or 0}s)", None)
    await repondre_via_claude(update, context, personne, f"[vocal transcrit] {texte}")


# ---------------------------------------------------------------------------
# Lot 3 : photos et documents
# ---------------------------------------------------------------------------
from coeur.fichiers import (  # noqa: F401  (noms historiques, lot 3)
    CONSIGNE_ALBUM,
    CONSIGNE_FICHIER,
    EXTENSIONS_ACCEPTEES,
    MIME_EXTENSIONS,
    TAILLE_MAX_FICHIER,
    chemin_entrant,
    construire_prompt_fichiers,
    convertir_heic,
    extension_document,
    nettoyer_nom_fichier,
    taille_lisible,
)

DELAI_ALBUM_S = 1.5


@dataclass
class _Album:
    update: Update
    personne: Personne
    chemins: list[tuple[int, Path]] = field(default_factory=list)  # (rang d'arrivée, chemin)
    recus: int = 0
    legendes: list[str] = field(default_factory=list)
    en_cours: int = 0
    minuterie: asyncio.Task | None = None


_albums: dict[str, _Album] = {}


async def _telecharger(update: Update, context: ContextTypes.DEFAULT_TYPE, personne: Personne) -> Path | None:
    """Télécharge la photo / le document du message dans ``data/fichiers/entrants/``. ``None`` si refusé."""
    ctx = get_ctx(context)
    msg = update.effective_message
    assert msg is not None
    if msg.photo:
        objet = msg.photo[-1]  # la plus grande résolution
        nom_original, extension = "photo", ".jpg"
    elif msg.document is not None:
        objet = msg.document
        nom_original = msg.document.file_name or "document"
        ext = extension_document(msg.document.file_name, msg.document.mime_type)
        if ext is None:
            await _envoyer(
                update, "Je lis les PDF et les images (JPG, PNG, HEIC). Ce format-là, pas encore.", html=False
            )
            return None
        extension = ext
    else:
        return None

    taille = objet.file_size or 0
    if taille > TAILLE_MAX_FICHIER:
        await _envoyer(
            update,
            "Ce fichier dépasse 20 Mo, je ne peux pas le récupérer. Tu peux l'envoyer compressé ou en photo ?",
            html=False,
        )
        return None

    dossier = ctx.cfg.data_dir.resolve() / "fichiers" / "entrants"
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = chemin_entrant(dossier, personne.prenom, nom_original, extension)
    chemin.touch(exist_ok=False)  # réserve le nom (albums : téléchargements concurrents)
    try:
        fichier = await context.bot.get_file(objet.file_id)
        await fichier.download_to_drive(custom_path=str(chemin))
    except TelegramError as exc:
        chemin.unlink(missing_ok=True)
        log.warning("Téléchargement du fichier impossible : %s", exc)
        await dbmod.run(
            ctx.db.log,
            personne.prenom,
            "fichier",
            f"échec téléchargement {nom_original}",
            None,
            dbmod.STATUS_ERROR,
            str(exc)[:300],
        )
        await _envoyer(update, "Je n'ai pas réussi à récupérer ce fichier. Tu peux réessayer ?", html=False)
        return None

    if extension == ".heic":
        try:
            chemin = await asyncio.to_thread(convertir_heic, chemin)
        except Exception as exc:  # noqa: BLE001
            chemin.unlink(missing_ok=True)
            if str(exc) != "heic-indisponible":
                log.warning("Conversion HEIC impossible : %s", exc)
            await _envoyer(
                update,
                "Je ne sais pas lire les photos HEIC sur ce serveur. Envoie-la plutôt comme photo (pas comme fichier) ou en JPG.",
                html=False,
            )
            return None

    taille_reelle = chemin.stat().st_size if chemin.exists() else taille
    await dbmod.run(
        ctx.db.log,
        personne.prenom,
        "fichier",
        f"reçu {chemin.name} ({taille_lisible(taille_reelle)})",
        {"chemin": str(chemin), "taille": taille_reelle, "album": msg.media_group_id},
    )
    return chemin


async def _vider_album(cle: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Après ``DELAI_ALBUM_S`` sans nouvel élément (et téléchargements finis) : un seul prompt pour l'album."""
    await asyncio.sleep(DELAI_ALBUM_S)
    album = _albums.get(cle)
    if album is None or album.en_cours > 0:
        return  # le dernier téléchargement en cours relancera la minuterie
    _albums.pop(cle, None)
    if not album.chemins:
        return
    legende = " ".join(lg for lg in album.legendes if lg)
    try:
        await repondre_via_claude(
            album.update,
            context,
            album.personne,
            construire_prompt_fichiers([c for _, c in sorted(album.chemins)], legende),
        )
    except Exception:
        log.exception("Traitement de l'album %s en erreur", cle)


async def on_fichier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo ou document (pdf, jpg, png, heic) : téléchargé dans ``data/fichiers/entrants/`` puis confié à Claude."""
    ctx = get_ctx(context)
    personne = verifier_acces(ctx, update)
    msg = update.effective_message
    if personne is None or msg is None:
        return
    legende = nettoyer_mention(msg.caption or "", context.bot.username)

    groupe_media = msg.media_group_id
    if not groupe_media:
        chemin = await _telecharger(update, context, personne)
        if chemin is not None:
            await repondre_via_claude(update, context, personne, construire_prompt_fichiers([chemin], legende))
        return

    cle = f"{msg.chat_id}:{groupe_media}"
    album = _albums.get(cle)
    if album is None:
        album = _albums[cle] = _Album(update=update, personne=personne)
    if album.minuterie is not None:
        album.minuterie.cancel()
        album.minuterie = None
    album.en_cours += 1
    rang = album.recus
    album.recus += 1
    try:
        chemin = await _telecharger(update, context, personne)
    finally:
        album.en_cours -= 1
    if chemin is not None:
        album.chemins.append((rang, chemin))
    if legende:
        album.legendes.append(legende)
    if album.en_cours == 0 and _albums.get(cle) is album:
        if album.minuterie is not None:
            album.minuterie.cancel()
        album.minuterie = asyncio.create_task(_vider_album(cle, context))


# --- fin Lot 3 ---


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_ctx(context)
    query = update.callback_query
    if query is None:
        return
    personne = verifier_acces(ctx, update)
    if personne is None:
        await query.answer("Tu n'es pas autorisé.", show_alert=False)
        return
    await query.answer()
    data = query.data or ""
    try:
        texte = await confirmations.traiter_callback(ctx.cfg, ctx.db, data, personne.prenom, personne=personne)
    except Exception:
        log.exception("Callback %s en erreur", data)
        texte = "⚠️ Une erreur s'est produite pendant le traitement."
    # On retire les boutons du message d'origine et on ajoute le verdict dessous.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    if query.message is not None:
        try:
            await query.message.reply_text(texte)
        except TelegramError:
            log.exception("Réponse au callback impossible")


async def on_erreur(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception non gérée dans un handler", exc_info=context.error)


async def ignorer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Autres contenus (stickers, vidéos…) : pris en charge plus tard."""
    ctx = get_ctx(context)
    personne = verifier_acces(ctx, update)
    if personne is None:
        return
    await _envoyer(update, "Pour l'instant je comprends le texte, les vocaux, les photos et les PDF.", html=False)


# Utilisé par /start pour laisser le temps à Telegram d'afficher l'accueil avant la suite.
async def petite_pause(s: float = 0.5) -> None:
    await asyncio.sleep(s)
