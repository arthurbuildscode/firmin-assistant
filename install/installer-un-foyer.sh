#!/usr/bin/env bash
# Firmin Assistant — installation guidée d'un nouveau foyer sur un Ubuntu 24.04 vierge (root via sudo).
#
# Pour qui : la personne qui installe, sans connaissance technique. Le script pose les questions (en français, une à
# la fois), puis fait tout le reste : système, assistant, comptes du foyer, Telegram, Tailscale et appli web,
# Vaultwarden (facultatif), sauvegardes, vérification. Il termine par la liste de ce qui reste « à faire par le
# foyer » (ce qui demande un téléphone ou un compte personnel : connexion Claude, Tailscale, copie hors site).
#
# Usage :
#   sudo bash install/installer-un-foyer.sh                       # questions puis installation
#   sudo bash install/installer-un-foyer.sh --reponses R.json     # sans question (voir docs/installation.md)
# Options :
#   --reponses <fichier.json>  réponses du questionnaire (install/configurer.py) : installation non interactive
#   --oui                      ne demande aucune confirmation (remplace config.yaml existant après sauvegarde)
#   --sans-verification        n'exécute pas scripts/verifier.sh à la fin
#   --admin=<compte>           transmis à install/install.sh (compte d'administration SSH, défaut : admin)
# Relançable : une configuration existante est sauvegardée (config.yaml.bak-<date>) avant d'être remplacée ; les
# secrets déjà posés ne sont remplacés que par une nouvelle valeur saisie.
#
# Ce script n'écrit dans install/install.sh rien d'autre que ce qu'il fait déjà : il prépare data/config.yaml et
# data/capacites/etat.json AVANT de l'appeler, puis pose les secrets saisis dans /etc/homeos APRÈS.
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CIBLE=/srv/home-os
DATA="$CIBLE/data"
SECRETS=/etc/homeos
REPONSES=""
OUI=0
VERIFIER=1
OPTIONS_INSTALL=()
A_FAIRE=()
for arg in "$@"; do
  case "$arg" in
    --reponses=*) REPONSES="${arg#--reponses=}" ;;
    --reponses) REPONSES="__suivant__" ;;
    --oui) OUI=1 ;;
    --sans-verification) VERIFIER=0 ;;
    --admin=*) OPTIONS_INSTALL+=("$arg") ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *)
      if [[ "$REPONSES" == "__suivant__" ]]; then REPONSES="$arg"
      else echo "option inconnue : $arg (voir --help)" >&2; exit 2; fi ;;
  esac
done
[[ "$REPONSES" == "__suivant__" ]] && { echo "--reponses : fichier attendu" >&2; exit 2; }

etape() { printf '\n\033[1;32m━━ %s ━━\033[0m\n' "$*"; }
info()  { printf '   %s\n' "$*"; }
die()   { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }
a_faire() { A_FAIRE+=("$*"); }
confirmer() { # confirmer "question" → 0 si oui
  [[ $OUI -eq 1 ]] && return 0
  local r; read -r -p " ? $1 (oui/non) [oui] : " r || return 1
  [[ -z "$r" || "$r" =~ ^[oOyY] ]]
}

[[ $EUID -eq 0 ]] || die "à lancer avec sudo : sudo bash install/installer-un-foyer.sh"
[[ -f "$KIT/install/install.sh" && -f "$KIT/install/configurer.py" ]] || die "kit incomplet ($KIT)"
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
  info "Attention : ce kit est prévu pour Ubuntu 24.04 ($(. /etc/os-release; echo "${PRETTY_NAME:-système inconnu}"))."
  confirmer "Continuer quand même ?" || exit 1
fi
[[ -z "$REPONSES" || -f "$REPONSES" ]] || die "fichier de réponses introuvable : $REPONSES"

cat <<EOF

  Firmin Assistant — installation d'un nouveau foyer ($(cat "$KIT/VERSION" 2>/dev/null || echo "version de développement"))
  Compter 30 minutes, dont 5 de questions. Les réponses restent sur ce serveur.
EOF

# --- 1. Questions ------------------------------------------------------------------------------------------------
etape "1/8 Questions"
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3 >/dev/null; }
REPONSES_DIR="$(mktemp -d /root/.firmin-installation.XXXXXX)"
chmod 700 "$REPONSES_DIR"
nettoyer() { [[ -f "$REPONSES_DIR/secrets.env" ]] && shred -u "$REPONSES_DIR/secrets.env" 2>/dev/null; rm -rf "$REPONSES_DIR"; }
trap nettoyer EXIT
if [[ -n "$REPONSES" ]]; then
  python3 "$KIT/install/configurer.py" --sortie "$REPONSES_DIR" --reponses "$REPONSES"
else
  python3 "$KIT/install/configurer.py" --sortie "$REPONSES_DIR"
fi
choix() { sed -n "s/^$1=//p" "$REPONSES_DIR/choix.env" | head -n1 | sed -e "s/^['\"]//" -e "s/['\"]\$//"; }
PERSONNES="$(choix PERSONNES)"

# --- 2. Configuration du foyer -----------------------------------------------------------------------------------
etape "2/8 Configuration du foyer"
install -d -m 2770 "$DATA" "$DATA/foyer" "$DATA/capacites"
if [[ -f "$DATA/config.yaml" ]] && ! cmp -s "$DATA/config.yaml" "$REPONSES_DIR/config.yaml"; then
  confirmer "Une configuration existe déjà ($DATA/config.yaml). La remplacer (une copie est gardée) ?" \
    || die "installation arrêtée : configuration existante conservée"
  cp -p "$DATA/config.yaml" "$DATA/config.yaml.bak-$(date +%Y%m%d-%H%M%S)"
fi
install -m 664 "$REPONSES_DIR/config.yaml" "$DATA/config.yaml"
info "configuration écrite : $DATA/config.yaml"
if [[ ! -s "$DATA/foyer/identite.md" ]]; then
  install -m 664 "$REPONSES_DIR/identite.md" "$DATA/foyer/identite.md"
  info "fiche « Identité du foyer » pré-remplie (complétée avec l'assistant à la première séance)"
fi
# Capacités non choisies : désactivées AVANT install.sh, qui n'installe alors pas leurs services. Transports : les
# réseaux locaux choisis vont dans data/capacites/transports/parametres.yaml (aucun réseau supposé sinon).
python3 - "$DATA/capacites" "$(choix KITCHEN_DAILY)" "$(choix TRANSPORTS)" "$(choix RESEAUX)" <<'PYEOF'
import json, os, sys
dossier, kd, transports, reseaux = sys.argv[1:5]
chemin = os.path.join(dossier, "etat.json")
try:
    etat = json.load(open(chemin, encoding="utf-8"))
except (OSError, ValueError):
    etat = {}
for nom, voulu in (("kitchen_daily", kd), ("transports", transports or "oui")):
    entree = etat.setdefault(nom, {})
    if voulu == "oui":
        if entree.get("statut") == "inactif":
            entree.pop("statut")
    else:
        entree["statut"] = "inactif"
tmp = chemin + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(etat, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
os.chmod(tmp, 0o664)
os.replace(tmp, chemin)
if reseaux.strip():
    d = os.path.join(dossier, "transports")
    os.makedirs(d, mode=0o2770, exist_ok=True)
    p = os.path.join(d, "parametres.yaml")
    lignes = [l for l in (open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else [])
              if not l.startswith("fournisseurs:")]
    lignes.append("fournisseurs: " + json.dumps(reseaux.strip(), ensure_ascii=False))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lignes) + "\n")
PYEOF
info "capacités : Kitchen Daily $(choix KITCHEN_DAILY), transports $(choix TRANSPORTS)$([[ -n "$(choix RESEAUX)" ]] && echo " (réseau local : $(choix RESEAUX))"), administration oui"

# --- 3. Tailscale (avant install.sh : l'appli web n'est publiée que si Tailscale est connecté) ---------------------
etape "3/8 Réseau privé Tailscale"
if [[ "$(choix TAILSCALE)" == oui ]]; then
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl ca-certificates gnupg ufw >/dev/null; }
  if [[ -n "$(choix TAILSCALE_CLE)" || $OUI -eq 0 ]]; then
    if TAILSCALE_CLE="$(choix TAILSCALE_CLE)" bash "$KIT/install/tailscale.sh" --hostname "$(choix HOTE_TAILSCALE)"; then
      info "Tailscale connecté"
    else
      a_faire "Connecter Tailscale : sudo bash $CIBLE/install/tailscale.sh --hostname $(choix HOTE_TAILSCALE), puis relancer sudo bash $CIBLE/install/install.sh (publie l'appli web)"
    fi
  else
    a_faire "Connecter Tailscale (lien à ouvrir sur un téléphone) : sudo bash $CIBLE/install/tailscale.sh --hostname $(choix HOTE_TAILSCALE), puis sudo bash $CIBLE/install/install.sh"
  fi
else
  info "non demandé : l'assistant reste joignable par Telegram (appli web et coffre désactivés)"
fi

# --- 4. Installation du système ----------------------------------------------------------------------------------
etape "4/8 Installation du système (10 à 20 minutes)"
# Les « étapes manuelles » affichées par install.sh sont remplacées ici par la liste finale « à faire par le foyer ».
bash "$KIT/install/install.sh" "${OPTIONS_INSTALL[@]}" | sed -u '/^Étapes manuelles restantes/,$d'

# --- 5. Secrets saisis -------------------------------------------------------------------------------------------
etape "5/8 Secrets (/etc/homeos)"
# Chaque valeur non vide va dans le fichier de son usage (droits réappliqués par migrer-secrets.sh) ; jamais affichée.
python3 - "$REPONSES_DIR/secrets.env" "$SECRETS" <<'PYEOF'
import os, re, shutil, sys
source, dossier = sys.argv[1:3]
def cible(cle):
    if cle.startswith("TELEGRAM_"):
        return "telegram.env"
    if cle.startswith("ANTHROPIC_"):
        return "anthropic.env"
    if cle.startswith("ICAL_"):
        return "ical.env"
    return None
poses = []
for ligne in open(source, encoding="utf-8"):
    ligne = ligne.strip()
    if not ligne or ligne.startswith("#") or "=" not in ligne:
        continue
    cle, valeur = ligne.split("=", 1)
    nom = cible(cle)
    if not valeur or not nom or not re.fullmatch(r"[A-Z][A-Z0-9_]*", cle):
        continue
    chemin = os.path.join(dossier, nom)
    lignes = open(chemin, encoding="utf-8").read().splitlines() if os.path.exists(chemin) else []
    lignes = [l for l in lignes if not re.match(rf"^(export )?{cle}=", l)] + [f"{cle}={valeur}"]
    tmp = chemin + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lignes) + "\n")
    if os.path.exists(chemin):
        st = os.stat(chemin)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, chemin)
    poses.append(cle)
print("   secrets posés : " + (", ".join(poses) if poses else "aucun"))
PYEOF
bash "$CIBLE/scripts/migrer-secrets.sh" >/dev/null
[[ "$(choix TELEGRAM)" == oui ]] || a_faire "Créer le bot Telegram (@BotFather → /newbot) et coller son token : sudo nano $SECRETS/telegram.env (TELEGRAM_BOT_TOKEN), puis sudo systemctl restart homeos-bot"
[[ "$(choix GROUPE)" == oui ]] || a_faire "Créer le groupe Telegram du foyer avec le bot, puis renseigner groupe_telegram_id dans $DATA/config.yaml"
[[ "$(choix CLE_API)" == oui ]] || a_faire "Clé API Anthropic (briefs automatiques, personnes en mode clé API) : sudo nano $SECRETS/anthropic.env"
if [[ "$(choix TELEGRAM)" == oui ]]; then
  systemctl restart homeos-bot.service || true
  sleep 5
  if systemctl is-active --quiet homeos-bot.service; then info "bot Telegram démarré"
  else a_faire "Le bot ne démarre pas (token à vérifier ?) : sudo journalctl -u homeos-bot -n 50"; fi
fi

# --- 6. Comptes Claude -------------------------------------------------------------------------------------------
etape "6/8 Comptes Claude"
for p in $PERSONNES; do
  u="${p%%:*}"; mode="${p#*:}"
  if [[ "$mode" == cle_api ]]; then
    info "$u : passe par la clé API du foyer (rien à connecter)"
    continue
  fi
  statut_claude="$(timeout 40 sudo -n -u "$u" -H claude auth status 2>&1 || true)"
  if grep -qE '"loggedIn"[[:space:]]*:[[:space:]]*true' <<<"$statut_claude" \
     || { ! grep -qiE 'not logged|non connect|logged out|no credentials|"loggedIn"' <<<"$statut_claude" \
          && grep -qiE 'logged in|authenticated' <<<"$statut_claude"; }; then
    info "$u : compte Claude déjà connecté"
  elif [[ $OUI -eq 0 && -t 0 ]] && confirmer "Connecter maintenant le compte Claude de « $u » (il faut son téléphone) ?"; then
    info "Ouvrez le lien affiché sur le téléphone de la personne, acceptez, puis collez le code ici."
    sudo -u "$u" -H claude auth login || a_faire "Connecter le compte Claude de $u : sudo -u $u -H claude auth login"
  else
    info "$u : compte Claude à connecter (avec son téléphone, voir la liste finale)"
    a_faire "Connecter le compte Claude de $u (avec SON téléphone) : sudo -u $u -H claude auth login"
  fi
done

# --- 7. Coffre et sauvegarde hors site ----------------------------------------------------------------------------
etape "7/8 Coffre et sauvegardes"
if [[ "$(choix VAULTWARDEN)" == oui ]]; then
  if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
    bash "$CIBLE/install/vaultwarden.sh" --tailscale || a_faire "Vaultwarden : relancer sudo bash $CIBLE/install/vaultwarden.sh --tailscale"
    a_faire "Créer les comptes Vaultwarden : sudo bash $CIBLE/install/vaultwarden.sh --tailscale --inscriptions, puis relancer sans --inscriptions (docs/coffre.md)"
  else
    a_faire "Vaultwarden (après Tailscale) : sudo bash $CIBLE/install/vaultwarden.sh --tailscale"
  fi
fi
info "sauvegarde chiffrée quotidienne sur le serveur : active (restic, 03:30)"
if [[ "$(choix HORS_SITE)" == oui ]]; then
  if [[ $OUI -eq 0 && -t 0 ]] && confirmer "Configurer maintenant la copie hors du serveur (Google Drive…, 5 minutes) ?"; then
    bash "$CIBLE/scripts/configurer-hors-site.sh" || a_faire "Copie hors site : sudo bash $CIBLE/scripts/configurer-hors-site.sh"
  else
    a_faire "Copie de la sauvegarde hors du serveur : sudo bash $CIBLE/scripts/configurer-hors-site.sh (docs/sauvegardes.md)"
  fi
fi
a_faire "Recopier RESTIC_PASSWORD ($SECRETS/restic.env) et COFFRE_CLE ($SECRETS/coffre.env) dans un gestionnaire de mots de passe : sans eux, pas de restauration"

# --- appli web : liens d'enrôlement -------------------------------------------------------------------------------
if [[ -f /etc/default/homeos-web-app ]]; then
  for p in $PERSONNES; do
    u="${p%%:*}"
    a_faire "Appli web, une fois par téléphone de $u : cd $CIBLE && sudo -u homeos HOMEOS_DATA=$DATA .venv/bin/python -m web.invitation $u --telegram"
  done
fi
[[ "$(choix KITCHEN_DAILY)" == oui ]] && a_faire "Kitchen Daily : connexion au site, une fois (README, section Kitchen Daily)"

# --- 8. Vérification ---------------------------------------------------------------------------------------------
etape "8/8 Vérification"
statut_verif="non lancée"
if [[ $VERIFIER -eq 1 ]]; then
  if bash "$CIBLE/scripts/verifier.sh" --sans-claude; then statut_verif="vert"; else statut_verif="ÉCHEC (détail ci-dessus)"; fi
fi
a_faire "Quand les comptes Claude sont connectés : sudo bash $CIBLE/scripts/verifier.sh (test complet), puis écrire /start au bot"

# --- Récapitulatif ---------------------------------------------------------------------------------------------
RECAP=/root/firmin-a-faire.txt
{
  echo "Firmin Assistant — installation du $(date '+%d/%m/%Y %H:%M') ($(cat "$CIBLE/VERSION" 2>/dev/null || echo dev))"
  echo "Assistant : $(choix NOM_ASSISTANT) ; vérification : $statut_verif"
  echo
  echo "À faire par le foyer :"
  i=1; for t in "${A_FAIRE[@]}"; do echo "  $i. $t"; i=$((i + 1)); done
  echo
  echo "Ensuite, dans Telegram : écrire /start au bot ; l'assistant propose la première séance (20 minutes)."
} >"$RECAP"
chmod 600 "$RECAP"
etape "Terminé"
cat "$RECAP"
echo
echo "(Cette liste est gardée dans $RECAP.)"
[[ "$statut_verif" == vert || $VERIFIER -eq 0 ]]
