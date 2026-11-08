#!/usr/bin/env bash
# Cron d'une capacité (timers homeos-cap-<nom>-<cron>, générés par install.sh depuis les manifestes) :
# lance `python -m tools.capacites cron <nom> <cron>` ; un texte éventuellement renvoyé est posté dans le groupe.
# Usage : scripts/capacite-cron.sh <capacité> <cron>
set -euo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
PY="$ROOT/.venv/bin/python"
nom="${1:?usage : capacite-cron.sh <capacité> <cron>}"
cron="${2:?usage : capacite-cron.sh <capacité> <cron>}"
LOG_TAG="homeos-cap-$nom-$cron"

log() { echo "[$LOG_TAG] $*" >&2; }

# Secrets : fournis par systemd (EnvironmentFile=/etc/homeos/{anthropic,telegram}.env) ; en manuel, on les charge.
# shellcheck source=scripts/commun.sh
. "$ROOT/scripts/commun.sh"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  charger_secrets anthropic telegram
  charger_proxy
fi

# Pause (/stop dans Telegram) : on ne fait rien (tools.capacites le vérifie aussi).
if [[ -e "$DATA/PAUSE" ]]; then
  log "data/PAUSE présent, tâche ignorée"
  exit 0
fi

cd "$ROOT"
export HOMEOS_DATA="$DATA"

# --publier : le résultat est aussi remis dans le fil de l'appli (message spontané, coeur.spontane) ; une capacité
# qui publie elle-même (alerte personnelle) ne renvoie rien à poster dans le groupe.
resultat="$("$PY" -m tools.capacites cron "$nom" "$cron" --publier)"

if [[ -z "${resultat//[[:space:]]/}" ]]; then
  log "terminé, rien à envoyer"
  exit 0
fi

printf '%s\n' "$resultat" | "$PY" "$ROOT/scripts/telegram_send.py"
log "message envoyé (${#resultat} caractères)"
