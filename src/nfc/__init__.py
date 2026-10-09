"""NFC card storage - typed records on ISO14443A tags.

A card holds one record, tagged with what it is. The only record written here
is a KEF envelope holding a seed; see kef.py for the envelope and seed.py for
the two flows that use it.

Tags: MIFARE Classic 1K/4K and Ultralight/NTAG21x, presented to callers as one
flat byte array. Classic offsets skip block 0 and every sector trailer;
Ultralight offsets start at page 4.

This module is the tag layer and the record layer. It knows how to wake a card,
select it, walk its blocks and read a record out of them, and it knows none of
that in terms of any particular chip - the reader behind NFC.reader answers the
calls in the Reader class and nothing more is asked of it.

A card is input where a stranger picked every byte, and it only has to be held
near the device. Every layer here refuses anything that is not exactly what
this firmware writes.

Record layout - 16 byte header at linear offset 0, payload right after:

    0..3    magic "KRN1"
    4       record type
    5       reserved, must be zero
    6..7    payload length, big endian
    8..15   reserved, must be zero

The layout comes from the Kern NFC branch and is shared with the Krux fork that
writes the same cards, so a card written by any of the three reads on the
others. There is no checksum: the KEF envelope is authenticated, so a half
written or decaying card fails to decrypt.

The protocol and the validation are a translation of odudex/Kern
components/nfc (branch nfc-card-storage) and of krux/nfc.py, both MIT.
"""


class NFCError(Exception):
    """Any NFC failure. The screens turn it into one short message, because
    the exact reason a hostile card was refused is not for the display."""


class NFCNotFound(NFCError):
    """No reader on the bus, no acceptable tag, or no record on the tag"""


class NFCSizeError(NFCError):
    """A reply did not fit its buffer, or a payload does not fit the tag"""


# No frame may exceed the reader FIFO. A CRC_A is two bytes, and receive
# buffers must have room for it, because the CRC arrives with the frame.
FIFO_SIZE = 64
CRC_LEN = 2

HEADER_LEN = 16
RECORD_MAGIC = b"KRN1"
RECORD_KEF = 1  # KEF envelope holding a seed
# Reserved, not free: Kern and Krux write these, so a card carrying one has a
# meaning that must not be reassigned here.
RECORD_DESCRIPTOR = 2
RECORD_DATUM = 3
RECORD_XPUB = 4
KNOWN_RECORD_TYPES = (RECORD_KEF, RECORD_DESCRIPTOR, RECORD_DATUM, RECORD_XPUB)

# Largest payload read off a card, whatever the card claims to hold. A KEF
# wrapped 24 word seed is under 100 bytes; the ceiling stops a hostile tag from
# driving a large allocation.
MAX_PAYLOAD = 704
# Nothing larger than one record is ever addressable, whatever a tag claims.
MAX_CAPACITY = MAX_PAYLOAD + HEADER_LEN

# PICC commands
CMD_WUPA = 0x52
CMD_HALT = 0x50
CMD_SEL_CL1 = 0x93
CMD_SEL_CL2 = 0x95
CMD_READ = 0x30
CMD_MF_WRITE = 0xA0
CMD_UL_WRITE = 0xA2

# SAK values accepted. Anything else reads as an empty field: the point is to
# talk only to what we know how to talk to. 0x88 is 1K silicon from a second
# source; 4K tags are addressed as 1K, because their upper sectors have a
# different layout and a seed needs a fraction of the first sixteen anyway.
SAK_CLASSIC = (0x08, 0x18, 0x88)
SAK_ULTRALIGHT = 0x00
SAK_CASCADE_BIT = 0x04
CASCADE_TAG = 0x88

CLASSIC = 1
ULTRALIGHT = 2  # Ultralight and NTAG21x

# MIFARE Classic geometry
MF_BLOCK_SIZE = 16
MF_DATA_BLOCKS = 47  # 64 blocks less block 0 and 16 sector trailers

# Ultralight / NTAG geometry
UL_PAGE_SIZE = 4
UL_DATA_FIRST_PAGE = 4
UL_CC_PAGE = 3
UL_MIN_CAPACITY = 48  # plain Ultralight, pages 4..15
UL_MAX_CAPACITY = 888  # NTAG216 user memory

# A READ answers with sixteen bytes on both families: one block of a Classic,
# four pages of an Ultralight.
READ_LEN = 16


class Reader:
    """What the tag layer needs from a reader chip, and nothing else.

    The boundary is drawn at frames rather than at anything electrical, so
    above this line nothing knows which chip is present and below it nothing
    knows what a record is.

    Subclasses implement init, deinit, field, transceive, calc_crc,
    authenticate and clear_crypto, and keep `ready` up to date.
    """

    ready = False

    def init(self):
        """Brings the chip up with the field off. Idempotent."""
        raise NotImplementedError

    def deinit(self):
        """Drops the field and releases the bus"""
        raise NotImplementedError

    def field(self, on):
        """Energizes or drops the RF antenna"""
        raise NotImplementedError

    def transceive(self, send, tx_last_bits=0, recv_size=0):
        """Exchanges one frame, returning (reply, rx_last_bits).

        recv_size is the largest reply accepted; a longer one must be refused
        rather than truncated. 0 means no reply is expected.
        """
        raise NotImplementedError

    def calc_crc(self, data):
        """Computes a CRC_A over data"""
        raise NotImplementedError

    def authenticate(self, uid, block):
        """Opens a MIFARE crypto1 session on the sector holding block"""
        raise NotImplementedError

    def clear_crypto(self):
        """Drops an open crypto1 session. Never raises."""
        raise NotImplementedError

    def transceive_crc(self, send, recv_size):
        """Appends a CRC_A and verifies the one on the reply, stripping it.

        recv_size must cover the payload plus CRC_LEN - the CRC arrives as part
        of the frame, and an undersized buffer reads as an oversized reply.
        """
        if not send or len(send) + CRC_LEN > FIFO_SIZE or recv_size < CRC_LEN:
            raise NFCSizeError("Bad frame")

        reply, _ = self.transceive(bytes(send) + self.calc_crc(send), 0, recv_size)
        # A reply carrying a CRC_A is at least three bytes
        if len(reply) < 3:
            raise NFCError("Malformed reply")
        if self.calc_crc(reply[:-2]) != reply[-2:]:
            raise NFCError("Bad CRC")
        return reply[:-2]


def parse_header(header, capacity, record_type=None):
    """Validates a header read off a tag, returns the payload length.

    capacity is the tag's usable linear byte count including the header, so the
    declared length is checked against what the card can physically hold as
    well as against the ceiling.

    record_type None accepts any known type, which is what "is there already a
    record here" has to ask before overwriting one. A caller about to parse the
    payload names the type it can parse, and anything else reads as no record:
    a type buys a parser, never a permission.
    """
    if len(header) < HEADER_LEN or bytes(header[:4]) != RECORD_MAGIC:
        raise NFCNotFound("No record")

    wanted = KNOWN_RECORD_TYPES if record_type is None else (record_type,)
    if header[4] not in KNOWN_RECORD_TYPES or header[4] not in wanted:
        raise NFCNotFound("No record")

    # Reserved bytes must be zero: it denies the field as a covert channel and
    # stops stale bytes from silently acquiring meaning in a later version.
    if header[5] != 0 or any(header[8:HEADER_LEN]):
        raise NFCNotFound("No record")

    # A number a stranger picked. Bound it before it sizes an allocation.
    length = (header[6] << 8) | header[7]
    if capacity < HEADER_LEN or not 0 < length <= min(MAX_PAYLOAD, capacity - HEADER_LEN):
        raise NFCSizeError("Invalid record length")
    return length


def build_header(length, capacity, record_type=RECORD_KEF):
    """Serializes the header for a payload about to be written"""
    if record_type not in KNOWN_RECORD_TYPES:
        raise NFCError("Unknown record type")
    if capacity < HEADER_LEN or not 0 < length <= min(MAX_PAYLOAD, capacity - HEADER_LEN):
        raise NFCSizeError("Record does not fit the card")
    header = bytearray(HEADER_LEN)
    header[0:4] = RECORD_MAGIC
    header[4] = record_type
    header[6] = length >> 8
    header[7] = length & 0xFF
    return bytes(header)


# The reader driver is board specific and lives with the port, so a build for
# a board without one simply has no NFC. None until the first look, then the
# module, or False so a missing driver is not searched for at every menu.
_driver = None


def _load_driver():
    global _driver
    if _driver is None:
        try:
            import nfc_ws1850s

            _driver = nfc_ws1850s
        except ImportError:
            _driver = False
    return _driver


def open_reader():
    """Builds the reader for this board, or raises NFCNotFound"""
    driver = _load_driver()
    if not driver:
        raise NFCNotFound("No NFC reader on this board")
    return driver.WS1850S()


def is_available():
    """True when a reader answers right now.

    The reader is an external module. Unplugged, every NFC entry disappears
    from the menus and nothing in here runs.
    """
    driver = _load_driver()
    if not driver:
        return False
    try:
        return bool(driver.is_present())
    except Exception:
        return False


class Tag:
    """A selected card"""

    def __init__(self, uid, kind, capacity):
        self.uid = uid
        self.kind = kind
        # usable linear bytes, header included
        self.capacity = capacity


class NFC:
    """Tag layer and record I/O over any reader"""

    def __init__(self, reader=None):
        self.reader = open_reader() if reader is None else reader
        self.authed_sector = None
        self.tag = None

    # ---------- Lifecycle ----------

    def init(self):
        """Brings the reader up with the field off. Idempotent."""
        self.reader.init()

    def deinit(self):
        """Releases the tag and the reader, field off first. Never raises."""
        try:
            if self.reader.ready:
                self.release()
            self.reader.deinit()
        except NFCError:
            pass

    def field(self, on):
        """Energizes or drops the RF antenna.

        Off after init(); it is turned on only for as long as a card is
        actively being looked for.
        """
        if not self.reader.ready:
            raise NFCError("Reader not ready")
        if not on:
            self.release()
        self.reader.field(on)

    # ---------- Selection ----------

    def _cascade(self, sel_cmd):
        """Runs one anticollision and select level, returning (uid, sak).

        No collision resolution: a single card is asked for, and two cards in
        the field read as nothing there until one is taken away.
        """
        reply, _ = self.reader.transceive(bytes([sel_cmd, 0x20]), 0, 5)
        if len(reply) != 5:
            raise NFCError("Bad anticollision")
        # BCC is a plain XOR check. A mismatch means a malformed frame, so stop
        # rather than build a UID out of it.
        if reply[0] ^ reply[1] ^ reply[2] ^ reply[3] != reply[4]:
            raise NFCError("Bad BCC")

        sak = self.reader.transceive_crc(bytes([sel_cmd, 0x70]) + reply, 1 + CRC_LEN)
        if len(sak) != 1:
            raise NFCError("Bad SAK")
        return bytes(reply[:4]), sak[0]

    def _ultralight_capacity(self):
        """Usable bytes of an Ultralight or NTAG, from its capability container"""
        pages = self.reader.transceive_crc(
            bytes([CMD_READ, UL_CC_PAGE]), READ_LEN + CRC_LEN
        )
        if len(pages) != READ_LEN:
            raise NFCError("Bad page read")
        # pages[2] is the size byte, written by whoever held the tag last. Take
        # it only when the NFC Forum magic byte is there, and clamp it at both
        # ends regardless.
        capacity = UL_MIN_CAPACITY
        if pages[0] == 0xE1 and pages[2] > 0:
            capacity = pages[2] * 8
        return max(UL_MIN_CAPACITY, min(capacity, UL_MAX_CAPACITY))

    def poll(self):
        """Wakes, identifies and selects one tag.

        Raises NFCNotFound when the field is empty, holds more than one tag, or
        holds a family that is not accepted.
        """
        if not self.reader.ready:
            raise NFCError("Reader not ready")
        self.release()

        try:
            # WUPA rather than REQA, as a 7 bit frame: release() just halted
            # whatever was there, and a halted tag answers WUPA but ignores
            # REQA, which would make the card unselectable while it stays in
            # the field.
            atqa, _ = self.reader.transceive(bytes([CMD_WUPA]), 7, 2)
            if len(atqa) != 2:
                raise NFCError("Bad ATQA")

            uid, sak = self._cascade(CMD_SEL_CL1)
            if sak & SAK_CASCADE_BIT:
                # Double size UID: the first byte of level 1 is the cascade
                # tag, not UID data. Ten byte UIDs are refused, not guessed at.
                if uid[0] != CASCADE_TAG:
                    raise NFCError("Unsupported UID")
                head = uid[1:4]
                uid, sak = self._cascade(CMD_SEL_CL2)
                if sak & SAK_CASCADE_BIT:
                    raise NFCError("Unsupported UID")
                uid = head + uid

            if sak in SAK_CLASSIC:
                kind = CLASSIC
                capacity = MF_DATA_BLOCKS * MF_BLOCK_SIZE
            elif sak == SAK_ULTRALIGHT:
                kind = ULTRALIGHT
                capacity = self._ultralight_capacity()
            else:
                raise NFCError("Unsupported card")
        except NFCError:
            self.release()
            raise NFCNotFound("No card")

        self.tag = Tag(uid, kind, min(capacity, MAX_CAPACITY))
        return self.tag

    def release(self):
        """Halts the tag and drops any crypto1 session. Safe to call always."""
        self.authed_sector = None
        self.tag = None
        if not self.reader.ready:
            return
        # HALT goes out before crypto is dropped: while a sector is
        # authenticated the reader enciphers the frame, and a plaintext HALT
        # would be ignored, leaving the tag awake in a state it thinks is still
        # authenticated. HALT draws no reply, so a timeout is the success case.
        try:
            halt = bytes([CMD_HALT, 0x00])
            self.reader.transceive(halt + self.reader.calc_crc(halt))
        except NFCError:
            pass
        self.reader.clear_crypto()

    # ---------- Linear addressing ----------

    @staticmethod
    def _block(index):
        """Maps a data block index onto a physical MIFARE Classic block.

        Skips the manufacturer block and every sector trailer: sector 0
        contributes two data blocks, every later sector three. A corrupted
        trailer bricks its sector permanently, so the result is re-checked - it
        catches a future edit to the arithmetic before it destroys a card.
        """
        if not 0 <= index < MF_DATA_BLOCKS:
            raise NFCSizeError("Block out of range")
        rest = index - 2
        block = index + 1 if index < 2 else (rest // 3 + 1) * 4 + rest % 3
        if block == 0 or block % 4 == 3:
            raise NFCError("Refusing to touch a sector trailer")
        return block

    def _authenticate(self, block):
        """Authenticates a Classic sector with the factory key A, once per sector.

        The protection is the KEF password, not the sector key: the card stays
        readable by any reader, and what a reader finds is ciphertext.
        """
        sector = block // 4
        if sector == self.authed_sector:
            return
        self.authed_sector = None
        self.reader.authenticate(self.tag.uid, block)
        self.authed_sector = sector

    def _ack(self, data):
        """Sends one frame and requires a 4 bit ACK back.

        An ACK is exactly one nibble holding 0x0A; anything else - a NAK, a
        full byte, a longer frame - is a failed write, not a partial success.
        """
        if len(data) + CRC_LEN > FIFO_SIZE:
            raise NFCSizeError("Frame too long")
        reply, valid_bits = self.reader.transceive(
            bytes(data) + self.reader.calc_crc(data), 0, 1
        )
        if len(reply) != 1 or valid_bits != 4 or reply[0] & 0x0F != 0x0A:
            raise NFCError("Write not acknowledged")

    def _check_range(self, offset, length):
        """Refuses a range that falls outside the selected tag"""
        if self.tag is None:
            raise NFCError("No tag selected")
        capacity = self.tag.capacity
        if length <= 0 or offset < 0 or offset > capacity or length > capacity - offset:
            raise NFCSizeError("Range outside tag")

    def read(self, offset, length):
        """Reads bytes at a linear offset, spanning blocks as needed"""
        self._check_range(offset, length)
        classic = self.tag.kind == CLASSIC

        out = bytearray()
        while len(out) < length:
            pos = offset + len(out)
            aligned = pos - pos % READ_LEN
            skip = pos - aligned
            take = min(READ_LEN - skip, length - len(out))

            if classic:
                unit = self._block(aligned // MF_BLOCK_SIZE)
                self._authenticate(unit)
            else:
                unit = UL_DATA_FIRST_PAGE + aligned // UL_PAGE_SIZE

            # The data arrives with its CRC_A attached; the buffer has to hold
            # both or the reply reads as oversized.
            data = self.reader.transceive_crc(bytes([CMD_READ, unit]), READ_LEN + CRC_LEN)
            if len(data) != READ_LEN:
                raise NFCError("Bad block read")
            out += data[skip : skip + take]
        return bytes(out)

    def write(self, offset, data):
        """Writes data at a unit aligned linear offset, zero padding the tail.

        The unit is a 16 byte block on a Classic and a 4 byte page on an
        Ultralight. Block 0 and the sector trailers can not be reached from
        here: _block refuses them.
        """
        self._check_range(offset, len(data))
        classic = self.tag.kind == CLASSIC
        size = MF_BLOCK_SIZE if classic else UL_PAGE_SIZE
        if offset % size:
            raise NFCSizeError("Unaligned write")

        done = 0
        while done < len(data):
            # Pad the tail so a short final unit still writes a full one; the
            # record header carries the real length.
            chunk = bytearray(size)
            take = min(size, len(data) - done)
            chunk[0:take] = data[done : done + take]

            if classic:
                # A Classic write is two frames, each answered by an ACK
                block = self._block((offset + done) // MF_BLOCK_SIZE)
                self._authenticate(block)
                self._ack(bytes([CMD_MF_WRITE, block]))
                self._ack(chunk)
            else:
                page = UL_DATA_FIRST_PAGE + (offset + done) // UL_PAGE_SIZE
                self._ack(bytes([CMD_UL_WRITE, page]) + chunk)
            done += size

    # ---------- Records ----------

    def has_record(self):
        """True when the selected tag already carries a record, of any type.

        Any type on purpose: this is what the overwrite warning asks, and a
        record this firmware can not parse is still something to lose. Absent
        or unreadable records report False.
        """
        try:
            parse_header(self.read(0, HEADER_LEN), self.tag.capacity)
            return True
        except NFCError:
            return False

    def read_record(self, record_type=RECORD_KEF):
        """Reads a record of the named type, validating it throughout"""
        if self.tag is None:
            raise NFCError("No tag selected")
        length = parse_header(self.read(0, HEADER_LEN), self.tag.capacity, record_type)
        # length is already bounded by the ceiling and by this tag's capacity
        return self.read(HEADER_LEN, length)

    def write_record(self, data, record_type=RECORD_KEF):
        """Writes a record, replacing whatever was there.

        Header and payload go out as one contiguous image so the write stays
        aligned from offset zero and never touches a block it does not fully
        own.
        """
        if self.tag is None:
            raise NFCError("No tag selected")
        header = build_header(len(data), self.tag.capacity, record_type)
        self.write(0, header + bytes(data))
