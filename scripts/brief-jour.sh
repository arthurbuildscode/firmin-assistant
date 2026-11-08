#!/usr/bin/env bash
# Brief du jour : exécute tools.runner et poste le résultat dans le groupe Telegram.
set -euo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
PY="$ROOT/.venv/bin/python"
LOG_TAG="homeos-brief-jour"

log() { echo "[$LOG_TAG] $*" >&2; }

# Secrets : fournis par systemd (EnvironmentFile=/etc/homeos/{anthropic,telegram}.env) ; en manuel, on les charge.
# shellcheck source=scripts/commun.sh
. "$ROOT/scripts/commun.sh"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${ANTHROPIC_API_KEY:-}" ]]; then
  charger_secrets anthropic telegram
  charger_proxy
fi

# Pause (/stop dans Telegram) : on ne fait rien.
if [[ -e "$DATA/PAUSE" ]]; then
  log "data/PAUSE présent, tâche ignorée"
  exit 0
fi

cd "$ROOT"
export HOMEOS_DATA="$DATA"

# Le runner assemble les données et appelle claude --bare (chantier outils).
# --publier : le brief est aussi remis dans le fil de l'appli de chaque adulte (message spontané, coeur.spontane).
resultat="$("$PY" -m tools.runner brief jour --publier)"

if [[ -z "${resultat//[[:space:]]/}" ]]; then
  log "runner sans sortie, rien envoyé"
  exit 0
fi

printf '%s\n' "$resultat" | "$PY" "$ROOT/scripts/telegram_send.py"
log "message envoyé (${#resultat} caractères)"
