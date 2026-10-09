Specter-DIY for ESP32-P4 -- v0.1.0-alpha.5 (pre-release)
Board: Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C (chip revision v1.x)
Source: https://github.com/sandman21vs/specter-diy/tree/esp32-p4-port (commit 5b22528)

WARNING: development build. No secure boot, no flash encryption.
Do not use with real funds.

FLASH WITH THE WEB FLASHER (Chrome, Edge or Brave on desktop)
1. Plug the board into its UART USB-C port with a data cable.
2. Open https://sandman21vs.github.io/specter-diy/
3. "Latest release" is this build. Or choose "Custom ZIP bundle" and select
   this zip file, unchanged. It lists 4 files:
     bootloader.bin       @ 0x02000
     partition-table.bin  @ 0x08000
     specter-diy.bin      @ 0x10000
     storage-reset.bin    @ 0x410000
4. Tick the acknowledgement, click "Connect", pick the board's serial port,
   then "Flash".
5. When the log says "Flash successful", the board reboots. The first boot
   formats the internal storage and then shows the Specter screen.

NOTES
- Flashing this bundle always wipes the device's internal storage
  (storage-reset.bin). Anything saved on the device is lost; keep your
  recovery phrase. Keys saved on a smartcard or microSD are not touched.
- If Connect fails: hold BOOT, tap RST, release BOOT, and try again.
- If the flasher says the image does not support your chip revision, your
  board is a v3.x chip; this build only supports v0.x/v1.x.
- microSD cards must be formatted as MBR + FAT32. Cards formatted with the
  macOS default (GPT) do not mount.
- NFC key backup (M5Stack RFID Unit 2) is off after flashing. Turn it on in
  Device settings -> Communication -> NFC card reader. It is experimental.
- The Specter smartcard can be read over NFC with the same reader. Turn it
  on in the same menu ("Use the smartcard over NFC") and restart. The card has
  to rest on the right spot of the reader and stay still. It is experimental.
- A key saved to a smartcard as "Encrypt" only reads back on the device that
  saved it, and not after that device is wiped - which flashing this bundle
  does. Save it as plain text if the card has to survive a reflash.

FLASH FROM THE COMMAND LINE (alternative)
  pip install esptool
  esptool.py --chip esp32p4 -b 460800 write_flash --flash_mode dio --flash_size 16MB \
    0x2000 bootloader.bin 0x8000 partition-table.bin \
    0x10000 specter-diy.bin 0x410000 storage-reset.bin
