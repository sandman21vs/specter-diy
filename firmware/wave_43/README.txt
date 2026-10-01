Specter-DIY for ESP32-P4 -- v0.1.0-alpha.2 (pre-release)
Board: Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C (chip revision v1.x)
Source: https://github.com/sandman21vs/specter-diy/tree/esp32p4-ui-camera-performance (commit d9c0c17)

WARNING: development build. No secure boot, no flash encryption.
Do not use with real funds.

FLASH WITH THE WEB FLASHER (Chrome, Edge or Brave on desktop)
1. Plug the board into its UART USB-C port with a data cable.
2. Open https://odudex.github.io/Kern/flash/
3. Choose "Custom ZIP Bundle" and select this zip file, unchanged.
   The flasher reads flasher_args.json and lists 4 files:
     bootloader.bin       @ 0x02000
     partition-table.bin  @ 0x08000
     specter-diy.bin      @ 0x10000
     storage-reset.bin    @ 0x410000
4. Click "Connect", pick the board's serial port, then "Flash".
5. When the log says "Flash successful", the board reboots. The first boot
   formats the internal storage and then shows the Specter screen.

NOTES
- Flashing this bundle always wipes the device's internal storage
  (storage-reset.bin). Anything saved on the device is lost; keep your
  recovery phrase.
- If Connect fails: hold BOOT, tap RST, release BOOT, and try again.
- If the flasher says the image does not support your chip revision, your
  board is a v3.x chip; this build only supports v0.x/v1.x.

FLASH FROM THE COMMAND LINE (alternative)
  pip install esptool
  esptool.py --chip esp32p4 -b 460800 write_flash --flash_mode dio --flash_size 16MB \
    0x2000 bootloader.bin 0x8000 partition-table.bin \
    0x10000 specter-diy.bin 0x410000 storage-reset.bin
