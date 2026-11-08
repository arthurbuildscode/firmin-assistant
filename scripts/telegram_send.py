#!/srv/home-os/.venv/bin/python
"""Envoie un message dans le groupe Telegram du foyer.

Usage : telegram_send.py [--chat-id ID] [texte]   (sans texte : lit stdin)
Lit TELEGRAM_BOT_TOKEN dans l'environnement (sinon /etc/homeos/telegram.env, sinon data/secrets.env)
et groupe_telegram_id dans data/config.yaml. Aucun secret n'est journalisé.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("HOMEOS_DATA", "/srv/home-os/data"))
MAX_LEN = 4000  # limite Telegram : 4096


SECRETS = Path(os.environ.get("HOMEOS_SECRETS", "/etc/homeos"))


def lire_secrets_env() -> dict[str, str]:
    """Parse /etc/homeos/telegram.env, sinon data/secrets.env (format KEY=VALUE), sans l'exécuter."""
    env: dict[str, str] = {}
    chemin = next((c for c in (SECRETS / "telegram.env", DATA / "secrets.env") if os.access(c, os.R_OK)), None)
    if chemin is None:
        return env
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, val = ligne.split("=", 1)
        env[cle.strip()] = val.strip().strip('"').strip("'")
    return env


def lire_groupe_id() -> str | None:
    """Lit groupe_telegram_id dans config.yaml (yaml si dispo, sinon regex)."""
    chemin = DATA / "config.yaml"
    if not chemin.exists():
        return None
    texte = chemin.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load(texte) or {}
        val = cfg.get("groupe_telegram_id")
        return str(val) if val is not None else None
    except Exception:  # noqa: BLE001 — repli regex si yaml absent
        m = re.search(r"^groupe_telegram_id:\s*(-?\d+)", texte, re.MULTILINE)
        return m.group(1) if m else None


def envoyer(token: str, chat_id: str, texte: str) -> None:
    """Appelle sendMessage, en découpant si le texte dépasse la limite."""
    morceaux = [texte[i : i + MAX_LEN] for i in range(0, len(texte), MAX_LEN)] or [""]
    for morceau in morceaux:
        corps = json.dumps(
            {"chat_id": chat_id, "text": morceau, "disable_web_page_preview": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=corps,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as rep:
                reponse = json.loads(rep.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"telegram_send : HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"telegram_send : réseau indisponible ({exc.reason})") from None
        if not reponse.get("ok"):
            raise SystemExit(f"telegram_send : refus Telegram ({reponse.get('description', '?')})")


def main(argv: list[str]) -> int:
    chat_id: str | None = None
    args = list(argv)
    if len(args) >= 2 and args[0] == "--chat-id":
        chat_id = args[1]
        args = args[2:]
    texte = " ".join(args) if args else sys.stdin.read()
    texte = texte.strip()
    if not texte:
        print("telegram_send : texte vide, rien envoyé", file=sys.stderr)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or lire_secrets_env().get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("telegram_send : TELEGRAM_BOT_TOKEN absent", file=sys.stderr)
        return 2
    chat_id = chat_id or lire_groupe_id()
    if not chat_id:
        print("telegram_send : groupe_telegram_id absent de config.yaml", file=sys.stderr)
        return 2

    envoyer(token, chat_id, texte)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
