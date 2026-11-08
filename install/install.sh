#!/usr/bin/env bash
# Firmin Assistant — installation sur Ubuntu 24.04 (root via sudo, idempotent).
#
# Usage (depuis le compte d'administration, ex. admin) :
#   sudo bash install/install.sh [options] [URL_GIT]
#     - lancé depuis une copie du kit : le dossier est copié vers /srv/home-os
#     - sinon : git clone URL_GIT vers /srv/home-os (si absent)
#
# Options (lot 5d) :
#   --fermer-root       applique install/ssh/10-homeos.conf (root SSH fermé, AllowUsers <admin>, mots de passe
#                       refusés…) après vérifications et `sshd -t`. À lancer SEULEMENT depuis une session admin
#                       dont la connexion par clé a été vérifiée à la main.
#   --admin=<compte>    compte d'administration autorisé en SSH (défaut : admin)
#   --filtrer-sortant   active le filtrage sortant : tinyproxy (127.0.0.1:3128, liste blanche
#                       install/reseau-autorise.txt + `reseau:` des capacités) et nftables (homeos et les
#                       personnes ne sortent que par le proxy, DNS et NTP). Une fois activé, le filtrage reste
#                       actif aux installations suivantes ; vérifier avec scripts/reseau-test.sh.
#   --ne-plus-filtrer   désactive le filtrage sortant.
#   --outils=http|stdio (lot 7) http : serveur d'outils MCP persistant sous homeos (homeos-outils.service, jetons
#                       par personne) et navigateur isolé (homeos-web.service) ; plus aucun secret lisible par les
#                       personnes. stdio : retour arrière (outils lancés sous chaque personne, droits du lot 5d).
#                       Sans l'option : mode actuel (/etc/default/homeos-outils), http pour une première installation.
#
# Appli web (lot 4a) : si Tailscale est connecté (install/tailscale.sh), installe Piper (synthèse vocale,
# /opt/piper), écrit /etc/default/homeos-web-app (origine https://<nom MagicDNS>:8443), publie l'appli avec
# `tailscale serve --https=8443` (Vaultwarden garde le 443) et démarre homeos-web-app (127.0.0.1:8095).
# Front (lot 4b) : le build commité (web/static/app) est servi tel quel ; si npm est présent, install.sh le
# reconstruit dans un dossier temporaire (`npm ci && npm run build`, versions du package-lock) et vérifie qu'il est
# identique (build reproductible). --construire-front remplace le build servi par celui reconstruit ici.
# Notifications Web Push : clés VAPID générées une fois dans /etc/homeos/push.env (640 root:homeos).
#
# Par défaut : secrets dans /etc/homeos, état dans /var/lib/homeos (migration automatique de l'ancien
# data/secrets.env), versions épinglées (install/versions.env), wrapper homeos-claude + sudoers restreint,
# systemd durci, redémarrage automatique à 04:40, zram, fail2ban (récidive), journald persistant, ufw (SSH
# limité, SMTP sortant bloqué). SSH et filtrage sortant ne changent qu'avec leurs options.
set -euo pipefail

CIBLE=/srv/home-os
DATA="$CIBLE/data"
SECRETS=/etc/homeos
ETAT=/var/lib/homeos
BACKUP_REPO=/var/backups/homeos
WRAPPER=/usr/local/sbin/homeos-claude
FICHIER_PROXY=/etc/default/homeos-proxy
FICHIER_OUTILS=/etc/default/homeos-outils
TRAVAIL="$ETAT-cwd"
PORT_OUTILS=8090
PORT_NAVIGATEUR=8091
PORT_APPLI=8095
PORT_APPLI_TAILSCALE=8443
FICHIER_APPLI=/etc/default/homeos-web-app
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL=""
FERMER_ROOT=0
FILTRE=""            # "" = inchangé, on = activer, off = désactiver
OUTILS=""            # "" = inchangé (http par défaut), http, stdio
CONSTRUIRE_FRONT=0   # 1 = remplacer web/static/app par le build fait sur le serveur
ADMIN="admin"
export DEBIAN_FRONTEND=noninteractive DISABLE_AUTOUPDATER=1

for arg in "$@"; do
  case "$arg" in
    --fermer-root) FERMER_ROOT=1 ;;
    --filtrer-sortant) FILTRE=on ;;
    --ne-plus-filtrer) FILTRE=off ;;
    --admin=*) ADMIN="${arg#--admin=}" ;;
    --outils=http|--outils=stdio) OUTILS="${arg#--outils=}" ;;
    --construire-front) CONSTRUIRE_FRONT=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*) echo "option inconnue : $arg (voir --help)" >&2; exit 2 ;;
    *) REPO_URL="$arg" ;;
  esac
done

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer avec sudo (sudo bash install/install.sh)"
grep -qi 'ubuntu' /etc/os-release || info "attention : système non Ubuntu, poursuite quand même"
[[ "$ADMIN" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "--admin : nom de compte invalide"
[[ -n "${SUDO_USER:-}" ]] && info "lancé par $SUDO_USER via sudo"

# shellcheck source=install/versions.env
. "$SRC_DIR/install/versions.env"
info "versions épinglées : Claude Code $CLAUDE_CODE, Playwright $PLAYWRIGHT, Node $NODE_MAJOR"

# ------------------------------------------------------------------------------
log "Paquets système"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git curl ca-certificates gnupg sudo rsync openssl \
  python3 python3-venv python3.12-venv python3-pip python3-yaml \
  ffmpeg ufw fail2ban unattended-upgrades restic rclone zram-tools >/dev/null

# ------------------------------------------------------------------------------
log "Node.js $NODE_MAJOR (dépôt NodeSource ajouté explicitement, sans curl | bash)"
install -d -m 755 /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/nodesource.gpg ]]; then
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
  chmod 644 /etc/apt/keyrings/nodesource.gpg
fi
depot_node="deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main"
# Un ancien dépôt posé par le script « curl | bash » de NodeSource entre en conflit : on le retire.
rm -f /etc/apt/sources.list.d/nodesource.sources
if [[ "$(cat /etc/apt/sources.list.d/nodesource.list 2>/dev/null)" != "$depot_node" ]]; then
  printf '%s\n' "$depot_node" >/etc/apt/sources.list.d/nodesource.list
  apt-get update -qq
fi
# Le paquet nodejs de NodeSource passe avant celui d'Ubuntu.
printf 'Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n' >/etc/apt/preferences.d/nodesource
node_major=0
command -v node >/dev/null 2>&1 && node_major=$(node -v | sed -E 's/^v([0-9]+).*/\1/')
if [[ "$node_major" -lt "$NODE_MAJOR" ]]; then
  apt-get install -y -qq nodejs >/dev/null
fi
info "node $(node -v), npm $(npm -v)"

# ------------------------------------------------------------------------------
log "Claude Code $CLAUDE_CODE (version épinglée, mise à jour automatique coupée)"
actuelle="$(claude --version 2>/dev/null | awk '{print $1}' | head -n1 || true)"
if [[ "$actuelle" != "$CLAUDE_CODE" ]]; then
  npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@$CLAUDE_CODE" >/dev/null \
    || die "installation de Claude Code $CLAUDE_CODE impossible (version absente de npm ?)"
  info "Claude Code ${actuelle:-absent} → $CLAUDE_CODE"
else
  info "Claude Code $CLAUDE_CODE déjà installé"
fi
CLAUDE_BIN="$(command -v claude)" || die "claude introuvable après installation"
info "claude : $CLAUDE_BIN (montée de version : install/versions.env puis scripts/verifier.sh)"

# ------------------------------------------------------------------------------
log "Groupe foyer et utilisateur de service homeos"
getent group foyer >/dev/null || groupadd foyer
if ! id homeos >/dev/null 2>&1; then
  useradd -m -s /usr/sbin/nologin -G foyer homeos
  info "utilisateur homeos créé"
fi
usermod -aG foyer homeos
usermod -s /usr/sbin/nologin homeos
# Le healthcheck lit les erreurs du journal système.
getent group systemd-journal >/dev/null && usermod -aG systemd-journal homeos
# Lot 7 : utilisateur système du navigateur isolé (Chromium), sans shell ni groupe foyer.
if ! id homeos-web >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/homeos-web --no-create-home --shell /usr/sbin/nologin homeos-web
  info "utilisateur homeos-web créé (navigateur isolé)"
fi
gpasswd -d homeos-web foyer >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
log "Dépôt $CIBLE"
if [[ "$SRC_DIR" == "$CIBLE" ]]; then
  info "lancé depuis $CIBLE, rien à copier"
elif [[ -f "$SRC_DIR/CONTRACT.md" && -d "$SRC_DIR/install" ]]; then
  info "copie depuis $SRC_DIR (data/ et .venv/ préservés)"
  mkdir -p "$CIBLE"
  rsync -a --delete --exclude 'data/' --exclude '.venv/' --exclude '__pycache__/' --exclude '.git/' \
    --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude 'node_modules/' "$SRC_DIR/" "$CIBLE/"
elif [[ -d "$CIBLE/.git" ]]; then
  info "dépôt déjà présent, git pull"
  git -c safe.directory="$CIBLE" -C "$CIBLE" pull --ff-only || info "git pull impossible (modifs locales ?), version actuelle conservée"
elif [[ -n "$REPO_URL" ]]; then
  git clone "$REPO_URL" "$CIBLE"
else
  die "ni copie locale du kit, ni dépôt existant, ni URL git fournie"
fi

install -d -m 2770 "$DATA"
for d in foyer foyer/journal personnes alimentation onboarding capacites fichiers photos; do
  install -d -m 2770 "$DATA/$d"
done
[[ -f "$DATA/config.yaml" ]] || { cp "$CIBLE/install/config.example.yaml" "$DATA/config.yaml"; info "data/config.yaml créé depuis l'exemple → à compléter"; }

# Personnes (utilisateurs Linux) et agendas lus dans config.yaml — aucun prénom en dur.
mapfile -t PERSONNES < <(python3 - "$DATA/config.yaml" <<'PYEOF'
import re, sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception as exc:
    print(f"config.yaml illisible ({exc})", file=sys.stderr)
    sys.exit(0)
vus = set()
for p in cfg.get("personnes") or []:
    u = str((p or {}).get("linux_user") or "")
    if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", u) and u not in vus and u != "homeos":
        vus.add(u)
        print(u)
PYEOF
)
if [[ ${#PERSONNES[@]} -eq 0 ]]; then
  # Repli générique : membres actuels du groupe foyer (hors homeos).
  mapfile -t PERSONNES < <(getent group foyer | cut -d: -f4 | tr ',' '\n' | grep -vx homeos | grep -v '^$' || true)
  info "aucune personne dans config.yaml : repli sur le groupe foyer (${PERSONNES[*]:-vide})"
fi

# ------------------------------------------------------------------------------
log "Utilisateurs des personnes (${PERSONNES[*]:-aucun}) : shell nologin, sessions Claude seulement"
for u in "${PERSONNES[@]}"; do
  if ! id "$u" >/dev/null 2>&1; then
    useradd -m -s /usr/sbin/nologin -G foyer "$u"
    info "utilisateur $u créé"
  fi
  usermod -aG foyer "$u"
  usermod -s /usr/sbin/nologin "$u"   # sudo -u <u> -H claude … fonctionne toujours (sudo n'utilise pas le shell)
done
# ~/.claude (sessions OAuth) et réglages par utilisateur : transcriptions conservées 30 jours, lecture bloquée
# hors des dossiers de travail, connecteurs claude.ai coupés (audit points 1 et 11).
for u in homeos "${PERSONNES[@]}"; do
  maison="$(getent passwd "$u" | cut -d: -f6)"
  install -d -o "$u" -g "$u" -m 700 "$maison/.claude"
  python3 - "$maison/.claude/settings.json" <<'PYEOF'
import json, os, sys
chemin = sys.argv[1]
try:
    reglages = json.load(open(chemin, encoding="utf-8"))
    if not isinstance(reglages, dict):
        reglages = {}
except (FileNotFoundError, ValueError):
    reglages = {}
reglages["cleanupPeriodDays"] = 30
reglages.setdefault("permissions", {})["blockReadsOutsideWorkingDirectories"] = True
reglages["disableClaudeAiConnectors"] = True
tmp = chemin + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(reglages, fh, ensure_ascii=False, indent=2)
os.replace(tmp, chemin)
PYEOF
  chown "$u:$u" "$maison/.claude/settings.json"; chmod 600 "$maison/.claude/settings.json"
done
# Dossiers privés par personne (revue R3) : data/personnes/<u>/prive, lisible par elle seule.
for u in "${PERSONNES[@]}"; do
  install -d -o "$u" -g foyer -m 2770 "$DATA/personnes/$u"
  install -d -o "$u" -g "$u" -m 700 "$DATA/personnes/$u/prive"
done

# ------------------------------------------------------------------------------
log "Mode des outils MCP (lot 7)"
# /etc/default/homeos-outils (644, sans secret) : lu par le wrapper, le bot, migrer-secrets.sh et le healthcheck.
mode_actuel=""
if [[ -r "$FICHIER_OUTILS" ]]; then
  mode_actuel="$(sed -nE 's/^HOMEOS_OUTILS=(stdio|http)$/\1/p' "$FICHIER_OUTILS" | tail -n1 || true)"
fi
MODE_OUTILS="${OUTILS:-${mode_actuel:-http}}"
{
  echo "# Généré par install.sh (lot 7). http : serveur d'outils persistant (homeos-outils.service) ; stdio : repli,"
  echo "# outils lancés sous chaque personne. Changer de mode : sudo bash install/install.sh --outils=stdio|http"
  echo "HOMEOS_OUTILS=$MODE_OUTILS"
  echo "HOMEOS_OUTILS_URL=http://127.0.0.1:$PORT_OUTILS/mcp"
  echo "HOMEOS_OUTILS_PORT=$PORT_OUTILS"
} >"$FICHIER_OUTILS"
chmod 644 "$FICHIER_OUTILS"
info "outils MCP : $MODE_OUTILS${mode_actuel:+ (avant : $mode_actuel)}"

# ------------------------------------------------------------------------------
log "Secrets (/etc/homeos) et état sensible ($ETAT)"
bash "$CIBLE/scripts/migrer-secrets.sh"
completer() { # completer <fichier> <CLE> [commentaire] : ajoute CLE= vide si absente
  grep -qE "^(export )?$2=" "$1" 2>/dev/null && return 0
  { [[ -n "${3:-}" ]] && printf '# %s\n' "$3"; printf '%s=\n' "$2"; } >>"$1"
}
completer "$SECRETS/telegram.env" TELEGRAM_BOT_TOKEN "Token du bot Telegram (BotFather → /newbot)"
completer "$SECRETS/anthropic.env" ANTHROPIC_API_KEY "Clé API Anthropic (crons --bare, personnes en mode cle_api)"
while read -r variable prive; do
  [[ -z "$variable" ]] && continue
  if [[ "$prive" != "-" ]] && id "$prive" >/dev/null 2>&1; then
    f="$SECRETS/ical-$prive.env"
    [[ -e "$f" ]] || printf '# Firmin Assistant — agenda privé de %s\n' "$prive" >"$f"
    completer "$f" "$variable" "URL iCal secrète (agenda privé, lu pour $prive seulement)"
    chmod 640 "$f"   # propriétaire : root:homeos (http) ou root:$prive (stdio), réappliqué par migrer-secrets.sh
  else
    completer "$SECRETS/ical.env" "$variable" "URL iCal secrète (Google Agenda → Adresse secrète au format iCal)"
  fi
done < <(python3 - "$DATA/config.yaml" <<'PYEOF'
import re, sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    sys.exit(0)
for a in cfg.get("agendas") or []:
    if isinstance(a, dict) and re.fullmatch(r"[A-Z][A-Z0-9_]*", str(a.get("ical_url_env") or "")):
        print(a["ical_url_env"], re.sub(r"[^a-z0-9_-]", "", str(a.get("prive") or "").lower()) or "-")
PYEOF
)
# Mot de passe restic et clé du coffre : générés une fois (à conserver HORS du VPS).
if ! grep -qE '^RESTIC_PASSWORD=.+' "$SECRETS/restic.env"; then
  sed -i '/^RESTIC_PASSWORD=$/d' "$SECRETS/restic.env"
  printf 'RESTIC_PASSWORD=%s\n' "$(openssl rand -hex 24)" >>"$SECRETS/restic.env"
  info "RESTIC_PASSWORD généré dans $SECRETS/restic.env (à conserver hors du VPS !)"
fi
if ! grep -qE '^COFFRE_CLE=.{16,}' "$SECRETS/coffre.env"; then
  sed -i '/^COFFRE_CLE=/d' "$SECRETS/coffre.env"
  printf 'COFFRE_CLE=%s\n' "$(openssl rand -hex 32)" >>"$SECRETS/coffre.env"
  info "COFFRE_CLE générée dans $SECRETS/coffre.env (à conserver hors du VPS, avec RESTIC_PASSWORD !)"
fi
# Lot 7 : adresse du navigateur isolé (port local + chemin secret), générée une fois.
if ! grep -qE '^HOMEOS_NAVIGATEUR_WS=ws://127\.0\.0\.1:[0-9]+/[0-9a-f]{32,}$' "$SECRETS/navigateur.env" 2>/dev/null; then
  chemin_nav="$(openssl rand -hex 24)"
  {
    echo "# Généré par install.sh (lot 7) : navigateur isolé homeos-web (serveur Playwright local, chemin secret)."
    echo "HOMEOS_NAVIGATEUR_PORT=$PORT_NAVIGATEUR"
    echo "HOMEOS_NAVIGATEUR_CHEMIN=$chemin_nav"
    echo "HOMEOS_NAVIGATEUR_WS=ws://127.0.0.1:$PORT_NAVIGATEUR/$chemin_nav"
  } >"$SECRETS/navigateur.env"
  info "adresse du navigateur isolé générée ($SECRETS/navigateur.env)"
fi
bash "$CIBLE/scripts/migrer-secrets.sh" >/dev/null   # réapplique les droits après les ajouts
# Lot 7 : un jeton par personne (+ homeos) pour le serveur d'outils ; idempotent, révoque les personnes retirées.
bash "$CIBLE/scripts/jetons-outils.sh" homeos "${PERSONNES[@]}" | sed -n '/changements=/!p'
# Dossiers de travail vides des sessions Claude (cwd), un par personne et un pour les crons, hors de l'état
# depuis le lot 7 ($ETAT est réservé à homeos).
install -d -o root -g foyer -m 750 "$TRAVAIL"
# Lot 7b : atelier (travaux et sous-agent, 700) et capacités qu'il fabrique (lues par homeos seulement : le code
# exécuté par le serveur d'outils n'est jamais modifiable par les personnes du foyer).
install -d -o homeos -g homeos -m 700 "$ETAT/atelier" "$ETAT/atelier-relecture"
install -d -o homeos -g homeos -m 750 "$ETAT/capacites-atelier" "$ETAT/capacites-etat"
for u in homeos "${PERSONNES[@]}"; do
  install -d -o root -g foyer -m 750 "$TRAVAIL/$u"
done

# ------------------------------------------------------------------------------
log "Environnement Python (.venv)"
if [[ ! -x "$CIBLE/.venv/bin/python" ]]; then
  python3 -m venv "$CIBLE/.venv"
fi
"$CIBLE/.venv/bin/pip" install -q --upgrade pip wheel
for req in bot/requirements.txt tools/requirements.txt web/requirements.txt; do
  if [[ -f "$CIBLE/$req" ]]; then
    info "pip install -r $req"
    "$CIBLE/.venv/bin/pip" install -q -r "$CIBLE/$req"
  fi
done
"$CIBLE/.venv/bin/python" -c 'import yaml' 2>/dev/null || "$CIBLE/.venv/bin/pip" install -q pyyaml

# ------------------------------------------------------------------------------
log "Playwright $PLAYWRIGHT + Chromium (Kitchen Daily)"
# Navigateurs partagés dans /opt/playwright (lisibles par homeos et les personnes) ;
# PLAYWRIGHT_BROWSERS_PATH est aussi exporté par les unités systemd et tools/mcp.json.
export PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
install -d -m 755 "$PLAYWRIGHT_BROWSERS_PATH"
pw_actuel="$("$CIBLE/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("playwright"))' 2>/dev/null || true)"
[[ "$pw_actuel" == "$PLAYWRIGHT" ]] || "$CIBLE/.venv/bin/pip" install -q "playwright==$PLAYWRIGHT"
if "$CIBLE/.venv/bin/playwright" install --with-deps chromium >/dev/null; then
  chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"
  info "Chromium installé dans $PLAYWRIGHT_BROWSERS_PATH"
else
  info "installation de Chromium impossible (réseau ?) : Kitchen Daily indisponible, relancer install.sh plus tard"
fi
if command -v apparmor_parser >/dev/null 2>&1 && [[ -d /etc/apparmor.d ]]; then
  install -m 644 "$CIBLE/install/apparmor/homeos-chromium" /etc/apparmor.d/homeos-chromium
  if apparmor_parser -r -W /etc/apparmor.d/homeos-chromium 2>/dev/null; then
    info "profil AppArmor homeos-chromium chargé (sandbox Chromium : HOMEOS_CHROMIUM_SANDBOX=1)"
  else
    info "profil AppArmor homeos-chromium non chargé (apparmor_parser en échec)"
  fi
fi

# ------------------------------------------------------------------------------
log "Droits"
chown -R homeos:foyer "$CIBLE"
chmod 2775 "$CIBLE"
find "$CIBLE" -path "$DATA" -prune -o -type d -exec chmod 2775 {} +
# data/ : groupe foyer, sauf les dossiers privés des personnes et les fichiers root (secrets.env.migre).
find "$DATA" -path "$DATA/personnes/*/prive" -prune -o -path "$DATA/fichiers/sensibles" -prune -o -user root -name 'secrets.env*' -prune -o -exec chown homeos:foyer {} +
for u in "${PERSONNES[@]}"; do chown "$u:foyer" "$DATA/personnes/$u"; done
find "$DATA" -path "$DATA/personnes/*/prive" -prune -o -path "$DATA/fichiers/sensibles" -prune -o -type d -exec chmod 2770 {} +
find "$DATA" -path "$DATA/personnes/*/prive" -prune -o -path "$DATA/fichiers/sensibles" -prune -o -type f ! -name 'secrets.env*' -exec chmod 664 {} +
[[ -d "$DATA/vaultwarden" ]] && { chown -R homeos:homeos "$DATA/vaultwarden"; chmod -R go-rwx "$DATA/vaultwarden"; }
# Documents sensibles chiffrés dans le navigateur (web/sensibles.py) : l'appli (homeos) seule, jamais le groupe foyer
# (les sessions Claude des personnes ne les voient pas, même chiffrés) ; sauvegardés par restic avec data/.
[[ -d "$DATA/fichiers" ]] || install -d -o homeos -g foyer -m 2770 "$DATA/fichiers"
install -d -o homeos -g homeos -m 700 "$DATA/fichiers/sensibles"
chown -R homeos:homeos "$DATA/fichiers/sensibles"; chmod -R go-rwx "$DATA/fichiers/sensibles"
chmod +x "$CIBLE"/scripts/*.sh "$CIBLE"/scripts/*.py "$CIBLE/install/install.sh" 2>/dev/null || true

# ------------------------------------------------------------------------------
log "Modèle de transcription (faster-whisper small) pré-téléchargé"
# Téléchargé une fois maintenant, par root (non filtré), dans le cache de homeos : le bot n'a plus besoin
# de huggingface.co, même avec le filtrage sortant.
if ! ls -d /home/homeos/.cache/huggingface/hub/models--*faster-whisper-small* >/dev/null 2>&1; then
  if HF_HOME=/home/homeos/.cache/huggingface "$CIBLE/.venv/bin/python" -c \
      'from faster_whisper import WhisperModel; WhisperModel("small", device="cpu", compute_type="int8")' >/dev/null 2>&1; then
    info "modèle téléchargé dans /home/homeos/.cache/huggingface"
  else
    info "téléchargement du modèle impossible (réseau ?) : relancer install.sh plus tard"
  fi
  [[ -d /home/homeos/.cache ]] && chown -R homeos:homeos /home/homeos/.cache
else
  info "modèle déjà présent"
fi

# ------------------------------------------------------------------------------
log "Wrapper $WRAPPER et sudoers restreint"
install -o root -g root -m 755 "$CIBLE/install/bin/homeos-claude" "$WRAPPER"
# Lot 5c : CLI du coffre pour l'administrateur (sudo -u homeos homeos-coffre set <cle>).
install -o root -g root -m 755 "$CIBLE/install/bin/homeos-coffre" /usr/local/bin/homeos-coffre
[[ -e /var/log/sudo-homeos.log ]] || install -o root -g adm -m 640 /dev/null /var/log/sudo-homeos.log
auth=""
for p in "$CLAUDE_BIN" /usr/bin/claude /usr/local/bin/claude; do
  [[ -x "$p" && ", $auth," != *", $p auth status,"* ]] && auth+="${auth:+, }$p auth status"
done
if [[ ${#PERSONNES[@]} -gt 0 ]]; then
  personnes_sudo="$(IFS=,; echo "${PERSONNES[*]}" | sed 's/,/, /g')"
  sed -e "s|__PERSONNES__|$personnes_sudo|" -e "s|__CLAUDE_AUTH__|$auth|" \
    "$CIBLE/install/sudoers.d/homeos" >/etc/sudoers.d/homeos.tmp
  sed -i "1a # Généré par install.sh le $(date -I)" /etc/sudoers.d/homeos.tmp
  chmod 440 /etc/sudoers.d/homeos.tmp
  visudo -cf /etc/sudoers.d/homeos.tmp >/dev/null || { rm -f /etc/sudoers.d/homeos.tmp; die "sudoers invalide"; }
  mv /etc/sudoers.d/homeos.tmp /etc/sudoers.d/homeos
  info "homeos → (${personnes_sudo}) : $WRAPPER et « claude auth status » uniquement"
else
  rm -f /etc/sudoers.d/homeos
  info "aucune personne : sudoers homeos retiré"
fi

# ------------------------------------------------------------------------------
log "Pare-feu ufw (SSH limité, SMTP sortant bloqué)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw limit OpenSSH >/dev/null       # 6 connexions / 30 s par adresse
ufw deny out 25/tcp >/dev/null     # aucun envoi de courrier direct depuis le VPS
ufw --force enable >/dev/null
info "$(ufw status | head -n1)"

# ------------------------------------------------------------------------------
log "fail2ban (sshd + récidive, bannissements croissants)"
cat >/etc/fail2ban/jail.d/homeos-sshd.local <<'EOF'
# Généré par install.sh (lot 5d, audit point 11)
[DEFAULT]
bantime.increment = true
bantime.maxtime = 1w

[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h

[recidive]
enabled = true
logpath = /var/log/fail2ban.log
backend = auto
findtime = 1d
maxretry = 3
bantime = 1w
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban

# ------------------------------------------------------------------------------
log "Journal systemd persistant (500 Mo max)"
install -d /etc/systemd/journald.conf.d /var/log/journal
printf '# Généré par install.sh (lot 5d)\n[Journal]\nStorage=persistent\nSystemMaxUse=500M\n' \
  >/etc/systemd/journald.conf.d/homeos.conf
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald

# ------------------------------------------------------------------------------
log "Mises à jour automatiques (+ redémarrage à 04:40 si le noyau l'exige)"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# Après la sauvegarde (03:30), avant le brief du jour (07:15).
cat >/etc/apt/apt.conf.d/52homeos-reboot <<'EOF'
// Généré par install.sh (lot 5d, audit point 2)
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:40";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
log "zram (2 Go de swap compressé en mémoire)"
cat >/etc/default/zramswap <<'EOF'
# Généré par install.sh (lot 5d) : deux Chromium + faster-whisper sur 4 Go frôlent l'OOM.
ALGO=zstd
SIZE=2048
PRIORITY=100
EOF
systemctl enable zramswap >/dev/null 2>&1 || true
systemctl restart zramswap 2>/dev/null && info "$(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')" \
  || info "zram indisponible sur ce noyau (module zram absent ?)"

# ------------------------------------------------------------------------------
log "SSH"
if [[ $FERMER_ROOT -eq 1 ]]; then
  id "$ADMIN" >/dev/null 2>&1 || die "--fermer-root : le compte $ADMIN n'existe pas"
  id -nG "$ADMIN" | tr ' ' '\n' | grep -qx sudo || die "--fermer-root : $ADMIN n'est pas dans le groupe sudo"
  cles="$(getent passwd "$ADMIN" | cut -d: -f6)/.ssh/authorized_keys"
  grep -qE '^(ssh-ed25519|ecdsa-sha2-|sk-ssh-ed25519|ssh-rsa) ' "$cles" 2>/dev/null \
    || die "--fermer-root : aucune clé publique dans $cles"
  [[ "${SUDO_USER:-}" == "$ADMIN" ]] \
    || die "--fermer-root : à lancer depuis une session SSH de $ADMIN (sudo), pour prouver que sa connexion fonctionne"
  sed "s|__ADMIN__|$ADMIN|" "$CIBLE/install/ssh/10-homeos.conf" >/etc/ssh/sshd_config.d/10-homeos.conf
  chmod 644 /etc/ssh/sshd_config.d/10-homeos.conf
  if ! sshd -t; then
    rm -f /etc/ssh/sshd_config.d/10-homeos.conf
    die "sshd -t refuse la configuration : rien n'a été appliqué"
  fi
  systemctl reload ssh.service 2>/dev/null || systemctl restart ssh.service
  info "root SSH fermé, seul $ADMIN peut se connecter (par clé). GARDE CETTE SESSION OUVERTE et teste"
  info "dans un autre terminal : ssh $ADMIN@<VPS>. Secours : console de l'hébergeur (root)."
  info "Empreintes des clés de $ADMIN :"
  ssh-keygen -lf "$cles" 2>/dev/null | sed 's/^/      /' || true
elif [[ -f /etc/ssh/sshd_config.d/10-homeos.conf ]]; then
  info "durcissement SSH déjà appliqué (/etc/ssh/sshd_config.d/10-homeos.conf) ; relancer avec --fermer-root pour le mettre à jour"
else
  info "SSH inchangé. Après avoir vérifié la connexion de $ADMIN par clé : sudo bash install/install.sh --fermer-root"
fi

# ------------------------------------------------------------------------------
log "Sauvegarde restic ($BACKUP_REPO)"
install -d -o homeos -g homeos -m 700 "$BACKUP_REPO"
# Mot de passe transmis par l'environnement (jamais en argument : visible dans ps).
RESTIC_PASSWORD="$(sed -nE 's/^RESTIC_PASSWORD=(.+)$/\1/p' "$SECRETS/restic.env" | tail -n1)"
export RESTIC_PASSWORD RESTIC_REPOSITORY="$BACKUP_REPO"
if ! sudo -n -u homeos --preserve-env=RESTIC_PASSWORD,RESTIC_REPOSITORY restic cat config >/dev/null 2>&1; then
  sudo -n -u homeos --preserve-env=RESTIC_PASSWORD,RESTIC_REPOSITORY restic init >/dev/null
  info "dépôt restic initialisé"
else
  info "dépôt restic déjà initialisé"
fi
unset RESTIC_PASSWORD RESTIC_REPOSITORY
if grep -qE '^RESTIC_REPOSITORY_DISTANT=.+' "$SECRETS/restic.env"; then
  info "copie hors site configurée : $(sed -nE 's/^RESTIC_REPOSITORY_DISTANT=(.+)$/\1/p' "$SECRETS/restic.env")"
else
  info "pas de copie hors site : sudo bash $CIBLE/scripts/configurer-hors-site.sh (docs/sauvegardes.md)"
fi

# ------------------------------------------------------------------------------
log "Filtrage sortant"
if [[ "$FILTRE" == "on" || ( "$FILTRE" == "" && -f "$FICHIER_PROXY" ) ]]; then
  apt-get install -y -qq --no-install-recommends tinyproxy nftables >/dev/null
  # Liste blanche : install/reseau-autorise.txt + hôtes des capacités chargées.
  hotes="$( { grep -vE '^[[:space:]]*(#|$)' "$CIBLE/install/reseau-autorise.txt" | awk '{print $1}';
              cd "$CIBLE" && sudo -n -u homeos env HOMEOS_DATA="$DATA" HOMEOS_ETAT="$ETAT" PYTHONPATH="$CIBLE" \
                "$CIBLE/.venv/bin/python" -m tools.capacites reseau 2>/dev/null || true; } \
            | tr 'A-Z' 'a-z' | grep -E '^(\*\.)?[a-z0-9.-]+$' | sort -u)"
  {
    echo "# Généré par install.sh depuis install/reseau-autorise.txt et les capacités ($(date -I))"
    while read -r h; do
      [[ -z "$h" ]] && continue
      base="${h#\*.}"; motif="${base//./\\.}"
      echo "^${motif}\$"
      if [[ "$h" == \*.* ]]; then echo "\\.${motif}\$"; fi
    done <<<"$hotes"
  } >/etc/tinyproxy/homeos-filtre
  [[ -f /etc/tinyproxy/tinyproxy.conf.origine ]] || cp /etc/tinyproxy/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf.origine 2>/dev/null || true
  cat >/etc/tinyproxy/tinyproxy.conf <<'EOF'
# Généré par install.sh (lot 5d, audit point 8) — proxy sortant filtrant, local uniquement.
User tinyproxy
Group tinyproxy
Port 3128
Listen 127.0.0.1
Timeout 600
Syslog On
LogLevel Notice
MaxClients 100
Allow 127.0.0.1
DisableViaHeader Yes
# Liste blanche de domaines (expressions régulières, une par ligne) ; tout le reste est refusé (403).
Filter "/etc/tinyproxy/homeos-filtre"
FilterDefaultDeny Yes
# CONNECT (HTTPS) vers le port 443 seulement.
ConnectPort 443
EOF
  systemctl enable tinyproxy >/dev/null 2>&1
  systemctl restart tinyproxy
  # nftables : homeos et les personnes ne sortent que vers la boucle locale (proxy 3128, services
  # locaux), le DNS et le NTP. Table dédiée : les règles d'ufw ne sont pas touchées.
  uids="$(for u in homeos homeos-web "${PERSONNES[@]}"; do id -u "$u"; done | paste -sd, -)"
  cat >/etc/homeos-sortant.nft <<EOF
# Généré par install.sh (lot 5d, audit point 8) — uids : homeos homeos-web ${PERSONNES[*]}
table inet homeos_sortant
delete table inet homeos_sortant
table inet homeos_sortant {
  chain sortie {
    type filter hook output priority filter; policy accept;
    meta skuid != { $uids } accept
    oifname "lo" accept
    udp dport { 53, 123 } accept
    tcp dport 53 accept
    counter reject with icmpx type admin-prohibited
  }
}
EOF
  cat >/etc/systemd/system/homeos-sortant.service <<'EOF'
[Unit]
Description=Firmin Assistant - filtrage sortant par utilisateur (nftables)
After=network-pre.target
Before=network-online.target homeos-bot.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/homeos-sortant.nft
ExecStop=-/usr/sbin/nft delete table inet homeos_sortant

[Install]
WantedBy=multi-user.target
EOF
  printf '# Généré par install.sh --filtrer-sortant (lot 5d) : proxy sortant de homeos et des personnes.\n' >"$FICHIER_PROXY"
  for v in HTTPS_PROXY HTTP_PROXY https_proxy http_proxy; do printf '%s=http://127.0.0.1:3128\n' "$v" >>"$FICHIER_PROXY"; done
  for v in NO_PROXY no_proxy; do printf '%s=127.0.0.1,localhost,::1\n' "$v" >>"$FICHIER_PROXY"; done
  chmod 644 "$FICHIER_PROXY"
  systemctl daemon-reload
  systemctl enable homeos-sortant.service >/dev/null
  systemctl restart homeos-sortant.service
  info "actif : $(wc -l <<<"$hotes") hôtes autorisés (/etc/tinyproxy/homeos-filtre) ; vérifier : sudo bash $CIBLE/scripts/reseau-test.sh"
elif [[ "$FILTRE" == "off" ]]; then
  systemctl disable --now homeos-sortant.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/homeos-sortant.service "$FICHIER_PROXY"
  systemctl disable --now tinyproxy >/dev/null 2>&1 || true
  info "filtrage sortant désactivé"
else
  info "désactivé (par défaut). Activation après validation : sudo bash install/install.sh --filtrer-sortant"
fi

# ------------------------------------------------------------------------------
log "systemd : services et timers"
# Lot 5a : le rappel Kitchen Daily est devenu le cron `rappel` de la capacité kitchen_daily.
if [[ -e /etc/systemd/system/homeos-rappel-kitchen-daily.timer || -e /etc/systemd/system/homeos-rappel-kitchen-daily.service ]]; then
  systemctl disable --now homeos-rappel-kitchen-daily.timer >/dev/null 2>&1 || true
  rm -rf /etc/systemd/system/homeos-rappel-kitchen-daily.timer /etc/systemd/system/homeos-rappel-kitchen-daily.service \
         /etc/systemd/system/homeos-rappel-kitchen-daily.timer.d
  info "ancien timer homeos-rappel-kitchen-daily retiré (remplacé par la capacité kitchen_daily)"
fi
install -m 644 "$CIBLE"/install/systemd/homeos-*.service "$CIBLE"/install/systemd/homeos-*.timer \
  "$CIBLE"/install/systemd/homeos-*.path /etc/systemd/system/
# Lot transports : l'ancien dossier capabilities/bus ne contient plus que des restes non versionnés (lien service/data,
# node_modules) ; sans manifeste il serait listé comme une capacité invalide.
if [[ -d "$CIBLE/capabilities/bus" && ! -e "$CIBLE/capabilities/bus/capability.yaml" ]]; then
  rm -rf "$CIBLE/capabilities/bus"
  info "restes de l'ancienne capacité bus retirés (remplacée par transports)"
fi
# Capacités remplacées (manifeste `remplace:`, ex. bus → transports) : leur état passe à la nouvelle dans etat.json.
(cd "$CIBLE" && sudo -n -u homeos env HOMEOS_DATA="$DATA" HOMEOS_ETAT="$ETAT" PYTHONPATH="$CIBLE" \
  "$CIBLE/.venv/bin/python" -m tools.capacites migrer 2>/dev/null | sed 's/^/    capacité reprise : /') || true
# Catégories de rangement renommées (chien → animaux) : fichiers rangés, index et liens des fiches déplacés
# (idempotent ; relancé aussi par scripts/appliquer-config.sh).
(cd "$CIBLE" && sudo -n -u homeos env HOMEOS_DATA="$DATA" HOMEOS_ETAT="$ETAT" PYTHONPATH="$CIBLE" \
  "$CIBLE/.venv/bin/python" -m tools.fichiers migrer 2>/dev/null | sed 's/^/    migration : /') || true
# Lot 7a : horaires des briefs (config.yaml) et timers des crons des capacités (homeos-cap-<nom>-<cron>) sont
# générés par scripts/appliquer-config.sh, relancé ensuite automatiquement par homeos-appliquer-config.path à
# chaque modification de data/config.yaml ou de data/capacites/etat.json (administration par la conversation).
# Copie root du script : le dépôt (modifiable par le groupe foyer) n'est jamais exécuté en root par systemd.
install -o root -g root -m 755 "$CIBLE/scripts/appliquer-config.sh" /usr/local/sbin/homeos-appliquer-config
HOMEOS_ETAT="$ETAT" HOMEOS_SECRETS="$SECRETS" /usr/local/sbin/homeos-appliquer-config "$CIBLE" \
  || info "application de la configuration en échec (détail ci-dessus) : corriger data/config.yaml"

systemctl daemon-reload
for t in brief-hebdo brief-jour backup healthcheck restaurer-test verifier-secrets maj-navigateur; do
  systemctl enable --now "homeos-$t.timer" >/dev/null
done
systemctl enable --now homeos-appliquer-config.path >/dev/null
# Lot 7b : l'atelier tourne à la demande (file surveillée), jamais en permanence.
systemctl enable --now homeos-atelier.path homeos-atelier-installer.path >/dev/null
# Lot 7 : serveur d'outils persistant et navigateur isolé (démarrés avant le bot, qui les utilise).
if [[ "$MODE_OUTILS" == http ]]; then
  systemctl enable homeos-web.service homeos-outils.service >/dev/null
  systemctl restart homeos-web.service || info "homeos-web ne démarre pas : journalctl -u homeos-web (Kitchen Daily indisponible)"
  systemctl restart homeos-outils.service
  for _ in $(seq 1 30); do
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT_OUTILS") 2>/dev/null && break
    sleep 1
  done
  if (cd "$CIBLE" && sudo -n -u homeos env HOMEOS_SECRETS="$SECRETS" PYTHONPATH="$CIBLE" "$CIBLE/.venv/bin/python" \
       -m tools.serveur_http sonde --url "http://127.0.0.1:$PORT_OUTILS/mcp" 2>/dev/null | grep -q '"ok": true'); then
    info "homeos-outils actif (127.0.0.1:$PORT_OUTILS, jeton homeos vérifié)"
  else
    info "homeos-outils ne répond pas : journalctl -u homeos-outils -n 50 (retour arrière : install.sh --outils=stdio)"
  fi
else
  systemctl disable --now homeos-outils.service homeos-web.service >/dev/null 2>&1 || true
  info "outils en stdio (repli) : homeos-outils et homeos-web arrêtés"
fi
systemctl enable homeos-bot.service >/dev/null
if grep -qE '^TELEGRAM_BOT_TOKEN=[0-9]+:[A-Za-z0-9_-]{20,}$' "$SECRETS/telegram.env" \
   && ! grep -qE '^TELEGRAM_BOT_TOKEN=123456789:' "$SECRETS/telegram.env"; then
  systemctl restart homeos-bot.service
  info "homeos-bot démarré"
else
  info "homeos-bot activé mais non démarré : TELEGRAM_BOT_TOKEN à renseigner dans $SECRETS/telegram.env"
fi

# ------------------------------------------------------------------------------
log "Appli web (lot 4a) : Piper, tailscale serve, homeos-web-app"
# Synthèse vocale locale (Piper, open source) dans /opt/piper : venv séparé, voix française. Téléchargée par root
# (non filtré) ; l'appli ne fait que lancer l'exécutable. Sans Piper, le front utilise la voix du navigateur.
VOIX_PIPER="${PIPER_VOIX:-fr_FR-siwis-medium}"
if [[ ! -x /opt/piper/venv/bin/piper ]] || ! /opt/piper/venv/bin/pip show piper-tts 2>/dev/null | grep -qx "Version: $PIPER_TTS"; then
  install -d -m 755 /opt/piper /opt/piper/voix
  [[ -x /opt/piper/venv/bin/python ]] || python3 -m venv /opt/piper/venv
  if /opt/piper/venv/bin/pip install -q "piper-tts==$PIPER_TTS"; then
    info "Piper $PIPER_TTS installé dans /opt/piper/venv"
  else
    info "installation de Piper impossible (réseau ?) : synthèse du navigateur en attendant, relancer install.sh"
  fi
fi
if [[ ! -s "/opt/piper/voix/$VOIX_PIPER.onnx" ]]; then
  install -d -m 755 /opt/piper/voix
  langue="${VOIX_PIPER%%-*}"; reste="${VOIX_PIPER#*-}"; locuteur="${reste%-*}"; qualite="${VOIX_PIPER##*-}"
  base="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/${langue%%_*}/$langue/$locuteur/$qualite/$VOIX_PIPER"
  if curl -fsSL "$base.onnx" -o "/opt/piper/voix/$VOIX_PIPER.onnx.tmp" \
     && curl -fsSL "$base.onnx.json" -o "/opt/piper/voix/$VOIX_PIPER.onnx.json"; then
    mv -f "/opt/piper/voix/$VOIX_PIPER.onnx.tmp" "/opt/piper/voix/$VOIX_PIPER.onnx"
    info "voix $VOIX_PIPER téléchargée"
  else
    rm -f "/opt/piper/voix/$VOIX_PIPER.onnx.tmp"
    info "voix $VOIX_PIPER non téléchargée (réseau ?)"
  fi
fi
chmod -R a+rX /opt/piper 2>/dev/null || true

# Front (lot 4b) : le build commité est servi ; reconstruction de contrôle si npm est là (Node est installé pour
# Claude Code). Jamais bloquant : en cas d'échec, le build commité reste en service.
if [[ -f "$CIBLE/web/front/package-lock.json" ]] && command -v npm >/dev/null 2>&1; then
  build_tmp="$(mktemp -d)"
  if (cd "$CIBLE/web/front" && npm ci --no-audit --no-fund --loglevel=error >/dev/null \
        && npm run build --silent -- --outDir "$build_tmp" --emptyOutDir >/dev/null 2>&1); then
    if diff -rq "$build_tmp" "$CIBLE/web/static/app" >/dev/null 2>&1; then
      info "front : build reproductible (identique à web/static/app)"
    elif [[ "$CONSTRUIRE_FRONT" == 1 ]]; then
      rsync -a --delete "$build_tmp/" "$CIBLE/web/static/app/"
      info "front : web/static/app remplacé par le build du serveur (--construire-front)"
    else
      info "front : le build du serveur diffère du build commité (sources modifiées sans rebuild ?) ;"
      info "        build commité conservé — relancer avec --construire-front pour le remplacer"
    fi
  else
    info "front : reconstruction impossible (npm ci / build) ; build commité conservé"
  fi
  rm -rf "$build_tmp"
  chmod -R a+rX "$CIBLE/web/static/app" 2>/dev/null || true
fi

# Origine publique : nom MagicDNS du serveur sur le réseau Tailscale, port 8443 (Vaultwarden occupe 443).
nom_ts=""
if command -v tailscale >/dev/null 2>&1; then
  nom_ts="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi
if [[ "$nom_ts" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)+$ ]]; then
  contenu_appli="# Généré par install.sh (lot 4a), sans secret. Appli publiée par : tailscale serve --https=$PORT_APPLI_TAILSCALE
HOMEOS_WEB_ORIGINE=https://$nom_ts:$PORT_APPLI_TAILSCALE
HOMEOS_WEB_HOTE=127.0.0.1
HOMEOS_WEB_PORT=$PORT_APPLI
HOMEOS_PIPER=/opt/piper/venv/bin/piper
HOMEOS_PIPER_VOIX=/opt/piper/voix/$VOIX_PIPER.onnx"
  if [[ "$(cat "$FICHIER_APPLI" 2>/dev/null)" != "$contenu_appli" ]]; then
    printf '%s\n' "$contenu_appli" >"$FICHIER_APPLI"
  fi
  chmod 644 "$FICHIER_APPLI"
  # Publication sur le réseau privé seulement (serve, jamais funnel) ; idempotent.
  if tailscale serve --bg --https="$PORT_APPLI_TAILSCALE" "http://127.0.0.1:$PORT_APPLI" >/dev/null 2>&1; then
    info "tailscale serve : https://$nom_ts:$PORT_APPLI_TAILSCALE → 127.0.0.1:$PORT_APPLI"
  else
    info "tailscale serve --https=$PORT_APPLI_TAILSCALE en échec : activer HTTPS (certificats) pour le tailnet dans la console Tailscale"
  fi
  # Notifications Web Push : paire de clés VAPID générée une fois (jamais dans le dépôt), lue par web/push.py.
  if ! grep -qs '^VAPID_CLE_PRIVEE=.' "$SECRETS/push.env"; then
    if cles_vapid="$(PYTHONPATH="$CIBLE" "$CIBLE/.venv/bin/python" -c 'from web.push import generer_cles; print(*generer_cles())')"; then
      read -r vapid_privee vapid_publique <<<"$cles_vapid"
      umask 077
      printf '# Firmin Assistant — clés VAPID des notifications Web Push (lot 4b), générées par install.sh\nVAPID_CLE_PRIVEE=%s\nVAPID_CLE_PUBLIQUE=%s\nVAPID_CONTACT=https://%s:%s\n' \
        "$vapid_privee" "$vapid_publique" "$nom_ts" "$PORT_APPLI_TAILSCALE" >"$SECRETS/push.env"
      umask 022
      unset cles_vapid vapid_privee vapid_publique
      info "clés VAPID générées dans $SECRETS/push.env"
    else
      info "clés VAPID non générées (pywebpush/cryptography absents ?) : notifications indisponibles"
    fi
  fi
  [[ -f "$SECRETS/push.env" ]] && { chown root:homeos "$SECRETS/push.env"; chmod 640 "$SECRETS/push.env"; }
  systemctl daemon-reload
  systemctl enable homeos-web-app.service >/dev/null
  systemctl restart homeos-web-app.service
  for _ in $(seq 1 20); do
    curl -fsS "http://127.0.0.1:$PORT_APPLI/api/sante" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -fsS "http://127.0.0.1:$PORT_APPLI/api/sante" 2>/dev/null | grep -q '"ok":true'; then
    info "homeos-web-app actif : https://$nom_ts:$PORT_APPLI_TAILSCALE (enrôlement : python -m web.invitation <personne>)"
  else
    info "homeos-web-app ne répond pas : journalctl -u homeos-web-app -n 50"
  fi
else
  info "Tailscale non connecté (install/tailscale.sh) : appli web non publiée, homeos-web-app arrêté"
  systemctl disable --now homeos-web-app.service >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------------------
log "Capacités : installations propres (services locaux)"
for script in "$CIBLE"/capabilities/*/install.sh; do
  [[ -f "$script" ]] || continue
  bash "$script" "$CIBLE" || info "échec de $script (le reste de l'installation continue)"
done

# ------------------------------------------------------------------------------
log "Vérification de l'étanchéité des secrets"
bash "$CIBLE/scripts/verifier-secrets.sh" || info "à corriger avant d'aller plus loin (détail ci-dessus)"

# ------------------------------------------------------------------------------
log "Récapitulatif"
info "dépôt            : $CIBLE (homeos:foyer)"
info "données          : $DATA, état $ETAT (sauvegardés dans $BACKUP_REPO)"
info "secrets          : $SECRETS (un fichier par usage, jamais sauvegardés)"
info "claude           : $CLAUDE_BIN $CLAUDE_CODE (wrapper $WRAPPER)"
info "services         : homeos-bot + $(systemctl list-timers --no-pager --no-legend 'homeos-*' | wc -l) timers actifs (dont $(find /etc/systemd/system -maxdepth 1 -name 'homeos-cap-*.timer' | wc -l) de capacités)"
info "pare-feu         : $(ufw status | head -n1), fail2ban sshd + récidive"
info "filtrage sortant : $([[ -f "$FICHIER_PROXY" ]] && echo actif || echo inactif)"
if [[ "$MODE_OUTILS" == http ]]; then
  info "outils MCP       : http (homeos-outils $(systemctl is-active homeos-outils 2>/dev/null || true), homeos-web $(systemctl is-active homeos-web 2>/dev/null || true))"
else
  info "outils MCP       : stdio (repli)"
fi
if [[ -f "$FICHIER_APPLI" ]]; then
  info "appli web        : $(grep -s '^HOMEOS_WEB_ORIGINE=' "$FICHIER_APPLI" | cut -d= -f2-) (homeos-web-app $(systemctl is-active homeos-web-app 2>/dev/null || true))"
fi

cat <<EOF

Étapes manuelles restantes :
  1. Compléter les secrets (un fichier par usage) :
       sudo nano $SECRETS/telegram.env     (TELEGRAM_BOT_TOKEN)
       sudo nano $SECRETS/anthropic.env    (ANTHROPIC_API_KEY)
       sudo nano $SECRETS/ical.env         (URLs iCal des agendas communs ; agendas privés : ical-<personne>.env)
     Copier RESTIC_PASSWORD ($SECRETS/restic.env) et COFFRE_CLE ($SECRETS/coffre.env) dans vos gestionnaires
     de mots de passe : ils ne sont pas sauvegardés.
  2. Compléter la configuration :  sudo nano $DATA/config.yaml   (telegram_id, groupe_telegram_id)
  3. Connecter les comptes Claude (une fois par personne, avec SON compte) :
EOF
for u in "${PERSONNES[@]}"; do echo "       sudo -u $u -H claude auth login"; done
cat <<EOF
  4. Démarrer le bot :              sudo systemctl restart homeos-bot
  5. Vérifier :                     sudo bash $CIBLE/scripts/verifier.sh   puis  /statut dans Telegram
  6. Sauvegarde hors site :         sudo bash $CIBLE/scripts/configurer-hors-site.sh   (docs/sauvegardes.md)
  6b. Appli web (Tailscale connecté) : lien d'enrôlement de la passkey, une fois par appareil :
       cd $CIBLE && sudo -u homeos HOMEOS_DATA=$DATA .venv/bin/python -m web.invitation <prénom> [--telegram]
  7. Plus tard, après vérification manuelle :
       sudo bash install/install.sh --fermer-root       (root SSH fermé, depuis une session $ADMIN)
       sudo bash install/install.sh --filtrer-sortant   (puis scripts/reseau-test.sh)
  (relancer ce script après modification des personnes ou de install/versions.env ; les heures de
   config.yaml et l'activation des capacités sont appliquées automatiquement par homeos-appliquer-config.path)
EOF
