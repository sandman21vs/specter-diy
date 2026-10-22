#!/usr/bin/env bash
# Baixa e prepara tudo o que o build do ESP32-P4 precisa. Idempotente: pode
# rodar de novo sem refazer o que ja existe.
#
#   ports/esp32p4/tools/setup.sh
#
# Cria em ports/esp32p4/deps/:
#   micropython/          micropython/micropython @ MICROPYTHON_REF
#   specter-bootloader/   sandman21vs/specter-bootloader @ port_esp32-p4,
#                         so pelo ESP-IDF v5.5.5 pinado nele
#   espressif/            toolchain RISC-V e venv Python do ESP-IDF

set -euo pipefail

P4_DIR="$(cd "$(dirname -- "$0")/.." && pwd)"
SPECTER_DIR="$(cd "$P4_DIR/../.." && pwd)"
P4_DEPS="${P4_DEPS:-$P4_DIR/deps}"
MICROPYTHON_DIR="${MICROPYTHON_DIR:-$P4_DEPS/micropython}"
SPECTER_BOOTLOADER_DIR="${SPECTER_BOOTLOADER_DIR:-$P4_DEPS/specter-bootloader}"
export IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$P4_DEPS/espressif}"

# Commit do MicroPython master verificado na placa (1.30.0-preview).
MICROPYTHON_REF=8cf130db34
BOOTLOADER_URL=https://github.com/sandman21vs/specter-bootloader.git
BOOTLOADER_BRANCH=port_esp32-p4

step() { printf '\n==> %s\n' "$*"; }

step "specter-diy submodules"
cd "$SPECTER_DIR"
git submodule update --init f469-disco
# O secp256k1-embedded traz o libsecp256k1 como submodulo proprio.
git submodule update --init --recursive \
  ports/esp32p4/components/secp256k1 ports/esp32p4/idf_components/k_quirc
# Do f469-disco so interessa o LVGL; o MicroPython antigo dele nao e usado.
git -C f469-disco submodule update --init usermods/udisplay_f469/lvgl

mkdir -p "$P4_DEPS"

step "MicroPython @ $MICROPYTHON_REF"
if [ ! -d "$MICROPYTHON_DIR/.git" ]; then
  git clone https://github.com/micropython/micropython.git "$MICROPYTHON_DIR"
fi
git -C "$MICROPYTHON_DIR" fetch --quiet origin master
git -C "$MICROPYTHON_DIR" checkout --quiet "$MICROPYTHON_REF"

step "ESP-IDF v5.5.5 (via specter-bootloader)"
if [ ! -d "$SPECTER_BOOTLOADER_DIR/.git" ]; then
  git clone --branch "$BOOTLOADER_BRANCH" "$BOOTLOADER_URL" "$SPECTER_BOOTLOADER_DIR"
fi
git -C "$SPECTER_BOOTLOADER_DIR" submodule update --init third_party/esp-idf
git -C "$SPECTER_BOOTLOADER_DIR/third_party/esp-idf" submodule update --init --recursive

step "ESP-IDF toolchain in $IDF_TOOLS_PATH"
"$SPECTER_BOOTLOADER_DIR/third_party/esp-idf/install.sh" esp32p4

step "mpy-cross and MicroPython esp32 submodules"
# shellcheck disable=SC1091
. "$P4_DIR/tools/env.sh"
make -C "$MICROPYTHON_DIR/mpy-cross" -j"$(getconf _NPROCESSORS_ONLN)"
make -C "$MICROPYTHON_DIR/ports/esp32" BOARD=ESP32_GENERIC_P4 submodules

step "Done. Build with: ports/esp32p4/tools/build.sh"
