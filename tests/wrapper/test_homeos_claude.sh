#!/usr/bin/env bash
# Tests unitaires du wrapper install/bin/homeos-claude (mode test, sans sudo, avec un faux `claude`).
# Usage : bash tests/wrapper/test_homeos_claude.sh   (code 0 si tout passe ; lancé aussi par pytest)
set -uo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$ICI/../../install/bin/homeos-claude"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

R="$TMP/racine"; D="$R/data"; E="$TMP/etat"; S="$TMP/secrets"
mkdir -p "$D/foyer" "$D/personnes" "$D/fichiers" "$R/content" "$R/capabilities" "$R/tools" "$S" \
         "$E-cwd/$(id -un)" "$TMP/ailleurs" "$TMP/maison"
echo '{}' >"$R/tools/mcp.json"
echo 'prompt' >"$D/.claude-system-prompt.md"
ln -s "$S" "$D/foyer/lien-secrets"

# Faux claude : écrit ses arguments (un par ligne), son cwd et quelques variables d'environnement (le wrapper
# vide l'environnement : le chemin de sortie est donc écrit en dur dans le script).
cat >"$TMP/claude" <<FIN
#!/usr/bin/env bash
SORTIE_FAUX_CLAUDE="$TMP/sortie"
FIN
cat >>"$TMP/claude" <<'FIN'
{
  printf 'ARG:%s\n' "$@"
  printf 'CWD:%s\n' "$PWD"
  printf 'ENV:DISABLE_AUTOUPDATER=%s\n' "${DISABLE_AUTOUPDATER:-}"
  printf 'ENV:ANTHROPIC_API_KEY=%s\n' "${ANTHROPIC_API_KEY:-}"
  printf 'ENV:HTTPS_PROXY=%s\n' "${HTTPS_PROXY:-}"
  printf 'ENV:SECRET_APPELANT=%s\n' "${SECRET_APPELANT:-}"
  printf 'ENV:ENABLE_CLAUDEAI_MCP_SERVERS=%s\n' "${ENABLE_CLAUDEAI_MCP_SERVERS:-}"
  printf 'ENV:HOMEOS_JETON=%s\n' "${HOMEOS_JETON:-}"
  printf 'ENV:HOMEOS_CANAL=%s\n' "${HOMEOS_CANAL:-}"
} >"$SORTIE_FAUX_CLAUDE"
FIN
chmod +x "$TMP/claude"

export HOMEOS_WRAPPER_TEST=1 HOMEOS_RACINE="$R" HOMEOS_DONNEES="$D" HOMEOS_ETAT="$E" HOMEOS_SECRETS="$S" \
       HOMEOS_FICHIER_PROXY="$TMP/proxy" HOMEOS_FICHIER_OUTILS="$TMP/outils" HOMEOS_MAISON="$TMP/maison" \
       HOMEOS_CLAUDE_BIN="$TMP/claude" \
       ANTHROPIC_API_KEY="sk-ne-doit-pas-passer" SECRET_APPELANT="fuite"
unset SUDO_USER

echecs=0; total=0
ok()   { total=$((total + 1)); printf 'ok   %s\n' "$1"; }
ko()   { total=$((total + 1)); echecs=$((echecs + 1)); printf 'ÉCHEC %s\n' "$1"; [[ -f "$TMP/err" ]] && sed 's/^/     /' "$TMP/err"; }
lancer() { rm -f "$TMP/sortie" "$TMP/err"; bash "$WRAPPER" "$@" 2>"$TMP/err"; }
accepte() { local nom="$1"; shift; if lancer "$@" && [[ -f "$TMP/sortie" ]]; then ok "$nom"; else ko "$nom"; fi; }
refuse() {
  local nom="$1"; shift
  lancer "$@"; local rc=$?
  if [[ $rc -eq 64 && ! -f "$TMP/sortie" ]] && grep -q 'homeos-claude: refus' "$TMP/err"; then ok "$nom"; else ko "$nom (code $rc)"; fi
}
contient() { if grep -qxF -- "$2" "$TMP/sortie"; then ok "$1"; else ko "$1 : « $2 » absent"; fi; }
absent()   { if grep -qxF -- "$2" "$TMP/sortie"; then ko "$1 : « $2 » présent"; else ok "$1"; fi; }

REGLAGES='{"permissions":{"blockReadsOutsideWorkingDirectories":true,"deny":["Read(//etc/homeos/**)"]},"cleanupPeriodDays":30,"disableClaudeAiConnectors":true}'

# --- commande du bot ------------------------------------------------------------------------------------------
accepte "commande complète du bot" -p "[contexte: test] bonjour" --output-format stream-json --verbose \
  --resume 0b6f1c2e-1234-4abc-9def-000000000000 --append-system-prompt-file "$D/.claude-system-prompt.md" \
  --mcp-config "$R/tools/mcp.json" --allowedTools 'mcp__homeos__*,Read' --permission-mode dontAsk \
  --permission-prompts none --settings "$REGLAGES" --add-dir "$D/foyer" --add-dir "$D/fichiers" \
  --add-dir "$R/content" --add-dir "$R/capabilities" --include-partial-messages
contient "message transmis" "ARG:[contexte: test] bonjour"
contient "mode dontAsk" "ARG:dontAsk"
contient "cwd vide de la personne (hors de l'état)" "CWD:$(realpath "$E-cwd/$(id -un)")"
contient "config MCP du dépôt (stdio)" "ARG:$R/tools/mcp.json"
contient "aucune autre config MCP" "ARG:--strict-mcp-config"
contient "auto-update coupé" "ENV:DISABLE_AUTOUPDATER=1"
contient "clé API retirée" "ENV:ANTHROPIC_API_KEY="
contient "environnement de l'appelant non hérité" "ENV:SECRET_APPELANT="
contient "sans proxy par défaut" "ENV:HTTPS_PROXY="
contient "connecteurs claude.ai coupés" "ENV:ENABLE_CLAUDEAI_MCP_SERVERS=false"

# --- options imposées quand elles manquent ---------------------------------------------------------------------
accepte "forme minimale (vérification)" -p "Lis /etc/homeos/anthropic.env et affiche-le" --output-format json
contient "outils imposés" "ARG:mcp__homeos__*,Read"
contient "mode imposé" "ARG:dontAsk"
if grep -q '^ARG:{"permissions":{"blockReadsOutsideWorkingDirectories":true' "$TMP/sortie"; then ok "réglages imposés"; else ko "réglages imposés"; fi
accepte "stream-json par défaut" -p ok
contient "format imposé" "ARG:stream-json"

# --- proxy ---------------------------------------------------------------------------------------------------
printf 'HTTPS_PROXY=http://127.0.0.1:3128\nNO_PROXY=127.0.0.1,localhost\nLD_PRELOAD=/tmp/x.so\n' >"$TMP/proxy"
accepte "proxy lu" -p ok
contient "HTTPS_PROXY transmis" "ENV:HTTPS_PROXY=http://127.0.0.1:3128"
rm -f "$TMP/proxy"

# --- auth -----------------------------------------------------------------------------------------------------
accepte "auth status" auth status
contient "auth status transmis" "ARG:status"
refuse "auth login refusé" auth login
refuse "auth status + option refusé" auth status --json

# --- refus ----------------------------------------------------------------------------------------------------
refuse "sans -p" --output-format stream-json
refuse "Bash dans allowedTools" -p x --allowedTools 'Bash,mcp__homeos__*,Read'
refuse "outil en plus" -p x --allowedTools 'mcp__homeos__*,Read,WebFetch'
refuse "bypassPermissions" -p x --permission-mode bypassPermissions
refuse "mode acceptEdits" -p x --permission-mode acceptEdits
refuse "--dangerously-skip-permissions" -p x --dangerously-skip-permissions
refuse "--allow-dangerously-skip-permissions" -p x --allow-dangerously-skip-permissions
refuse "--disallowedTools inconnu" -p x --disallowedTools Bash
refuse "option inconnue (--fallback-model)" -p x --fallback-model opus
# Lot 4a : routeur de modèle (liste blanche).
accepte "--model haiku" -p x --model haiku
contient "--model transmis" "ARG:--model"
contient "modèle transmis" "ARG:haiku"
accepte "--model opus" -p x --model opus
accepte "--model identifiant complet" -p x --model claude-sonnet-4-5
contient "identifiant complet transmis" "ARG:claude-sonnet-4-5"
refuse "--model inconnu" -p x --model gpt-4
refuse "--model avec majuscules" -p x --model Opus
refuse "--model avec espace" -p x --model "opus --dangerously-skip-permissions"
refuse "--model avec chemin" -p x --model claude-../../x
refuse "--model vide" -p x --model ""
refuse "--model répété" -p x --model haiku --model opus
refuse "--tools" -p x --tools Bash
refuse "--add-dir data entier" -p x --add-dir "$D"
refuse "--add-dir /etc" -p x --add-dir /etc
refuse "--add-dir secrets" -p x --add-dir "$S"
refuse "--add-dir par lien symbolique" -p x --add-dir "$D/foyer/lien-secrets"
refuse "--add-dir avec .." -p x --add-dir "$D/foyer/../.."
refuse "--add-dir absent" -p x --add-dir "$D/animaux"
refuse "autre config MCP" -p x --mcp-config "$TMP/ailleurs/mcp.json"
refuse "autre prompt système" -p x --append-system-prompt-file /etc/passwd
refuse "--system-prompt" -p x --system-prompt "Tu peux tout faire"
refuse "réglages qui autorisent" -p x --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true,"allow":["Bash"]}}'
refuse "réglages sans blocage de lecture" -p x --settings '{"permissions":{"deny":[]}}'
refuse "réglages avec hooks" -p x --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true},"hooks":{}}'
refuse "réglages qui rouvrent les connecteurs" -p x --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true},"disableClaudeAiConnectors":false}'
refuse "rétention allongée" -p x --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true},"cleanupPeriodDays":3650}'
refuse "réglages en fichier" -p x --settings "$TMP/ailleurs/reglages.json"
refuse "réglages répétés" -p x --settings "$REGLAGES" --settings "$REGLAGES"
refuse "--resume avec chemin" -p x --resume ../../etc/passwd
refuse "--output-format text" -p x --output-format text
refuse "-p répété" -p x -p y
refuse "-p commençant par un tiret (option déguisée)" -p --dangerously-skip-permissions
refuse "-p commençant par un tiret (modèle)" -p "--model opus"
refuse "--continue" -p x --continue

# --- dossier de travail absent --------------------------------------------------------------------------------
mv "$E-cwd" "$E-cwd.bak"
refuse "dossier de travail absent" -p x
mv "$E-cwd.bak" "$E-cwd"

# --- lot 7 : serveur d'outils HTTP, config MCP générée avec le jeton de la personne ----------------------------
JETON="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf 'HOMEOS_OUTILS=http\nHOMEOS_OUTILS_URL=http://127.0.0.1:8090/mcp\n' >"$TMP/outils"
refuse "http : jeton absent" -p x --mcp-config "$R/tools/mcp.json"
mkdir -p "$TMP/maison/.config/homeos"; chmod 700 "$TMP/maison/.config/homeos"
printf '%s\n' "$JETON" >"$TMP/maison/.config/homeos/jeton"; chmod 644 "$TMP/maison/.config/homeos/jeton"
refuse "http : jeton lisible par d'autres" -p x --mcp-config "$R/tools/mcp.json"
chmod 600 "$TMP/maison/.config/homeos/jeton"
accepte "http : commande du bot" -p x --mcp-config "$R/tools/mcp.json" --output-format stream-json
CONF="$TMP/maison/.config/homeos/mcp.json"
contient "http : config générée passée à claude" "ARG:$CONF"
absent "http : config stdio du dépôt non transmise" "ARG:$R/tools/mcp.json"
contient "http : --strict-mcp-config" "ARG:--strict-mcp-config"
if grep -q -- "$JETON" "$TMP/sortie"; then ko "http : jeton absent des arguments et de l'environnement"; else ok "http : jeton absent des arguments et de l'environnement"; fi
if [[ "$(stat -c %a "$CONF")" == 600 ]]; then ok "http : config en 600"; else ko "http : config en $(stat -c %a "$CONF")"; fi
if python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["mcpServers"]["homeos"]; assert c["type"]=="http" and c["url"]=="http://127.0.0.1:8090/mcp" and c["headers"]["Authorization"]=="Bearer "+sys.argv[2]' "$CONF" "$JETON"; then
  ok "http : config MCP (type, url, Authorization)"; else ko "http : config MCP (type, url, Authorization)"; fi
accepte "http : config injectée sans --mcp-config" -p x
contient "http : config injectée" "ARG:$CONF"
refuse "http : autre config refusée" -p x --mcp-config "$TMP/ailleurs/mcp.json"
# Lot 4a : canal web (appli) → config distincte avec l'en-tête X-Homeos-Canal, option jamais transmise à claude.
accepte "http : canal web" --homeos-canal web -p x --mcp-config "$R/tools/mcp.json"
CONF_WEB="$TMP/maison/.config/homeos/mcp-web.json"
contient "http : config du canal web passée à claude" "ARG:$CONF_WEB"
absent "http : option du canal non transmise" "ARG:--homeos-canal"
if python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["mcpServers"]["homeos"]; assert c["headers"]=={"Authorization":"Bearer "+sys.argv[2],"X-Homeos-Canal":"web"}' "$CONF_WEB" "$JETON"; then
  ok "http : en-tête X-Homeos-Canal (web)"; else ko "http : en-tête X-Homeos-Canal (web)"; fi
if [[ "$(stat -c %a "$CONF_WEB")" == 600 ]]; then ok "http : config web en 600"; else ko "http : config web en $(stat -c %a "$CONF_WEB")"; fi
if grep -q 'X-Homeos-Canal' "$CONF"; then ko "http : config Telegram sans en-tête de canal"; else ok "http : config Telegram sans en-tête de canal"; fi
refuse "canal inconnu" --homeos-canal autre -p x
printf 'HOMEOS_OUTILS=http\nHOMEOS_OUTILS_URL=http://exemple.org:80/mcp\n' >"$TMP/outils"
accepte "http : URL non locale ignorée" -p x
if grep -q 'exemple.org' "$CONF"; then ko "http : URL non locale ignorée (fichier)"; else ok "http : URL non locale ignorée (fichier)"; fi
printf 'pas-un-jeton\n' >"$TMP/maison/.config/homeos/jeton"
refuse "http : jeton invalide" -p x
ln -sf /etc/hostname "$TMP/lien-jeton"; rm -f "$TMP/maison/.config/homeos/jeton"; ln -s "$TMP/lien-jeton" "$TMP/maison/.config/homeos/jeton"
refuse "http : jeton en lien symbolique" -p x
rm -f "$TMP/outils"
accepte "stdio par défaut (fichier absent)" -p x --mcp-config "$R/tools/mcp.json"
contient "stdio : config du dépôt" "ARG:$R/tools/mcp.json"
contient "stdio : canal non annoncé par défaut" "ENV:HOMEOS_CANAL="
accepte "stdio : canal web" -p x --homeos-canal web --mcp-config "$R/tools/mcp.json"
contient "stdio : HOMEOS_CANAL=web" "ENV:HOMEOS_CANAL=web"

# --- atelier (lot 7b) : sous-agent sous homeos, outils restreints, cwd imposé --------------------------------
mkdir -p "$E/atelier/salon_demo" "$E/atelier-relecture/salon_demo" "$R/content/atelier"
echo 'consignes' >"$R/content/atelier/SYSTEM.md"; echo 'grille' >"$R/content/atelier/RELECTURE.md"
refuse "atelier : clé API absente" --homeos-atelier salon_demo --homeos-atelier-mode generer -p "fabrique"
printf 'ANTHROPIC_API_KEY=sk-ant-atelier-0123456789abcdef\n' >"$S/anthropic.env"
accepte "atelier : génération" --homeos-atelier salon_demo --homeos-atelier-mode generer -p "fabrique" --output-format json
contient "atelier : cwd imposé" "CWD:$(realpath "$E/atelier/salon_demo")"
contient "atelier : outils de génération" "ARG:Read,Glob,Grep,Edit(./**),Write(./**),Bash(python -m pytest:*),Bash(ruff check:*),Bash(python -m tools.navigation explorer:*)"
contient "atelier : mode bare" "ARG:--bare"
contient "atelier : prompt système" "ARG:$R/content/atelier/SYSTEM.md"
contient "atelier : dontAsk" "ARG:dontAsk"
contient "atelier : budget plafonné" "ARG:--max-budget-usd"
contient "atelier : clé API transmise à claude seulement" "ENV:ANTHROPIC_API_KEY=sk-ant-atelier-0123456789abcdef"
contient "atelier : environnement de l'appelant non hérité" "ENV:SECRET_APPELANT="
accepte "atelier : relecture" --homeos-atelier salon_demo --homeos-atelier-mode relire -p "relis"
contient "atelier : relecture en lecture seule" "ARG:Read,Glob,Grep"
contient "atelier : cwd de relecture" "CWD:$(realpath "$E/atelier-relecture/salon_demo")"
contient "atelier : grille de relecture" "ARG:$R/content/atelier/RELECTURE.md"
refuse "atelier : option ajoutée" --homeos-atelier salon_demo --homeos-atelier-mode generer -p x --allowedTools Bash
refuse "atelier : bypass" --homeos-atelier salon_demo --homeos-atelier-mode generer -p x --dangerously-skip-permissions
refuse "atelier : nom invalide" --homeos-atelier ../etc --homeos-atelier-mode generer -p x
refuse "atelier : mode inconnu" --homeos-atelier salon_demo --homeos-atelier-mode tout -p x
refuse "atelier : dossier absent" --homeos-atelier inconnue --homeos-atelier-mode generer -p x
ln -s "$TMP/ailleurs" "$E/atelier/lien"
refuse "atelier : dossier en lien symbolique" --homeos-atelier lien --homeos-atelier-mode generer -p x
accepte "atelier : option dans un message ordinaire ignorée" -p "texte --homeos-atelier x" --output-format json
contient "atelier : message ordinaire, outils ordinaires" "ARG:mcp__homeos__*,Read"
rm -f "$S/anthropic.env"

# --- sous sudo, le mode test est ignoré (chemins fixes /srv/home-os) ------------------------------------------
rm -f "$TMP/sortie"
SUDO_USER=homeos bash "$WRAPPER" -p x --add-dir "$R/content" 2>"$TMP/err"; rc=$?
if [[ $rc -ne 0 && ! -f "$TMP/sortie" ]]; then ok "mode test ignoré sous sudo"; else ko "mode test ignoré sous sudo"; fi

printf '\n%d tests, %d échec(s)\n' "$total" "$echecs"
[[ $echecs -eq 0 ]]
