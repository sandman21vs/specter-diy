# ESP32-P4 UI fixes: local build and hardware results

Firmware source commit: `35191318`, branch `fix/esp32p4-modal-button-flicker`.
Base: `sandman21vs/specter-diy`, `esp32-p4-port`,
`870767d0e6a807fec5f7a1b473fe9319e273d0d7`.

## Changes

The modal resolves its backdrop layout before reading available dimensions,
sets wrapping width before measuring its label, and resolves the label layout
before sizing the dialog. The 480x800 board gets a centered 400-pixel-wide box
with a minimum height of 160 pixels. Long messages are bounded to the display
and scroll vertically. Required LVGL alignment offsets are passed explicitly.
The loader keeps its title, message and two display updates before PIN work.

The display uses two complete RGB565 framebuffers. LVGL renders into the free
buffer and waits for confirmed panel frame boundaries before reusing the old
scanout buffer. The pinned IDF ISR ordering requires two fresh completion events
to cover an already-running ISR. ISR code only signals a semaphore; LVGL remains
in task context. Timeout/error handling avoids releasing an unsafe framebuffer.
Legacy framebuffer drawing is isolated from LVGL ownership.

## Verification on 2026-09-30

- Native ext4/WSL build succeeded using ESP-IDF 5.5.5 and the pinned port
  dependencies. Image size: 3,425,392 bytes; 18% of the 4 MiB app partition free.
- Five existing native parser tests passed. The host-only `os.sync` call was
  stubbed because WSL global filesystem synchronization blocked; parser and
  firmware code were not replaced by mocks.
- COM5 identified ESP32-P4 revision v1.3. App-only flash at `0x10000` succeeded
  with esptool 5.4.0 and hash verification. No bootloader, partition table,
  storage-reset image or eFuse changes were written. Existing partition-table
  hash matched the build and original release bundle before updating.
- Real LVGL hardware probe identified frozen firmware `35191318` and measured
  the first PIN-style message: backdrop 480x800, dialog 400x160, label height 72.
- Long message: dialog height 752, label height 2880; bounded scrollable content.
- 100 alternating short/PIN-style messages presented without assertion,
  LVGL exception or display timeout; elapsed 5,011 ms. This is a probe duration,
  not a claimed frame rate. Probe objects were removed and the board hard-reset.
- User confirmed that button rendering is cleaner and, after the final update,
  confirmed that the PIN dialog is fixed. No PIN was guessed or logged.
  Camera/QR stress testing and a ten-minute visual soak were not performed.
- Final hard boot had no modal exception or display timeout. Existing GPIO ISR
  and pyb compatibility notices remain; the startup log also reports ENOENT
  messages without a traceback. Full startup logs are retained with the image.

The earlier `f969bf0d` image had a binding-arity error, confirmed by the supplied
photo. The intermediate `12c91d82` image resolved the exception but measured a
1x1 dialog because the backdrop layout was unresolved. Both are superseded by
`35191318`; use only the final package below.

## Delivery

Local update ZIP: `C:\Users\finnn\Downloads\specter-diy-wave_43-ui-fixes-35191318.zip`.
It schedules only `specter-diy.bin` at `0x10000`, preserving existing storage.
This is an update for the existing v0.1.0-alpha.1 partition layout, not an initial
installation bundle. Source and commits remain local; nothing was published.

Firmware SHA-256:
`27afe721b4399d6ffaef6636a8cba8adffb51a78f89e8344817d062831f285b6`.

Build, flash, modal probe and boot logs are alongside the ZIP in the unpacked
`specter-diy-wave_43-ui-fixes-35191318` directory.
