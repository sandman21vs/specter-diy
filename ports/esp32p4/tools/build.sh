#!/usr/bin/env bash
# Compila e grava o firmware Specter na Waveshare 4.3-C.
#
#   ports/esp32p4/tools/build.sh [build]          compila
#   ports/esp32p4/tools/build.sh flash  [PORTA]   grava bootloader, particoes e app
#   ports/esp32p4/tools/build.sh erase  [PORTA]   apaga a flash inteira
#   ports/esp32p4/tools/build.sh clean            apaga o diretorio de build
#
# Sem PORTA, usa $PORT ou tenta detectar a ponte serial da placa.
# Rode tools/setup.sh uma vez antes.

set -euo pipefail
. "$(dirname -- "$0")/env.sh" > /dev/null

BUILD_DIR="$MICROPYTHON_DIR/ports/esp32/build-W43"

detect_port() {
  local p
  [ -n "${PORT:-}" ] && { echo "$PORT"; return; }
  # Prefer the board's CH343 USB-UART bridge (VID 1A86), the only port that can
  # flash. With the Specter USB port enabled the board also shows up as a
  # usbmodem device, and it sorts first on macOS.
  p="$(python -c "
import serial.tools.list_ports as l
print(next((x.device for x in l.comports() if x.vid == 0x1A86), ''))" 2>/dev/null || true)"
  [ -n "$p" ] && { echo "$p"; return; }
  for p in /dev/ttyACM* /dev/ttyUSB* /dev/cu.usbmodem* /dev/cu.wchusbserial*; do
    [ -e "$p" ] && { echo "$p"; return; }
  done
  echo "no serial port found: plug the board's UART port, or pass the port as an argument" >&2
  exit 1
}

esptool() {
  python -m esptool --chip esp32p4 -p "$PORT" -b 460800 \
    --before default_reset --after hard_reset "$@"
}

case "${1:-build}" in
  build)
    # Metadados do git exibidos pelo app; opcional, o app cai em "unknown".
    (cd "$SPECTER_DIR" && python tools/embed_git_info.py src/git_info.py > /dev/null 2>&1) || true
    cd "$MICROPYTHON_DIR/ports/esp32"
    idf.py -D MICROPY_BOARD="$MP_BOARD" \
           -D MICROPY_BOARD_DIR="$MP_BOARD_DIR" \
           -D USER_C_MODULES="$MP_USER_C_MODULES" \
           -D EXTRA_COMPONENT_DIRS="$MP_EXTRA_COMPONENTS" \
           -B build-W43 build
    echo
    echo "Firmware: $BUILD_DIR"
    ;;
  flash)
    # idf.py flash falha aqui: o wrapper procura components/esptool_py/esptool.py,
    # que nao existe mais nesta versao (esptool virou pacote pip). Chamamos o
    # modulo direto, com os offsets do proprio flash_args do build.
    PORT="${2:-$(detect_port)}"
    echo "Flashing via $PORT"
    cd "$BUILD_DIR"
    esptool write_flash --flash_mode dio --flash_freq 40m --flash_size 16MB \
      0x2000   bootloader/bootloader.bin \
      0x8000   partition_table/partition-table.bin \
      0x10000  micropython.bin
    ;;
  erase)
    PORT="${2:-$(detect_port)}"
    echo "Erasing flash via $PORT"
    esptool erase_flash
    ;;
  clean)
    rm -rf "$BUILD_DIR"
    ;;
  *)
    echo "usage: $0 {build|flash|erase|clean} [PORT]" >&2; exit 2
    ;;
esac
