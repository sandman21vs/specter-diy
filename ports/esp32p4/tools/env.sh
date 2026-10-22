# Ambiente de build do firmware ESP32-P4.
#
#   . ports/esp32p4/tools/env.sh
#
# Todos os caminhos derivam da posicao deste arquivo. As dependencias externas
# (MicroPython, ESP-IDF e toolchain) ficam em ports/esp32p4/deps/, criado pelo
# tools/setup.sh e ignorado pelo git. Qualquer variavel abaixo pode ser
# sobrescrita no ambiente para reaproveitar uma arvore ja existente.

if [ -n "${BASH_SOURCE:-}" ]; then
  _p4_env="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  _p4_env="${(%):-%x}"
else
  echo "env.sh: source this from bash or zsh" >&2
  return 1
fi
P4_DIR="$(cd "$(dirname -- "$_p4_env")/.." && pwd)"
unset _p4_env
export P4_DIR
export SPECTER_DIR="$(cd "$P4_DIR/../.." && pwd)"
export P4_DEPS="${P4_DEPS:-$P4_DIR/deps}"

# MicroPython master: o board ESP32_GENERIC_P4 ainda nao saiu em release.
export MICROPYTHON_DIR="${MICROPYTHON_DIR:-$P4_DEPS/micropython}"

# ESP-IDF v5.5.5, pinado como submodulo do specter-bootloader.
# Nao esta na lista oficialmente suportada pelo MicroPython (5.3-5.5.4), mas
# compila o alvo esp32p4 sem erro.
export SPECTER_BOOTLOADER_DIR="${SPECTER_BOOTLOADER_DIR:-$P4_DEPS/specter-bootloader}"
export IDF_PATH="$SPECTER_BOOTLOADER_DIR/third_party/esp-idf"
export IDF_PATH_FORCE=1
export IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$P4_DEPS/espressif}"

# A placa e ESP32-P4 revisao v1.3; o board embute sdkconfig.p4_pre_rev3.
export MP_BOARD=WAVESHARE_P4_43
export MP_BOARD_DIR="$P4_DIR/boards/WAVESHARE_P4_43"
export MP_USER_C_MODULES="$P4_DIR/components/micropython.cmake"

# Componentes ESP-IDF de verdade. Necessarios para dependencias gerenciadas: o
# MicroPython so le idf_component.yml de ports/esp32/main/, e um usermod nao
# pode declarar as suas.
export MP_EXTRA_COMPONENTS="$P4_DIR/idf_components"

# Sem variante de WiFi de proposito: a placa tem um ESP32-C6, mas o alvo e um
# dispositivo airgapped.

if [ ! -f "$IDF_PATH/export.sh" ]; then
  echo "ESP-IDF not found at $IDF_PATH -- run ports/esp32p4/tools/setup.sh first" >&2
  return 1
fi
. "$IDF_PATH/export.sh" > /dev/null 2>&1 || {
  echo "could not activate ESP-IDF; run: . \"$IDF_PATH/export.sh\" to see why" >&2
  return 1
}

echo "ESP32-P4:"
echo "  IDF            = $(idf.py --version 2>&1 | tail -1)"
echo "  IDF_TOOLS_PATH = $IDF_TOOLS_PATH"
echo "  MICROPYTHON    = $MICROPYTHON_DIR"
echo "  BOARD          = $MP_BOARD"
