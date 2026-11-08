#!/usr/bin/env bash
# Firmin Assistant — applique config.yaml et l'état des capacités aux timers systemd (root, idempotent).
#
# Lancé par :
#   - install/install.sh (partie « timers », extraite ici au lot 7a) ;
#   - homeos-appliquer-config.path, dès que data/config.yaml ou data/capacites/etat.json change
#     (administration par la conversation : le bot et les outils n'ont jamais besoin de sudo pour ça).
# Usage : sudo homeos-appliquer-config [/srv/home-os]      (copie root de ce script, installée par install.sh)
#
# Ce que fait le script :
#   1. vérifie data/config.yaml (python -m tools.config_edition valider, sous homeos) : invalide → rien ne change ;
#      puis migrations idempotentes des données (python -m tools.fichiers migrer : catégorie chien → animaux) ;
#   2. horaires des briefs (crons.brief_hebdo / brief_jour) → drop-ins homeos-brief-*.timer.d/override.conf ;
#   3. crons des capacités actives → homeos-cap-<nom>-<cron>.service/.timer (créés, mis à jour ou retirés) ;
#   4. capacité activée ou désactivée depuis la dernière application → ses services locaux déjà installés
#      (capabilities/<nom>/systemd/*) sont démarrés ou arrêtés ;
#   5. daemon-reload et redémarrage des seuls timers modifiés ; résultat dans data/administration/application.json ;
#   6. serveur d'outils persistant actif (lot 7) : `systemctl reload homeos-outils` (capacités rechargées à chaud) ;
#   7. filtrage sortant actif : liste blanche du proxy régénérée (install/reseau-autorise.txt + hôtes des capacités
#      chargées et sites approuvés par un propriétaire pour l'atelier, lot 7b), tinyproxy rechargé si elle change.
#
# Sécurité : le code du dépôt (modifiable par le groupe foyer) n'est jamais exécuté en root : Python tourne sous
# homeos, ses sorties sont revalidées ici (noms [a-z0-9_], OnCalendar au format attendu) avant d'écrire une unité.
# Variables pour les tests : HOMEOS_SYSTEMD_DIR, HOMEOS_SANS_SYSTEMCTL=1, HOMEOS_PYTHON, HOMEOS_ETAT.
set -euo pipefail

CIBLE="${1:-${HOMEOS_ROOT:-/srv/home-os}}"
DATA="${HOMEOS_DATA:-$CIBLE/data}"
SECRETS="${HOMEOS_SECRETS:-/etc/homeos}"
ETAT="${HOMEOS_ETAT:-/var/lib/homeos}"
FICHIER_PROXY=/etc/default/homeos-proxy
UNITES="${HOMEOS_SYSTEMD_DIR:-/etc/systemd/system}"
PY="${HOMEOS_PYTHON:-$CIBLE/.venv/bin/python}"
SANS_SYSTEMCTL="${HOMEOS_SANS_SYSTEMCTL:-0}"
MARQUEUR="$DATA/administration/application.json"
STATUTS="$ETAT/appliquer-config.statuts"

RE_NOM='^[a-z0-9_]+$'
# « HH:MM:00 », ou périodique « HH..HH:00/NN:00 » (crons « 06:00..21:00/10 » des capacités, tools.capacites.oncalendar).
RE_ONCAL='^([A-Z][a-z]{2}((,|\.\.)[A-Z][a-z]{2})* )?\*-\*-\* [0-2][0-9](\.\.[0-2][0-9])?:([0-5][0-9]|00/[0-9]{2}):00$'

info() { printf '    %s\n' "$*"; }

[[ -d "$CIBLE" ]] || { echo "dépôt introuvable : $CIBLE" >&2; exit 2; }
[[ -x "$PY" ]] || PY="$(command -v python3)"

systemctl_() { # systemctl, sauf en test
  [[ "$SANS_SYSTEMCTL" == "1" ]] && return 0
  systemctl "$@"
}

python_homeos() { # python -m … sous homeos (jamais en root), cwd = dépôt
  local env_py=(env HOMEOS_DATA="$DATA" HOMEOS_ETAT="$ETAT" HOMEOS_SECRETS="$SECRETS" PYTHONPATH="$CIBLE")
  if [[ $EUID -eq 0 ]] && id homeos >/dev/null 2>&1; then
    (cd "$CIBLE" && sudo -n -u homeos "${env_py[@]}" "$PY" "$@")
  else
    (cd "$CIBLE" && "${env_py[@]}" "$PY" "$@")
  fi
}

marquer() { # marquer <ok:true|false> <message>
  local dossier
  dossier="$(dirname "$MARQUEUR")"
  if [[ ! -d "$dossier" ]]; then
    if [[ $EUID -eq 0 ]] && id homeos >/dev/null 2>&1; then
      install -d -o homeos -g foyer -m 2770 "$dossier" 2>/dev/null || return 0
    else
      mkdir -p "$dossier" 2>/dev/null || return 0
    fi
  fi
  local message="${2//\\/\\\\}"; message="${message//\"/\\\"}"
  printf '{"quand": "%s", "ok": %s, "message": "%s"}\n' "$(date -Iseconds)" "$1" "$message" >"$MARQUEUR.tmp" \
    && mv -f "$MARQUEUR.tmp" "$MARQUEUR"
  if [[ $EUID -eq 0 ]] && id homeos >/dev/null 2>&1; then
    chown homeos:foyer "$MARQUEUR" 2>/dev/null || true
  fi
  chmod 664 "$MARQUEUR" 2>/dev/null || true
}

CHANGES=()   # timers (re)écrits
ecrire_si_change() { # ecrire_si_change <fichier> <unité à redémarrer|-> ; contenu sur stdin
  local fichier="$1" unite="$2" contenu
  contenu="$(cat)"
  if [[ -f "$fichier" ]] && [[ "$(cat "$fichier")" == "$contenu" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$fichier")"
  printf '%s\n' "$contenu" >"$fichier.tmp"
  chmod 644 "$fichier.tmp"
  mv -f "$fichier.tmp" "$fichier"
  [[ "$unite" != "-" ]] && CHANGES+=("$unite")
  return 0
}

# --- 1. configuration valide ? ------------------------------------------------------------
if ! erreur="$(python_homeos -m tools.config_edition valider 2>&1 >/dev/null)"; then
  info "config.yaml invalide, rien n'est appliqué : ${erreur##*$'\n'}"
  marquer false "config.yaml invalide : ${erreur##*$'\n'}"
  exit 1
fi

# --- 1 bis. migrations des données (idempotentes : rien à faire la plupart du temps) ----------------------
# Catégorie de rangement renommée (chien → animaux) : fichiers rangés, index et liens des fiches.
if migrations="$(python_homeos -m tools.fichiers migrer 2>/dev/null)"; then
  while IFS= read -r m; do [[ -z "$m" ]] || info "migration : $m"; done <<<"$migrations"
else
  info "migration des fichiers rangés en échec (relancée à la prochaine application)"
fi

# --- 2. briefs ------------------------------------------------------------------------------
while IFS=$'\t' read -r cle oncal; do
  [[ -z "$cle" ]] && continue
  if [[ ! "$cle" =~ $RE_NOM || ! "$oncal" =~ $RE_ONCAL ]]; then
    info "ligne ignorée (format inattendu) : $cle"
    continue
  fi
  unite="homeos-${cle//_/-}.timer"
  ecrire_si_change "$UNITES/$unite.d/override.conf" "$unite" <<EOF
# Généré par scripts/appliquer-config.sh depuis data/config.yaml (crons.$cle)
[Timer]
OnCalendar=
OnCalendar=$oncal
EOF
  info "$unite → $oncal"
done < <(python_homeos -m tools.config_edition timers-foyer)

# --- 3. crons des capacités -------------------------------------------------------------------
declare -A CAP_UNITES=()
sortie_timers="$(python_homeos -m tools.capacites timers)" || { marquer false "lecture des crons des capacités impossible"; exit 1; }
while IFS=$'\t' read -r nom cron oncal; do
  [[ -z "$nom" || -z "$cron" || -z "$oncal" ]] && continue
  if [[ ! "$nom" =~ $RE_NOM || ! "$cron" =~ $RE_NOM || ! "$oncal" =~ $RE_ONCAL ]]; then
    info "cron ignoré (format inattendu) : $nom $cron"
    continue
  fi
  unite="homeos-cap-${nom}-${cron}"
  ecrire_si_change "$UNITES/$unite.service" - <<EOF
[Unit]
Description=Firmin Assistant - capacité $nom, cron $cron
After=network-online.target
Wants=network-online.target

[Service]
UMask=0002
Type=oneshot
User=homeos
Group=foyer
WorkingDirectory=$CIBLE
EnvironmentFile=-$SECRETS/anthropic.env
EnvironmentFile=-$SECRETS/telegram.env
EnvironmentFile=-$FICHIER_PROXY
Environment=HOMEOS_DATA=$DATA
Environment=HOMEOS_ETAT=$ETAT
Environment=HOMEOS_SECRETS=$SECRETS
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
Environment=DISABLE_AUTOUPDATER=1
Environment=CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ExecStart=$CIBLE/scripts/capacite-cron.sh $nom $cron
# Durcissement (lot 5d) : profil strict des oneshots sans sudo.
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$DATA -$ETAT
ProtectHome=tmpfs
BindPaths=/home/homeos
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
MemoryMax=1500M
TasksMax=256
EOF
  ecrire_si_change "$UNITES/$unite.timer" "$unite.timer" <<EOF
[Unit]
Description=Firmin Assistant - timer capacité $nom, cron $cron

[Timer]
# Généré par scripts/appliquer-config.sh depuis capabilities/$nom/capability.yaml (surcharge : config.yaml capacites.$nom.crons.$cron)
OnCalendar=$oncal
Persistent=false
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
  CAP_UNITES["$unite"]=1
  info "$unite.timer → $oncal"
done <<<"$sortie_timers"

RETIRES=()
for f in "$UNITES"/homeos-cap-*.timer; do
  [[ -e "$f" ]] || continue
  unite="$(basename "$f" .timer)"
  if [[ -z "${CAP_UNITES[$unite]:-}" ]]; then
    systemctl_ disable --now "$unite.timer" >/dev/null 2>&1 || true
    rm -f "$UNITES/$unite.timer" "$UNITES/$unite.service"
    RETIRES+=("$unite")
    info "$unite retiré"
  fi
done

# --- 4. services locaux des capacités (activées / désactivées depuis la dernière fois) --------------
mapfile -t statuts < <(python_homeos -m tools.capacites list --json \
  | python3 -I -c 'import json, sys
for c in json.load(sys.stdin):
    print(c["nom"], c["statut"], sep="\t")' 2>/dev/null || true)
declare -A AVANT=()
if [[ -r "$STATUTS" ]]; then
  while IFS=$'\t' read -r nom statut; do [[ -n "$nom" ]] && AVANT["$nom"]="$statut"; done <"$STATUTS"
fi
nouveaux=""
for ligne in "${statuts[@]}"; do
  IFS=$'\t' read -r nom statut <<<"$ligne"
  [[ "$nom" =~ $RE_NOM && "$statut" =~ ^[a-z]+$ ]] || continue
  nouveaux+="$nom"$'\t'"$statut"$'\n'
  [[ -r "$STATUTS" ]] || continue            # première application : install.sh vient de tout installer
  [[ "${AVANT[$nom]:-}" == "$statut" ]] && continue
  for unite_src in "$CIBLE/capabilities/$nom"/systemd/homeos-*.service "$CIBLE/capabilities/$nom"/systemd/homeos-*.path; do
    [[ -e "$unite_src" ]] || continue
    u="$(basename "$unite_src")"
    [[ "$u" =~ ^homeos-[a-z0-9_-]+\.(service|path)$ ]] || continue
    if [[ ! -e "$UNITES/$u" ]]; then
      info "$u non installé : sudo bash $CIBLE/install/install.sh"
      continue
    fi
    if [[ "$statut" == "actif" || "$statut" == "essai" ]]; then
      systemctl_ enable --now "$u" >/dev/null 2>&1 || info "$u : démarrage impossible"
      info "$u démarré (capacité $nom : $statut)"
    else
      systemctl_ disable --now "$u" >/dev/null 2>&1 || true
      info "$u arrêté (capacité $nom : $statut)"
    fi
  done
done
if [[ -n "$nouveaux" ]]; then
  mkdir -p "$(dirname "$STATUTS")" 2>/dev/null && printf '%s' "$nouveaux" >"$STATUTS" 2>/dev/null || true
fi

# --- 5. prise en compte ---------------------------------------------------------------------------
if [[ ${#CHANGES[@]} -gt 0 || ${#RETIRES[@]} -gt 0 ]]; then
  systemctl_ daemon-reload
  for unite in "${CHANGES[@]}"; do
    systemctl_ enable "$unite" >/dev/null 2>&1 || true
    systemctl_ restart "$unite" >/dev/null 2>&1 || info "$unite : redémarrage impossible"
  done
fi
# Timers des capacités actives : activés (idempotent, utile après une installation).
for unite in "${!CAP_UNITES[@]}"; do
  systemctl_ enable --now "$unite.timer" >/dev/null 2>&1 || true
done
# --- 6. serveur d'outils persistant (lot 7) : capacités et jetons rechargés à chaud (SIGHUP) ----------------
# Une capacité activée, désactivée ou installée change ses outils : le serveur les recharge sans couper le service.
if [[ "$SANS_SYSTEMCTL" != "1" ]] && systemctl is-active --quiet homeos-outils.service 2>/dev/null; then
  systemctl reload homeos-outils.service >/dev/null 2>&1 && info "homeos-outils rechargé (capacités)" \
    || info "homeos-outils : rechargement impossible (systemctl reload homeos-outils)"
fi
# --- 7. liste blanche du proxy sortant (lot 7b : sites approuvés pour l'atelier et ses capacités) ------------
FILTRE_PROXY="${HOMEOS_FILTRE_PROXY:-/etc/tinyproxy/homeos-filtre}"
if [[ -f "$FILTRE_PROXY" && ( -f "$FICHIER_PROXY" || -n "${HOMEOS_FILTRE_PROXY:-}" ) ]]; then
  hotes="$( { grep -vE '^[[:space:]]*(#|$)' "$CIBLE/install/reseau-autorise.txt" 2>/dev/null | awk '{print $1}';
              python_homeos -m tools.capacites reseau 2>/dev/null || true; } \
            | tr 'A-Z' 'a-z' | grep -E '^(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' | sort -u)"
  nouveau="$(
    echo "# Généré par appliquer-config.sh depuis install/reseau-autorise.txt et les capacités"
    while read -r h; do
      [[ -z "$h" ]] && continue
      base="${h#\*.}"; motif="${base//./\\.}"
      echo "^${motif}\$"
      if [[ "$h" == \*.* ]]; then echo "\\.${motif}\$"; fi
    done <<<"$hotes"
  )"
  if [[ "$(grep -v '^#' "$FILTRE_PROXY")" != "$(grep -v '^#' <<<"$nouveau")" ]]; then
    printf '%s\n' "$nouveau" >"$FILTRE_PROXY.tmp" && mv -f "$FILTRE_PROXY.tmp" "$FILTRE_PROXY"
    systemctl_ reload tinyproxy.service >/dev/null 2>&1 || systemctl_ restart tinyproxy.service >/dev/null 2>&1 || true
    info "liste blanche du proxy mise à jour ($(wc -l <<<"$hotes") hôtes)"
  fi
fi

message="${#CHANGES[@]} timer(s) mis à jour, ${#RETIRES[@]} retiré(s), ${#CAP_UNITES[@]} timer(s) de capacités"
info "$message"
marquer true "$message"
