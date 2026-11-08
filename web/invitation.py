"""Lien d'enrôlement à usage unique (10 min) pour l'appli web (lot 4a).

    sudo -u homeos HOMEOS_DATA=/srv/home-os/data /srv/home-os/.venv/bin/python -m web.invitation <personne> [--telegram]
    … -m web.invitation <personne> --revoquer      # déconnecte tous les appareils de la personne (passkeys gardées)

``<personne>`` : ``linux_user`` ou prénom de ``config.yaml``. Le lien (``<origine>/inscription#<jeton>``) est affiché
dans le terminal, ou envoyé en message privé Telegram à la personne avec ``--telegram``. Le jeton est dans le
fragment de l'URL : il n'arrive jamais dans un journal de serveur. Seule son empreinte est stockée
(``<etat>/appli.sqlite``) ; il ne sert qu'une fois.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from bot.config import load_config
from web import config as configweb
from web.stockage import StockageAuth


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(prog="python -m web.invitation", description="Lien d'enrôlement (passkey).")
    analyseur.add_argument("personne", help="linux_user ou prénom (config.yaml)")
    analyseur.add_argument("--telegram", action="store_true", help="envoyer le lien en message privé Telegram")
    analyseur.add_argument("--revoquer", action="store_true", help="fermer toutes les sessions de la personne")
    args = analyseur.parse_args(argv)

    cfg = load_config()
    nom = args.personne.strip().lower()
    personne = next((p for p in cfg.personnes if nom in (p.linux_user.lower(), p.prenom.lower())), None)
    if personne is None:
        print(f"Personne inconnue : {args.personne} (voir personnes[] dans config.yaml).", file=sys.stderr)
        return 2
    reglages = configweb.charger()
    stockage = StockageAuth(cfg.etat_dir / "appli.sqlite")
    if args.revoquer:
        n = stockage.revoquer_tout(personne.linux_user)
        print(f"{n} session(s) de {personne.prenom} fermée(s).")
        return 0
    jeton = stockage.creer_invitation(personne.linux_user, timedelta(minutes=configweb.DUREE_INVITATION_MIN))
    url = f"{reglages.origine}/inscription#{jeton}"
    if args.telegram:
        from web.telegram import envoyer_message

        texte = (
            f"Ton lien pour activer l'appli {cfg.assistant_nom} sur ce téléphone ou ce PC (valable "
            f"{configweb.DUREE_INVITATION_MIN} minutes, une seule fois ; Tailscale doit être connecté) :\n{url}"
        )
        if not envoyer_message(cfg.telegram_token, personne.telegram_id, texte):
            print("Envoi Telegram impossible ; voici le lien à ouvrir :", file=sys.stderr)
            print(url)
            return 1
        print(f"Lien envoyé à {personne.prenom} sur Telegram (valable {configweb.DUREE_INVITATION_MIN} min).")
        return 0
    print(f"Lien d'enrôlement de {personne.prenom} (valable {configweb.DUREE_INVITATION_MIN} min, une seule fois) :")
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
