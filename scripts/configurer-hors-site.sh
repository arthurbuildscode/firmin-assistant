#!/usr/bin/env bash
# Configure la sauvegarde hors site sur Google Drive (lot 5d, audit point 5) — root, interactif, idempotent.
# Procédure complète et pas à pas : docs/sauvegardes.md.
#
#   1. installe rclone (logiciel libre) si absent ;
#   2. lance `rclone config` sous homeos (configuration dans /var/lib/homeos/rclone/rclone.conf, 600 homeos)
#      si le remote « gdrive » n'existe pas encore — répondre comme indiqué dans docs/sauvegardes.md ;
#   3. génère un mot de passe restic distinct pour le dépôt distant (affiché UNE fois : à ranger dans les
#      gestionnaires de mots de passe des deux conjoints) et complète /etc/homeos/restic.env ;
#   4. initialise le dépôt rclone:gdrive:<dossier> (mêmes paramètres de découpage que le dépôt local) ;
#   5. lance une première sauvegarde + copie (systemctl start homeos-backup).
# Usage : sudo bash /srv/home-os/scripts/configurer-hors-site.sh [dossier-drive]   (défaut : homeos-sauvegardes)
set -euo pipefail

DOSSIER="${1:-homeos-sauvegardes}"
REMOTE=gdrive
ETAT=/var/lib/homeos
CONF="$ETAT/rclone/rclone.conf"
ENVF=/etc/homeos/restic.env
[[ $EUID -eq 0 ]] || { echo "à lancer en root (sudo)" >&2; exit 2; }
[[ -r "$ENVF" ]] || { echo "$ENVF absent : lancer d'abord install/install.sh" >&2; exit 2; }
command -v restic >/dev/null || { echo "restic absent : lancer d'abord install/install.sh" >&2; exit 2; }

etape() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
valeur() { sed -nE "s/^(export )?$1=[\"']?([^\"']*)[\"']?\$/\\2/p" "$ENVF" | tail -n1; }
definir() { # definir CLE valeur : remplace ou ajoute dans restic.env (600 root:root)
  if grep -qE "^(export )?$1=" "$ENVF"; then sed -i -E "s|^(export )?$1=.*|$1=$2|" "$ENVF"; else printf '%s=%s\n' "$1" "$2" >>"$ENVF"; fi
  chown root:root "$ENVF"; chmod 600 "$ENVF"
}
if [[ -r /etc/default/homeos-proxy ]]; then set -a; . /etc/default/homeos-proxy; set +a; fi
export RCLONE_CONFIG="$CONF"
# Les mots de passe passent par l'environnement (jamais en argument : visible dans ps).
comme_homeos() {
  sudo -n -u homeos \
    --preserve-env=RCLONE_CONFIG,RESTIC_REPOSITORY,RESTIC_PASSWORD,RESTIC_FROM_REPOSITORY,RESTIC_FROM_PASSWORD,HTTPS_PROXY,HTTP_PROXY,NO_PROXY \
    env HOME=/home/homeos "$@"
}

etape "1. rclone"
if ! command -v rclone >/dev/null; then apt-get install -y -qq rclone >/dev/null; fi
rclone version | head -n1

etape "2. Connexion à Google Drive (remote « $REMOTE »)"
install -d -o homeos -g homeos -m 700 "$ETAT/rclone"
if comme_homeos rclone listremotes 2>/dev/null | grep -qx "$REMOTE:"; then
  echo "remote $REMOTE déjà configuré"
else
  cat <<TXT
Réponses à donner (détail dans docs/sauvegardes.md) :
  n (nouveau remote) → nom : $REMOTE → type : drive → client_id / client_secret : Entrée (vides)
  → scope : drive.file (rclone ne voit que les fichiers qu'il crée) → service_account_file : Entrée
  → Edit advanced config : n → Use web browser / auto config : n
  → sur ton ordinateur : rclone authorize "drive" "…" (commande affichée), coller le jeton obtenu ici
  → Shared Drive : n → y (garder) → q (quitter)
TXT
  comme_homeos rclone config
fi
chown homeos:homeos "$CONF"; chmod 600 "$CONF"
comme_homeos rclone mkdir "$REMOTE:$DOSSIER"
comme_homeos rclone lsd "$REMOTE:" >/dev/null && echo "Google Drive joignable"

etape "3. Mot de passe du dépôt distant"
if [[ -z "$(valeur RESTIC_PASSWORD_DISTANT)" ]]; then
  mdp="$(openssl rand -base64 30 | tr -d '\n')"
  definir RESTIC_PASSWORD_DISTANT "$mdp"
  printf '\n\033[1;33mMot de passe du dépôt hors site (à copier MAINTENANT dans vos deux gestionnaires de mots de passe,\nil ne sera plus affiché ; sans lui la sauvegarde Google Drive est illisible) :\033[0m\n\n    %s\n\n' "$mdp"
  read -r -p "Appuie sur Entrée une fois le mot de passe rangé… " _
  unset mdp
else
  echo "RESTIC_PASSWORD_DISTANT déjà présent"
fi
definir RESTIC_REPOSITORY_DISTANT "rclone:$REMOTE:$DOSSIER"
definir RCLONE_CONFIG "$CONF"

etape "4. Dépôt restic distant"
local_repo="$(valeur RESTIC_REPOSITORY)"; local_repo="${local_repo:-/var/backups/homeos}"
RESTIC_FROM_PASSWORD="$(valeur RESTIC_PASSWORD)"
RESTIC_PASSWORD="$(valeur RESTIC_PASSWORD_DISTANT)"
export RESTIC_FROM_REPOSITORY="$local_repo" RESTIC_FROM_PASSWORD RESTIC_REPOSITORY="rclone:$REMOTE:$DOSSIER" RESTIC_PASSWORD
if comme_homeos restic cat config >/dev/null 2>&1; then
  echo "dépôt distant déjà initialisé"
else
  comme_homeos restic init --copy-chunker-params --from-repo "$local_repo" >/dev/null
  echo "dépôt distant initialisé"
fi
unset RESTIC_PASSWORD RESTIC_FROM_PASSWORD

etape "5. Première sauvegarde avec copie hors site"
systemctl start homeos-backup.service && journalctl -u homeos-backup -n 8 --no-pager
echo
echo "Terminé. Vérifier : /statut dans Telegram (« Copie hors site »), puis le test de restauration :"
echo "    sudo systemctl start homeos-restaurer-test && journalctl -u homeos-restaurer-test -n 5"
