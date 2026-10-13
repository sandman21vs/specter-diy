"""A WS1850S and the cards in front of it, simulated for the NFC tests.

Two models, stacked the way the hardware is:

  SimChip   the reader as the driver sees it: an I2C device with the MFRC522
            register set - FIFO, command register, IRQ flags, CRC coprocessor.
  SimSpiBus the same register set behind the SPI framing of a real MFRC522,
            with its chip select and its reset pin.
  SimCard   a tag as the reader sees it: ISO14443A frames in, frames out.
            MIFARE Classic 1K or Ultralight/NTAG.
  IsoDepCard  a smartcard: the same selection, then RATS and ISO 14443-4
            blocks carrying APDUs to an applet.
  SimPN532  another reader: the firmware of a PN532 on I2C, with a SimChip
            as the radio inside it.

The driver and the tag layer run unmodified against them. The models keep the
behaviours the real parts punish mistakes with: a halted card ignores REQA, a
Classic sector answers nothing until authenticated, a frame sent in the clear
to an authenticated card is ignored, and so is anything sent with the reader's
crypto still on to a card that is not.
"""

FIFO_SIZE = 64


def crc_a(data):
    crc = 0x6363
    for byte in data:
        byte ^= crc & 0xFF
        byte = (byte ^ (byte << 4)) & 0xFF
        crc = (crc >> 8) ^ (byte << 8) ^ (byte << 3) ^ (byte >> 4)
    crc &= 0xFFFF
    return bytes([crc & 0xFF, crc >> 8])


ACK = (b"\x0a", 4)
NAK = (b"\x04", 4)


class SimCard:
    """One ISO14443A tag. Frames carry their CRC_A where the standard says so."""

    def __init__(self, uid=b"\x04\xa1\xb2\xc3", sak=0x08, key=b"\xff" * 6,
                 ul_pages=None, ul_cc=None):
        self.uid = bytes(uid)
        self.sak = sak
        self.key = key
        self.classic = ul_pages is None
        if self.classic:
            self.blocks = [bytearray(16) for _ in range(64)]
            self.blocks[0][:4] = self.uid[:4]
            for trailer in range(3, 64, 4):
                self.blocks[trailer][:] = b"\xff" * 6 + b"\xff\x07\x80\x69" + b"\xff" * 6
        else:
            self.pages = [bytearray(4) for _ in range(ul_pages)]
            if ul_cc is not None:
                self.pages[3][:] = ul_cc
        self.reset()
        self.writes = []  # physical blocks or pages written, in order

    def reset(self):
        """Field dropped"""
        self.halted = False
        self.selected = False
        self.authed = None  # sector with an open crypto1 session
        self.pending_write = None

    # ----- what the reader's crypto1 engine asks -----

    def authenticate(self, block, key, uid_tail):
        if not self.classic or not self.selected:
            return False
        if key != self.key or uid_tail != self.uid[-4:]:
            # a failed authentication drops the card out of the active state
            self.selected = False
            self.authed = None
            return False
        self.authed = block // 4
        return True

    # ----- frames -----

    def _levels(self):
        if len(self.uid) == 4:
            return [self.uid]
        return [b"\x88" + self.uid[:3], self.uid[3:]]

    def exchange(self, frame, bits, encrypted):
        """Returns (reply, valid_bits_of_last_byte) or None for silence"""
        frame = bytes(frame)
        # The two ends must agree on whether crypto1 is running
        if encrypted != (self.authed is not None):
            return None

        if bits == 7 and len(frame) == 1:
            if frame[0] == 0x52 or (frame[0] == 0x26 and not self.halted):
                self.reset()
                return b"\x44\x00" if len(self.uid) == 7 else b"\x04\x00", 0
            return None
        if bits:
            return None

        if self.pending_write is not None:
            block, self.pending_write = self.pending_write, None
            if len(frame) != 18 or crc_a(frame[:16]) != frame[16:]:
                return NAK
            self.blocks[block][:] = frame[:16]
            self.writes.append(block)
            return ACK

        levels = self._levels()
        for level, sel in enumerate((0x93, 0x95)):
            if frame[0] != sel or level >= len(levels):
                continue
            part = levels[level]
            bcc = part[0] ^ part[1] ^ part[2] ^ part[3]
            if frame == bytes([sel, 0x20]):
                return part + bytes([bcc]), 0
            if frame[:2] == bytes([sel, 0x70]) and len(frame) == 9:
                if frame[2:7] != part + bytes([bcc]) or crc_a(frame[:7]) != frame[7:]:
                    return None
                last = level == len(levels) - 1
                self.selected = last
                sak = self.sak if last else 0x04
                return bytes([sak]) + crc_a(bytes([sak])), 0
            return None

        if not self.selected or len(frame) < 4 or crc_a(frame[:-2]) != frame[-2:]:
            return None
        cmd, arg = frame[0], frame[1]

        if cmd == 0x50:  # HALT
            self.reset()
            self.halted = True
            return None
        if cmd == 0x30:  # READ
            if self.classic:
                if self.authed != arg // 4 or arg >= 64:
                    return NAK
                data = bytes(self.blocks[arg])
            else:
                if arg >= len(self.pages):
                    return NAK
                # four pages, rolling over to page 0 past the end
                data = b"".join(
                    bytes(self.pages[(arg + i) % len(self.pages)]) for i in range(4)
                )
            return data + crc_a(data), 0
        if cmd == 0xA0 and self.classic:  # WRITE, first of two frames
            if self.authed != arg // 4 or arg >= 64:
                return NAK
            self.pending_write = arg
            return ACK
        if cmd == 0xA2 and not self.classic and len(frame) == 8:
            if arg >= len(self.pages):
                return NAK
            self.pages[arg][:] = frame[2:6]
            self.writes.append(arg)
            return ACK
        return NAK


class IsoDepCard(SimCard):
    """An ISO 14443-4 card (SAK 0x20) running one applet.

    applet is called with each complete command APDU and returns the response
    APDU. The knobs model what a real card does to a reader: ask for more time,
    chain a long answer, lose a frame, or brown out in the middle of a command.
    tamper, when set, rewrites every protocol block on its way out (CRC is
    added after it) and is how the tests build a hostile card.
    """

    def __init__(self, applet, uid=b"\x04\x3e\x19\x6a\xa6\x6e\x80",
                 ats=b"\x05\x78\x80\x70\x02", inf_size=61):
        self.applet = applet
        self.ats = bytes(ats)
        self.inf_size = inf_size  # largest INF the card puts in one I-block
        self.wtx = 0  # S(WTX) requests before each answer
        self.wtxm = 1
        self.mute = 0  # protocol frames to swallow, as if lost in the air
        self.brownout_ins = None  # INS that makes the card lose power
        self.tamper = None
        self.apdus = []  # complete command APDUs received, in order
        super().__init__(uid=uid, sak=0x20)

    def reset(self):
        super().reset()
        # the applet loses its RAM with the field
        if hasattr(self.applet, "reset"):
            self.applet.reset()
        self.protocol = False
        self.bn = 1
        self.rx = b""
        self.tx = b""
        self.wtx_left = 0
        self.last = None

    def exchange(self, frame, bits, encrypted):
        frame = bytes(frame)
        if not self.protocol:
            if (self.selected and not bits and len(frame) == 4
                    and frame[0] == 0xE0 and crc_a(frame[:2]) == frame[2:]):
                self.protocol = True
                self.fsd = (16, 24, 32, 40, 48, 64, 96, 128, 256)[min(frame[1] >> 4, 8)]
                return self.ats + crc_a(self.ats), 0
            return super().exchange(frame, bits, encrypted)

        # In the protocol state the card ignores WUPA and anything malformed
        if bits or len(frame) < 3 or crc_a(frame[:-2]) != frame[-2:]:
            return None
        if self.mute:
            self.mute -= 1
            return None
        reply = self._block(frame[0], frame[1:-2])
        if reply is None:
            return None
        if self.tamper is not None:
            reply = self.tamper(reply)
            if reply is None:
                return None
        return reply + crc_a(reply), 0

    def _send(self):
        """The next block of the answer: a WTX request or an I-block"""
        if self.wtx_left:
            self.wtx_left -= 1
            return bytes([0xF2, self.wtxm])
        size = min(self.inf_size, self.fsd - 3)
        chunk, self.tx = self.tx[:size], self.tx[size:]
        self.last = bytes([0x02 | (0x10 if self.tx else 0) | self.bn]) + chunk
        return self.last

    def _block(self, pcb, inf):
        if pcb == 0xC2:  # S(DESELECT)
            self.reset()
            self.halted = True
            return b"\xC2"
        if pcb == 0xF2:  # S(WTX) response
            return self._send()
        if pcb & 0xE6 == 0xA2:  # R-block
            if pcb & 0x10:  # NAK
                if pcb & 1 == self.bn:
                    return self.last
                return bytes([0xA2 | self.bn])
            if pcb & 1 == self.bn:
                return self.last
            self.bn ^= 1
            return self._send()
        if pcb & 0xE6 == 0x02:  # I-block
            self.bn ^= 1
            self.rx += inf
            if pcb & 0x10:
                self.last = bytes([0xA2 | self.bn])
                return self.last
            apdu, self.rx = self.rx, b""
            self.apdus.append(apdu)
            if self.brownout_ins is not None and len(apdu) > 1 and apdu[1] == self.brownout_ins:
                # Supply collapsed mid-command: back to idle, nothing sent
                self.reset()
                return None
            self.tx = bytes(self.applet(apdu))
            self.wtx_left = self.wtx
            return self._send()
        return None


class SimChip:
    """The reader, as an I2C device. Has the shape of machine.I2C."""

    ADDR = 0x28

    def __init__(self, card=None):
        self.card = card
        self.present = True  # False: nothing ACKs on the bus
        self.reg = {}
        self.fifo = bytearray()
        self.pointer = 0
        self.transfers = 0

    # ----- the bus -----

    def _check(self, addr):
        self.transfers += 1
        if not self.present or addr != self.ADDR:
            raise OSError("i2c nack")

    def writeto(self, addr, data):
        self._check(addr)
        data = bytes(data)
        self.pointer = data[0]
        for value in data[1:]:
            self._write(self.pointer, value)

    def readfrom(self, addr, length):
        self._check(addr)
        return bytes(self._read(self.pointer) for _ in range(length))

    # ----- registers -----

    @property
    def field_on(self):
        return self.reg.get(0x14, 0) & 0x03 == 0x03

    @property
    def crypto_on(self):
        return bool(self.reg.get(0x08, 0) & 0x08)

    def _read(self, reg):
        if reg == 0x09:
            if not self.fifo:
                return 0
            value, self.fifo = self.fifo[0], self.fifo[1:]
            return value
        if reg == 0x0A:
            return len(self.fifo)
        return self.reg.get(reg, 0)

    def _write(self, reg, value):
        if reg == 0x09:
            if len(self.fifo) < FIFO_SIZE:
                self.fifo.append(value)
        elif reg == 0x0A:
            if value & 0x80:
                self.fifo = bytearray()
        elif reg in (0x04, 0x05):
            # bit 7 clear: the marked flags are cleared
            if not value & 0x80:
                self.reg[reg] = self.reg.get(reg, 0) & ~value & 0x7F
        elif reg == 0x01:
            self._command(value & 0x0F)
        elif reg == 0x0D:
            self.reg[reg] = value
            if value & 0x80 and self.reg.get(0x01) == 0x0C:
                self._transceive(value & 0x07)
        elif reg == 0x14:
            was_on = self.field_on
            self.reg[reg] = value
            if was_on and not self.field_on and self.card:
                self.card.reset()
        else:
            self.reg[reg] = value

    def _irq(self, flag):
        self.reg[0x04] = self.reg.get(0x04, 0) | flag

    def _command(self, command):
        self.reg[0x01] = command
        if command == 0x0F:  # soft reset
            self.reg = {0x01: 0x00}
            self.fifo = bytearray()
            if self.card:
                self.card.reset()
        elif command == 0x03:  # CalcCRC
            crc = crc_a(self.fifo)
            self.fifo = bytearray()
            self.reg[0x22], self.reg[0x21] = crc[0], crc[1]
            self.reg[0x05] = self.reg.get(0x05, 0) | 0x04
        elif command == 0x0E:  # MFAuthent
            frame, self.fifo = bytes(self.fifo), bytearray()
            ok = (
                len(frame) == 12
                and frame[0] == 0x60
                and self.card is not None
                and self.field_on
                and self.card.authenticate(frame[1], frame[2:8], frame[8:12])
            )
            if ok:
                self.reg[0x08] = self.reg.get(0x08, 0) | 0x08
                self._irq(0x10)
            else:
                self._irq(0x01)  # the timer runs out

    def _transceive(self, tx_bits):
        frame, self.fifo = bytes(self.fifo), bytearray()
        self.reg[0x06] = 0  # ErrorReg is cleared when a command starts
        reply = None
        if self.card is not None and self.field_on:
            reply = self.card.exchange(frame, tx_bits, self.crypto_on)
        if reply is None:
            self._irq(0x01)
            return
        data, rx_bits = reply
        self.fifo = bytearray(data[:FIFO_SIZE])
        if len(data) > FIFO_SIZE:
            self.reg[0x06] = 0x10  # BufferOvfl
        self.reg[0x0C] = rx_bits
        self._irq(0x20)


class SimPN532:
    """A PN532 on I2C: its firmware's command frames in front of a SimChip.

    The radio inside a PN532 (the CIU) is a PN512, with the register map of
    the MFRC522 moved to 0x63xx, so the SimChip plays that part and this class
    plays the firmware: frames with their checksums, the ACK, the status byte
    that says whether there is anything to read, and the handful of commands
    the driver uses.

    Has the shape of machine.I2C. Anything else asked of it - card, present,
    field_on, transfers - is the chip's, so a test can hold either.
    """

    ADDR = 0x24
    ACK = b"\x00\x00\xff\x00\xff\x00"

    def __init__(self, chip=None, ic=0x32, busy=0):
        self.chip = SimChip() if chip is None else chip
        self.ic = ic
        self.busy = busy  # status reads that say "not yet" before each frame
        self.waits = 0
        self.pending = []  # frames waiting to be read, oldest first
        self.commands = []  # (command, params) as received
        self.timeout_code = 0x0A
        self.thru_timeouts = []  # timeout code in force at each frame sent
        self.parameters = 0x14
        self.mangle = None  # rewrites a reply frame on its way out

    def __getattr__(self, name):
        return getattr(self.chip, name)

    # ----- the bus -----

    def _check(self, addr):
        self.chip.transfers += 1
        if not self.chip.present or addr != self.ADDR:
            raise OSError("i2c nack")

    def writeto(self, addr, data):
        self._check(addr)
        data = bytes(data)
        # a new command drops whatever was not read
        self.pending = []
        if len(data) < 8 or data[:3] != b"\x00\x00\xff":
            return
        length = data[3]
        if (length + data[4]) & 0xFF or len(data) != length + 7:
            return
        body = data[5 : 5 + length]
        if (sum(body) + data[5 + length]) & 0xFF or body[0] != 0xD4:
            return
        self.commands.append((body[1], body[2:]))
        reply = self._firmware(body[1], body[2:])
        if reply is None:
            frame = b"\x00\x00\xff\x01\xff\x7f\x81\x00"
        else:
            reply = bytes([0xD5, body[1] + 1]) + bytes(reply)
            frame = (
                b"\x00\x00\xff"
                + bytes([len(reply), (-len(reply)) & 0xFF])
                + reply
                + bytes([(-sum(reply)) & 0xFF, 0x00])
            )
        if self.mangle is not None:
            frame = self.mangle(frame)
        self.pending = [self.ACK, frame]
        self.waits = self.busy

    def readfrom(self, addr, length):
        self._check(addr)
        if not self.pending:
            return bytes(length)
        if self.waits:
            self.waits -= 1
            return bytes(length)
        if length == 1:
            return b"\x01"
        # A frame is read in one go; what does not fit is lost
        frame = b"\x01" + self.pending.pop(0)
        self.waits = self.busy
        return (frame + bytes(length))[:length]

    # ----- the firmware -----

    @staticmethod
    def _reg(address):
        """CIU address to the MFRC522 register number it mirrors"""
        return ((address & 0xFF) + 0x10) & 0x3F

    def _firmware(self, command, params):
        chip = self.chip
        if command == 0x02:  # GetFirmwareVersion
            return bytes([self.ic, 0x01, 0x06, 0x07])
        if command == 0x14:  # SAMConfiguration
            return b""
        if command == 0x12 and len(params) == 1:  # SetParameters
            self.parameters = params[0]
            return b""
        if command == 0x32 and params:  # RFConfiguration
            if params[0] == 0x01 and len(params) == 2:
                tx = chip.reg.get(0x14, 0x80)
                chip._write(0x14, tx | 0x03 if params[1] & 1 else tx & ~0x03 & 0xFF)
                # no field, no power: whatever state the card was in is gone
                if not params[1] & 1 and chip.card:
                    chip.card.reset()
            elif params[0] == 0x02 and len(params) == 4:
                self.timeout_code = params[3]
            return b""
        if command == 0x06 and params and len(params) % 2 == 0:  # ReadRegister
            return bytes(
                chip._read(self._reg(params[i + 1])) for i in range(0, len(params), 2)
            )
        if command == 0x08 and params and len(params) % 3 == 0:  # WriteRegister
            for i in range(0, len(params), 3):
                chip._write(self._reg(params[i + 1]), params[i + 2])
            return b""
        if command == 0x42 and params:  # InCommunicateThru
            return self._thru(params)
        return None

    def _thru(self, frame):
        chip = self.chip
        self.thru_timeouts.append(self.timeout_code)
        tx_bits = chip.reg.get(0x0D, 0) & 0x07
        if chip.reg.get(0x12, 0) & 0x80:  # TxCRCEn
            frame += crc_a(frame)
        reply = None
        if chip.card is not None and chip.field_on:
            reply = chip.card.exchange(frame, tx_bits, chip.crypto_on)
        if reply is None:
            return b"\x01"  # timeout
        data, rx_bits = reply
        if chip.reg.get(0x13, 0) & 0x80:  # RxCRCEn
            if len(data) < 3 or crc_a(data[:-2]) != data[-2:]:
                return b"\x02"
            data = data[:-2]
        chip.reg[0x0C] = (chip.reg.get(0x0C, 0) & ~0x07) | rx_bits
        return b"\x00" + bytes(data)


class SimPin:
    """One output pin. Has the shape of machine.Pin in what the driver uses."""

    def __init__(self, level, on_change=None):
        self.level = level
        self.on_change = on_change

    def value(self, level=None):
        if level is None:
            return self.level
        level = 1 if level else 0
        changed, self.level = level != self.level, level
        if changed and self.on_change:
            self.on_change(level)


class SimSpiBus:
    """An RC522 module on SPI: a SimChip behind MFRC522 framing.

    Has the shape of the bus the driver opens on the board: spi with
    write_readinto(), cs and rst. It holds the chip to what the real part
    does - nothing is heard with the select high or the reset low, the first
    byte after the select falls is the address, a reset wipes every register
    and the oscillator takes a while to come back.

    chip.present False is an empty header: MISO floats, and every byte read is
    `idle`.
    """

    def __init__(self, chip=None, version=0x92, wake=0, idle=0x00):
        self.chip = SimChip() if chip is None else chip
        self.version = version
        self.wake = wake  # transfers ignored while the oscillator starts
        self.idle = idle
        self.spi = self
        self.cs = SimPin(1, self._select)
        self.rst = SimPin(0, self._reset)
        self.asleep = 0
        self.fresh = False
        self.resets = 0
        self.frames = []  # every MOSI frame, as sent

    def _select(self, level):
        self.fresh = not level

    def _reset(self, level):
        chip = self.chip
        if level:
            self.asleep = self.wake
            return
        # NRSTPD low: everything back to the reset state, the antenna with it
        self.resets += 1
        chip.reg = {}
        chip.fifo = bytearray()
        if chip.card:
            chip.card.reset()

    def write_readinto(self, tx, rx):
        chip = self.chip
        chip.transfers += 1
        tx = bytes(tx)
        self.frames.append(tx)
        if len(rx) != len(tx):
            raise ValueError("buffers must be the same length")
        for i in range(len(rx)):
            rx[i] = self.idle
        if self.cs.level or not self.fresh:
            # no select, or a second transfer under the same one: the chip
            # would take its first byte for data
            raise AssertionError("SPI transfer outside one chip select")
        self.fresh = False
        if not chip.present or not self.rst.level:
            return
        if self.asleep:
            self.asleep -= 1
            return

        if tx[0] & 0x01:
            raise AssertionError("address byte with bit 0 set")
        reg = (tx[0] >> 1) & 0x3F
        if not tx[0] & 0x80:
            rx[0] = 0
            for i, value in enumerate(tx[1:], 1):
                rx[i] = 0
                chip._write(reg, value)
            return
        # Read: each byte sent addresses the next byte received
        rx[0] = 0
        for i in range(1, len(tx)):
            reg = (tx[i - 1] >> 1) & 0x3F
            rx[i] = self.version if reg == 0x37 else chip._read(reg)
        if tx[-1] != 0:
            raise AssertionError("read not closed with a zero byte")
