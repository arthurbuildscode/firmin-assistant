#!/usr/bin/env bash
# Sauvegarde restic de /srv/home-os/data et de l'état sensible (/var/lib/homeos : coffre chiffré, session
# Kitchen Daily, sessions du bot) vers le dépôt local /var/backups/homeos, puis copie hors site.
# Rétention : 30 quotidiennes (local et distant) + 12 mensuelles (distant). Vérification d'intégrité mensuelle.
#
# Hors site (lot 5d, audit point 5) : si RESTIC_REPOSITORY_DISTANT est défini (ex. rclone:gdrive:homeos,
# voir docs/sauvegardes.md), `restic copy` y recopie les instantanés (mot de passe distinct
# RESTIC_PASSWORD_DISTANT, configuration rclone RCLONE_CONFIG). Les secrets de /etc/homeos ne sont jamais
# sauvegardés : ils se conservent hors du VPS (gestionnaires de mots de passe).
set -euo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
# shellcheck source=scripts/commun.sh
. "$ROOT/scripts/commun.sh"
ETAT="$HOMEOS_ETAT"
MARQUEUR_CHECK="$DATA/.restic-last-check"
MARQUEUR_PRUNE_DISTANT="$DATA/.restic-distant-last-prune"

log() { echo "[homeos-backup] $*" >&2; }

# Secrets : fournis par systemd (EnvironmentFile=/etc/homeos/restic.env) ; en manuel (root), on les charge.
if [[ -z "${RESTIC_PASSWORD:-}" ]]; then charger_secrets restic; charger_proxy; fi
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-/var/backups/homeos}"
if [[ -z "${RESTIC_PASSWORD:-}" ]]; then
  log "RESTIC_PASSWORD absent (/etc/homeos/restic.env) — abandon"
  exit 1
fi
export RESTIC_PASSWORD
[[ -n "${RCLONE_CONFIG:-}" ]] && export RCLONE_CONFIG

# Dépôt non initialisé (ne devrait pas arriver après install.sh).
if ! restic cat config >/dev/null 2>&1; then
  log "dépôt absent, initialisation"
  restic init
fi

# Purge des fichiers temporaires avant la sauvegarde (entrants > 30 j, captures > 90 j, vocaux > 7 j).
# Un échec de la purge n'empêche jamais la sauvegarde.
log "purge des fichiers temporaires"
( cd "$ROOT" && HOMEOS_DATA="$DATA" "$ROOT/.venv/bin/python" -m tools.runner purge >/dev/null ) \
  || log "purge en échec (sauvegarde poursuivie)"

cibles=("$DATA")
if [[ -d "$ETAT" && "$ETAT" != "$DATA" ]]; then cibles+=("$ETAT"); fi
log "sauvegarde de ${cibles[*]}"
# Exclus : copie root de l'ancien secrets.env (illisible par homeos, jamais sauvegardée), dossiers de travail
# vides des sessions Claude, configuration rclone (contient le jeton Google Drive).
restic backup "${cibles[@]}" --tag homeos --exclude "$DATA/.venv" --exclude "$DATA/secrets.env*" \
  --exclude "$DATA/.cwd" --exclude "$ETAT/cwd" --exclude "$ETAT/outils" --exclude "$ETAT/rclone" --quiet

date -Is > "$DATA/.derniere-sauvegarde"   # lu par /statut et healthcheck

log "rétention locale : keep-daily 30"
restic forget --tag homeos --keep-daily 30 --prune --quiet

# Check mensuel : si aucun check depuis 30 jours.
if [[ ! -e "$MARQUEUR_CHECK" ]] || [[ -n "$(find "$MARQUEUR_CHECK" -mtime +30 2>/dev/null)" ]]; then
  log "vérification mensuelle du dépôt"
  restic check --quiet
  touch "$MARQUEUR_CHECK"
fi

# --- copie hors site -------------------------------------------------------------------------------------------
if [[ -n "${RESTIC_REPOSITORY_DISTANT:-}" ]]; then
  if [[ -z "${RESTIC_PASSWORD_DISTANT:-}" ]]; then
    log "RESTIC_REPOSITORY_DISTANT défini sans RESTIC_PASSWORD_DISTANT : copie hors site ignorée"
    exit 1
  fi
  distant() { RESTIC_REPOSITORY="$RESTIC_REPOSITORY_DISTANT" RESTIC_PASSWORD="$RESTIC_PASSWORD_DISTANT" restic "$@"; }
  log "copie hors site vers $RESTIC_REPOSITORY_DISTANT"
  RESTIC_FROM_REPOSITORY="$RESTIC_REPOSITORY" RESTIC_FROM_PASSWORD="$RESTIC_PASSWORD" \
    distant copy --tag homeos --quiet
  date -Is > "$DATA/.derniere-copie-distante"   # lu par /statut et healthcheck (âge du dépôt distant)
  # Rétention distante une fois par semaine (le nettoyage télécharge des index : on le limite).
  if [[ ! -e "$MARQUEUR_PRUNE_DISTANT" ]] || [[ -n "$(find "$MARQUEUR_PRUNE_DISTANT" -mtime +7 2>/dev/null)" ]]; then
    log "rétention distante : keep-daily 30, keep-monthly 12"
    distant forget --tag homeos --keep-daily 30 --keep-monthly 12 --prune --quiet
    touch "$MARQUEUR_PRUNE_DISTANT"
  fi
else
  log "pas de dépôt hors site configuré (RESTIC_REPOSITORY_DISTANT) — voir docs/sauvegardes.md"
fi

log "terminé"
