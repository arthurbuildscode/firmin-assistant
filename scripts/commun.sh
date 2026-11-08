#!/usr/bin/env bash
# Fonctions communes aux scripts (sourcé, jamais exécuté seul).
#
# Secrets (lot 5d) : sous systemd, chaque unité reçoit ses secrets par EnvironmentFile= (/etc/homeos/*.env).
# Lancé à la main, un script les charge avec `charger_secrets telegram anthropic …` : chaque fichier
# /etc/homeos/<nom>.env lisible par l'utilisateur courant est chargé, sinon l'ancien data/secrets.env
# (VPS pas encore migré). Rien n'est jamais affiché.

HOMEOS_SECRETS="${HOMEOS_SECRETS:-/etc/homeos}"
HOMEOS_ETAT="${HOMEOS_ETAT:-/var/lib/homeos}"
export HOMEOS_SECRETS HOMEOS_ETAT

charger_secrets() { # charger_secrets <nom>... (telegram, anthropic, restic, ical, coffre)
  local nom f charge=0
  for nom in "$@"; do
    f="$HOMEOS_SECRETS/$nom.env"
    if [[ -r "$f" ]]; then
      set -a; # shellcheck disable=SC1090
      . "$f"; set +a
      charge=1
    fi
  done
  if [[ $charge -eq 0 && -r "${DATA:-/srv/home-os/data}/secrets.env" ]]; then
    set -a; # shellcheck disable=SC1091
    . "${DATA:-/srv/home-os/data}/secrets.env"; set +a
  fi
  return 0
}

# Proxy sortant filtrant (install.sh --filtrer-sortant) pour un lancement manuel.
charger_proxy() {
  if [[ -z "${HTTPS_PROXY:-}" && -r /etc/default/homeos-proxy ]]; then
    set -a; . /etc/default/homeos-proxy; set +a
  fi
  return 0
}
