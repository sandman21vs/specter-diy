#!/usr/bin/env bash
# Compila e grava o firmware Specter na Waveshare 4.3-C.
#
#   ports/esp32p4/tools/build.sh [build]          compila
#   ports/esp32p4/tools/build.sh flash  [PORTA]   grava bootloader, particoes e app
#   ports/esp32p4/tools/build.sh erase  [PORTA]   apaga a flash inteira
#   ports/esp32p4/tools/build.sh clean            apaga o diretorio de build
#   ports/esp32p4/tools/build.sh package [NOME]   monta o zip do web flasher
#
# Sem PORTA, usa $PORT ou tenta detectar a ponte serial da placa.
# Rode tools/setup.sh uma vez antes.
#
# Revisao do chip: o ESP-IDF gera imagem para v0.x/v1.x ou para v3.x, nunca as
# duas. O padrao e v0.x/v1.x; para placas com ESP32-P4 v3.x use P4_REV3=1:
#
#   P4_REV3=1 ports/esp32p4/tools/build.sh
#   P4_REV3=1 ports/esp32p4/tools/build.sh flash
#
# Cada familia compila no seu proprio diretorio (build-W43 / build-W43-rev3).
# Para saber a revisao: python -m esptool --chip esp32p4 -p PORTA flash_id
# mostra "Chip is ESP32-P4 (revision v1.3)" ou v3.x.

set -euo pipefail
. "$(dirname -- "$0")/env.sh" > /dev/null

if [ "${P4_REV3:-0}" = "1" ]; then
  BUILD_NAME=build-W43-rev3
  P4_REV3_FLAG=1
  ZIP_BOARD=wave_43_v3
else
  BUILD_NAME=build-W43
  P4_REV3_FLAG=0
  ZIP_BOARD=wave_43
fi
BUILD_DIR="$MICROPYTHON_DIR/ports/esp32/$BUILD_NAME"

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
           -D P4_REV3="$P4_REV3_FLAG" \
           -B "$BUILD_NAME" build
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
    # keep: grava os parametros de flash que o build ja pos no cabecalho do
    # bootloader (40m no v1.x, 80m no v3.x), sem reescrever a imagem.
    esptool write_flash --flash_mode keep --flash_freq keep --flash_size keep \
      0x2000   bootloader/bootloader.bin \
      0x8000   partition_table/partition-table.bin \
      0x10000  micropython.bin
    ;;
  erase)
    PORT="${2:-$(detect_port)}"
    echo "Erasing flash via $PORT"
    esptool erase_flash
    ;;
  package)
    # Mesmo formato dos zips das releases, aceito pelo "Custom ZIP bundle" do
    # web flasher e pelo publish-flasher.sh. O nome leva _v3 no build v3.x, e
    # e por ele que o publish-flasher.sh separa as duas familias.
    NAME="${2:-$(cd "$SPECTER_DIR" && git rev-parse --short HEAD)}"
    ZIP_NAME="specter-diy-$ZIP_BOARD-$NAME.zip"
    [ -f "$BUILD_DIR/micropython.bin" ] || { echo "compile antes: $0 build" >&2; exit 1; }
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    cp "$BUILD_DIR/bootloader/bootloader.bin" "$STAGE/"
    cp "$BUILD_DIR/partition_table/partition-table.bin" "$STAGE/"
    cp "$BUILD_DIR/micropython.bin" "$STAGE/specter-diy.bin"
    # 8 KB de 0xFF no inicio da particao vfs: o primeiro boot nao acha um
    # filesystem e formata, entao o flash sempre zera o armazenamento interno.
    python -c "import sys; sys.stdout.buffer.write(b'\xff' * 8192)" > "$STAGE/storage-reset.bin"
    cat > "$STAGE/flasher_args.json" <<'JSON'
{
    "flash_files": {
        "0x2000": "bootloader.bin",
        "0x8000": "partition-table.bin",
        "0x10000": "specter-diy.bin",
        "0x410000": "storage-reset.bin"
    }
}
JSON
    (cd "$STAGE" && shasum -a 256 bootloader.bin partition-table.bin specter-diy.bin storage-reset.bin > SHA256SUMS.txt)
    OUT="$SPECTER_DIR/ports/esp32p4/deps/$ZIP_NAME"
    rm -f "$OUT"
    (cd "$STAGE" && zip -q "$OUT" ./*)
    echo "Bundle: $OUT"
    ;;
  clean)
    rm -rf "$BUILD_DIR"
    ;;
  *)
    echo "usage: $0 {build|flash|erase|clean|package} [PORT|NAME]" >&2; exit 2
    ;;
esac
