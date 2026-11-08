#!/usr/bin/env bash
# Healthcheck (toutes les 10 min) : bot actif, disque, sessions Claude, dernière sauvegarde (locale et hors
# site), dernier test de restauration, unités en échec, redémarrage en attente, erreurs du journal, proxy
# sortant, serveur d'outils et navigateur isolé (lot 7, mode http), smoke tests des capacités.
# Envoie un message Telegram uniquement en cas de problème, max 1 alerte par problème par 6 h.
# Secrets : seul TELEGRAM_BOT_TOKEN (EnvironmentFile=/etc/homeos/telegram.env) ; aucun accès à restic.
set -euo pipefail

ROOT="${HOMEOS_ROOT:-/srv/home-os}"
DATA="${HOMEOS_DATA:-$ROOT/data}"
PY="$ROOT/.venv/bin/python"
FICHIER_ALERTES="$DATA/.healthcheck-last-alert"
SEUIL_DISQUE=85          # %
ANTI_SPAM=21600          # 6 h en secondes
AGE_MAX_BACKUP=172800    # 48 h en secondes (sauvegarde locale et copie hors site)
AGE_MAX_RESTAURATION=3456000   # 40 jours : le test est mensuel
AGE_MAX_REBOOT=172800    # redémarrage demandé depuis plus de 48 h (le redémarrage automatique est à 04:40)
SEUIL_ERREURS_JOURNAL=20 # lignes de priorité err ou pire en 10 min

log() { echo "[homeos-healthcheck] $*" >&2; }

# Secrets : fournis par systemd (EnvironmentFile=/etc/homeos/telegram.env) ; en manuel, on les charge.
# shellcheck source=scripts/commun.sh
. "$ROOT/scripts/commun.sh"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then charger_secrets telegram; charger_proxy; fi
maintenant=$(date +%s)
age_fichier() { # âge en secondes d'un fichier, vide s'il n'existe pas
  local m
  m=$(stat -c %Y "$1" 2>/dev/null) || return 0
  echo $(( maintenant - m ))
}

declare -A PROBLEMES=()   # cle -> message

# --- 1. Bot systemd -----------------------------------------------------------
if ! systemctl is-active --quiet homeos-bot.service; then
  PROBLEMES[bot]="Le bot (homeos-bot) n'est pas actif : $(systemctl is-active homeos-bot.service || true)"
fi

# --- 2. Disque ----------------------------------------------------------------
usage=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [[ "${usage:-0}" -gt "$SEUIL_DISQUE" ]]; then
  PROBLEMES[disque]="Disque plein à ${usage} % (seuil ${SEUIL_DISQUE} %)"
fi

# --- 3. Sessions Claude -------------------------------------------------------
# Utilisateurs Linux lus dans config.yaml ; repli : membres du groupe foyer (hors homeos).
utilisateurs=$("$PY" - "$DATA/config.yaml" 2>/dev/null <<'PYEOF' || true
import sys
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    print(" ".join(p["linux_user"] for p in cfg.get("personnes", []) if p.get("linux_user") and p.get("auth", "abonnement") != "cle_api"))
except Exception:
    pass
PYEOF
)
if [[ -z "$utilisateurs" ]]; then
  utilisateurs="$(getent group foyer | cut -d: -f4 | tr ',' '\n' | grep -vx homeos | tr '\n' ' ' || true)"
fi

claude_connecte() { # $1 = utilisateur linux ; retour 0 si session OK
  local sortie
  sortie=$(timeout 40 sudo -n -H -u "$1" claude auth status 2>&1) || return 1
  # Sortie JSON éventuelle : {"loggedIn": true, ...}
  if grep -qE '"loggedIn"[[:space:]]*:[[:space:]]*true' <<<"$sortie"; then return 0; fi
  if grep -qE '"loggedIn"[[:space:]]*:[[:space:]]*false' <<<"$sortie"; then return 1; fi
  # Sortie texte, tolérante.
  if grep -qiE 'not logged|non connect|logged out|no credentials' <<<"$sortie"; then return 1; fi
  if grep -qiE 'logged in|authenticated|connect' <<<"$sortie"; then return 0; fi
  return 1
}

for u in $utilisateurs; do
  if ! id "$u" >/dev/null 2>&1; then continue; fi
  if ! claude_connecte "$u"; then
    PROBLEMES["claude_$u"]="Session Claude de $u déconnectée → sudo -u $u -H claude auth login"
  fi
done

# --- 4. Sauvegardes et restauration -----------------------------------------
# Lu dans les marqueurs écrits par backup.sh et restaurer-test.sh (le healthcheck n'a pas les mots de passe).
age=$(age_fichier "$DATA/.derniere-sauvegarde")
if [[ -z "$age" ]]; then
  PROBLEMES[backup]="Aucune sauvegarde restic enregistrée (data/.derniere-sauvegarde absent)"
elif (( age > AGE_MAX_BACKUP )); then
  PROBLEMES[backup]="Dernière sauvegarde trop ancienne : $(date -d "@$(( maintenant - age ))" '+%d/%m %H:%M')"
fi
age=$(age_fichier "$DATA/.derniere-copie-distante")
if [[ -n "$age" ]] && (( age > AGE_MAX_BACKUP )); then
  PROBLEMES[hors_site]="Copie hors site trop ancienne : $(date -d "@$(( maintenant - age ))" '+%d/%m %H:%M') (journalctl -u homeos-backup)"
fi
if [[ -r "$DATA/.derniere-restauration" ]]; then
  restauration=$("$PY" -c '
import json, sys
try:
    i = json.load(open(sys.argv[1], encoding="utf-8"))
    print("ok" if i.get("ok") else "ko", str(i.get("message") or "")[:150].replace("\n", " "), sep="\t")
except Exception:
    print("ko\tfichier illisible")
' "$DATA/.derniere-restauration" 2>/dev/null || printf 'ko\tfichier illisible')
  if [[ "${restauration%%$'\t'*}" != "ok" ]]; then
    PROBLEMES[restauration]="Test de restauration en échec : ${restauration#*$'\t'}"
  else
    age=$(age_fichier "$DATA/.derniere-restauration")
    if [[ -n "$age" ]] && (( age > AGE_MAX_RESTAURATION )); then
      PROBLEMES[restauration]="Aucun test de restauration depuis plus de 40 jours (homeos-restaurer-test.timer)"
    fi
  fi
fi

# --- 4 bis. Système : unités en échec, redémarrage en attente, erreurs du journal, proxy --------
echecs_unites=$(systemctl list-units --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ' || true)
if [[ -n "${echecs_unites// /}" ]]; then
  PROBLEMES[unites]="Services en échec : ${echecs_unites% } (systemctl --failed)"
fi
age=$(age_fichier /var/run/reboot-required)
if [[ -n "$age" ]] && (( age > AGE_MAX_REBOOT )); then
  PROBLEMES[reboot]="Redémarrage demandé depuis plus de 48 h (/var/run/reboot-required) : vérifier unattended-upgrades"
fi
# Lecture du journal système : homeos est dans le groupe systemd-journal (install.sh).
nb_erreurs=$(journalctl -p err --since -10min -q --no-pager 2>/dev/null | wc -l || echo 0)
if (( nb_erreurs > SEUIL_ERREURS_JOURNAL )); then
  PROBLEMES[journal]="$nb_erreurs erreurs dans le journal en 10 min (journalctl -p err --since -10min)"
fi
if [[ -r /etc/default/homeos-proxy ]]; then
  if ! systemctl is-active --quiet tinyproxy.service || ! (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null; then
    PROBLEMES[proxy]="Proxy sortant (tinyproxy, 127.0.0.1:3128) arrêté : plus aucune sortie réseau pour le bot"
  fi
fi

# --- 4 ter. Serveur d'outils persistant et navigateur isolé (lot 7, mode http) ----------------
# Service vivant, 401 sans jeton, puis initialize + tools/list avec le jeton homeos (python -m tools.serveur_http sonde).
if grep -qsx 'HOMEOS_OUTILS=http' "${HOMEOS_FICHIER_OUTILS:-/etc/default/homeos-outils}"; then
  if ! systemctl is-active --quiet homeos-outils.service; then
    PROBLEMES[outils]="Serveur d'outils (homeos-outils) arrêté : l'assistant n'a plus d'outils (journalctl -u homeos-outils)"
  elif ! sonde=$(cd "$ROOT" && timeout 60 "$PY" -m tools.serveur_http sonde 2>/dev/null) || ! grep -q '"ok": true' <<<"$sonde"; then
    PROBLEMES[outils]="Serveur d'outils (homeos-outils) ne répond pas correctement : ${sonde:0:150}"
  fi
  if [[ -e /etc/homeos/navigateur.env ]] && ! systemctl is-active --quiet homeos-web.service; then
    PROBLEMES[navigateur]="Navigateur isolé (homeos-web) arrêté : Kitchen Daily indisponible (journalctl -u homeos-web)"
  fi
fi

# --- 4 quater. Appli web (lot 4a), si elle est installée (Tailscale connecté) ------------------
if [[ -f /etc/default/homeos-web-app ]] && systemctl is-enabled --quiet homeos-web-app.service 2>/dev/null; then
  port_appli="$(sed -n 's/^HOMEOS_WEB_PORT=\([0-9]\{1,5\}\)$/\1/p' /etc/default/homeos-web-app)"
  if ! systemctl is-active --quiet homeos-web-app.service; then
    PROBLEMES[appli]="Appli web (homeos-web-app) arrêtée (journalctl -u homeos-web-app) ; Telegram reste disponible"
  elif ! curl -fsS --max-time 10 "http://127.0.0.1:${port_appli:-8095}/api/sante" 2>/dev/null | grep -q '"ok":true'; then
    PROBLEMES[appli]="Appli web (homeos-web-app) ne répond pas sur 127.0.0.1:${port_appli:-8095}"
  fi
fi

# --- 5. Capacités : smoke tests ----------------------------------------------
# Une ligne par capacité chargée : nom<TAB>ok(1|0)<TAB>titre<TAB>message. Résultat réutilisé pendant 55 min
# (certains smoke tests lancent un navigateur) ; une alerte par capacité en échec (même anti-spam 6 h).
SMOKE_CACHE_MIN=55
if [[ -x "$PY" ]]; then
  while IFS=$'\t' read -r nom ok titre message; do
    [[ -z "$nom" ]] && continue
    if [[ "$ok" != "1" ]]; then
      PROBLEMES["capacite_$nom"]="Capacité ${titre:-$nom} : ${message:-échec du smoke test}"
    fi
  done < <(cd "$ROOT" && HOMEOS_DATA="$DATA" timeout 600 "$PY" -m tools.capacites smoke --tsv --cache "$SMOKE_CACHE_MIN" 2>/dev/null || true)
fi

# --- Quarantaines du filtre de données sensibles (lot 5c) ----------------------
# Valeurs détectées dans un message et gardées 10 min dans le coffre (quarantaine/<id>) : effacées une fois
# expirées (le cœur purge aussi à chaque message). Aucune alerte : un échec ici n'empêche rien.
(cd "$ROOT" && HOMEOS_DATA="$DATA" timeout 60 "$PY" -m tools.coffre purger >/dev/null 2>&1) || log "purge des quarantaines impossible"

# --- Anti-spam : 1 alerte par problème par 6 h --------------------------------
touch "$FICHIER_ALERTES" 2>/dev/null || true
declare -A DERNIERE=()
if [[ -r "$FICHIER_ALERTES" ]]; then
  while IFS=$'\t' read -r cle ts; do
    [[ -n "$cle" && -n "$ts" ]] && DERNIERE["$cle"]="$ts"
  done <"$FICHIER_ALERTES"
fi

a_envoyer=()
nouveau_contenu=""
for cle in "${!PROBLEMES[@]}"; do
  precedente="${DERNIERE[$cle]:-0}"
  if (( maintenant - precedente >= ANTI_SPAM )); then
    a_envoyer+=("${PROBLEMES[$cle]}")
    nouveau_contenu+="${cle}"$'\t'"${maintenant}"$'\n'
  else
    nouveau_contenu+="${cle}"$'\t'"${precedente}"$'\n'
  fi
done
# Les problèmes résolus disparaissent du fichier : une rechute alertera aussitôt.
printf '%s' "$nouveau_contenu" >"$FICHIER_ALERTES" 2>/dev/null || true

if (( ${#PROBLEMES[@]} == 0 )); then
  log "OK"
  exit 0
fi

log "problèmes : ${!PROBLEMES[*]}"
if (( ${#a_envoyer[@]} > 0 )); then
  message="⚠️ Firmin Assistant — healthcheck $(date '+%d/%m %H:%M')"$'\n'
  for m in "${a_envoyer[@]}"; do message+="• $m"$'\n'; done
  if ! printf '%s' "$message" | "$PY" "$ROOT/scripts/telegram_send.py"; then
    log "envoi Telegram impossible"
  fi
else
  log "déjà alerté il y a moins de 6 h, rien envoyé"
fi
