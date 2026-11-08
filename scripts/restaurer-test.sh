#!/usr/bin/env bash
# Test de restauration mensuel (lot 5d, audit point 5, revue R8) : `restic restore latest` dans un dossier
# temporaire, puis vérifications — base SQLite intègre (PRAGMA integrity_check), config.yaml présent,
# nombre de fichiers comparable à data/. Résultat (JSON) dans data/.derniere-restauration, lu par /statut et
# le healthcheck (qui alerte si le test a échoué ou date de plus de 40 jours).
#
# Dépôt testé : le dépôt hors site s'il est configuré (c'est celui qui sert si le VPS disparaît), sinon le
# dépôt local. Usage : restaurer-test.sh [--local|--distant]
set -euo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
# shellcheck source=scripts/commun.sh
. "$ROOT/scripts/commun.sh"
RESULTAT="$DATA/.derniere-restauration"

log() { echo "[homeos-restaurer-test] $*" >&2; }

if [[ -z "${RESTIC_PASSWORD:-}" ]]; then charger_secrets restic; charger_proxy; fi
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-/var/backups/homeos}"
[[ -n "${RCLONE_CONFIG:-}" ]] && export RCLONE_CONFIG

choix="${1:-auto}"
depot="local"
if [[ "$choix" == "--distant" || ( "$choix" == "auto" && -n "${RESTIC_REPOSITORY_DISTANT:-}" ) ]]; then
  [[ -n "${RESTIC_REPOSITORY_DISTANT:-}" && -n "${RESTIC_PASSWORD_DISTANT:-}" ]] \
    || { log "dépôt distant non configuré"; exit 2; }
  export RESTIC_REPOSITORY="$RESTIC_REPOSITORY_DISTANT" RESTIC_PASSWORD="$RESTIC_PASSWORD_DISTANT"
  depot="distant"
fi

ecrire_resultat() { # ok(true|false) message fichiers snapshot
  "$PY" - "$RESULTAT" "$1" "$2" "$3" "$4" "$depot" <<'PYEOF'
import json, sys, datetime, os
chemin, ok, message, fichiers, snapshot, depot = sys.argv[1:7]
info = {
    "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "ok": ok == "true",
    "depot": depot,
    "fichiers": int(fichiers) if fichiers.isdigit() else None,
    "snapshot": snapshot or None,
    "message": message,
}
tmp = chemin + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(info, fh, ensure_ascii=False)
os.replace(tmp, chemin)
PYEOF
}

if [[ -z "${RESTIC_PASSWORD:-}" ]]; then
  ecrire_resultat false "mot de passe restic absent" "" ""
  exit 1
fi

CIBLE="$(mktemp -d /var/tmp/homeos-restauration.XXXXXX)"
trap 'rm -rf "$CIBLE"' EXIT

echec() { log "ÉCHEC : $1"; ecrire_resultat false "$1" "${2:-}" "${snapshot:-}"; exit 1; }

snapshot="$(restic snapshots --tag homeos --latest 1 --json 2>/dev/null | "$PY" -c '
import json, sys
try:
    s = json.load(sys.stdin)
    print(s[-1]["short_id"] if s else "")
except Exception:
    print("")
')" || snapshot=""
[[ -n "$snapshot" ]] || echec "aucun instantané lisible dans le dépôt $depot"

log "restauration de $snapshot ($depot) dans $CIBLE"
restic restore "$snapshot" --target "$CIBLE" --quiet || echec "restic restore a échoué" ""

restaure="$CIBLE$DATA"
[[ -d "$restaure" ]] || echec "dossier de données absent de la sauvegarde" ""
nb=$(find "$restaure" -type f | wc -l)
nb_actuel=$(find "$DATA" -type f 2>/dev/null | wc -l)
[[ -f "$restaure/config.yaml" ]] || echec "config.yaml absent de la sauvegarde" "$nb"

if [[ -f "$restaure/homeos.sqlite" ]]; then
  integrite="$("$PY" -c '
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(con.execute("PRAGMA integrity_check").fetchone()[0])
' "$restaure/homeos.sqlite" 2>&1 || true)"
  [[ "$integrite" == "ok" ]] || echec "base SQLite restaurée corrompue : ${integrite:0:120}" "$nb"
fi

# Une sauvegarde de la veille peut avoir un peu moins de fichiers ; moins de la moitié est suspect.
if (( nb_actuel > 20 && nb * 2 < nb_actuel )); then
  echec "seulement $nb fichiers restaurés pour $nb_actuel actuellement" "$nb"
fi

ecrire_resultat true "restauration vérifiée" "$nb" "$snapshot"
log "OK : $nb fichiers, base intègre (instantané $snapshot, dépôt $depot)"
