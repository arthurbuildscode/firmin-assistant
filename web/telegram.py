"""Envoi d'un message Telegram privé depuis l'appli web (code de connexion, lien d'invitation) — lot 4a.

Appel direct de l'API Bot (``sendMessage``) avec le jeton de ``/etc/homeos/telegram.env`` ; le proxy sortant éventuel
est pris dans l'environnement. Le jeton n'apparaît jamais dans les journaux (seulement le code HTTP en cas d'échec).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

URL_API = "https://api.telegram.org"


def envoyer_message(jeton: str, chat_id: int, texte: str, delai_s: float = 10) -> bool:
    if not jeton or not chat_id:
        return False
    corps = json.dumps({"chat_id": chat_id, "text": texte, "disable_web_page_preview": True}).encode("utf-8")
    requete = urllib.request.Request(
        f"{URL_API}/bot{jeton}/sendMessage", data=corps, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(requete, timeout=delai_s) as reponse:
            return 200 <= reponse.status < 300
    except urllib.error.HTTPError as exc:
        log.warning("envoi Telegram refusé (HTTP %s)", exc.code)
    except Exception as exc:  # noqa: BLE001 - URLError, OSError, http.client.HTTPException… : jamais de traceback
        # (il citerait l'URL, donc le jeton) ; seule la classe de l'erreur est journalisée.
        log.warning("envoi Telegram impossible (%s)", exc.__class__.__name__)
    return False
