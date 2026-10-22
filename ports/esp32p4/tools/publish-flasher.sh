#!/usr/bin/env bash
# Publica o web flasher no GitHub Pages, com o firmware de uma release.
#
#   ports/esp32p4/tools/publish-flasher.sh            # release esp32p4-* mais recente
#   ports/esp32p4/tools/publish-flasher.sh <tag>      # uma release especifica
#   ports/esp32p4/tools/publish-flasher.sh --dry-run  # so monta o site, sem publicar
#
# Monta ports/esp32p4/flasher/ + o zip da release extraido em
# firmware/wave_43/ e faz push forcado para o branch gh-pages do remote
# origin. O branch gh-pages so contem o site; o historico dele nao importa.
#
# Precisa do gh autenticado (gh auth login). O GitHub Pages do repositorio deve
# servir o branch gh-pages, pasta raiz.

set -euo pipefail

P4_DIR="$(cd "$(dirname -- "$0")/.." && pwd)"
SPECTER_DIR="$(cd "$P4_DIR/../.." && pwd)"

DRY_RUN=0
TAG=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) TAG="$arg" ;;
  esac
done

ORIGIN_URL="$(git -C "$SPECTER_DIR" remote get-url origin)"
REPO="$(printf '%s\n' "$ORIGIN_URL" | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"

if [ -z "$TAG" ]; then
  TAG="$(gh release list -R "$REPO" --limit 50 --json tagName,publishedAt \
    --jq '[.[] | select(.tagName | startswith("esp32p4-"))] | sort_by(.publishedAt) | last | .tagName')"
fi
if [ -z "$TAG" ] || [ "$TAG" = "null" ]; then
  echo "nenhuma release esp32p4-* em $REPO" >&2
  exit 1
fi

SITE="$(mktemp -d)"
trap 'rm -rf "$SITE"' EXIT

echo "==> Site: $P4_DIR/flasher"
cp -R "$P4_DIR/flasher/." "$SITE/"
# Sem Jekyll: o GitHub Pages serve os arquivos como estao.
touch "$SITE/.nojekyll"

echo "==> Firmware: $REPO $TAG"
FW="$SITE/firmware/wave_43"
mkdir -p "$FW"
gh release download "$TAG" -R "$REPO" -p 'specter-diy-wave_43-*.zip' -D "$FW"
ZIP="$(cd "$FW" && ls specter-diy-wave_43-*.zip | head -1)"
unzip -q "$FW/$ZIP" -d "$FW"
for f in flasher_args.json bootloader.bin partition-table.bin; do
  [ -f "$FW/$f" ] || { echo "o zip da release nao tem $f" >&2; exit 1; }
done

# Lido pela pagina para mostrar a versao e o link da release.
gh release view "$TAG" -R "$REPO" --json tagName,url,publishedAt \
  --jq "{tag: .tagName, url: .url, published: .publishedAt, zip: \"$ZIP\"}" > "$FW/release.json"

if [ "$DRY_RUN" = 1 ]; then
  KEEP="$SPECTER_DIR/ports/esp32p4/deps/flasher-site"
  rm -rf "$KEEP"; mkdir -p "$(dirname "$KEEP")"; cp -R "$SITE" "$KEEP"
  echo "==> Dry run: site montado em $KEEP"
  exit 0
fi

echo "==> Publicando no branch gh-pages de $REPO"
cd "$SITE"
git init -q
git checkout -q -b gh-pages
git add -A
git commit -q -m "Publish web flasher with $TAG"
# Usa a autenticacao do gh para o push, sem depender do credential helper do git.
git -c credential.helper= -c credential.helper='!gh auth git-credential' \
  push -q --force "$ORIGIN_URL" gh-pages

OWNER="${REPO%%/*}"
NAME="${REPO#*/}"
echo "==> Pronto: https://${OWNER}.github.io/${NAME}/"
