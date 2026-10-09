"""ISO 14443-4 (ISO-DEP), reader side: APDUs to a contactless smartcard.

The half-duplex block protocol a JavaCard speaks once it is selected: RATS,
then I-blocks carrying the APDU with chaining in both directions, R-blocks
acknowledging them and S(WTX) when the card asks for more time. 106 kbit/s, no
CID, no NAD, no PPS - the plain mode every card supports.

The card picks its frame size, its waiting time, how often it asks for more of
it and how long its answer is. Each of those is bounded here before it is used.

Checked against a JCOP card running the Specter MemoryCard applet on a
WS1850S: frame waiting time 39 ms, one S(WTX) every 27 ms and up to 61 of them
while the card does elliptic curve work.
"""
import time

from . import FIFO_SIZE, NFCError, NFCTimeout, SAK_ISO_DEP_BIT

# The largest frame the reader FIFO holds, CRC included, and its code in RATS
FSD = FIFO_SIZE
_FSDI = 5
_MAX_ATS = 20
_FSC_TABLE = (16, 24, 32, 40, 48, 64, 96, 128, 256)
_FWT_MAX_MS = 4949  # FWI 14, the longest the standard allows
_ACTIVATION_MS = 30
MAX_APDU = 261
MAX_RESPONSE = 258  # short APDU: 256 bytes of data and the status word
MAX_WTX = 100  # requests for more time honoured per APDU
MAX_RETRIES = 2  # invalid or lost blocks in a row
APDU_BUDGET_MS = 15000


class IsoDepError(NFCError):
    """The card left the protocol or went past a limit"""


def parse_ats(ats):
    """Validates an ATS (CRC removed), returns (fsc, fwi, sfgi)"""
    ats = bytes(ats)
    if not 1 <= len(ats) <= _MAX_ATS or ats[0] != len(ats):
        raise IsoDepError("Bad ATS length")
    fsci, fwi, sfgi = 2, 4, 0
    if len(ats) > 1:
        t0 = ats[1]
        fsci = t0 & 0x0F
        pos = 2
        # Every bit of T0 announces a byte; check it is there before reading
        if pos + bin(t0 & 0x70).count("1") > len(ats):
            raise IsoDepError("Truncated ATS")
        if t0 & 0x10:
            pos += 1  # TA1: bit rates, and only 106 kbit/s is used
        if t0 & 0x20:
            fwi, sfgi = ats[pos] >> 4, ats[pos] & 0x0F
    # 15 is reserved in both fields; the standard says to fall back
    if fwi == 15:
        fwi = 4
    if sfgi == 15:
        sfgi = 0
    return _FSC_TABLE[min(fsci, 8)], fwi, sfgi


def _fwt_ms(fwi):
    # FWT = 256 * 16 / fc * 2^FWI = 302 us * 2^FWI
    return (302 * (1 << fwi)) // 1000 + 1


class IsoDep:
    """One smartcard session over an nfc.NFC link"""

    def __init__(self, link):
        self.link = link
        self.reader = link.reader
        self.active = False
        self.uid = None
        self.ats = None
        self.bn = 0
        self.fsc = 32
        self.fwt_ms = _fwt_ms(4)

    def _frame(self, block, timeout_ms):
        self.reader.set_timeout(timeout_ms)
        return self.reader.transceive_crc(block, FSD)

    def connect(self):
        """Selects the card in the field and brings the protocol up.

        The field must be on. Raises NFCTimeout when nothing answers,
        NFCError for a card that is not a smartcard or misbehaves.
        """
        self.active = False
        self.bn = 0
        self.reader.set_timeout(None)
        uid, sak = self.link.select()
        if not sak & SAK_ISO_DEP_BIT:
            raise IsoDepError("Not a smartcard")
        ats = self._frame(bytes([0xE0, _FSDI << 4]), _ACTIVATION_MS)
        fsc, fwi, sfgi = parse_ats(ats)
        # Never send more than our own FIFO holds
        self.fsc = min(fsc, FSD)
        self.fwt_ms = _fwt_ms(fwi)
        # SFGT: the time the card asks for before the first block
        time.sleep_ms(min(_fwt_ms(sfgi), _FWT_MAX_MS))
        self.uid = uid
        self.ats = bytes(ats)
        self.active = True

    def deselect(self):
        """S(DESELECT), and back to the short timeout. Never raises."""
        was_active, self.active = self.active, False
        try:
            if was_active:
                self._frame(b"\xC2", _ACTIVATION_MS)
        except NFCError:
            pass
        try:
            self.reader.set_timeout(None)
        except NFCError:
            pass

    def _iblock(self, apdu, offset):
        chunk = apdu[offset : offset + self.fsc - 3]  # PCB and CRC take 3
        more = offset + len(chunk) < len(apdu)
        pcb = 0x02 | (0x10 if more else 0) | self.bn
        return bytes([pcb]) + chunk, len(chunk), more

    def exchange(self, apdu):
        """Sends one APDU, returns the whole response with its status word.

        Any failure ends the session: the block numbers are no longer known to
        agree, so the card has to be selected again.
        """
        apdu = bytes(apdu)
        if not self.active or not 4 <= len(apdu) <= MAX_APDU:
            raise IsoDepError("Bad APDU or card not activated")
        try:
            return self._exchange(apdu)
        except NFCError:
            self.active = False
            raise

    def _exchange(self, apdu):
        offset = 0
        block, sent, more = self._iblock(apdu, 0)
        last = block  # what a retransmission repeats
        receiving = False  # the answer has started; `last` is an R(ACK)
        out = bytearray()
        wtx = 0
        errors = 0
        timeout = self.fwt_ms
        deadline = time.ticks_add(time.ticks_ms(), APDU_BUDGET_MS)

        while True:
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                raise IsoDepError("APDU took too long")
            try:
                reply = self._frame(block, timeout)
            except NFCError as error:
                errors += 1
                if errors > MAX_RETRIES:
                    if isinstance(error, NFCTimeout):
                        raise NFCTimeout("Card stopped answering")
                    raise IsoDepError("No valid block")
                # Rules 4 and 5: repeat the R(ACK) if the card was chaining,
                # otherwise ask it with an R(NAK) to repeat itself
                block = last if receiving else bytes([0xB2 | self.bn])
                timeout = self.fwt_ms
                continue

            pcb, inf = reply[0], reply[1:]
            timeout = self.fwt_ms

            if pcb == 0xF2:  # S(WTX): the card asks for more time
                wtxm = inf[0] & 0x3F if len(inf) == 1 else 0
                wtx += 1
                if not 1 <= wtxm <= 59 or wtx > MAX_WTX:
                    raise IsoDepError("Bad or too many WTX")
                block = bytes([0xF2, wtxm])
                timeout = min(self.fwt_ms * wtxm, _FWT_MAX_MS)
                errors = 0

            elif pcb & 0xFE == 0xA2:  # R(ACK)
                if inf or receiving:
                    raise IsoDepError("Unexpected R(ACK)")
                if pcb & 1 == self.bn:
                    # Rule 7: the chained piece arrived, the next one follows
                    if not more:
                        raise IsoDepError("R(ACK) for a final block")
                    self.bn ^= 1
                    offset += sent
                    block, sent, more = self._iblock(apdu, offset)
                    last = block
                    errors = 0
                else:
                    # Rule 6: the card did not get the last I-block
                    errors += 1
                    if errors > MAX_RETRIES:
                        raise IsoDepError("Too many retransmissions")
                    block = last

            elif pcb & 0xEE == 0x02:  # I-block, no CID and no NAD
                if more or pcb & 1 != self.bn:
                    raise IsoDepError("I-block out of sequence")
                self.bn ^= 1
                # The card chose the total length: bound it before joining
                if len(out) + len(inf) > MAX_RESPONSE:
                    raise IsoDepError("Response too long")
                out += inf
                if not pcb & 0x10:
                    if len(out) < 2:
                        raise IsoDepError("Response without status word")
                    return bytes(out)
                # An empty piece moves nothing forward and would chain forever
                if not inf:
                    raise IsoDepError("Empty chained block")
                receiving = True
                block = last = bytes([0xA2 | self.bn])
                errors = 0

            else:
                raise IsoDepError("Unexpected block")
