#!/usr/bin/env bash
# Migration du lot 5d (root, idempotent) : sort les secrets et l'état sensible des dossiers lisibles par
# les sessions Claude. Appelé par install/install.sh ; relançable seul :
#     sudo bash /srv/home-os/scripts/migrer-secrets.sh
#
# 1. data/secrets.env est découpé par usage dans /etc/homeos (750 root:foyer) :
#      telegram.env  TELEGRAM_*                       640 root:homeos  (bot, briefs, alertes)
#      anthropic.env ANTHROPIC_*                      640 root:homeos  (crons, personnes en clé API)
#      restic.env    RESTIC_*, RCLONE_*               600 root:root    (lu par systemd seulement)
#      ical.env      ICAL_* des agendas communs       640 root:homeos  (serveur d'outils, lot 7 ; stdio : root:foyer)
#      ical-<u>.env  ICAL_* d'un agenda `prive: <u>`  640 root:homeos  (serveur d'outils ; stdio : root:<u>)
#      outils-jetons.env, navigateur.env              640 root:homeos  (lot 7 : jetons, navigateur distant)
#      push.env      VAPID_* (Web Push, lot 4b)       640 root:homeos  (appli web)
#      coffre.env    COFFRE_CLE, VAULTWARDEN_*        640 root:homeos  (coffre, sous homeos)
#      autres.env    toute autre variable             600 root:root    (à trier à la main)
#    Une valeur déjà présente (non vide) dans /etc/homeos n'est jamais écrasée. L'ancien fichier devient
#    data/secrets.env.migre (600 root:root, exclu des sauvegardes et du git de data/).
# 2. L'état sensible part dans /var/lib/homeos (hors des --add-dir) : data/.kd → kd/, data/coffre.json et
#    data/.coffre.lock, data/sessions.json (identifiants de session vidés : le dossier de travail de Claude
#    change, les anciennes conversations ne peuvent plus être reprises).
# 3. Droits selon le mode des outils (/etc/default/homeos-outils, lot 7) :
#      http  (serveur d'outils persistant sous homeos) : plus aucun secret ni état lisible par les personnes —
#            ical*.env 640 root:homeos, /var/lib/homeos 700 homeos:homeos (kd/ compris) ;
#      stdio (repli, outils sous chaque personne) : droits du lot 5d (ical.env et kd/ via le groupe foyer).
#    Les dossiers de travail vides des sessions Claude sont dans /var/lib/homeos-cwd (hors de l'état).
set -euo pipefail

DATA="${HOMEOS_DATA:-/srv/home-os/data}"
SECRETS="${HOMEOS_SECRETS:-/etc/homeos}"
ETAT="${HOMEOS_ETAT:-/var/lib/homeos}"
TRAVAIL="${ETAT%/}-cwd"
FICHIER_OUTILS="${HOMEOS_FICHIER_OUTILS:-/etc/default/homeos-outils}"
MODE_OUTILS=stdio
grep -qsx 'HOMEOS_OUTILS=http' "$FICHIER_OUTILS" && MODE_OUTILS=http
PY=/usr/bin/python3; [[ -x "$PY" ]] || PY="$(command -v python3)"

info() { printf '    [migration] %s\n' "$*"; }
die()  { printf 'ERREUR [migration] : %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "à lancer en root"
getent group foyer >/dev/null || die "groupe foyer absent (lancer install/install.sh)"
id homeos >/dev/null 2>&1 || die "utilisateur homeos absent (lancer install/install.sh)"

# --- dossiers ---------------------------------------------------------------------------------------------------
install -d -o root -g foyer -m 750 "$SECRETS"
install -d -o homeos -g foyer -m 750 "$ETAT"

# --- 1. secrets ---------------------------------------------------------------------------------------------------
if [[ -f "$DATA/secrets.env" ]]; then
  info "découpage de data/secrets.env vers $SECRETS"
  "$PY" - "$DATA/secrets.env" "$SECRETS" "$DATA/config.yaml" <<'PYEOF'
import os, re, sys
source, dossier, config = sys.argv[1:4]

def lire(chemin):
    vals, ordre = {}, []
    try:
        texte = open(chemin, encoding="utf-8").read()
    except FileNotFoundError:
        return vals, ordre
    for ligne in texte.splitlines():
        l = ligne.strip()
        if not l or l.startswith("#") or "=" not in l:
            continue
        l = l.removeprefix("export ")
        k, v = l.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            vals[k] = v
            ordre.append(k)
    return vals, ordre

prives = {}  # variable ICAL -> utilisateur
try:
    import yaml
    cfg = yaml.safe_load(open(config, encoding="utf-8")) or {}
    for a in cfg.get("agendas") or []:
        if isinstance(a, dict) and a.get("prive") and a.get("ical_url_env"):
            prives[str(a["ical_url_env"])] = re.sub(r"[^a-z0-9_-]", "", str(a["prive"]).lower())
except Exception as exc:  # config absente ou yaml indisponible : tous les agendas sont communs
    print(f"    [migration] agendas privés non lus ({exc.__class__.__name__})", file=sys.stderr)

def destination(k):
    if k.startswith("TELEGRAM_"):
        return "telegram"
    if k.startswith("ANTHROPIC_"):
        return "anthropic"
    if k.startswith(("RESTIC_", "RCLONE_")):
        return "restic"
    if k.startswith("ICAL_"):
        return f"ical-{prives[k]}" if prives.get(k) else "ical"
    if k == "COFFRE_CLE" or k.startswith("VAULTWARDEN_"):
        return "coffre"
    return "autres"

valeurs, ordre = lire(source)
par_fichier = {}
for k in ordre:
    par_fichier.setdefault(destination(k), []).append(k)
for nom, cles in par_fichier.items():
    chemin = os.path.join(dossier, f"{nom}.env")
    existants, _ = lire(chemin)
    ajouts = [k for k in dict.fromkeys(cles) if valeurs[k] and not existants.get(k)]
    if not ajouts:
        continue
    lignes = []
    if os.path.exists(chemin):
        texte = open(chemin, encoding="utf-8").read()
        # retire les lignes vides de valeur pour les clés complétées
        for l in texte.splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$", l)
            if m and m.group(1) in ajouts:
                continue
            lignes.append(l)
    else:
        lignes.append(f"# Firmin Assistant — {nom}.env (migré depuis data/secrets.env)")
    lignes += [f"{k}={valeurs[k]}" for k in ajouts]
    ancien = os.umask(0o077)
    try:
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lignes) + "\n")
        os.replace(tmp, chemin)
    finally:
        os.umask(ancien)
    print(f"    [migration] {nom}.env : {', '.join(ajouts)}", file=sys.stderr)
PYEOF
  cible="$DATA/secrets.env.migre"
  [[ -e "$cible" ]] && cible="$DATA/secrets.env.migre-$(date +%Y%m%d%H%M%S)"
  mv "$DATA/secrets.env" "$cible"
  chown root:root "$cible"; chmod 600 "$cible"
  info "ancien fichier conservé : $cible (600 root:root ; à supprimer quand tout fonctionne)"
fi

# Droits des fichiers de /etc/homeos (créés vides s'ils manquent, pour EnvironmentFile=).
for nom in telegram anthropic restic ical coffre; do
  [[ -e "$SECRETS/$nom.env" ]] || printf '# Firmin Assistant — %s.env\n' "$nom" >"$SECRETS/$nom.env"
done
for f in "$SECRETS"/*.env; do
  nom="$(basename "$f" .env)"
  case "$nom" in
    telegram|anthropic|coffre|outils-jetons|navigateur|push) chown root:homeos "$f"; chmod 640 "$f" ;;
    ical)
      if [[ "$MODE_OUTILS" == http ]]; then chown root:homeos "$f"; else chown root:foyer "$f"; fi
      chmod 640 "$f" ;;
    ical-*)
      u="${nom#ical-}"
      if [[ "$MODE_OUTILS" == http ]]; then chown root:homeos "$f"; chmod 640 "$f"
      elif id "$u" >/dev/null 2>&1; then chown "root:$(id -gn "$u")" "$f"; chmod 640 "$f"
      else chown root:root "$f"; chmod 600 "$f"; info "$nom.env : utilisateur $u inconnu, fichier réservé à root"; fi ;;
    *) chown root:root "$f"; chmod 600 "$f" ;;
  esac
done

# --- 2. état sensible ---------------------------------------------------------------------------------------------
# Dossiers de travail vides des sessions Claude (root:foyer 750, non inscriptibles), hors de l'état depuis le
# lot 7 : $TRAVAIL/<personne> (install.sh crée un sous-dossier par personne et un pour homeos). L'ancien
# $ETAT/cwd (vide) est retiré.
install -d -o root -g foyer -m 750 "$TRAVAIL"
if [[ -d "$ETAT/cwd" && ! -L "$ETAT/cwd" ]]; then
  find "$ETAT/cwd" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
  rmdir "$ETAT/cwd" 2>/dev/null && info "ancien $ETAT/cwd retiré (dossiers de travail : $TRAVAIL)" || true
fi
deplacer() { # deplacer <source> <destination>
  local src="$1" dst="$2"
  [[ -e "$src" || -L "$src" ]] || return 0
  if [[ -e "$dst" ]]; then
    if [[ -d "$dst" && -d "$src" ]] && [[ -z "$(find "$dst" -mindepth 1 ! -name .gitignore -print -quit 2>/dev/null)" ]]; then
      rm -rf "$dst"
    else
      info "$(basename "$src") : déjà présent dans $ETAT, ancien laissé en place ($src)"
      return 0
    fi
  fi
  mv "$src" "$dst"
  info "$(basename "$src") → $dst"
}
deplacer "$DATA/.kd" "$ETAT/kd"
deplacer "$DATA/coffre.json" "$ETAT/coffre.json"
deplacer "$DATA/.coffre.lock" "$ETAT/.coffre.lock"
if [[ -f "$DATA/sessions.json" && ! -e "$ETAT/sessions.json" ]]; then
  "$PY" - "$DATA/sessions.json" "$ETAT/sessions.json" <<'PYEOF'
import json, sys
src, dst = sys.argv[1:3]
try:
    d = json.load(open(src, encoding="utf-8"))
except Exception:
    d = {}
for v in (d.values() if isinstance(d, dict) else []):
    if isinstance(v, dict):
        v.pop("session_id", None)
json.dump(d if isinstance(d, dict) else {}, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
  rm -f "$DATA/sessions.json"
  info "sessions.json → $ETAT/sessions.json (conversations repartent de zéro)"
fi

# Droits de l'état. http : tout à homeos seul. stdio : Kitchen Daily lisible par le groupe foyer (outils MCP
# sous chaque personne) ; coffre et sessions réservés à homeos dans les deux cas.
if [[ "$MODE_OUTILS" == http ]]; then
  install -d -o homeos -g homeos -m 700 "$ETAT/kd" "$ETAT/kd/captures"
  [[ -f "$ETAT/kd/.gitignore" ]] || printf '# État de session Kitchen Daily et captures : jamais versionnés\n*\n' >"$ETAT/kd/.gitignore"
  chown -R homeos:homeos "$ETAT/kd"
  find "$ETAT/kd" -type d -exec chmod 700 {} +
  find "$ETAT/kd" -type f -exec chmod 600 {} +
else
  install -d -o homeos -g foyer -m 2770 "$ETAT/kd" "$ETAT/kd/captures"
  [[ -f "$ETAT/kd/.gitignore" ]] || printf '# État de session Kitchen Daily et captures : jamais versionnés\n*\n' >"$ETAT/kd/.gitignore"
  chown -R homeos:foyer "$ETAT/kd"
  find "$ETAT/kd" -type d -exec chmod 2770 {} +
  find "$ETAT/kd" -type f -exec chmod 660 {} +
  [[ -f "$ETAT/kd/state.json" ]] && chmod 640 "$ETAT/kd/state.json"
fi
for f in coffre.json .coffre.lock sessions.json; do
  [[ -e "$ETAT/$f" ]] || continue
  chown homeos:homeos "$ETAT/$f"
  if [[ "$f" == coffre.json ]]; then chmod 640 "$ETAT/$f"; else chmod 600 "$ETAT/$f"; fi
done
# Le bot et le coffre créent sessions.json, coffre.json et .coffre.lock dans ce dossier (écriture atomique) :
# homeos en est propriétaire. http : 700 homeos:homeos (lot 7, les personnes n'y ont plus rien à lire) ;
# stdio : 750 homeos:foyer (le groupe traverse vers kd/).
if [[ "$MODE_OUTILS" == http ]]; then
  [[ -d "$ETAT/outils" ]] && { chown -R homeos:homeos "$ETAT/outils"; chmod 700 "$ETAT/outils"; }
  chown homeos:homeos "$ETAT"; chmod 700 "$ETAT"
else
  chown homeos:foyer "$ETAT"; chmod 750 "$ETAT"
fi
info "migration terminée (outils : $MODE_OUTILS)"
