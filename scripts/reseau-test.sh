#!/usr/bin/env bash
# Vérifie le filtrage sortant (lot 5d, audit point 8) — root, après `install.sh --filtrer-sortant`.
# Pour homeos et chaque personne : un hôte non listé est bloqué (en direct par nftables, via le proxy par
# tinyproxy), api.anthropic.com passe par le proxy, la résolution DNS fonctionne, le direct est bloqué.
# Usage : sudo bash /srv/home-os/scripts/reseau-test.sh [hote-non-liste]   (défaut : example.com)
set -uo pipefail

PROXY="http://127.0.0.1:3128"
INTERDIT="${1:-example.com}"
[[ $EUID -eq 0 ]] || { echo "à lancer en root (sudo)" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl absent" >&2; exit 2; }
[[ -r /etc/default/homeos-proxy ]] || echo "attention : filtrage non activé (/etc/default/homeos-proxy absent)"

utilisateurs=(homeos)
for u in $(getent group foyer | cut -d: -f4 | tr ',' ' '); do
  [[ "$u" != homeos ]] && utilisateurs+=("$u")
done

echecs=0
ok() { printf '    ok    %s\n' "$*"; }
ko() { printf '    ÉCHEC %s\n' "$*"; echecs=$((echecs + 1)); }
code_http() { # utilisateur, options curl…
  local u="$1"; shift
  sudo -n -u "$u" env -i PATH=/usr/bin:/bin curl -sS -o /dev/null -m 10 -w '%{http_code}' "$@" 2>/dev/null
}

for u in "${utilisateurs[@]}"; do
  id "$u" >/dev/null 2>&1 || continue
  echo "==> $u"
  if sudo -n -u "$u" getent hosts api.anthropic.com >/dev/null; then ok "DNS"; else ko "DNS indisponible"; fi
  c="$(code_http "$u" --noproxy '*' "https://$INTERDIT/")"
  if [[ "$c" == "000" || -z "$c" ]]; then ok "direct vers $INTERDIT bloqué"; else ko "direct vers $INTERDIT possible (HTTP $c)"; fi
  c="$(code_http "$u" --noproxy '*' "https://api.anthropic.com/")"
  if [[ "$c" == "000" || -z "$c" ]]; then ok "direct vers api.anthropic.com bloqué (passage obligé par le proxy)"; else ko "direct vers api.anthropic.com possible (HTTP $c)"; fi
  c="$(code_http "$u" -x "$PROXY" "https://$INTERDIT/")"
  if [[ "$c" == "000" || "$c" == "403" || -z "$c" ]]; then ok "$INTERDIT refusé par le proxy"; else ko "$INTERDIT autorisé par le proxy (HTTP $c)"; fi
  c="$(code_http "$u" -x "$PROXY" "https://api.anthropic.com/")"
  if [[ -n "$c" && "$c" != "000" ]]; then ok "api.anthropic.com joignable via le proxy (HTTP $c)"; else ko "api.anthropic.com injoignable via le proxy"; fi
done

echo "==> root (non filtré : apt, npm, pip)"
c="$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "https://$INTERDIT/" 2>/dev/null)"
if [[ -n "$c" && "$c" != "000" ]]; then ok "root sort en direct (HTTP $c)"; else echo "    (root ne joint pas $INTERDIT : réseau du VPS ?)"; fi

if [[ $echecs -eq 0 ]]; then echo "==> Filtrage sortant conforme"; exit 0; fi
echo "==> $echecs problème(s)"; exit 1
