#!/usr/bin/env bash
# Tailscale (client open source, réseau privé chiffré entre vos appareils et le VPS) — root, idempotent.
# Choix de conception : Vaultwarden et l'appli web ne sont jamais exposés sur Internet ; ils n'écoutent
# que sur l'adresse Tailscale du serveur (100.x.y.z), joignable uniquement depuis vos appareils enrôlés.
#
# Usage : sudo [TAILSCALE_CLE=tskey-…] bash /srv/home-os/install/tailscale.sh [--hostname <nom>]
#   (nom par défaut : firmin ; installer-un-foyer.sh passe le prénom choisi pour l'assistant).
#   TAILSCALE_CLE (facultatif, lot 6) : clé d'authentification de la console Tailscale, passée par un fichier
#   temporaire (jamais en argument) : pas d'URL à ouvrir pendant l'installation.
#   1. ajoute le dépôt officiel (clé GPG vérifiée, pas de « curl | sh ») et installe le paquet ;
#   2. lance `tailscale up` : affiche une URL d'autorisation à ouvrir sur votre téléphone ou PC ;
#   3. autorise le trafic Tailscale dans ufw (interface tailscale0) sans ouvrir de port public ;
#   4. affiche l'adresse Tailscale du serveur, à utiliser dans les apps Bitwarden et l'appli web.
# Pour le téléphone et le PC : installer l'app Tailscale (Play Store / Microsoft Store), se connecter avec le
# même compte (Google ou GitHub), c'est tout. Tailscale (service de coordination) est le seul tiers : il ne
# voit jamais le contenu du trafic (chiffré de bout en bout, WireGuard) ; alternative auto-hébergée : Headscale.
set -euo pipefail
HOSTNAME_TS="firmin"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_TS="${2:-$HOSTNAME_TS}"; shift 2 ;;
    --hostname=*) HOSTNAME_TS="${1#*=}"; shift ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done
[[ "$HOSTNAME_TS" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || { echo "--hostname invalide : $HOSTNAME_TS" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "à lancer en root (sudo)" >&2; exit 2; }
etape() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

etape "Dépôt Tailscale"
. /etc/os-release
install -d -m 755 /usr/share/keyrings
if [[ ! -s /usr/share/keyrings/tailscale-archive-keyring.gpg ]]; then
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  chmod 644 /usr/share/keyrings/tailscale-archive-keyring.gpg
fi
depot="deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu ${VERSION_CODENAME} main"
if [[ "$(cat /etc/apt/sources.list.d/tailscale.list 2>/dev/null)" != "$depot" ]]; then
  printf '%s\n' "$depot" >/etc/apt/sources.list.d/tailscale.list
  apt-get update -qq
fi
command -v tailscale >/dev/null || apt-get install -y -qq tailscale >/dev/null
systemctl enable --now tailscaled >/dev/null

etape "Pare-feu : trafic Tailscale autorisé (aucun port public ouvert)"
ufw allow in on tailscale0 >/dev/null
# Sortie UDP 41641 (WireGuard) : déjà permise par la politique sortante.

etape "Connexion"
if tailscale status >/dev/null 2>&1 && tailscale ip -4 >/dev/null 2>&1; then
  echo "    déjà connecté : $(tailscale ip -4)"
else
  if [[ -n "${TAILSCALE_CLE:-}" ]]; then
    fichier_cle="$(mktemp)"; chmod 600 "$fichier_cle"
    printf '%s' "$TAILSCALE_CLE" >"$fichier_cle"
    tailscale up --hostname "$HOSTNAME_TS" --ssh=false --accept-dns=false --auth-key="file:$fichier_cle" \
      || { rm -f "$fichier_cle"; echo "connexion refusée : clé Tailscale expirée ou invalide ?" >&2; exit 1; }
    rm -f "$fichier_cle"
  else
    echo "    ouvre l'URL ci-dessous sur ton téléphone ou ton PC et connecte-toi avec le compte du foyer :"
    tailscale up --hostname "$HOSTNAME_TS" --ssh=false --accept-dns=false
  fi
  echo "    adresse Tailscale du serveur : $(tailscale ip -4)"
fi
echo
echo "Ensuite : installer l'app Tailscale sur chaque téléphone/PC (même compte). Vaultwarden et l'appli web"
echo "écouteront sur $(tailscale ip -4 2>/dev/null || echo '100.x.y.z') uniquement (voir install/vaultwarden.sh)."
