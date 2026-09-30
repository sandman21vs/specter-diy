# ESP32-P4 UI fixes: build and hardware results

Target: Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C, ESP32-P4 revision v1.3.
Base: `sandman21vs/specter-diy` branch `esp32-p4-port`, commit
`870767d0e6a807fec5f7a1b473fe9319e273d0d7`.

## Changes

The PIN progress dialog now resolves the backdrop and text layout before it
measures the content. On the 480x800 display it is centered, 400 pixels wide,
and at least 160 pixels high. Long messages stay within the screen and scroll
vertically. The loader preserves both display updates before PIN verification.

The display uses two RGB565 panel framebuffers. LVGL renders a complete frame
into the free buffer and waits for panel completion events before reusing the
previous scanout buffer. The ISR signals a semaphore; LVGL stays in task context.
The Python framebuffer API keeps its drawing buffer separate from scanout.

## Verification

- ESP-IDF 5.5.5 build succeeded. The app image was 3,425,392 bytes, leaving 18%
  of the 4 MiB application partition free.
- Five existing native parser tests passed. The host-only `os.sync` call was
  stubbed because WSL filesystem synchronization blocked; parser and firmware
  behavior were not mocked.
- App-only flash at `0x10000` succeeded with esptool hash verification. The
  existing partition table and internal storage were preserved.
- On-device LVGL measured a 400x160 PIN-style dialog with a 72-pixel-high label.
  Long content stayed in a 752-pixel dialog with vertical scrolling.
- 100 alternating short and PIN-style messages completed without an LVGL error
  or display timeout. The probe took 5,011 ms; this is elapsed test time, not a
  frame-rate measurement.
- The board owner confirmed that button rendering and the PIN dialog looked
  correct after flashing firmware source commit `35191318`.

Firmware image SHA-256 for that board-tested build:
`27afe721b4399d6ffaef6636a8cba8adffb51a78f89e8344817d062831f285b6`.

## Follow-up hardening

Commit `6ca04d35` fails closed if a framebuffer present starts but does not
complete. The scanout identity then becomes unknown, and direct present and
legacy flush calls are rejected until the display is deinitialized and
initialized again. This prevents a later legacy copy from writing into a buffer
that may still be scanned out.

Frame-completion events now use a counting semaphore, so two events remain
observable when they arrive before the waiting task runs. The follow-up ESP-IDF
build succeeded. This internal error-path change was not reflashed to the board;
the hardware measurements above apply to the UI build at `35191318`.
