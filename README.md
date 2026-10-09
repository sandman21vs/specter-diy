# Specter-DIY for ESP32-P4

This is a fork of [cryptoadvance/specter-diy](https://github.com/cryptoadvance/specter-diy)
that runs the Specter firmware on the **Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C**:
a single off-the-shelf board with a 4.3" touchscreen, a camera for QR codes and a
microSD slot. The wallet itself needs no soldering and no extra parts; an
optional [smartcard hat](#smartcard-hat-optional) adds the Specter smartcard
keystore.

> [!WARNING]
> **Work in progress. Do not use with real funds.** The firmware is built in a
> development profile: no Secure Boot, no flash encryption, no eFuses burned.
> Everything is reversible, but the device is not protected against someone
> with physical access to it.

## What works

Every item below was tested on the board.

| Feature | Status |
|---|---|
| 480x800 MIPI-DSI display and GT911 touch | working |
| Specter GUI (LVGL 9.3) | working |
| secp256k1 (ECDSA, Schnorr/BIP340 test vectors) | working |
| Bitcoin stack (`embit`, official BIP84 test vectors) | working |
| OV5647 camera over MIPI-CSI, QR scanning | working, ~11 scans/s |
| microSD (MBR + FAT32) | working |
| USB with Specter Desktop / HWI (native USB port) | working |
| Battery level | working, three states |
| Smartcard keystore, with the [SEC1210 hat](#smartcard-hat-optional) | working |
| Key backup on NFC cards, with the [M5Stack RFID Unit 2](#nfc-card-reader-optional-experimental) | **experimental, not yet tested on the board** |
| Hardware random number generator | working |
| ESP32-C6 radio | held in reset (airgapped by design) |
| Secure boot / flash encryption / secure wipe | **not implemented** |

## What you need

- [Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C](https://www.waveshare.com/esp32-p4-wifi6-touch-lcd-4.3.htm)
- A USB-C data cable, plugged into the port labelled **UART** (not USB-OTG)
- A computer running **macOS** or **Linux**, with ~6 GB of free disk space
- Optional: a [smartcard hat](#smartcard-hat-optional) and a JavaCard with the
  Specter applet, to keep the key on a smartcard

## Flash from the browser

The quickest way to try it: open the
**[Specter-DIY Web Flasher](https://sandman21vs.github.io/specter-diy/)** in
Chrome, Edge or Brave on a desktop computer, plug the board into its **UART**
port, then click **Connect** and **Flash**. It installs the latest
[release](https://github.com/sandman21vs/specter-diy/releases).

Flashing wipes everything stored on the board.

The flasher is adapted from the [Kern Web Flasher](https://odudex.github.io/Kern/flash/);
its source is in [`ports/esp32p4/flasher/`](./ports/esp32p4/flasher). To
publish a new release to it, run `ports/esp32p4/tools/publish-flasher.sh`.

## Smartcard hat (optional)

Specter can keep the key on a smartcard instead of on the device. On this board
that works with the **SEC1210 Smartcard Hat** by CryptoGuide:

- Buy it ready-made: **[cryptoguide.tips/shop](https://cryptoguide.tips/shop/)**
- Design files and build notes:
  [3rdIteration/seedsigner, `electronics/SmartcardHat`](https://github.com/3rdIteration/seedsigner/tree/main/electronics/SmartcardHat)

The card needs the Specter applet from
[specter-javacard](https://github.com/cryptoadvance/specter-javacard)
(MemoryCard). Other cards are detected by the reader, but Specter cannot use
them.

### The hat needs a small rework

The hat was designed for a Raspberry Pi. On a Pi header its serial lines are on
pins 8 and 10, and on this board those pins are **GPIO37 and GPIO38**: the
console, also used to flash the firmware. Plugged in as it is, the hat would
fight with the console and the card would see the boot log.

So the two serial lines are moved to GPIO21 and GPIO22. This takes a soldering
iron and two short wires:

1. **Lift R44 and R45** on the hat. They are the two 100 Ω resistors in series
   with the serial lines, between the header and the SEC1210 chip. With them
   lifted, the hat no longer touches GPIO37 and GPIO38.
2. **Solder a wire from each resistor's chip-side pad to the board**:

   | Hat | Signal | Board | Header pin |
   |---|---|---|---|
   | R44, chip side | P4 TX → SEC1210 RXD | **GPIO21** | 15 |
   | R45, chip side | P4 RX ← SEC1210 TXD | **GPIO22** | 17 |

3. Plug the hat onto the 40-pin header as usual. It takes 5 V and ground from
   the header; nothing else needs wiring.

Insert the card and power the board. Specter picks the smartcard keystore at
boot when it finds a card with the applet.

If the card is not found, copy
[`ports/esp32p4/test_uscard.py`](./ports/esp32p4/test_uscard.py) to the board
and run it from the serial console. It says which step fails: the reader, the
card, the ATR or the command exchange. If the reader does not answer, the two
wires are most likely swapped.

## NFC card reader (optional, experimental)

> **Proof of concept. Do not rely on it for a real key yet.** It has passed its
> tests against a simulated reader and simulated cards, and it builds into the
> firmware, but it has **not been run on the board with a real reader**. It has
> had no security review. Keep another backup.

With an **[M5Stack RFID Unit 2](https://shop.m5stack.com/products/rfid-unit-2-ws1850s)**
(WS1850S) plugged in, Specter can save the recovery phrase to an NFC card,
encrypted with a password, and load it back:

- **Save:** Settings → *Save key to NFC card*, with a key loaded.
- **Load:** *Load key from NFC card*, on the first menu.

Both entries only appear while the reader answers on the bus. Unplug it and
they are gone.

### Wiring

The reader goes on the board's I2C bus, next to the touch controller:

| RFID Unit 2 (Grove) | Board |
|---|---|
| SDA (yellow) | **GPIO7** |
| SCL (white) | **GPIO8** |
| VCC (red) | **3V3**, not 5 V |
| GND (black) | GND |

It answers at address `0x28`, which collides with nothing on the board.

### What is on the card

The 12 to 24 words are turned into their BIP39 entropy, sealed in a KEF
envelope (the Krux encryption format: AES-256-GCM, key from PBKDF2-HMAC-SHA256
with 100,000 rounds) and written as one record. The words never go over the antenna, and nothing
unencrypted is written.

The record layout and the envelope are the ones used by the NFC branches of
[Kern](https://github.com/sandman21vs/Kern/blob/nfc-card-storage/docs/nfc.md)
and [Krux](https://github.com/sandman21vs/krux), so a card written by one is
meant to load on the others. Specter reads every KEF version and writes
AES-GCM.

Things to know:

- **The password is the only protection.** The card answers any reader held
  near it, so anyone who gets the card can copy it and try passwords offline.
- **The passphrase is not on the card.** After loading, enter it again.
- **Cards:** MIFARE Classic 1K/4K (the ones sold with the reader) and NTAG21x.
  A plain 48-byte Ultralight is too small.
- **A phone shows the card as empty.** The record is not NDEF. A MIFARE Classic
  card that was formatted as NDEF has to be erased with a tag tool first, or it
  reads as blank.
- The antenna is on only while the "hold the card" screen is up.

If it does not work, copy [`ports/esp32p4/test_nfc.py`](./ports/esp32p4/test_nfc.py)
to the board and run `test_nfc.run()` from the serial console. It says which
step fails: the bus, the reader, the card or the record.

## Build and flash

Copy and paste each block into your terminal. The first run downloads the
ESP-IDF toolchain and MicroPython, which takes a while; later builds are fast.

### 1. Install the system packages

**macOS** (with [Homebrew](https://brew.sh)):

```bash
xcode-select --install; brew install git cmake ninja python
```

**Debian / Ubuntu:**

```bash
sudo apt update && sudo apt install -y git cmake ninja-build python3 python3-venv python3-pip build-essential libusb-1.0-0 wget flex bison gperf ccache libffi-dev libssl-dev dfu-util
```

On Linux, also allow your user to access the serial port, then log out and back in:

```bash
sudo usermod -aG dialout $USER
```

### 2. Get the code

```bash
git clone -b esp32-p4-port https://github.com/sandman21vs/specter-diy.git
cd specter-diy
```

### 3. Download the dependencies (once)

```bash
ports/esp32p4/tools/setup.sh
```

This fetches everything into `ports/esp32p4/deps/`, without touching any other
ESP-IDF install you may have:

- [MicroPython](https://github.com/micropython/micropython) at the verified
  `master` commit (the ESP32-P4 board is not in a release yet)
- ESP-IDF v5.5.5, pinned by [sandman21vs/specter-bootloader](https://github.com/sandman21vs/specter-bootloader/tree/port_esp32-p4)
- the RISC-V toolchain and the ESP-IDF Python environment

### 4. Build

```bash
ports/esp32p4/tools/build.sh
```

It ends with the firmware size and the line `Firmware: .../build-W43`.

### 5. Flash

Plug the board into the **UART** port. The first time, erase the whole flash
(this also clears the factory demo):

```bash
ports/esp32p4/tools/build.sh erase
```

Then write the firmware:

```bash
ports/esp32p4/tools/build.sh flash
```

The script finds the serial port by itself. If you have more than one device
connected, pass the port explicitly, for example
`ports/esp32p4/tools/build.sh flash /dev/cu.usbmodem1101` on macOS or
`ports/esp32p4/tools/build.sh flash /dev/ttyACM0` on Linux.

The board resets and the Specter interface appears on the screen.

### Updating

```bash
git pull && git submodule update --init
ports/esp32p4/tools/build.sh && ports/esp32p4/tools/build.sh flash
```

Only erase again if the partition table changed. If a build fails after an
update with odd `MP_QSTR_` errors, run `ports/esp32p4/tools/build.sh clean` and
build again.

### Troubleshooting

| Problem | Fix |
|---|---|
| `No serial data received` / port busy while flashing | Close any serial monitor, then hold **BOOT**, tap **RST**, release **BOOT** and flash again |
| No serial port shows up | Use the **UART** USB-C port and a data cable (not a charge-only one) |
| `filesystem appears to be corrupted` on the console | Run `build.sh erase`, then `build.sh flash` |
| `setup.sh` stops during the ESP-IDF install | Run it again; it resumes where it stopped |

### Serial console (optional)

For the MicroPython REPL and the on-board hardware tests:

```bash
pip3 install mpremote
mpremote
```

The hardware tests live in [`ports/esp32p4/`](./ports/esp32p4) (`test_*.py`).

## How the port works

- Firmware: MicroPython `master` + ESP-IDF v5.5.5, with the Specter app and its
  libraries frozen into the binary.
- C modules in [`ports/esp32p4/components/`](./ports/esp32p4/components):
  display and touch, camera, LVGL 9.3 binding, secp256k1, SHA-512/RIPEMD-160.
- A small `pyb` shim maps the STM32 API the Specter app expects onto the ESP32.
  The camera shows up as a virtual QR scanner, so the app's QR protocol code
  (animated QR, UR, BBQr) runs unchanged.

Design notes, pin maps and every problem found along the way are in
[`ports/esp32p4/README.md`](./ports/esp32p4/README.md) (Portuguese) and in
[`reports/`](./reports).

## Credits

- [cryptoadvance/specter-diy](https://github.com/cryptoadvance/specter-diy):
  the Specter wallet itself
- [sandman21vs/specter-bootloader](https://github.com/sandman21vs/specter-bootloader/tree/port_esp32-p4)
  (from [miketlk/specter-bootloader](https://github.com/miketlk/specter-bootloader)):
  ESP32-P4 pin map, panel timings, pinned ESP-IDF
- [sandman21vs/secp256k1-embedded](https://github.com/sandman21vs/secp256k1-embedded/tree/micropython-master-api):
  secp256k1 bindings updated for current MicroPython
- [odudex/Kern](https://github.com/odudex/Kern) and
  [odudex/k_quirc](https://github.com/odudex/k_quirc): Waveshare 4.3 BSP, camera
  pipeline, QR decoder and the web flasher
- [odudex/Kern](https://github.com/odudex/Kern) (NFC branch) and
  [selfcustody/krux](https://github.com/selfcustody/krux): the NFC card format,
  the WS1850S driver and the KEF encryption format, translated to MicroPython
- [diybitcoinhardware/f469-disco](https://github.com/diybitcoinhardware/f469-disco):
  LVGL, `embit` and the other shared libraries

---

# Original Specter-DIY

    "Cypherpunks write code. We know that someone has to write software to defend privacy, 
    and since we can't get privacy unless we all do, we're going to write it."
    A Cypherpunk's Manifesto - Eric Hughes - 9 March 1993

    ...and Cypherpunks do build their own Bitcoin Hardware Wallets.

![](./docs/pictures/kit.jpg)

The idea of the project is to build a hardware wallet from off-the-shelf components.
Even though we have [an extension board](./shield) that puts everything in a nice form-factor and helps you to avoid any soldering, we will continue supporting and maintaining compatibility with standard components.

We also want to keep the project flexible such that it can work on any other set of components with minimal changes. Maybe you want to make a hardware wallet on a different architecture (RISC-V?), with an audio modem as a communication channel - you should be able to do it. It should be easy to add or change functionality of Specter and we try to abstract logical modules as much as we can.

QR codes are a default way for Specter to communicate with the host. QR codes are pretty convenient and allow the user to be in control of the data transmission - every QR code has a very limited capacity and communication happens unidirectionally. And it's airgapped - you don't need to connect the wallet to the computer at any time.

For secret storage we support agnostic mode (wallet forgets all secrets when turned off), reckless mode (stores secrets in flash of the application microcontroller) and secure element integration is coming soon.

Our main focus is multisignature setup with other hardware wallets, but wallet can also work as a single signer. We try to make it compatible with Bitcoin Core where we can - PSBT for unsigned transactions, wallet descriptors for importing/exporting multisig wallets. To communicate with Bitcoin Core easier we are also working on [Specter Desktop app](https://github.com/cryptoadvance/specter-desktop) - a small python flask server talking to your Bitcoin Core node.

Most of the firmware is written in MicroPython which makes the code easy to audit and change. We use [secp256k1](https://github.com/bitcoin-core/secp256k1) library from Bitcoin Core for elliptic curve calculations and [LVGL](https://lvgl.io/) library for GUI.

## DISCLAIMER

The project has significantly matured, to the extent that the security level of Specter-DIY is now comparable to commercial hardware wallets on the market. We implemented a secure bootloader that verifies firmware upgrades, so you can be sure that only signed firmware can be uploaded to the device after initial setup. However, unlike with commercial signing devices the bootloader has to be installed manually by the user and is not set in the factory of the device vendor. Thus, pay extra attention during the initial firmware installation and make sure you verified PGP signatures and flash the firmware from a secure computer.

If something doesn't work open an issue here or ask a question in our [Telegram group](https://t.me/+VEinVSYkW5TUl5Ai).

## Documentation

All the docs are stored in the [`docs/`](./docs) folder:

- [`shopping.md`](./docs/shopping.md) explains what to buy
- [`assembly.md`](./docs/assembly.md) shows how to put everything together.
- [`quickstart.md`](./docs/quickstart.md) guides you through the initial steps how to get firmware on the board
- [`reproducible-build.md`](./docs/reproducible-build.md) describes how to build the initial firmware and upgrade files with the same hash as in the release using Docker
- [`build.md`](./docs/build.md) describes how to build the firmware and the simulator yourself
- [`security-model.md`](./docs/security-model.md) explains possible attack vectors and security model of the project
- [`SECURITY.md`](./SECURITY.md) describes how to report vulnerabilities (disclosure policy)
- [`development.md`](./docs/development.md) explains how to start developing on Specter
- [`simulator.md`](./docs/simulator.md) shows how to run a simulator on unix/macOS
- [`communication.md`](./docs/communication.md) defines communication protocol with the host over QR codes and USB
- [`roadmap.md`](./docs/roadmap.md) explains what we need to implement before we can consider the wallet be ready to use with real funds.

Specter-Shield documentation and all the files are available in the [`shield/`](./shield) folder:

- [What it looks like](./shield/README.md)
- [How to print a 3d case](./shield/3dprinting.md)

Specter Shield-Lite documentation is available in the [`shield-lite/`](./shield-lite) folder:

- [Specter Shield-Lite overview](./shield-lite/readme.md)

Supported networks: Mainnet, Testnet, Regtest, Signet.

## Running tests

The unit test suite runs on the Unix simulator build. Install the required
system packages and then run the `make` target:

```
sudo apt-get update
sudo apt-get install libsdl2-dev libffi-dev pkg-config libreadline-dev libgmp-dev build-essential python3
make test
```

The build system will fetch the necessary submodules and compile the simulator
before executing the tests.

The KEF and NFC tests need no simulator and no hardware. They run on plain
Python against a simulated reader and simulated cards:

```
pip install cryptography embit
python3 -m unittest discover -s test/tests_native -p "test_kef.py"
python3 -m unittest discover -s test/tests_native -p "test_nfc*.py"
```

## USB communication on Linux

You may need to set up udev rules and add yourself to `dialout` group. Read more in [`udev`](./udev/README.md) folder.

## Video and screenshots

Check out [this video](https://www.youtube.com/watch?v=1H7FqG_FmCw) to get an idea how to assemble it and how it works.

Here is a [Gallery](./docs/pictures/gallery/README.md) with devices assembled by the community.

A few pictures of the UI:

### Wallet screens

![](./docs/pictures/wallet_screens.jpg)

### Key generation and recovery

![](./docs/pictures/init_screens.jpg)
