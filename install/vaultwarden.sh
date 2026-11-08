#!/usr/bin/env bash
# Firmin Assistant — Vaultwarden (OPTIONNEL, idempotent, root). Voir docs/coffre.md.
#
# Gestionnaire de mots de passe open source compatible Bitwarden (apps téléphone et extensions du foyer),
# en conteneur Podman (rootful, processus sous l'utilisateur homeos), derrière Caddy (TLS Let's Encrypt).
# Données dans data/vaultwarden (homeos, 700) : incluses dans la sauvegarde restic quotidienne,
# avec une copie cohérente de la base SQLite faite juste avant chaque sauvegarde.
#
# Usage : sudo bash install/vaultwarden.sh [--tailscale | --domaine coffre.exemple.fr] [--inscriptions] [--ouvrir-80]
#   --tailscale     (recommandé) : aucun port public ; HTTPS servi par `tailscale serve`
#                   sur le nom MagicDNS du serveur (ex. https://firmin.<tailnet>.ts.net), certificat Let's Encrypt
#                   obtenu par Tailscale. Prérequis : console Tailscale → DNS → MagicDNS et HTTPS Certificates activés.
#   --domaine       nom DNS public pointant vers ce VPS (mode Caddy + port 443 ouvert)
#   --inscriptions  ouvre temporairement la création de comptes (relancer SANS l'option ensuite)
#   --ouvrir-80     ouvre aussi le port 80 (redirection http→https, challenge ACME HTTP-01)
# Variables : VW_IMAGE (défaut : version épinglée dans install/versions.env, lot 5d),
#             VW_PORT_LOCAL (défaut 8082, écoute sur 127.0.0.1 uniquement).
set -euo pipefail

CIBLE=/srv/home-os
DATA="$CIBLE/data"
VW_DIR="$DATA/vaultwarden"
DOMAINE=""            # aucun défaut (lot 6) : --tailscale ou --domaine obligatoire
INSCRIPTIONS=false
OUVRIR_80=false
TAILSCALE=false
VERSIONS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/versions.env"
VW_IMAGE_EPINGLEE="$(sed -nE 's/^VW_IMAGE=(.+)$/\1/p' "$VERSIONS" 2>/dev/null | tail -n1)"
IMAGE="${VW_IMAGE:-${VW_IMAGE_EPINGLEE:-docker.io/vaultwarden/server:latest}}"
PORT_LOCAL="${VW_PORT_LOCAL:-8082}"
UNITE=homeos-vaultwarden
QUADLET="/etc/containers/systemd/$UNITE.container"
CADDY_SITE=/etc/caddy/sites/homeos-vaultwarden.caddy
export DEBIAN_FRONTEND=noninteractive

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domaine) DOMAINE="${2:-}"; shift 2 ;;
    --domaine=*) DOMAINE="${1#*=}"; shift ;;
    --tailscale) TAILSCALE=true; shift ;;
    --inscriptions) INSCRIPTIONS=true; shift ;;
    --ouvrir-80) OUVRIR_80=true; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "option inconnue : $1 (voir --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "à lancer en root (sudo bash install/vaultwarden.sh)"
if [[ "$TAILSCALE" == true ]]; then
  command -v tailscale >/dev/null || die "tailscale absent : lancer d'abord install/tailscale.sh"
  DOMAINE="$(tailscale status --json 2>/dev/null | sed -nE 's/.*"DNSName": *"([^"]+)\.".*/\1/p' | head -n1)"
  [[ -n "$DOMAINE" ]] || die "nom MagicDNS introuvable : activer MagicDNS dans la console Tailscale (DNS)"
fi
[[ -n "$DOMAINE" ]] || die "préciser --tailscale (recommandé) ou --domaine <nom DNS public du VPS>"
[[ "$DOMAINE" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "domaine invalide : $DOMAINE"
[[ "$PORT_LOCAL" =~ ^[0-9]+$ ]] || die "VW_PORT_LOCAL invalide : $PORT_LOCAL"
id homeos >/dev/null 2>&1 || die "utilisateur homeos absent : lancer d'abord install/install.sh"
[[ -d "$DATA" ]] || die "$DATA absent : lancer d'abord install/install.sh"

# ------------------------------------------------------------------------------
log "Paquets (podman, sqlite3$([[ "$TAILSCALE" == true ]] || echo ', caddy'))"
apt-get update -qq
if [[ "$TAILSCALE" == true ]]; then
  apt-get install -y -qq --no-install-recommends podman sqlite3 curl >/dev/null
  info "podman $(podman --version | awk '{print $3}') ; HTTPS par tailscale serve (pas de Caddy)"
else
  apt-get install -y -qq --no-install-recommends podman caddy sqlite3 curl >/dev/null
  info "podman $(podman --version | awk '{print $3}'), caddy $(caddy version 2>/dev/null | awk '{print $1}')"
fi

# ------------------------------------------------------------------------------
log "Données ($VW_DIR)"
install -d -o homeos -g homeos -m 700 "$VW_DIR"
chown -R homeos:homeos "$VW_DIR"
chmod -R go-rwx "$VW_DIR"
UID_HOMEOS="$(id -u homeos)"
GID_HOMEOS="$(id -g homeos)"

# ------------------------------------------------------------------------------
log "Conteneur Vaultwarden (Podman Quadlet → $UNITE.service)"
install -d -m 755 /etc/containers/systemd
cat >"$QUADLET.tmp" <<EOF
# Généré par install/vaultwarden.sh — ne pas modifier à la main (relancer le script)
[Unit]
Description=Firmin Assistant - Vaultwarden (coffre du foyer)
After=network-online.target
Wants=network-online.target

[Container]
Image=$IMAGE
ContainerName=$UNITE
# Processus non root (homeos) : fichiers de data/vaultwarden lisibles par la sauvegarde restic
User=$UID_HOMEOS
Group=$GID_HOMEOS
# Écoute locale uniquement : seul Caddy (TLS) est exposé
PublishPort=127.0.0.1:$PORT_LOCAL:8080
Volume=$VW_DIR:/data
Environment=ROCKET_PORT=8080
Environment=DOMAIN=https://$DOMAINE
Environment=SIGNUPS_ALLOWED=$INSCRIPTIONS
Environment=INVITATIONS_ALLOWED=false
Environment=SHOW_PASSWORD_HINT=false
Environment=PASSWORD_HINTS_ALLOWED=false
Environment=IP_HEADER=X-Real-IP
Environment=LOG_LEVEL=warn
# Pas d'ADMIN_TOKEN : la page /admin reste désactivée
NoNewPrivileges=true

[Service]
Restart=always
RestartSec=5
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
if [[ -f "$QUADLET" ]] && cmp -s "$QUADLET" "$QUADLET.tmp"; then
  rm -f "$QUADLET.tmp"; info "configuration inchangée"
else
  mv "$QUADLET.tmp" "$QUADLET"; info "configuration écrite : $QUADLET"
fi
podman pull -q "$IMAGE" >/dev/null || info "téléchargement de l'image impossible (réseau ?) : nouvel essai au démarrage"
systemctl daemon-reload
systemctl restart "$UNITE.service"

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT_LOCAL/alive" >/dev/null 2>&1 && break
  sleep 2
done
if curl -fsS "http://127.0.0.1:$PORT_LOCAL/alive" >/dev/null 2>&1; then
  info "Vaultwarden répond sur 127.0.0.1:$PORT_LOCAL"
else
  info "Vaultwarden ne répond pas encore : journalctl -u $UNITE -n 50"
fi

# ------------------------------------------------------------------------------
if [[ "$TAILSCALE" == true ]]; then
log "HTTPS privé : tailscale serve → https://$DOMAINE (aucun port public)"
# tailscale serve demande le certificat (Let's Encrypt via DNS-01 géré par Tailscale) et ne répond qu'au tailnet.
tailscale serve --bg --https=443 "http://127.0.0.1:$PORT_LOCAL" >/dev/null
info "$(tailscale serve status 2>/dev/null | head -n3 | tr '\n' ' ')"
# Sortie ufw : aucune règle 443 publique ; l'interface tailscale0 est déjà autorisée par install/tailscale.sh.
else
log "Caddy (TLS Let's Encrypt pour $DOMAINE)"
install -d -m 755 /etc/caddy/sites
cat >"$CADDY_SITE" <<EOF
# Généré par install/vaultwarden.sh
$DOMAINE {
	encode zstd gzip
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:$PORT_LOCAL {
		header_up X-Real-IP {remote_host}
	}
}
EOF
CADDYFILE=/etc/caddy/Caddyfile
if [[ ! -f "$CADDYFILE" ]] || grep -q '/usr/share/caddy' "$CADDYFILE"; then
  # Caddyfile d'origine du paquet (page de démonstration sur :80) : remplacé par les imports
  printf '# Géré par Firmin Assistant : un fichier par site dans /etc/caddy/sites/\nimport sites/*.caddy\n' >"$CADDYFILE"
  info "Caddyfile remplacé (import sites/*.caddy)"
elif ! grep -qE '^\s*import\s+sites/\*\.caddy' "$CADDYFILE"; then
  printf '\nimport sites/*.caddy\n' >>"$CADDYFILE"
  info "import sites/*.caddy ajouté au Caddyfile existant"
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || die "Caddyfile invalide (caddy validate --config $CADDYFILE)"
systemctl enable caddy >/dev/null 2>&1 || true
if systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl restart caddy; fi

log "Pare-feu ufw"
ufw allow 443/tcp >/dev/null
info "443/tcp ouvert (HTTPS, challenge ACME TLS-ALPN)"
if [[ "$OUVRIR_80" == true ]]; then
  ufw allow 80/tcp >/dev/null
  info "80/tcp ouvert (redirection http→https)"
fi
fi

# ------------------------------------------------------------------------------
log "Sauvegarde : copie cohérente de la base avant restic"
# data/vaultwarden est déjà dans le périmètre restic (data/) ; la copie .backup évite une base SQLite
# sauvegardée en pleine écriture. Exécutée par homeos-backup.service (utilisateur homeos), sans bloquer.
install -d -m 755 /etc/systemd/system/homeos-backup.service.d
cat >/etc/systemd/system/homeos-backup.service.d/vaultwarden.conf <<EOF
# Généré par install/vaultwarden.sh
[Service]
ExecStartPre=-/usr/bin/sqlite3 $VW_DIR/db.sqlite3 ".backup '$VW_DIR/db-sauvegarde.sqlite3'"
EOF
systemctl daemon-reload

# ------------------------------------------------------------------------------
log "Récapitulatif"
info "URL              : https://$DOMAINE ($([[ "$TAILSCALE" == true ]] && echo 'réseau Tailscale uniquement, certificat via tailscale serve' || echo 'certificat obtenu par Caddy au premier accès'))"
info "conteneur        : $UNITE.service ($IMAGE), 127.0.0.1:$PORT_LOCAL"
info "données          : $VW_DIR (sauvegardées par restic)"
info "inscriptions     : $INSCRIPTIONS"
cat <<EOF

Étapes suivantes (docs/coffre.md) :
  1. $([[ "$TAILSCALE" == true ]] && echo "Accès : depuis un appareil connecté à Tailscale (app installée, même compte ou invité)." || echo "DNS : $DOMAINE doit pointer vers l'IP de ce VPS.")
  2. Comptes : sudo bash install/vaultwarden.sh --inscriptions, créer sur https://$DOMAINE
     les comptes d'Alex, de Camille et le compte de service « Firmin Assistant », puis relancer
     sudo bash install/vaultwarden.sh   (inscriptions refermées).
  3. Apps : Bitwarden (iOS/Android/extension) → « Auto-hébergé » → https://$DOMAINE
  4. Le coffre de l'assistant reste en backend « fichier » tant que le backend vaultwarden
     n'est pas implémenté (tools/coffre.py, docs/coffre.md).
EOF
