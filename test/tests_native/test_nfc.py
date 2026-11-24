"""NFC tag and record layers, and the WS1850S driver, against simulated parts.

No reader and no card are needed: nfc_sim.py models the chip at its I2C
registers and the card at its frames, and the real driver runs between them.

    python3 -m unittest discover -s test/tests_native -p "test_nfc.py"
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(ROOT, "src"))
sys.path.append(os.path.join(ROOT, "ports", "esp32p4", "lib"))

# The driver is written for MicroPython's time module
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000)

import nfc
from nfc import NFC, NFCError, NFCNotFound, NFCSizeError
from nfc_sim import SimCard, SimChip, crc_a
from nfc_ws1850s import WS1850S

NTAG213_CC = b"\xe1\x10\x12\x00"  # 144 bytes
NTAG215_CC = b"\xe1\x10\x3e\x00"  # 496 bytes
NTAG216_CC = b"\xe1\x10\x6d\x00"  # 872 bytes


def ntag(cc, pages):
    return SimCard(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00, ul_pages=pages, ul_cc=cc)


def setup(card):
    chip = SimChip(card)
    link = NFC(WS1850S(i2c=chip))
    link.init()
    link.field(True)
    return chip, link


class HeaderTest(unittest.TestCase):
    # The same sixteen bytes Kern and Krux pin in their own suites
    GOLDEN = bytes.fromhex("4b524e310100002d0000000000000000")

    def test_golden_vector(self):
        self.assertEqual(nfc.build_header(45, 752), self.GOLDEN)
        self.assertEqual(nfc.parse_header(self.GOLDEN, 752, nfc.RECORD_KEF), 45)
        self.assertEqual(nfc.parse_header(self.GOLDEN, 752), 45)

    def test_type_numbers_are_pinned(self):
        self.assertEqual(
            (nfc.RECORD_KEF, nfc.RECORD_DESCRIPTOR, nfc.RECORD_DATUM, nfc.RECORD_XPUB),
            (1, 2, 3, 4),
        )

    def test_another_type_reads_as_no_record(self):
        header = nfc.build_header(45, 752, nfc.RECORD_DESCRIPTOR)
        with self.assertRaises(NFCNotFound):
            nfc.parse_header(header, 752, nfc.RECORD_KEF)
        # but it is still something an overwrite would destroy
        self.assertEqual(nfc.parse_header(header, 752), 45)

    def test_hostile_headers(self):
        def mutate(index, value):
            header = bytearray(self.GOLDEN)
            header[index] = value
            return bytes(header)

        for header in (
            b"",
            self.GOLDEN[:15],
            mutate(0, ord("k")),
            mutate(3, ord("2")),
            mutate(4, 0),
            mutate(4, 5),
            mutate(4, 0xFF),
            mutate(5, 1),
            mutate(8, 1),
            mutate(15, 0x80),
        ):
            with self.assertRaises(NFCNotFound):
                nfc.parse_header(header, 752)

    def test_hostile_lengths(self):
        def with_length(length):
            header = bytearray(self.GOLDEN)
            header[6], header[7] = length >> 8, length & 0xFF
            return bytes(header)

        for length, capacity in (
            (0, 752),
            (705, 752),  # past the ceiling
            (0xFFFF, 752),
            (33, 48),  # past what the tag can hold
            (1, 16),
            (1, 0),
        ):
            with self.assertRaises(NFCSizeError):
                nfc.parse_header(with_length(length), capacity)
        self.assertEqual(nfc.parse_header(with_length(704), 720), 704)
        self.assertEqual(nfc.parse_header(with_length(32), 48), 32)

    def test_build_refuses(self):
        with self.assertRaises(NFCError):
            nfc.build_header(45, 752, 9)
        for length, capacity in ((0, 752), (705, 752), (33, 48), (1, 8)):
            with self.assertRaises(NFCSizeError):
                nfc.build_header(length, capacity)


class BlockMapTest(unittest.TestCase):
    def test_never_block_zero_or_a_trailer(self):
        blocks = [NFC._block(i) for i in range(nfc.MF_DATA_BLOCKS)]
        self.assertEqual(blocks[:5], [1, 2, 4, 5, 6])
        self.assertEqual(blocks[-1], 62)
        self.assertEqual(len(set(blocks)), 47)
        for block in blocks:
            self.assertNotEqual(block, 0)
            self.assertNotEqual(block % 4, 3)
        for index in (-1, 47, 48, 1000):
            with self.assertRaises(NFCSizeError):
                NFC._block(index)


class ReaderTest(unittest.TestCase):
    def test_no_reader_on_the_bus(self):
        chip = SimChip()
        chip.present = False
        link = NFC(WS1850S(i2c=chip))
        with self.assertRaises(NFCNotFound):
            link.init()
        self.assertFalse(link.reader.ready)
        with self.assertRaises(NFCError):
            link.field(True)
        link.deinit()  # must not raise

    def test_something_else_at_the_address(self):
        class Deaf(SimChip):
            def _write(self, reg, value):
                pass

        with self.assertRaises(NFCNotFound):
            WS1850S(i2c=Deaf()).init()

    def test_field_is_off_until_asked_for(self):
        chip = SimChip(SimCard())
        link = NFC(WS1850S(i2c=chip))
        link.init()
        self.assertFalse(chip.field_on)
        with self.assertRaises(NFCNotFound):
            link.poll()
        link.field(True)
        self.assertTrue(chip.field_on)
        link.poll()
        link.deinit()
        self.assertFalse(chip.field_on)

    def test_crc_coprocessor(self):
        chip, link = setup(None)
        self.assertEqual(link.reader.calc_crc(b"\x50\x00"), b"\x57\xcd")
        self.assertEqual(crc_a(b"\x50\x00"), b"\x57\xcd")

    def test_reader_unplugged_midway(self):
        chip, link = setup(SimCard())
        link.poll()
        chip.present = False
        with self.assertRaises(NFCError):
            link.read(0, 16)
        link.deinit()  # must not raise


class SelectTest(unittest.TestCase):
    def test_empty_field(self):
        chip, link = setup(None)
        before = chip.transfers
        with self.assertRaises(NFCNotFound):
            link.poll()
        # bounded: an empty field costs a fixed, small number of transfers
        self.assertLess(chip.transfers - before, 80)

    def test_single_and_double_uid(self):
        for uid in (b"\x04\xa1\xb2\xc3", b"\x04\x11\x22\x33\x44\x55\x66"):
            chip, link = setup(SimCard(uid=uid))
            tag = link.poll()
            self.assertEqual(tag.uid, uid)
            self.assertEqual(tag.kind, nfc.CLASSIC)
            self.assertEqual(tag.capacity, 720)

    def test_card_stays_selectable(self):
        # every poll halts what it held; a halted card only answers WUPA
        chip, link = setup(SimCard())
        for _ in range(5):
            link.poll()
        link.read(0, 16)
        for _ in range(3):
            link.poll()

    def test_unsupported_families(self):
        for sak in (0x20, 0x28, 0x10, 0x09, 0x01):
            chip, link = setup(SimCard(sak=sak))
            with self.assertRaises(NFCNotFound):
                link.poll()
            self.assertIsNone(link.tag)

    def test_classic_4k_and_second_source(self):
        for sak in (0x18, 0x88):
            chip, link = setup(SimCard(sak=sak))
            self.assertEqual(link.poll().capacity, 720)

    def test_ultralight_capacity(self):
        for cc, pages, expected in (
            (None, 16, 48),  # plain Ultralight, no container
            (NTAG213_CC, 45, 144),
            (NTAG215_CC, 135, 496),
            (NTAG216_CC, 231, 720),  # 872, capped at one record
            (b"\xe1\x10\xff\x00", 45, 720),  # a tag lying upwards
            (b"\xe1\x10\x01\x00", 45, 48),  # and downwards
            (b"\x00\x10\x3e\x00", 45, 48),  # size byte without the magic
        ):
            chip, link = setup(ntag(cc, pages))
            tag = link.poll()
            self.assertEqual(tag.kind, nfc.ULTRALIGHT)
            self.assertEqual(tag.capacity, expected, cc)

    def test_bad_bcc(self):
        class BadBCC(SimCard):
            def exchange(self, frame, bits, encrypted):
                reply = super().exchange(frame, bits, encrypted)
                if bytes(frame) == b"\x93\x20":
                    return reply[0][:4] + bytes([reply[0][4] ^ 1]), 0
                return reply

        chip, link = setup(BadBCC())
        with self.assertRaises(NFCNotFound):
            link.poll()

    def test_oversized_replies_are_refused(self):
        class Babbler(SimCard):
            def __init__(self, on, size):
                super().__init__()
                self.on, self.size = on, size

            def exchange(self, frame, bits, encrypted):
                reply = super().exchange(frame, bits, encrypted)
                if bytes(frame[:1]) == self.on and reply is not None:
                    return bytes(self.size), 0
                return reply

        for on, size in ((b"\x52", 3), (b"\x52", 64), (b"\x93", 6), (b"\x93", 64)):
            chip, link = setup(Babbler(on, size))
            with self.assertRaises(NFCNotFound):
                link.poll()

        chip, link = setup(Babbler(b"\x30", 64))
        link.poll()
        with self.assertRaises(NFCSizeError):
            link.read(0, 16)

    def test_ten_byte_uid_is_refused(self):
        class Triple(SimCard):
            def exchange(self, frame, bits, encrypted):
                reply = super().exchange(frame, bits, encrypted)
                if bytes(frame[:2]) == b"\x95\x70" and reply is not None:
                    return b"\x04" + crc_a(b"\x04"), 0
                return reply

        chip, link = setup(Triple(uid=b"\x04\x11\x22\x33\x44\x55\x66"))
        with self.assertRaises(NFCNotFound):
            link.poll()


class RecordTest(unittest.TestCase):
    def cards(self):
        return (
            SimCard(),
            SimCard(uid=b"\x04\x11\x22\x33\x44\x55\x66"),
            ntag(NTAG213_CC, 45),
            ntag(NTAG215_CC, 135),
        )

    def test_round_trip(self):
        for card in self.cards():
            for size in (1, 15, 16, 17, 45, 61, 128):
                chip, link = setup(card)
                payload = os.urandom(size)
                link.poll()
                link.write_record(payload)
                # a fresh selection, as a later session would make
                link.field(False)
                link.field(True)
                link.poll()
                self.assertTrue(link.has_record())
                self.assertEqual(link.read_record(), payload)

    def test_largest_record(self):
        for card in (SimCard(), ntag(NTAG216_CC, 231)):
            chip, link = setup(card)
            payload = os.urandom(nfc.MAX_PAYLOAD)
            link.poll()
            link.write_record(payload)
            link.poll()
            self.assertEqual(link.read_record(), payload)
            with self.assertRaises(NFCSizeError):
                link.write_record(payload + b"\x00")

    def test_too_small_a_card(self):
        chip, link = setup(ntag(None, 16))  # 48 bytes
        link.poll()
        with self.assertRaises(NFCSizeError):
            link.write_record(bytes(33))
        self.assertEqual(link.reader.i2c.card.writes, [])
        link.write_record(bytes(range(32)))
        link.poll()
        self.assertEqual(link.read_record(), bytes(range(32)))

    def test_classic_writes_only_data_blocks(self):
        card = SimCard()
        before = [bytes(b) for b in card.blocks]
        chip, link = setup(card)
        link.poll()
        link.write_record(os.urandom(nfc.MAX_PAYLOAD))
        self.assertEqual(len(card.writes), 45)
        for block in card.writes:
            self.assertNotEqual(block, 0)
            self.assertNotEqual(block % 4, 3)
        for block in [0] + list(range(3, 64, 4)):
            self.assertEqual(bytes(card.blocks[block]), before[block])

    def test_ultralight_writes_only_user_pages(self):
        card = ntag(NTAG213_CC, 45)
        chip, link = setup(card)
        link.poll()
        link.write_record(os.urandom(128))
        self.assertEqual(card.writes, list(range(4, 40)))
        self.assertEqual(bytes(card.pages[3]), NTAG213_CC)

    def test_blank_card(self):
        for card in self.cards():
            chip, link = setup(card)
            link.poll()
            self.assertFalse(link.has_record())
            with self.assertRaises(NFCNotFound):
                link.read_record()

    def test_record_of_another_type(self):
        chip, link = setup(SimCard())
        link.poll()
        link.write_record(b"wpkh(...)", nfc.RECORD_DESCRIPTOR)
        link.poll()
        self.assertTrue(link.has_record())
        with self.assertRaises(NFCNotFound):
            link.read_record(nfc.RECORD_KEF)
        self.assertEqual(link.read_record(nfc.RECORD_DESCRIPTOR), b"wpkh(...)")

    def test_overwrite(self):
        chip, link = setup(SimCard())
        link.poll()
        link.write_record(os.urandom(200))
        link.poll()
        link.write_record(b"short")
        link.poll()
        self.assertEqual(link.read_record(), b"short")

    def test_card_with_other_keys(self):
        # what an NDEF formatted Classic looks like: the factory key is gone
        chip, link = setup(SimCard(key=b"\xd3\xf7\xd3\xf7\xd3\xf7"))
        link.poll()
        self.assertFalse(link.has_record())
        link.poll()
        with self.assertRaises(NFCError):
            link.read_record()
        link.poll()
        with self.assertRaises(NFCError):
            link.write_record(b"data")
        self.assertEqual(chip.card.writes, [])

    def test_card_removed(self):
        chip, link = setup(SimCard())
        link.poll()
        chip.card = None
        with self.assertRaises(NFCError):
            link.write_record(b"data")
        with self.assertRaises(NFCNotFound):
            link.poll()
        with self.assertRaises(NFCError):
            link.read_record()

    def test_write_not_acknowledged(self):
        class ReadOnly(SimCard):
            def exchange(self, frame, bits, encrypted):
                if bytes(frame[:1]) == b"\xa0":
                    return b"\x04", 4
                return super().exchange(frame, bits, encrypted)

        chip, link = setup(ReadOnly())
        link.poll()
        with self.assertRaises(NFCError):
            link.write_record(b"data")

    def test_hostile_record_on_the_card(self):
        card = SimCard()
        chip, link = setup(card)
        # a length the card can not hold, straight into the first data block
        card.blocks[1][:] = b"KRN1\x01\x00\xff\xff" + bytes(8)
        link.poll()
        self.assertFalse(link.has_record())
        with self.assertRaises(NFCSizeError):
            link.read_record()
        # reserved bytes carrying something
        card.blocks[1][:] = b"KRN1\x01\x00\x00\x10" + b"\x00" * 7 + b"\x01"
        link.poll()
        with self.assertRaises(NFCNotFound):
            link.read_record()

    def test_nothing_without_a_selected_tag(self):
        chip, link = setup(SimCard())
        for call in (
            lambda: link.read(0, 16),
            lambda: link.write(0, bytes(16)),
            link.read_record,
            lambda: link.write_record(b"data"),
        ):
            with self.assertRaises(NFCError):
                call()

    def test_range_checks(self):
        chip, link = setup(SimCard())
        link.poll()
        for offset, length in ((0, 0), (0, 721), (720, 1), (721, 0), (-16, 16), (700, 21)):
            with self.assertRaises(NFCSizeError):
                link.read(offset, length)
        with self.assertRaises(NFCSizeError):
            link.write(8, bytes(16))  # not on a block boundary
        self.assertEqual(len(link.read(719, 1)), 1)


if __name__ == "__main__":
    unittest.main()
