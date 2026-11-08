#!/usr/bin/env bash
# Firmin Assistant — archive produit versionnée (lot 6). Sans root, depuis le dépôt git.
#
#   bash scripts/empaqueter.sh [--sortie DOSSIER] [--ref REF] [--sans-controle]
#
# Produit DOSSIER/firmin-assistant-<VERSION>.tar.gz (+ .sha256) à partir de `git archive REF` (défaut HEAD : seuls
# les fichiers commités, jamais data/ ni les secrets), en retirant :
#   - docs/interne/           notes internes datées (revues, retours d'usage, études)
#   - docs/maquette/          maquette de travail et ses captures
#   - docs/landing/captures/  captures lourdes de la page de présentation
#   - docs/spec-lot4b-*.md    spécification de la maquette (lot 4b, en cours)
#   - tout fichier de plus de 2 Mo (aucun aujourd'hui hors des dossiers ci-dessus)
# Contrôles avant d'écrire l'archive : VERSION, CHANGELOG.md (entrée de la version), LICENCE (signalée si absente),
# docs/installation.md, et tests/test_generique.py sur le contenu extrait (motifs du foyer lus hors dépôt :
# HOMEOS_MOTIFS_INTERDITS / HOMEOS_MOTIFS_INTERDITS_FICHIER). --sans-controle saute ce dernier test.
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SORTIE="$RACINE/dist"
REF=HEAD
CONTROLE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sortie) SORTIE="$2"; shift 2 ;;
    --sortie=*) SORTIE="${1#*=}"; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --sans-controle) CONTROLE=0; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done
EXCLUSIONS=(docs/interne docs/maquette docs/landing/captures docs/spec-lot4b-maquette-v2.md)
info() { printf '   %s\n' "$*"; }
die()  { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }

git -C "$RACINE" rev-parse --verify -q "$REF^{commit}" >/dev/null || die "référence git inconnue : $REF"
VERSION="$(git -C "$RACINE" show "$REF:VERSION" 2>/dev/null | tr -d '[:space:]')" || true
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || die "VERSION absente ou invalide dans $REF"
git -C "$RACINE" show "$REF:CHANGELOG.md" 2>/dev/null | grep -qE "^## ${VERSION//./\\.}( |$)" \
  || die "CHANGELOG.md n'a pas d'entrée « ## $VERSION »"
git -C "$RACINE" cat-file -e "$REF:docs/installation.md" 2>/dev/null || die "docs/installation.md absent"
LICENCE=""
for f in LICENCE LICENSE LICENCE.md LICENSE.md COPYING; do
  git -C "$RACINE" cat-file -e "$REF:$f" 2>/dev/null && { LICENCE="$f"; break; }
done
[[ -n "$LICENCE" ]] || info "attention : aucune licence dans le dépôt (LICENCE) : l'archive part sans licence"
[[ -n "$(git -C "$RACINE" status --porcelain 2>/dev/null)" && "$REF" == HEAD ]] \
  && info "attention : modifications non commitées ignorées (seul $REF est empaqueté)"

NOM="firmin-assistant-$VERSION"
TRAVAIL="$(mktemp -d)"
trap 'rm -rf "$TRAVAIL"' EXIT
git -C "$RACINE" archive --format=tar --prefix="$NOM/" "$REF" | tar -x -C "$TRAVAIL"
for e in "${EXCLUSIONS[@]}"; do rm -rf "${TRAVAIL:?}/$NOM/$e"; done
find "$TRAVAIL/$NOM" -type f -size +2M -print -delete | sed "s|^$TRAVAIL/$NOM/|   retiré (> 2 Mo) : |"
find "$TRAVAIL/$NOM" -name '.gitkeep' -delete
printf '%s\n' "$VERSION" >"$TRAVAIL/$NOM/VERSION"
{
  echo "Firmin Assistant $VERSION"
  echo "commit $(git -C "$RACINE" rev-parse --short "$REF") du $(git -C "$RACINE" show -s --format=%cs "$REF")"
  echo "empaqueté le $(date -u +%Y-%m-%dT%H:%MZ)"
} >"$TRAVAIL/$NOM/PAQUET.txt"

if [[ $CONTROLE -eq 1 ]]; then
  PY="${PYTHON:-}"
  for c in "$RACINE/.venv/bin/python" python3; do [[ -z "$PY" ]] && command -v "$c" >/dev/null && "$c" -c 'import pytest, yaml' 2>/dev/null && PY="$c"; done
  [[ -n "$PY" ]] || die "pytest introuvable pour le contrôle (ou --sans-controle)"
  info "contrôle « zéro donnée personnelle » sur le contenu de l'archive"
  (cd "$TRAVAIL/$NOM" && "$PY" -m pytest -q -p no:cacheprovider tests/test_generique.py) >"$TRAVAIL/controle.log" 2>&1 \
    || { cat "$TRAVAIL/controle.log"; die "contrôle en échec : archive non produite"; }
fi

find "$TRAVAIL/$NOM" -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +   # traces du contrôle
mkdir -p "$SORTIE"
ARCHIVE="$SORTIE/$NOM.tar.gz"
tar -C "$TRAVAIL" --owner=0 --group=0 --numeric-owner --sort=name -czf "$ARCHIVE" "$NOM"
(cd "$SORTIE" && sha256sum "$NOM.tar.gz" >"$NOM.tar.gz.sha256")
info "archive : $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1), $(tar -tzf "$ARCHIVE" | grep -vc '/$') fichiers)"
info "empreinte : $(cut -d' ' -f1 "$ARCHIVE.sha256")"
info "licence : ${LICENCE:-AUCUNE}"
