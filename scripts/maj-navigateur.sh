#!/usr/bin/env bash
# Mise à jour mensuelle de Playwright et de son Chromium à la version épinglée (install/versions.env), puis
# test Kitchen Daily (`kd statut`) — root, timer homeos-maj-navigateur (lot 5d, audit point 10).
# Alerte Telegram en cas d'échec. Pour monter de version : modifier PLAYWRIGHT dans install/versions.env.
set -uo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
PY="$ROOT/.venv/bin/python"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/playwright}"
[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 2; }
# shellcheck source=install/versions.env
. "$ROOT/install/versions.env"

log() { echo "[homeos-maj-navigateur] $*" >&2; }
alerter() {
  log "ÉCHEC : $*"
  printf '🌐 Firmin Assistant — mise à jour du navigateur (Playwright %s) : %s\n' "$PLAYWRIGHT" "$*" \
    | "$PY" "$ROOT/scripts/telegram_send.py" || true
  exit 1
}

actuelle="$("$PY" -c 'import importlib.metadata as m; print(m.version("playwright"))' 2>/dev/null || echo "?")"
log "Playwright installé : $actuelle, épinglé : $PLAYWRIGHT"
if [[ "$actuelle" != "$PLAYWRIGHT" ]]; then
  "$ROOT/.venv/bin/pip" install -q "playwright==$PLAYWRIGHT" || alerter "pip install playwright==$PLAYWRIGHT impossible"
  chown -R homeos:foyer "$ROOT/.venv"
fi
"$ROOT/.venv/bin/playwright" install --with-deps chromium >/dev/null || alerter "playwright install chromium impossible"
chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"
# Lot 7 : le client (serveur d'outils) et le serveur Playwright (homeos-web) doivent avoir la même version.
if [[ "$actuelle" != "$PLAYWRIGHT" ]]; then
  for s in homeos-web homeos-outils; do
    systemctl is-active --quiet "$s.service" && { systemctl restart "$s.service" || alerter "redémarrage de $s impossible"; }
  done
fi

# Test réel : la session Kitchen Daily s'ouvre-t-elle avec ce navigateur ? (sous homeos, proxy si activé)
proxy=()
if [[ -r /etc/default/homeos-proxy ]]; then
  while IFS= read -r l; do [[ "$l" =~ ^[A-Za-z_]+=.+$ ]] && proxy+=("$l"); done </etc/default/homeos-proxy
fi
if ! sortie="$(cd "$ROOT" && sudo -n -u homeos env HOME=/home/homeos HOMEOS_DATA="$DATA" HOMEOS_ETAT=/var/lib/homeos \
      PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" "${proxy[@]}" "$PY" -m tools.kitchen_daily statut 2>&1)"; then
  alerter "kd statut en échec après mise à jour : ${sortie: -200}"
fi
log "OK : ${sortie: -120}"
