"""A WS1850S and the cards in front of it, simulated for the NFC tests.

Two models, stacked the way the hardware is:

  SimChip   the reader as the driver sees it: an I2C device with the MFRC522
            register set - FIFO, command register, IRQ flags, CRC coprocessor.
  SimCard   a tag as the reader sees it: ISO14443A frames in, frames out.
            MIFARE Classic 1K or Ultralight/NTAG.

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
        reply = None
        if self.card is not None and self.field_on:
            reply = self.card.exchange(frame, tx_bits, self.crypto_on)
        if reply is None:
            self._irq(0x01)
            return
        data, rx_bits = reply
        self.fifo = bytearray(data[:FIFO_SIZE])
        self.reg[0x0C] = rx_bits
        self._irq(0x20)
