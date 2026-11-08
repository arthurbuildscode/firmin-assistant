"""Point d'entrée : ``python -m bot.main`` (long polling, aucun port entrant).

Variables d'environnement : ``HOMEOS_DATA`` (défaut /srv/home-os/data), ``HOMEOS_ROOT``
(défaut : racine du dépôt), ``HOMEOS_NO_SUDO=1`` (dev local : lance ``claude`` directement),
``HOMEOS_LOG_LEVEL`` (défaut INFO).
"""

from __future__ import annotations

import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import commands, handlers
from bot.claude_runner import ClaudeRunner, preparer_prompt_systeme
from bot.config import Config, load_config
from bot.context import BotContext
from bot.db import Db
from bot.sessions import Sessions
from bot.transcribe import Transcripteur
from tools.common import appliquer_proxy, proxy_sortant

os.umask(0o002)  # fichiers partagés par le groupe foyer (homeos et les personnes)

log = logging.getLogger("bot")


def configurer_logs() -> None:
    niveau = os.environ.get("HOMEOS_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, niveau, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def construire_application(cfg: Config) -> Application:
    token = cfg.telegram_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN absent (data/secrets.env ou environnement).")

    db = Db(cfg.db_path)
    db.init()
    ctx = BotContext(
        cfg=cfg,
        db=db,
        sessions=Sessions(cfg.sessions_path),
        runner=ClaudeRunner(cfg),
        transcripteur=Transcripteur(),
    )

    async def post_init(app: Application) -> None:
        app.bot_data["ctx"] = ctx
        try:
            await app.bot.set_my_commands(commands.commandes_telegram(cfg))
        except Exception:
            log.warning("Impossible d'enregistrer la liste des commandes", exc_info=True)
        me = await app.bot.get_me()
        log.info("Bot @%s démarré — %d personne(s) autorisée(s), données dans %s", me.username, len(cfg.personnes), cfg.data_dir)
        db.log(None, "bot", "démarrage", {"personnes": len(cfg.personnes)})

    constructeur = ApplicationBuilder().token(token).post_init(post_init).concurrent_updates(True)
    # Proxy sortant filtrant (lot 5d, install.sh --filtrer-sortant) : passé explicitement à httpx.
    proxy = proxy_sortant()
    serveur = proxy.get("HTTPS_PROXY") or proxy.get("https_proxy")
    if serveur:
        constructeur = constructeur.proxy(serveur).get_updates_proxy(serveur)
        log.info("Telegram via le proxy sortant %s", serveur)
    app = constructeur.build()
    app.bot_data["ctx"] = ctx

    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("nouveau", commands.cmd_nouveau))
    app.add_handler(CommandHandler("profil", commands.cmd_profil))
    app.add_handler(CommandHandler("journal", commands.cmd_journal))
    app.add_handler(CommandHandler("stop", commands.cmd_stop))
    app.add_handler(CommandHandler("reprendre", commands.cmd_reprendre))
    app.add_handler(CommandHandler("statut", commands.cmd_statut))
    app.add_handler(CommandHandler("brief", commands.cmd_brief))
    app.add_handler(CommandHandler("courses", commands.cmd_courses))
    app.add_handler(CommandHandler("identifiants", commands.cmd_identifiants))  # lot 5b
    app.add_handler(CommandHandler(["aide", "help"], commands.cmd_aide))
    app.add_handler(CommandHandler("capacites", commands.cmd_capacites))  # lot 5a
    app.add_handler(CommandHandler("admin", commands.cmd_admin))  # lot 7a
    app.add_handler(CallbackQueryHandler(handlers.on_callback, pattern=r"^(confirm|cancel):\d+$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handlers.on_vocal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_texte))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handlers.on_fichier))  # lot 3
    app.add_handler(MessageHandler(filters.Sticker.ALL | filters.VIDEO, handlers.ignorer))
    # Lot 5a : toute autre commande → commandes déclarées par les capacités actives (lues à chaque message,
    # pas besoin de redémarrer après enable/disable). Doit rester après les CommandHandler de base.
    app.add_handler(MessageHandler(filters.COMMAND, commands.cmd_capacite_dynamique))
    app.add_error_handler(handlers.on_erreur)
    return app


def main() -> None:
    configurer_logs()
    appliquer_proxy()  # sous-processus (runner des actions, briefs à la demande) et urllib
    cfg = load_config()
    if cfg.no_sudo:
        log.warning("HOMEOS_NO_SUDO=1 : `claude` est lancé directement, sans sudo (mode dev)")
    chemin = preparer_prompt_systeme(cfg)
    log.info("Prompt système écrit dans %s (assistant « %s »)", chemin, cfg.assistant_nom)
    app = construire_application(cfg)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
