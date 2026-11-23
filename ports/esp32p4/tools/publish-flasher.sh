#!/usr/bin/env bash
# Publica o web flasher no GitHub Pages, com o firmware de uma release.
#
#   ports/esp32p4/tools/publish-flasher.sh            # release esp32p4-* mais recente (sem "rev3" no nome)
#   ports/esp32p4/tools/publish-flasher.sh <tag>      # uma release especifica
#   ports/esp32p4/tools/publish-flasher.sh --dry-run  # so monta o site, sem publicar
#   ports/esp32p4/tools/publish-flasher.sh <tag> --rev3 <tag-v3>
#
# Monta ports/esp32p4/flasher/ + os zips extraidos e faz push forcado para o
# branch gh-pages do remote origin. O branch gh-pages so contem o site; o
# historico dele nao importa.
#
# Duas familias de chip, uma pasta cada, escolhida pela pagina na conexao:
#
#   firmware/wave_43/     specter-diy-wave_43-*.zip     (v0.x/v1.x)
#   firmware/wave_43_v3/  specter-diy-wave_43_v3-*.zip  (v3.x, P4_REV3=1)
#
# O zip v3.x vem da release <tag-v3> se --rev3 for passado, senao da propria
# <tag>. Sem zip v3.x a pasta fica de fora e a pagina diz que nao ha build.
#
# Precisa do gh autenticado (gh auth login). O GitHub Pages do repositorio deve
# servir o branch gh-pages, pasta raiz.

set -euo pipefail

P4_DIR="$(cd "$(dirname -- "$0")/.." && pwd)"
SPECTER_DIR="$(cd "$P4_DIR/../.." && pwd)"

DRY_RUN=0
TAG=""
REV3_TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --rev3) shift; REV3_TAG="${1:?--rev3 precisa de uma tag}" ;;
    *) TAG="$1" ;;
  esac
  shift
done

ORIGIN_URL="$(git -C "$SPECTER_DIR" remote get-url origin)"
REPO="$(printf '%s\n' "$ORIGIN_URL" | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"

if [ -z "$TAG" ]; then
  TAG="$(gh release list -R "$REPO" --limit 50 --json tagName,publishedAt \
    --jq '[.[] | select(.tagName | startswith("esp32p4-") and (contains("rev3") | not))] | sort_by(.publishedAt) | last | .tagName')"
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

# stage_profile <tag> <padrao do zip> <pasta>: baixa, extrai e grava o
# release.json lido pela pagina. Retorna 1 se a release nao tem o zip.
stage_profile() {
  local tag="$1" pattern="$2" fw="$3" zip f
  local tmp; tmp="$(mktemp -d)"
  if ! gh release download "$tag" -R "$REPO" -p "$pattern" -D "$tmp" 2>/dev/null; then
    rm -rf "$tmp"; return 1
  fi
  zip="$(cd "$tmp" && ls $pattern | head -1)"
  mkdir -p "$fw"
  mv "$tmp/$zip" "$fw/"
  rm -rf "$tmp"
  unzip -q "$fw/$zip" -d "$fw"
  for f in flasher_args.json bootloader.bin partition-table.bin; do
    [ -f "$fw/$f" ] || { echo "o zip $zip nao tem $f" >&2; exit 1; }
  done
  gh release view "$tag" -R "$REPO" --json tagName,url,publishedAt \
    --jq "{tag: .tagName, url: .url, published: .publishedAt, zip: \"$zip\"}" > "$fw/release.json"
}

echo "==> Firmware v0.x/v1.x: $REPO $TAG"
stage_profile "$TAG" 'specter-diy-wave_43-*.zip' "$SITE/firmware/wave_43" \
  || { echo "a release $TAG nao tem specter-diy-wave_43-*.zip" >&2; exit 1; }

if [ -n "$REV3_TAG" ]; then
  echo "==> Firmware v3.x: $REPO $REV3_TAG"
  stage_profile "$REV3_TAG" 'specter-diy-wave_43_v3-*.zip' "$SITE/firmware/wave_43_v3" \
    || { echo "a release $REV3_TAG nao tem specter-diy-wave_43_v3-*.zip" >&2; exit 1; }
elif stage_profile "$TAG" 'specter-diy-wave_43_v3-*.zip' "$SITE/firmware/wave_43_v3"; then
  echo "==> Firmware v3.x: $REPO $TAG"
else
  echo "==> Sem build v3.x em $TAG: a pagina vai indicar o Custom ZIP"
fi

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
git commit -q -m "Publish web flasher with $TAG${REV3_TAG:+ and $REV3_TAG}"
# Usa a autenticacao do gh para o push, sem depender do credential helper do git.
git -c credential.helper= -c credential.helper='!gh auth git-credential' \
  push -q --force "$ORIGIN_URL" gh-pages

OWNER="${REPO%%/*}"
NAME="${REPO#*/}"
echo "==> Pronto: https://${OWNER}.github.io/${NAME}/"
