#!/usr/bin/env bash
# Jetons du serveur d'outils MCP (lot 7) — root, idempotent. Appelé par install/install.sh ; relançable seul :
#     sudo bash /srv/home-os/scripts/jetons-outils.sh homeos <personne>...
#
# Pour chaque utilisateur (homeos + les linux_user de config.yaml) : un jeton aléatoire de 32 octets (64 caractères
# hexadécimaux), stocké
#   - côté serveur : /etc/homeos/outils-jetons.env (640 root:homeos), une ligne JETON_<utilisateur>=… ;
#   - côté personne : ~/.config/homeos/jeton (600 <personne>, dossier 700), lu par le wrapper homeos-claude.
#     (homeos n'a pas de copie : le bot et le healthcheck lisent directement outils-jetons.env.)
# Un jeton existant et cohérent des deux côtés est conservé ; absent, invalide ou différent → régénéré.
# Les utilisateurs qui ne sont plus listés perdent leur jeton (révocation). Après un changement :
#     sudo systemctl reload homeos-outils     (SIGHUP : le serveur relit les jetons sans couper le service)
#
# Variables pour les tests : HOMEOS_SECRETS, HOMEOS_MAISONS (racine des dossiers personnels : <racine>/<u>),
# HOMEOS_SANS_CHOWN=1 (pas de chown, utilisable sans root).
# Sortie : une ligne par utilisateur ; dernière ligne « changements=<n> ».
set -euo pipefail

SECRETS="${HOMEOS_SECRETS:-/etc/homeos}"
FICHIER="$SECRETS/outils-jetons.env"
SANS_CHOWN="${HOMEOS_SANS_CHOWN:-0}"
RE_UTILISATEUR='^[a-z_][a-z0-9_-]{0,31}$'
RE_JETON='^[0-9a-f]{64}$'

info() { printf '    [jetons] %s\n' "$*"; }
die()  { printf 'ERREUR [jetons] : %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 || "$SANS_CHOWN" == "1" ]] || die "à lancer en root"
[[ $# -gt 0 ]] || die "usage : jetons-outils.sh homeos <personne>..."
[[ -d "$SECRETS" ]] || die "$SECRETS absent (lancer install/install.sh)"

nouveau_jeton() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; echo; fi
}

maison_de() { # dossier personnel d'un utilisateur
  if [[ -n "${HOMEOS_MAISONS:-}" ]]; then printf '%s/%s\n' "$HOMEOS_MAISONS" "$1"
  else getent passwd "$1" | cut -d: -f6; fi
}

proprietaire() { # proprietaire <utilisateur> <chemin>...
  local u="$1"; shift
  [[ "$SANS_CHOWN" == "1" ]] && return 0
  chown "$u:$(id -gn "$u")" "$@"
}

# Jetons actuels (côté serveur).
declare -A ACTUELS=()
if [[ -r "$FICHIER" ]]; then
  while IFS= read -r ligne || [[ -n "$ligne" ]]; do
    if [[ "$ligne" =~ ^JETON_([a-z_][a-z0-9_-]{0,31})=([0-9a-f]{64})$ ]]; then
      ACTUELS["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
    fi
  done <"$FICHIER"
fi

changements=0
declare -A FINAUX=()
ordre=()
for u in "$@"; do
  [[ "$u" =~ $RE_UTILISATEUR ]] || die "nom d'utilisateur invalide : $u"
  [[ -n "${FINAUX[$u]:-}" ]] && continue
  ordre+=("$u")
  jeton="${ACTUELS[$u]:-}"
  copie=""
  if [[ "$u" != homeos ]]; then
    maison="$(maison_de "$u")"
    [[ -n "$maison" && -d "$maison" ]] || die "dossier personnel de $u introuvable"
    perso="$maison/.config/homeos/jeton"
    if [[ -f "$perso" && ! -L "$perso" ]]; then copie="$(tr -d '[:space:]' <"$perso")"; fi
  fi
  if [[ "$jeton" =~ $RE_JETON ]] && { [[ "$u" == homeos ]] || [[ "$copie" == "$jeton" ]]; }; then
    info "$u : jeton conservé"
  else
    jeton="$(nouveau_jeton)"
    [[ "$jeton" =~ $RE_JETON ]] || die "génération du jeton impossible"
    changements=$((changements + 1))
    info "$u : jeton généré"
  fi
  FINAUX["$u"]="$jeton"
  if [[ "$u" != homeos ]]; then
    # ~/.config (créé au besoin) puis ~/.config/homeos (700) et le jeton (600), propriété de la personne.
    if [[ ! -d "$maison/.config" ]]; then
      install -d -m 700 "$maison/.config"; proprietaire "$u" "$maison/.config"
    fi
    [[ -L "$maison/.config/homeos" ]] && rm -f "$maison/.config/homeos"
    install -d -m 700 "$maison/.config/homeos"; proprietaire "$u" "$maison/.config/homeos"
    if [[ "$copie" != "$jeton" || "$(stat -c %a "$perso" 2>/dev/null)" != 600 ]]; then
      tmp="$(mktemp "$maison/.config/homeos/.jeton.XXXXXX")"
      printf '%s\n' "$jeton" >"$tmp"
      chmod 600 "$tmp"; proprietaire "$u" "$tmp"
      mv -f "$tmp" "$perso"
    fi
    proprietaire "$u" "$perso"; chmod 600 "$perso"
  fi
done

for u in "${!ACTUELS[@]}"; do
  if [[ -z "${FINAUX[$u]:-}" ]]; then
    info "$u : jeton révoqué (plus dans la liste)"
    changements=$((changements + 1))
  fi
done

# Fichier serveur réécrit seulement s'il change (atomique, 640 root:homeos).
contenu="# Firmin Assistant — jetons du serveur d'outils MCP (lot 7). Généré par scripts/jetons-outils.sh :
# ne pas modifier à la main ; après un changement : systemctl reload homeos-outils"
for u in "${ordre[@]}"; do contenu+=$'\n'"JETON_$u=${FINAUX[$u]}"; done
if [[ ! -f "$FICHIER" || "$(cat "$FICHIER")" != "$contenu" ]]; then
  tmp="$(mktemp "$SECRETS/.outils-jetons.XXXXXX")"
  printf '%s\n' "$contenu" >"$tmp"
  chmod 640 "$tmp"
  if [[ "$SANS_CHOWN" != "1" ]]; then chown root:homeos "$tmp"; fi
  mv -f "$tmp" "$FICHIER"
fi
chmod 640 "$FICHIER"
if [[ "$SANS_CHOWN" != "1" ]]; then chown root:homeos "$FICHIER"; fi
echo "changements=$changements"
