#!/usr/bin/env bash
# Vérification complète après une installation ou une montée de version (lot 5d, audit point 3) — root.
#   1. pytest (sous homeos, sans réseau)      2. ruff
#   3. serveur d'outils (lot 7, mode http) : homeos-outils et homeos-web actifs, 401 sans jeton, appel d'outil avec
#      le jeton de homeos et de chaque personne (python -m tools.serveur_http sonde)
#   4. appli web (lot 4a) : /api/sante, 401 sans session (conversations, écrans, documents sensibles), 403 sans en-tête
#      CSRF, dossier des documents sensibles en 700 homeos, front servi (lot 4b), écoute locale, tailscale serve
#   5. coffre : homeos-coffre installé, statut, purge des quarantaines (lot 5c)
#   6. scripts/verifier-secrets.sh             7. `claude -p "liste les courses"` pour chaque mode utilisé
# Usage : sudo bash /srv/home-os/scripts/verifier.sh [--sans-claude]
set -uo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
PY="$ROOT/.venv/bin/python"
WRAPPER=/usr/local/sbin/homeos-claude
sans_claude=0; [[ "${1:-}" == "--sans-claude" ]] && sans_claude=1
[[ $EUID -eq 0 ]] || { echo "à lancer en root (sudo)" >&2; exit 2; }
[[ -x "$PY" ]] || { echo "$PY absent : lancer install/install.sh" >&2; exit 2; }

resultats=()
etape() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
noter() { resultats+=("$1 : $2"); }

# Outils de vérification dans le venv (absents des requirements de production).
if ! "$PY" -c 'import pytest' 2>/dev/null || [[ ! -x "$ROOT/.venv/bin/ruff" ]]; then
  "$ROOT/.venv/bin/pip" install -q pytest ruff && chown -R homeos:foyer "$ROOT/.venv"
fi

etape "pytest"
if (cd "$ROOT" && sudo -n -u homeos env HOME=/home/homeos PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
      "$PY" -m pytest -q -p no:cacheprovider); then noter pytest OK; else noter pytest ÉCHEC; fi

etape "ruff"
if (cd "$ROOT" && "$ROOT/.venv/bin/ruff" check --no-cache .); then noter ruff OK; else noter ruff ÉCHEC; fi

etape "bash -n"
ok_bash=OK
for s in "$ROOT"/scripts/*.sh "$ROOT"/install/*.sh "$ROOT"/install/bin/homeos-claude "$ROOT"/install/bin/homeos-coffre \
         "$ROOT"/capabilities/*/install.sh; do
  [[ -f "$s" ]] || continue
  bash -n "$s" || { echo "syntaxe : $s"; ok_bash=ÉCHEC; }
done
noter "bash -n" "$ok_bash"

etape "administration par la conversation (lot 7a)"
ok_admin=OK
systemctl is-active --quiet homeos-appliquer-config.path || { echo "homeos-appliquer-config.path inactif"; ok_admin=ÉCHEC; }
cmp -s "$ROOT/scripts/appliquer-config.sh" /usr/local/sbin/homeos-appliquer-config \
  || { echo "/usr/local/sbin/homeos-appliquer-config différent du dépôt : relancer install/install.sh"; ok_admin=ÉCHEC; }
if ! grep -qs '"ok": true' "$ROOT/data/administration/application.json"; then
  echo "dernière application de la configuration en échec ou absente : $(cat "$ROOT/data/administration/application.json" 2>/dev/null)"
  ok_admin=ÉCHEC
fi
noter "administration" "$ok_admin"

etape "serveur d'outils (lot 7)"
if grep -qsx 'HOMEOS_OUTILS=http' /etc/default/homeos-outils; then
  ok_outils=OK
  for s in homeos-outils homeos-web; do
    systemctl is-active --quiet "$s.service" || { echo "$s inactif : journalctl -u $s -n 50"; ok_outils=ÉCHEC; }
  done
  sonder() { # sonder <utilisateur> <outil>
    (cd "$ROOT" && sudo -n -u homeos env HOMEOS_SECRETS=/etc/homeos PYTHONPATH="$ROOT" "$PY" -m tools.serveur_http sonde \
      --utilisateur "$1" --appel "$2" 2>/dev/null)
  }
  sortie="$(sonder homeos capacites_lister)"
  echo "  homeos : $sortie"
  grep -q '"ok": true' <<<"$sortie" || ok_outils=ÉCHEC
  for u in $("$PY" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}; print(" ".join(p["linux_user"] for p in c.get("personnes") or [] if isinstance(p, dict) and p.get("linux_user")))' "$ROOT/data/config.yaml" 2>/dev/null); do
    sortie="$(sonder "$u" courses_lister)"
    if grep -q '"ok": true' <<<"$sortie"; then echo "  $u : jeton accepté, courses_lister OK"; else echo "  $u : $sortie"; ok_outils=ÉCHEC; fi
  done
  noter "serveur d'outils" "$ok_outils"
else
  echo "  mode stdio (repli) : rien à vérifier"
  noter "serveur d'outils" "stdio"
fi

etape "appli web (lot 4a)"
if [[ -f /etc/default/homeos-web-app ]]; then
  ok_appli=OK
  port_appli="$(sed -n 's/^HOMEOS_WEB_PORT=\([0-9]\{1,5\}\)$/\1/p' /etc/default/homeos-web-app)"
  origine="$(sed -n 's/^HOMEOS_WEB_ORIGINE=//p' /etc/default/homeos-web-app)"
  systemctl is-active --quiet homeos-web-app.service || { echo "homeos-web-app inactif : journalctl -u homeos-web-app -n 50"; ok_appli=ÉCHEC; }
  if curl -fsS --max-time 10 "http://127.0.0.1:${port_appli:-8095}/api/sante" | grep -q '"ok":true'; then
    echo "  /api/sante (127.0.0.1:${port_appli:-8095}) : OK"
  else
    echo "  /api/sante ne répond pas"; ok_appli=ÉCHEC
  fi
  # Sans session : 401 ; écriture sans en-tête CSRF : 403 ; l'appli n'écoute pas sur une adresse publique.
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port_appli:-8095}/api/conversations")"
  [[ "$code" == 401 ]] || { echo "  /api/conversations sans session : $code (401 attendu)"; ok_appli=ÉCHEC; }
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${port_appli:-8095}/auth/connexion/options")"
  [[ "$code" == 403 ]] || { echo "  POST sans en-tête CSRF : $code (403 attendu)"; ok_appli=ÉCHEC; }
  # Écrans sans modèle et documents sensibles : jamais sans session.
  for r in /api/courses /api/memoire /api/fichiers/entrants /api/sensibles; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port_appli:-8095}$r")"
    [[ "$code" == 401 ]] || { echo "  $r sans session : $code (401 attendu)"; ok_appli=ÉCHEC; }
  done
  # Documents sensibles chiffrés : dossier de l'appli seule (700 homeos), jamais du groupe foyer.
  sensibles="$ROOT/data/fichiers/sensibles"
  if [[ -d "$sensibles" && "$(stat -c '%U %a' "$sensibles")" != "homeos 700" ]]; then
    echo "  $sensibles : $(stat -c '%U %a' "$sensibles") (homeos 700 attendu : relancer install/install.sh)"; ok_appli=ÉCHEC
  fi
  # Lot 4b : l'appli (build statique) est servie sur / avec une CSP à nonce, et ses ressources existent.
  page="$(curl -s -D - --max-time 10 "http://127.0.0.1:${port_appli:-8095}/")"
  if grep -qi "^content-security-policy:.*'nonce-" <<<"$page" && grep -q 'name="csp-nonce"' <<<"$page"; then
    for r in $(grep -o '/assets/[A-Za-z0-9._-]*' <<<"$page" | sort -u) /sw.js /manifest.webmanifest; do
      c="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port_appli:-8095}$r")"
      [[ "$c" == 200 ]] || { echo "  ressource de l'appli $r : $c"; ok_appli=ÉCHEC; }
    done
    echo "  front (lot 4b) servi sur / : OK"
  else
    echo "  front absent (web/static/app) : la page de démonstration est servie à sa place"; ok_appli=ÉCHEC
  fi
  if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ":${port_appli:-8095}$" | grep -vqE '^(127\.0\.0\.1|\[::1\]):'; then
    echo "  l'appli écoute ailleurs qu'en local"; ok_appli=ÉCHEC
  fi
  if command -v tailscale >/dev/null 2>&1; then
    tailscale serve status 2>/dev/null | grep -q "127.0.0.1:${port_appli:-8095}" \
      && echo "  tailscale serve : $origine" || { echo "  tailscale serve ne publie pas l'appli"; ok_appli=ÉCHEC; }
    tailscale funnel status 2>/dev/null | grep -qi 'funnel on' && { echo "  Funnel actif : l'appli ne doit pas être publique"; ok_appli=ÉCHEC; }
  fi
  noter "appli web" "$ok_appli"
else
  echo "  non installée (Tailscale non connecté)"
  noter "appli web" "non installée"
fi

etape "coffre (lot 5c)"
# CLI installée, coffre lisible sous homeos, quarantaines expirées effacées ; aucune valeur affichée.
ok_coffre=OK
[[ -x /usr/local/bin/homeos-coffre ]] || { echo "  /usr/local/bin/homeos-coffre absent (relancer install.sh)"; ok_coffre=ÉCHEC; }
if [[ $ok_coffre == OK ]]; then
  sudo -n -u homeos /usr/local/bin/homeos-coffre statut || ok_coffre=ÉCHEC
  sudo -n -u homeos /usr/local/bin/homeos-coffre purger || ok_coffre=ÉCHEC
fi
noter coffre "$ok_coffre"

etape "secrets"
if [[ $sans_claude -eq 1 ]]; then opt=(--sans-claude); else opt=(); fi
if bash "$ROOT/scripts/verifier-secrets.sh" "${opt[@]}"; then noter secrets OK; else noter secrets ÉCHEC; fi

if [[ $sans_claude -eq 0 ]] && command -v claude >/dev/null 2>&1; then
  etape "claude -p \"liste les courses\" ($(claude --version 2>/dev/null | head -n1))"
  mapfile -t lignes < <("$PY" - "$ROOT/data/config.yaml" 2>/dev/null <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for p in cfg.get("personnes") or []:
    if isinstance(p, dict) and p.get("linux_user"):
        print(p["linux_user"], p.get("auth") or "abonnement")
PYEOF
  )
  cle_api_utilisee=0
  for l in "${lignes[@]}"; do
    u="${l%% *}"; mode="${l#* }"
    if [[ "$mode" == cle_api ]]; then cle_api_utilisee=1; continue; fi
    # Commande du bot (config du dépôt) : en mode http, le wrapper y substitue la config HTTP avec le jeton.
    sortie="$(cd / && timeout 180 sudo -n -H -u "$u" "$WRAPPER" -p "Liste les courses avec l'outil courses_lister, en une ligne." \
      --output-format json --mcp-config "$ROOT/tools/mcp.json" 2>&1)"
    if grep -q '"is_error":[[:space:]]*false' <<<"$sortie"; then noter "claude ($u, abonnement)" OK
    else noter "claude ($u, abonnement)" "ÉCHEC ${sortie:0:160}"; fi
  done
  if [[ $cle_api_utilisee -eq 1 || -n "$(grep -s '^ANTHROPIC_API_KEY=.' /etc/homeos/anthropic.env)" ]]; then
    sortie="$(cd /var/lib/homeos-cwd/homeos 2>/dev/null || cd /; set -a; . /etc/homeos/anthropic.env; [[ -r /etc/default/homeos-proxy ]] && . /etc/default/homeos-proxy; set +a
              timeout 180 sudo -n -u homeos --preserve-env=ANTHROPIC_API_KEY,HTTPS_PROXY,HTTP_PROXY,NO_PROXY \
                env HOME=/home/homeos DISABLE_AUTOUPDATER=1 claude --bare -p "ok" --output-format json --max-turns 1 2>&1)"
    if grep -q '"is_error":[[:space:]]*false' <<<"$sortie"; then noter "claude (clé API, homeos)" OK
    else noter "claude (clé API, homeos)" "ÉCHEC ${sortie:0:160}"; fi
  fi
fi

etape "Résumé"
statut=0
for r in "${resultats[@]}"; do
  echo "  $r"
  [[ "$r" == *ÉCHEC* ]] && statut=1
done
exit $statut
