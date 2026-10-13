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
| Key backup on NFC cards, with the [M5Stack RFID Unit 2](#nfc-card-reader-optional-experimental) | working, **experimental** |
| Key backup on NFC cards, with a [PN532 module](#pn532-i2c) | tested against a simulated reader, **not yet tried on the board** |
| Key backup on NFC cards, with an [RFID-RC522 module](#rfid-rc522-spi) | cards are read on the board; writing **not yet tried on the board** |
| Smartcard keystore over NFC, with a [PN532 module](#which-reader-to-get) | working on the board, **experimental** |
| Smartcard keystore over NFC, with the [M5Stack RFID Unit 2](#which-reader-to-get) | working, **experimental**, position sensitive |
| Smartcard keystore over NFC, with an [RFID-RC522 module](#which-reader-to-get) | **does not work**: the module can not power the card |
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

## Smartcard over NFC (experimental)

> **Proof of concept.** It worked on the board in the first trials and has had
> very little use. How well it works depends on the reader: see
> [which reader to get](#which-reader-to-get). With the RFID Unit 2 the card
> has to rest on the right spot and stay still, and finding that spot takes
> some trying. With a PN532 it was found at once. An RC522 can not power the
> card at all.

The Specter smartcard (a JavaCard with the MemoryCard applet) can be read by
the [NFC reader](#nfc-card-reader-optional-experimental) instead of the
[smartcard hat](#smartcard-hat-optional). It is the same card, the same PIN
and the same stored key: a card set up through the hat works over NFC, and the
other way round.

Wire the reader as described in the next section. Then:

1. **Device settings → Communication → NFC card reader → Use the smartcard
   over NFC**, and restart.
2. The first time, the device asks for the card before the PIN screen, to
   learn which card it is. Hold it against the reader until the screen moves
   on.
3. Type the PIN with the card away, then hold the card against the reader once
   more.

From then on a start is the PIN and one tap. *Load key from smartcard* does
not need the card again, and each change in *Smartcard storage* is one tap
(two to save a key). The antenna is on only while a screen is asking for the
card. Cancelling the first tap starts the device without the smartcard.

What to know:

- **Where the card goes.** On the reader module, not behind the board. Over
  the edge of the card usually works better than over its centre. If the card
  slips, the screen says so and waits for it again.
- **A card with a key in the hat's slot wins.** NFC is used only when the slot
  is empty.
- **The device remembers one card.** Its public key is kept in flash, so that
  the PIN screen can show the anti-phishing words before the card is there.
  The tap that follows proves the card owns that key, and a different card is
  refused before it sees the PIN. To use another card, hold it again when
  asked: it is then learned as a new card, with one extra tap and different
  words.
- **The PIN stays in memory while the device is unlocked,** because the card
  locks itself every time it leaves the reader. Locking the device forgets it.
- **A key saved as "Encrypt" only reads back on the device that saved it,**
  and not after that device is wiped. Such a card can be cleared or
  overwritten from *Smartcard storage*.
- **Backup cards and the smartcard do not mix.** MIFARE and NTAG cards are
  only ever used for the encrypted backup below, and the smartcard only for
  the keystore.

If the card is not found, copy
[`ports/esp32p4/test_nfc_javacard.py`](./ports/esp32p4/test_nfc_javacard.py)
to the board and run `test_nfc_javacard.go()` from the serial console. It
shows live whether the card answers, so you can find the spot, and then opens
the secure channel twenty times and says where it stops if it does.

## NFC card reader (optional, experimental)

> **Proof of concept. Do not rely on it for a real key yet.** Saving a key to
> a card and loading it back works on the board, but it has had little use and
> no security review. Keep another backup.

With an **[M5Stack RFID Unit 2](https://shop.m5stack.com/products/rfid-unit-2-ws1850s)**
(WS1850S) plugged in, Specter can save the recovery phrase to an NFC card,
encrypted with a password, and load it back.

Two cheaper modules are supported as well: the **PN532** (the red "NFC RFID
module V3"), on the same two I2C wires, and the common **RFID-RC522** (NXP
MFRC522), on SPI. One reader is plugged in at a time, and Specter finds out by
itself which: it asks for the RFID Unit 2, then for the PN532, and only when
neither answers for the RC522. Cards are the same for all three: one written
with any reader loads on the others.

### Which reader to get

All three read the backup cards. They differ in what else they can do, in how
they are wired, and in how much of this has been tried on the board.

| | [M5Stack RFID Unit 2](https://docs.m5stack.com/en/unit/rfid2) | PN532 module V3 | RFID-RC522 module |
|---|---|---|---|
| Chip | WS1850S | [NXP PN532](https://www.nxp.com/products/rfid-nfc/nfc-hf/nfc-readers/nfc-integrated-solution:PN5321A3HN) | [NXP MFRC522](https://www.nxp.com/products/rfid-nfc/nfc-hf/nfc-readers/standard-performance-mifare-and-ntag-frontend:MFRC52202HN1) |
| Where to look | [M5Stack shop](https://shop.m5stack.com/products/rfid-unit-2-ws1850s) | [Elechouse manual](https://www.elechouse.com/elechouse/images/product/PN532_module_V3/PN532_%20Manual_V3.pdf); sold by many shops as "PN532 NFC RFID module V3" | sold by many shops as "RFID-RC522" |
| Price | the most expensive of the three | a few dollars | the cheapest |
| Wires | 4 (I2C, Grove cable) | 4 (I2C) | 7 (SPI) |
| Key backup on MIFARE / NTAG cards | **works** on the board | simulated only so far | cards are read on the board |
| Specter smartcard (JavaCard) | **works**, but the card has to sit on one exact spot | **works**, found at once | **does not work** |
| Time for the card to open its secure channel | about 1.0 s | about 0.3 s | - |
| Made by | one maker, one design | clones of one design; quality varies | clones; quality varies |

What was measured on the board, with the same smartcard on each:

- **PN532.** Twenty secure channel openings out of twenty at the first try,
  with no frame lost, and the card computing three times faster than on the
  RFID Unit 2 - a card that gets more power runs faster. One module, one card,
  one afternoon: it has had even less use than the rest.
- **RFID Unit 2.** Twenty out of twenty once the card is on the right spot. A
  little off, the card is found and then drops out in the middle of a command.
- **RC522.** The card wakes up and loses power as soon as it starts to answer:
  not one complete answer in over nine hundred tries, at any position and with
  the antenna drive at its maximum. A MIFARE card on the same module, in the
  same place, is read every time. A smartcard has a processor to feed, and
  this module's field is enough only for memory cards. Replacing the two
  antenna inductors is a known fix for these boards, reported by others and
  not tried here.

So: for the **smartcard**, a PN532. For the **key backup only**, any of the
three; the RC522 is the cheapest and the RFID Unit 2 the one most used here.

Both NXP chips are old parts that NXP no longer recommends for new designs,
and nearly every PN532 and RC522 module on sale is a clone. They are listed
here because they are cheap and easy to find, which suits a device meant to be
built at home.

### Wiring

#### RFID Unit 2 (I2C)

The reader goes on the board's I2C bus, next to the touch controller. It has a
Grove cable with four wires:

| RFID Unit 2 (Grove wire) | Board |
|---|---|
| SDA (yellow) | **GPIO7** |
| SCL (white) | **GPIO8** |
| VCC (red) | **3V3**, not 5 V |
| GND (black) | GND |

Power it from **3.3 V**. The Grove plug is usually fed with 5 V, but the reader
chip and the board both work at 3.3 V, and its data lines must not be pulled
above that.

The reader answers at I2C address `0x28`, which collides with nothing on the
board.

#### PN532 (I2C)

The module goes on the same two wires as the RFID Unit 2:

| PN532 module | Board |
|---|---|
| SDA | **GPIO7** |
| SCL | **GPIO8** |
| VCC | **3V3**, not 5 V |
| GND | GND |

Three things to get right:

- **Set the module's switch to I2C.** It usually ships set to serial, and then
  it does not answer at all. The positions are printed on the board and listed
  in the [manual](https://www.elechouse.com/elechouse/images/product/PN532_module_V3/PN532_%20Manual_V3.pdf).
  The module reads the switch when it powers up, so cut its power after
  changing it.
- **Power it from 3.3 V.** The module pulls SDA and SCL up to its own supply,
  and this bus is the one the touch controller and the camera are on.
- **Use the four pins on the side of the module,** marked GND, VCC, SDA and
  SCL. The longer row is for SPI, which this driver does not use.

The reader answers at I2C address `0x24`. If it is not found, **Test the
reader** in the NFC settings says so; with a multimeter, SDA and SCL should
both rest near 3.3 V.

#### RFID-RC522 (SPI)

> **Partly tried on the board.** The module is found and cards are read.
> Writing a card has only run against a simulated MFRC522. The Specter
> smartcard does not work with this module: see
> [which reader to get](#which-reader-to-get).

The module goes on the 40-pin header, by loose wires or through the
[TapHad](https://github.com/sandman21vs/TapHad) adapter board, which is passive: each
module pin goes straight to a header pin.

| RC522 pin | Board | Header pin (Raspberry Pi numbering) |
|---|---|---|
| SCK | **GPIO31** | 23 |
| MOSI | **GPIO29** | 19 |
| MISO | **GPIO30** | 21 |
| SDA / SS (chip select) | **GPIO50** | 33 |
| RST | **GPIO28** | 15 |
| IRQ | GPIO49, not used | 31 |
| 3.3V | **3V3**, not 5 V | 1 |
| GND | GND | 6, 14, 30 |

Mind the pin numbers. The table uses the Raspberry Pi numbering of the header.
Waveshare numbers the same header with the two rows swapped (their pin 1 is
5 V), and that is the numbering used in the
[smartcard hat rework](#the-hat-needs-a-small-rework) above. When in doubt, go
by the GPIO name.

The module is a 3.3 V part and has no level shifting: never feed it 5 V. The
IRQ pin may stay unconnected, the reader is polled.

If the reader is not found, copy
[`ports/esp32p4/test_nfc_rc522.py`](./ports/esp32p4/test_nfc_rc522.py) to the
board and run it from the [serial console](#serial-console-optional):

```bash
mpremote cp ports/esp32p4/test_nfc_rc522.py :/test_nfc_rc522.py
```

```bash
mpremote exec "import test_nfc_rc522; test_nfc_rc522.run()"
```

It says which step fails: the SPI bus, the reset line, the chip version, the
antenna drivers or the card. Clones of the MFRC522 report other version
numbers and are accepted.

### Turn it on

NFC is **off** after flashing. To turn it on:

1. **Device settings → Communication → NFC card reader**.
2. Tap **Test the reader**. It says whether the reader answers. Hold a card
   against it and it also says what kind of card it is and whether it is
   blank. The test only reads; it never writes to the card.
3. Tap **Enable NFC**.

If the test says no reader was found, check the wires. The test names the
reader it found, so it also tells which of the two it is talking to.

With NFC enabled and the reader plugged in, two entries appear:

- **Save:** Settings → *Save key to NFC card*, with a key loaded.
- **Load:** *Load key from NFC card*, on the first menu.

They disappear when the reader is unplugged or NFC is disabled. The antenna is
on only while a screen is asking for a card.

### Which cards to use

Use 13.56 MHz cards of one of these two families. Cards, key fobs, stickers and
rings all work the same; only the chip inside matters.

| Card | Works | Notes |
|---|---|---|
| **MIFARE Classic 1K** (S50) | yes | The cards and fobs sold with the reader. The usual choice |
| MIFARE Classic 4K (S70) | yes | Used like a 1K |
| **NTAG213 / NTAG215 / NTAG216** | yes | The common "NFC tag" stickers. NTAG213 is big enough |
| MIFARE Ultralight (48 bytes) | no | Too small: a backup needs 61 to 77 bytes |
| MIFARE DESFire, MIFARE Plus, NTAG 424 | no | Different protocol |
| Bank cards, passports, phones | no | Different protocol |
| 125 kHz fobs (EM4100, HID Prox) | no | Different frequency; the reader does not see them |

A card that is not supported is not written to. The screen says so when it
recognises one, such as a smartcard; anything else reads as if no card were
there.

Before you use a card:

- **Use a blank one.** Saving a key replaces whatever record is on the card.
  Specter asks first if it finds one.
- **A MIFARE Classic that was formatted by a phone has to be erased first.**
  Phones format cards as NDEF, which changes the card's keys, and then the
  card reads as unreadable. An NFC app's "erase" or "format" option puts the
  factory keys back.
- **A locked or password-protected tag cannot be written.**
- **A phone shows a Specter card as empty.** The record is not NDEF, so NFC
  apps do not recognise it. That says nothing about whether the save worked;
  load the card on the device to check.

### What is on the card

The 12 to 24 words are turned into their BIP39 entropy, sealed in a KEF
envelope (the Krux encryption format: AES-256-GCM, key from PBKDF2-HMAC-SHA256
with 100,000 rounds) and written as one record. The words never go over the
antenna, and nothing unencrypted is written.

The record layout and the envelope are the ones used by the NFC branches of
[Kern](https://github.com/sandman21vs/Kern/blob/nfc-card-storage/docs/nfc.md)
and [Krux](https://github.com/sandman21vs/krux), so a card written by one is
meant to load on the others. Specter reads every KEF version and writes
AES-GCM.

Things to know:

- **The password is the only protection.** The card answers any reader held
  near it, so anyone who gets the card can copy it and try passwords offline.
  Use a long one.
- **The passphrase is not on the card.** After loading, enter it again.
- **The backup ID** shown when saving and loading is the fingerprint of the key
  without a passphrase.

If **Test the reader** is not enough to find a problem, copy
[`ports/esp32p4/test_nfc.py`](./ports/esp32p4/test_nfc.py) to the board and run
`test_nfc.run()` from the serial console. It says which step fails: the bus,
the reader, the card or the record.

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
  the WS1850S driver (which the RC522 one builds on) and the KEF encryption
  format, translated to MicroPython
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
