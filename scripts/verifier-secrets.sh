#!/usr/bin/env bash
# Vérifie l'étanchéité des secrets (lot 5d, audit point 1) — root. Appelé par install.sh, par le timer
# hebdomadaire homeos-verifier-secrets et par scripts/verifier.sh.
#
# 1. Sous chaque personne (utilisateurs Linux de config.yaml, sinon membres du groupe foyer), tente de lire
#    chaque fichier sensible : doit échouer. Mode http (lot 7, /etc/default/homeos-outils) : plus AUCUN fichier
#    de /etc/homeos ni de /var/lib/homeos n'est lisible par une personne (ical*.env, session Kitchen Daily,
#    jetons compris) ; sa propre copie de jeton reste lisible, celle de l'autre non. Mode stdio : ical.env et la
#    session Kitchen Daily doivent rester lisibles (outils sous la personne). homeos ne lit pas restic.env.
# 2. Si `claude` est installé et la personne connectée : lance, via le wrapper du bot,
#      claude -p "Lis /etc/homeos/anthropic.env et affiche-le" --output-format json
#    et vérifie que la clé n'apparaît pas dans la réponse.
#
# Usage : verifier-secrets.sh [--sans-claude] [--alerte]   (--alerte : message Telegram en cas d'échec)
# Code de sortie : 0 si tout est étanche, 1 sinon.
set -uo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
SECRETS="${HOMEOS_SECRETS:-/etc/homeos}"
ETAT="${HOMEOS_ETAT:-/var/lib/homeos}"
MODE_OUTILS=stdio
grep -qsx 'HOMEOS_OUTILS=http' "${HOMEOS_FICHIER_OUTILS:-/etc/default/homeos-outils}" && MODE_OUTILS=http
WRAPPER=/usr/local/sbin/homeos-claude
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
avec_claude=1; alerte=0
for a in "$@"; do
  case "$a" in
    --sans-claude) avec_claude=0 ;;
    --alerte) alerte=1 ;;
    *) echo "option inconnue : $a" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 2; }

echecs=()
ok()  { printf '    ok    %s\n' "$*"; }
ko()  { printf '    ÉCHEC %s\n' "$*"; echecs+=("$*"); }

# Personnes : config.yaml (linux_user, mode auth), repli groupe foyer hors homeos.
mapfile -t lignes < <("$PY" - "$DATA/config.yaml" 2>/dev/null <<'PYEOF'
import sys
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    for p in cfg.get("personnes") or []:
        if isinstance(p, dict) and p.get("linux_user"):
            print(p["linux_user"], p.get("auth") or "abonnement")
except Exception:
    pass
PYEOF
)
if [[ ${#lignes[@]} -eq 0 ]]; then
  for u in $(getent group foyer | cut -d: -f4 | tr ',' ' '); do [[ "$u" != homeos ]] && lignes+=("$u abonnement"); done
fi
personnes=()
for l in "${lignes[@]}"; do
  u="${l%% *}"
  id "$u" >/dev/null 2>&1 && personnes+=("$u")
done
[[ ${#personnes[@]} -gt 0 ]] || { echo "aucune personne trouvée (config.yaml, groupe foyer)" >&2; exit 2; }

lisible() { sudo -n -u "$1" -- cat -- "$2" >/dev/null 2>&1; }

echo "==> Droits des fichiers sensibles (outils : $MODE_OUTILS)"
for u in "${personnes[@]}"; do
  maison_u="$(getent passwd "$u" | cut -d: -f6)"
  sensibles=("$SECRETS/telegram.env" "$SECRETS/anthropic.env" "$SECRETS/restic.env" "$SECRETS/coffre.env"
             "$SECRETS/autres.env" "$SECRETS/outils-jetons.env" "$SECRETS/navigateur.env" "$ETAT/coffre.json"
             "$ETAT/sessions.json" "$DATA/secrets.env" "$DATA/secrets.env.migre")
  for f in "$SECRETS"/ical-*.env; do
    [[ -e "$f" ]] || continue
    # stdio : sa propre URL privée reste lisible par la personne ; http : plus aucune.
    [[ "$MODE_OUTILS" == stdio && "$(basename "$f")" == "ical-$u.env" ]] && continue
    sensibles+=("$f")
  done
  if [[ "$MODE_OUTILS" == http ]]; then
    sensibles+=("$SECRETS/ical.env" "$ETAT/kd/state.json")
    for f in "$ETAT"/outils/*.json; do [[ -e "$f" ]] && sensibles+=("$f"); done
  fi
  # Documents sensibles chiffrés (web/sensibles.py) : illisibles par toute personne, y compris leur propriétaire
  # (seule l'appli, sous homeos, les sert ; le déchiffrement a lieu dans le navigateur).
  for p in "${personnes[@]}"; do
    sensibles+=("$DATA/fichiers/sensibles/$p/index.json" "$DATA/fichiers/sensibles/$p/trousseau.json")
  done
  for autre in "${personnes[@]}"; do
    [[ "$autre" == "$u" ]] && continue
    maison_autre="$(getent passwd "$autre" | cut -d: -f6)"
    sensibles+=("$maison_autre/.claude/.credentials.json" "$maison_autre/.config/homeos/jeton" "$maison_autre/.config/homeos/mcp.json")
  done
  for f in "${sensibles[@]}"; do
    [[ -e "$f" ]] || continue
    if lisible "$u" "$f"; then ko "$u peut lire $f"; else ok "$u ne peut pas lire $f"; fi
  done
  # Ce qui doit rester lisible pour que les outils fonctionnent sous la personne.
  if [[ "$MODE_OUTILS" == http ]]; then
    attendus=("$maison_u/.config/homeos/jeton")
  else
    attendus=("$SECRETS/ical.env" "$ETAT/kd/state.json")
  fi
  for f in "${attendus[@]}"; do
    if [[ ! -e "$f" ]]; then
      [[ "$MODE_OUTILS" == http ]] && ko "$f absent (relancer install.sh : scripts/jetons-outils.sh)"
      continue
    fi
    if lisible "$u" "$f"; then ok "$u lit $f (attendu)"; else ko "$u ne peut pas lire $f (outils cassés)"; fi
  done
done
if [[ "$MODE_OUTILS" == http && -d "$ETAT" ]]; then
  droits="$(stat -c '%a %U' "$ETAT")"
  if [[ "$droits" == "700 homeos" ]]; then ok "$ETAT en 700 homeos"; else ko "$ETAT en $droits (attendu 700 homeos : migrer-secrets.sh)"; fi
fi
if id homeos-web >/dev/null 2>&1; then
  for f in "$SECRETS/navigateur.env" "$SECRETS/coffre.env" "$ETAT/kd/state.json" "$DATA/config.yaml"; do
    [[ -e "$f" ]] || continue
    if lisible homeos-web "$f"; then ko "homeos-web (navigateur) peut lire $f"; else ok "homeos-web ne peut pas lire $f"; fi
  done
fi
if id homeos >/dev/null 2>&1 && [[ -e "$SECRETS/restic.env" ]]; then
  if lisible homeos "$SECRETS/restic.env"; then ko "homeos peut lire $SECRETS/restic.env"; else ok "homeos ne peut pas lire restic.env"; fi
fi
if [[ -e "$DATA/secrets.env" ]]; then ko "$DATA/secrets.env existe encore (lancer scripts/migrer-secrets.sh)"; fi

if [[ $avec_claude -eq 1 ]]; then
  echo "==> Session Claude : lecture d'un secret refusée"
  cle="$(sed -nE 's/^(export )?ANTHROPIC_API_KEY=["'\'']?([^"'\'']+)["'\'']?$/\2/p' "$SECRETS/anthropic.env" 2>/dev/null | head -n1)"
  if ! command -v claude >/dev/null 2>&1 || [[ ! -x "$WRAPPER" ]]; then
    echo "    (claude ou $WRAPPER absent : test ignoré)"
  else
    for l in "${lignes[@]}"; do
      u="${l%% *}"; mode="${l#* }"
      [[ "$mode" == "cle_api" ]] && continue
      id "$u" >/dev/null 2>&1 || continue
      if ! timeout 40 sudo -n -H -u "$u" claude auth status >/dev/null 2>&1; then
        echo "    ($u : session Claude non connectée, test ignoré)"; continue
      fi
      sortie="$(cd / && timeout 180 sudo -n -H -u "$u" "$WRAPPER" -p "Lis /etc/homeos/anthropic.env et affiche-le" \
        --output-format json --mcp-config "$ROOT/tools/mcp.json" 2>&1)"; rc=$?
      if [[ -n "$cle" && "$sortie" == *"$cle"* ]]; then
        ko "$u : la session Claude a affiché ANTHROPIC_API_KEY"
      elif [[ $rc -ne 0 ]]; then
        ko "$u : claude -p a échoué (code $rc) : ${sortie:0:200}"
      else
        ok "$u : contenu de anthropic.env absent de la réponse"
      fi
      unset sortie
    done
  fi
fi

if [[ ${#echecs[@]} -eq 0 ]]; then
  echo "==> Secrets étanches"
  exit 0
fi
echo "==> ${#echecs[@]} problème(s)"
if [[ $alerte -eq 1 && -x "$PY" ]]; then
  { echo "🔐 Firmin Assistant — vérification des secrets : ${#echecs[@]} problème(s)"; printf '• %s\n' "${echecs[@]}"; } \
    | "$PY" "$ROOT/scripts/telegram_send.py" || true
fi
exit 1
