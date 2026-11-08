"""Réglages de l'appli web (lot 4a), sans secret : ``/etc/default/homeos-web-app`` (écrit par install.sh) ou
l'environnement.

    HOMEOS_WEB_ORIGINE=https://<machine>.<tailnet>.ts.net:8443   origine publique (tailscale serve), RP WebAuthn
    HOMEOS_WEB_HOTE=127.0.0.1                                      écoute locale seulement (tailscale serve devant)
    HOMEOS_WEB_PORT=8095

L'appli n'écoute jamais ailleurs qu'en local : ``tailscale serve --https=8443`` la publie sur le réseau privé
Tailscale (certificat TLS du nom MagicDNS), sans aucun port public. Vaultwarden garde ``https://<magicdns>/`` (443).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tools.common import _lire_env

FICHIER = Path("/etc/default/homeos-web-app")
PORT_DEFAUT = 8095
HOTE_DEFAUT = "127.0.0.1"
HOTES_LOCAUX = ("127.0.0.1", "::1", "localhost")

DUREE_SESSION_JOURS = 30  # session ouverte par passkey : 30 jours glissants
DUREE_SESSION_SECOURS_H = 24  # session ouverte par le code Telegram (niveau « secours ») : 24 h, jamais prolongée
DUREE_STEP_UP_S = 5 * 60
DUREE_INVITATION_MIN = 10
DUREE_CODE_MIN = 10
DUREE_CEREMONIE_S = 5 * 60
ESSAIS_CODE = 5

# Limites de taille des corps de requête (octets).
LIMITE_JSON = 256 * 1024
LIMITE_FICHIER = 20 * 1024 * 1024
LIMITE_AUDIO = 10 * 1024 * 1024
LONGUEUR_MAX_MESSAGE = 8000

ENTETE_CSRF = "x-requested-with"
VALEUR_CSRF = "homeos"
ENTETE_STEP_UP = "x-homeos-step-up"
NOM_COOKIE = "__Host-homeos_session"
# Identité Tailscale : posée par `tailscale serve` sur chaque requête venant d'un appareil d'utilisateur du tailnet
# (absente pour un appareil « tagué »), après avoir retiré celle qu'enverrait le client (anti-usurpation).
ENTETE_TAILSCALE_LOGIN = "tailscale-user-login"
ENTETE_TRANSMIS = "x-forwarded-for"  # posé par tailscale serve : la requête est passée par le proxy

METHODE_SECOURS = "code"  # web_sessions.methode d'une session ouverte par le code Telegram
NIVEAU_PASSKEY = "passkey"
NIVEAU_SECOURS = "secours"


@dataclass(frozen=True)
class ReglagesWeb:
    origine: str
    hote: str = HOTE_DEFAUT
    port: int = PORT_DEFAUT

    @property
    def rp_id(self) -> str:
        """Identifiant de la « relying party » WebAuthn : le nom d'hôte de l'origine (sans port)."""
        return urlsplit(self.origine).hostname or "localhost"

    @property
    def securise(self) -> bool:
        return self.origine.startswith("https://")


def charger(fichier: Path | None = None) -> ReglagesWeb:
    lus = _lire_env(Path(os.environ.get("HOMEOS_FICHIER_WEB", str(fichier or FICHIER))))

    def valeur(cle: str, defaut: str) -> str:
        return (os.environ.get(cle) or lus.get(cle) or defaut).strip()

    origine = valeur("HOMEOS_WEB_ORIGINE", f"http://localhost:{PORT_DEFAUT}").rstrip("/")
    hote = valeur("HOMEOS_WEB_HOTE", HOTE_DEFAUT)
    try:
        port = int(valeur("HOMEOS_WEB_PORT", str(PORT_DEFAUT)))
    except ValueError:
        port = PORT_DEFAUT
    return ReglagesWeb(origine=origine, hote=hote, port=port)
