#!/usr/bin/env bash
# Capacité transports — installation (root, idempotent). Appelé par install/install.sh (boucle sur
# capabilities/*/install.sh) ; peut être relancé seul :
#   sudo bash /srv/home-os/capabilities/transports/install.sh [/srv/home-os]
#
# 1. migration douce depuis l'ancienne capacité bus : trajets, grille, choix du réseau (data/capacites/transports/),
#    retrait des anciennes unités homeos-bus*.
# 2. service local du fournisseur Cartreize (homeos-transports-cartreize : node server.mjs, 127.0.0.1:8081,
#    User=homeos) seulement si le foyer utilise ce réseau ; sinon arrêté (rien n'est supposé sur le réseau du foyer).
#    data/capacites/transports/cartreize/gtfs/ (grille GTFS ≈ 25 Mo, jamais versionnée) + lien service/data.
# Les timers des crons (rafraichir_grilles, alertes_retard, veille_reservations) sont générés par le cadre des
# capacités à partir du manifeste.
set -euo pipefail

CIBLE="${1:-/srv/home-os}"
CAP="$CIBLE/capabilities/transports"
SERVICE="$CAP/fournisseurs/cartreize/service"
DATA="$CIBLE/data"
GTFS="$DATA/capacites/transports/cartreize/gtfs"
UNITE="homeos-transports-cartreize"

info() { printf '    [transports] %s\n' "$*"; }
die()  { printf '\033[1;31mERREUR [transports] : %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root"
[[ -f "$CAP/capability.yaml" ]] || die "$CAP/capability.yaml introuvable"

PY="$CIBLE/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
en_homeos() { (cd "$CIBLE" && sudo -n -u homeos env HOMEOS_DATA="$DATA" PYTHONPATH="$CIBLE" "$PY" "$@"); }

# --- 1. migration depuis la capacité bus ------------------------------------------------------------------------
install -d -o homeos -g foyer -m 2770 "$DATA/capacites" "$DATA/capacites/transports"
if [[ -d "$DATA/capacites/bus" ]]; then
  resume="$(en_homeos -m capabilities.transports.crons migrer --donnees "$DATA" 2>/dev/null || true)"
  [[ -n "$resume" && "$resume" != "null" ]] && info "reprise de la capacité bus : $resume"
fi
for ancienne in homeos-bus.service homeos-bus-recharger.path homeos-bus-recharger.service; do
  if [[ -e "/etc/systemd/system/$ancienne" ]]; then
    systemctl disable --now "$ancienne" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$ancienne"
    info "ancienne unité $ancienne retirée (remplacée par $UNITE)"
  fi
done
rm -rf /etc/systemd/system/homeos-bus.service.d
if [[ -d "$CIBLE/capabilities/bus" && ! -e "$CIBLE/capabilities/bus/capability.yaml" ]]; then
  rm -rf "$CIBLE/capabilities/bus"   # restes non versionnés (lien service/data) de l'ancienne capacité
fi

# --- 2. statut de la capacité et usage du réseau Cartreize ---------------------------------------------------------
statut=""
if [[ -x "$CIBLE/.venv/bin/python" ]]; then
  statut="$(en_homeos -m tools.capacites statut transports 2>/dev/null \
    | sed -nE 's/^[[:space:]]*"statut":[[:space:]]*"([a-z]+)".*/\1/p' | head -n1 || true)"
fi
[[ -n "$statut" ]] || statut="$(sed -nE 's/^statut:[[:space:]]*([a-z]+).*/\1/p' "$CAP/capability.yaml" | head -n1)"
arreter() {
  systemctl disable --now "$UNITE.service" "$UNITE-recharger.path" >/dev/null 2>&1 || true
  info "$1 : service Cartreize arrêté (données conservées)"
}
if [[ "$statut" == "inactif" || "$statut" == "invalide" ]]; then
  arreter "capacité inactive"
  exit 0
fi
if ! en_homeos -m capabilities.transports.crons utilise cartreize --donnees "$DATA" >/dev/null 2>&1; then
  arreter "réseau Cartreize non utilisé par le foyer"
  exit 0
fi

NODE="$(command -v node || true)"
[[ -n "$NODE" ]] || die "node introuvable (install/install.sh installe Node.js 22)"
node_major="$("$NODE" -v | sed -E 's/^v([0-9]+).*/\1/')"
[[ "$node_major" -ge 20 ]] || die "Node.js >= 20 requis (trouvé $("$NODE" -v))"

# --- 3. grille Cartreize -----------------------------------------------------------------------------------------------
install -d -o homeos -g foyer -m 2770 "$DATA/capacites/transports/cartreize" "$GTFS"
[[ -f "$GTFS/.gitignore" ]] || printf '# Grille GTFS Cartreize (≈ 25 Mo, régénérée chaque semaine) : jamais versionnée\n*\n' >"$GTFS/.gitignore"
# service/data doit être un lien vers la grille runtime (une copie réelle est récupérée puis remplacée par le lien)
if [[ -d "$SERVICE/data" && ! -L "$SERVICE/data" ]]; then
  for f in reseau.json horaires.json; do
    if [[ -f "$SERVICE/data/$f" && ! -f "$GTFS/$f" ]]; then mv "$SERVICE/data/$f" "$GTFS/$f"; fi
  done
  rm -rf "$SERVICE/data"
fi
ln -sfn "$GTFS" "$SERVICE/data"
chown -h homeos:foyer "$SERVICE/data"
if [[ ! -s "$GTFS/horaires.json" || ! -s "$GTFS/reseau.json" ]]; then
  info "grille GTFS Cartreize absente : téléchargement et préparation (une à deux minutes)"
  if en_homeos -m capabilities.transports.crons rafraichir_grilles cartreize --forcer --donnees "$DATA"; then
    info "grille GTFS prête"
  else
    info "grille GTFS impossible à générer (réseau ?) : relancer ce script plus tard"
  fi
fi
chown -R homeos:foyer "$DATA/capacites/transports"
find "$DATA/capacites/transports" -type d -exec chmod 2770 {} +

# --- 4. systemd ----------------------------------------------------------------------------------------------------------
install -m 644 "$CAP/systemd/$UNITE.service" "$CAP/systemd/$UNITE-recharger.service" \
  "$CAP/systemd/$UNITE-recharger.path" /etc/systemd/system/
install -d "/etc/systemd/system/$UNITE.service.d"
if [[ "$NODE" != "/usr/bin/node" ]]; then
  printf '# Généré par capabilities/transports/install.sh\n[Service]\nExecStart=\nExecStart=%s server.mjs\n' "$NODE" \
    >"/etc/systemd/system/$UNITE.service.d/node.conf"
else
  rm -f "/etc/systemd/system/$UNITE.service.d/node.conf"
fi
# Proxy sortant filtrant (lot 5d) : fetch() de Node n'utilise HTTPS_PROXY qu'avec NODE_USE_ENV_PROXY=1 (Node 22.21+).
cat >"/etc/systemd/system/$UNITE.service.d/proxy.conf" <<'EOF'
# Généré par capabilities/transports/install.sh
[Service]
EnvironmentFile=-/etc/default/homeos-proxy
Environment=NODE_USE_ENV_PROXY=1
EOF
systemctl daemon-reload
systemctl enable --now "$UNITE-recharger.path" >/dev/null
systemctl enable "$UNITE.service" >/dev/null
if [[ -s "$GTFS/horaires.json" ]]; then
  systemctl restart "$UNITE.service"
  for _ in $(seq 1 30); do
    curl -fsS -m 2 http://127.0.0.1:8081/sante >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -fsS -m 2 http://127.0.0.1:8081/sante >/dev/null 2>&1; then
    info "$UNITE démarré (http://127.0.0.1:8081, local uniquement)"
  else
    info "$UNITE ne répond pas : journalctl -u $UNITE -n 50"
  fi
else
  info "$UNITE activé mais non démarré (pas de grille GTFS)"
fi
