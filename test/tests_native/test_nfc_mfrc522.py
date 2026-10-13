"""The RC522 driver: SPI framing, reset, presence, and picking a reader.

nfc_sim.SimSpiBus puts the simulated chip behind the SPI framing of a real
MFRC522, with its chip select and reset pin, and the real driver runs against
it. The tag, record, seed and smartcard suites written for the I2C reader then
run again, unchanged, over SPI.

    python3 -m unittest discover -s test/tests_native -p "test_nfc_mfrc522.py"
"""
import contextlib
import importlib.util
import io
import os
import sys
import time
import unittest
from unittest import mock

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
if not hasattr(time, "ticks_add"):
    time.ticks_add = lambda a, b: a + b
if not hasattr(time, "ticks_us"):
    time.ticks_us = lambda: int(time.monotonic() * 1000000)

import nfc
import nfc_mfrc522
import nfc_pn532
import nfc_ws1850s
import test_nfc as tags
import test_nfc_seed as seeds
import test_nfc_smartcard as cards
from nfc import NFC, NFCError, NFCNotFound, NFCTimeout
from nfc_mfrc522 import MFRC522
from nfc_sim import IsoDepCard, SimCard, SimChip, SimSpiBus
from nfc_ws1850s import WS1850S

# Loaded by path: the directory it lives in has a test_nfc.py of its own
spec = importlib.util.spec_from_file_location(
    "nfc_rc522_diag", os.path.join(ROOT, "ports", "esp32p4", "test_nfc_rc522.py")
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def over_spi(i2c=None):
    """Stands in for WS1850S(i2c=chip): the same chip, reached over SPI"""
    reader = MFRC522(bus=SimSpiBus(i2c))
    reader.i2c = i2c  # where those suites look for the chip
    return reader


def spi_setup(card, **bus_options):
    bus = SimSpiBus(SimChip(card), **bus_options)
    link = NFC(MFRC522(bus=bus))
    link.init()
    link.field(True)
    return bus, link


class Rc522FramingTest(unittest.TestCase):
    def reader(self):
        bus = SimSpiBus()
        reader = MFRC522(bus=bus)
        bus.rst.value(1)
        return bus, reader

    def test_write_is_address_then_data(self):
        bus, reader = self.reader()
        reader._write(0x2D, 0x5A)
        # (0x2D << 1) & 0x7E, bit 7 clear for a write
        self.assertEqual(bus.frames, [b"\x5a\x5a"])
        reader._write(0x01, 0x00)
        self.assertEqual(bus.frames[-1], b"\x02\x00")

    def test_read_sets_bit_7_and_closes_with_zero(self):
        bus, reader = self.reader()
        bus.chip.reg[0x2D] = 0xC3
        self.assertEqual(reader._read(0x2D), b"\xc3")
        self.assertEqual(bus.frames, [b"\xda\x00"])
        self.assertEqual(reader.version(), 0x92)
        self.assertEqual(bus.frames[-1], b"\xee\x00")

    def test_fifo_goes_out_and_comes_back_in_one_transaction(self):
        bus, reader = self.reader()
        reader._raw(bytes([0x09]) + bytes(range(1, 65)))
        self.assertEqual(bus.frames[-1], b"\x12" + bytes(range(1, 65)))
        self.assertEqual(reader._byte(0x0A), 64)
        self.assertEqual(reader._read(0x09, 64), bytes(range(1, 65)))
        self.assertEqual(bus.frames[-1], b"\x92" * 64 + b"\x00")
        self.assertEqual(reader._byte(0x0A), 0)

    def test_read_length_is_bounded(self):
        bus, reader = self.reader()
        for length in (0, -1, 65, 4096):
            with self.assertRaises(NFCError):
                reader._read(0x09, length)
        self.assertEqual(bus.frames, [])

    def test_select_frames_every_transfer(self):
        bus, reader = self.reader()
        levels = []
        transfer = bus.write_readinto

        def watched(tx, rx):
            levels.append(bus.cs.level)
            transfer(tx, rx)

        bus.write_readinto = watched
        reader._write(0x2D, 1)
        reader._read(0x2D, 3)
        self.assertEqual(levels, [0, 0])
        self.assertEqual(bus.cs.level, 1)

    def test_select_is_released_when_the_bus_fails(self):
        bus, reader = self.reader()

        def broken(tx, rx):
            raise OSError("spi")

        bus.write_readinto = broken
        with self.assertRaises(NFCError):
            reader._write(0x2D, 1)
        self.assertEqual(bus.cs.level, 1)

    def test_no_bus_is_an_nfc_error(self):
        reader = MFRC522(bus=None)
        with self.assertRaises(NFCError):
            reader._write(0x2D, 1)


class Rc522LifecycleTest(unittest.TestCase):
    def test_reset_pulse_then_held_high(self):
        bus = SimSpiBus(SimChip(SimCard()))
        bus.rst.value(1)  # as a previous session might have left it
        bus.chip.reg[0x14] = 0x83  # with the antenna on
        link = NFC(MFRC522(bus=bus))
        link.init()
        self.assertEqual(bus.resets, 1)
        self.assertEqual(bus.rst.level, 1)
        self.assertFalse(bus.chip.field_on)
        link.field(True)
        link.poll()
        self.assertEqual(bus.rst.level, 1)

    def test_deinit_is_a_hard_power_down(self):
        bus, link = spi_setup(SimCard())
        link.poll()
        self.assertTrue(bus.chip.field_on)
        link.deinit()
        self.assertEqual(bus.rst.level, 0)
        self.assertFalse(bus.chip.field_on)
        self.assertFalse(link.reader.ready)
        link.deinit()  # must not raise
        # and it comes back
        link.init()
        link.field(True)
        link.poll()

    def test_clones_are_not_refused_by_version(self):
        for version in (0x91, 0x92, 0x88, 0xB2, 0x12, 0x00, 0xFF):
            bus, link = spi_setup(SimCard(), version=version)
            self.assertEqual(link.reader.version(), version)
            link.poll()

    def test_waits_for_the_oscillator(self):
        bus, link = spi_setup(SimCard(), wake=20)
        link.poll()

    def test_wait_for_the_oscillator_is_bounded(self):
        bus = SimSpiBus(SimChip(SimCard()), wake=10 ** 9)
        reader = MFRC522(bus=bus)
        with self.assertRaises(NFCNotFound):
            reader.init()
        self.assertFalse(reader.ready)
        # two transfers per attempt, one attempt per ms of the limit
        self.assertEqual(len(bus.frames), 2 * nfc_mfrc522._WAKE_TIMEOUT_MS)
        self.assertEqual(bus.rst.level, 0)

    def test_empty_header(self):
        # SPI has no ACK: an absent module reads as whatever MISO floats at
        for idle in (0x00, 0xFF, 0x55, 0xAA):
            bus = SimSpiBus(idle=idle)
            bus.chip.present = False
            link = NFC(MFRC522(bus=bus))
            with self.assertRaises(NFCNotFound):
                link.init()
            self.assertFalse(link.reader.ready)
            self.assertEqual(bus.rst.level, 0)
            with self.assertRaises(NFCError):
                link.field(True)
            link.deinit()  # must not raise

    def test_failed_bring_up_powers_the_chip_down(self):
        class Stuck(SimChip):
            def _command(self, command):
                if command == 0x0F:
                    self.reg[0x01] = 0x10  # never leaves the soft reset
                else:
                    super()._command(command)

        bus = SimSpiBus(Stuck())
        with self.assertRaises(NFCError):
            MFRC522(bus=bus).init()
        self.assertEqual(bus.rst.level, 0)

    def test_module_pulled_midway(self):
        for idle in (0x00, 0xFF):
            bus, link = spi_setup(SimCard(), idle=idle)
            link.poll()
            bus.chip.present = False
            with self.assertRaises(NFCError):
                link.read(0, 16)
            link.deinit()  # must not raise
            self.assertEqual(bus.rst.level, 0)

    def test_poll_cap_holds_with_a_stopped_clock(self):
        bus, link = spi_setup(None)
        reader = link.reader
        reader.set_timeout(1)
        before = len(bus.frames)
        with mock.patch.object(time, "ticks_ms", lambda: 0):
            with self.assertRaises(NFCTimeout):
                reader.transceive(b"\x52", 7, 2)
        irq_reads = bus.frames[before:].count(b"\x88\x00")
        self.assertEqual(irq_reads, 4000 + (1 + 20) * 100)

    def test_long_timeouts_keep_the_chip_timer_out_of_the_way(self):
        bus, link = spi_setup(None)
        link.reader.set_timeout(500)
        self.assertEqual(
            [bus.chip.reg[r] for r in (0x2A, 0x2B, 0x2C, 0x2D)], [0x8F, 0xFF, 0xFF, 0xFF]
        )
        link.reader.set_timeout(None)
        self.assertEqual(
            [bus.chip.reg[r] for r in (0x2A, 0x2B, 0x2C, 0x2D)], [0x80, 0xA9, 0x03, 0xE8]
        )


class BoardBus:
    """Puts a simulated module where the driver opens the board's SPI"""

    def __init__(self, bus):
        self.bus = bus

    def __enter__(self):
        self.patch = mock.patch.object(nfc_mfrc522, "_board_bus", self.bus)
        self.patch.start()
        return self.bus

    def __exit__(self, *exc):
        self.patch.stop()


class Rc522PresenceTest(unittest.TestCase):
    def test_no_spi_on_this_machine(self):
        # run_native_tests.py may have left a stub `machine` behind
        with mock.patch.object(nfc_mfrc522, "_board_bus", None), \
                mock.patch.dict(sys.modules, {"machine": None}):
            self.assertFalse(nfc_mfrc522.is_present())
            with self.assertRaises(NFCNotFound):
                MFRC522().init()

    def test_present_and_left_powered_down(self):
        with BoardBus(SimSpiBus(SimChip(SimCard()))) as bus:
            self.assertTrue(nfc_mfrc522.is_present())
            self.assertEqual(bus.rst.level, 0)
            self.assertFalse(bus.chip.field_on)

    def test_absent(self):
        for idle in (0x00, 0xFF):
            bus = SimSpiBus(idle=idle)
            bus.chip.present = False
            with BoardBus(bus):
                self.assertFalse(nfc_mfrc522.is_present())
                self.assertEqual(bus.rst.level, 0)

    def test_asking_does_not_reset_a_reader_in_use(self):
        with BoardBus(SimSpiBus(SimChip(SimCard()))) as bus:
            link = NFC(MFRC522())
            link.init()
            link.field(True)
            link.poll()
            link.reader.set_timeout(None)
            timer = dict(bus.chip.reg)
            self.assertTrue(nfc_mfrc522.is_present())
            self.assertEqual(bus.resets, 0)
            self.assertEqual(bus.rst.level, 1)
            self.assertTrue(bus.chip.field_on)
            self.assertEqual(bus.chip.reg, timer)
            link.read(0, 16)
            # pulled out while in use: the answer is live, not remembered
            bus.chip.present = False
            self.assertFalse(nfc_mfrc522.is_present())
            bus.chip.present = True
            link.deinit()
            self.assertEqual(bus.resets, 1)
            self.assertTrue(nfc_mfrc522.is_present())
            self.assertEqual(bus.rst.level, 0)

    def test_a_reader_left_up_does_not_answer_for_the_next_one(self):
        with BoardBus(SimSpiBus(SimChip(SimCard()))):
            NFC(MFRC522()).init()  # never deinitialized
        bus = SimSpiBus()
        bus.chip.present = False
        with BoardBus(bus):
            self.assertFalse(nfc_mfrc522.is_present())


class Rc522SelectionTest(unittest.TestCase):
    """nfc.open_reader(): the I2C reader if it answers, else the SPI one"""

    def pick(self, i2c, spi):
        with mock.patch.object(nfc_ws1850s, "is_present", lambda: i2c), \
                mock.patch.object(nfc_mfrc522, "is_present", lambda: spi), \
                mock.patch.object(nfc, "_drivers", None):
            return type(nfc.open_reader()), nfc.reader_name(), nfc.is_available()

    def test_both_drivers_are_in_the_build(self):
        with mock.patch.object(nfc, "_drivers", None):
            self.assertTrue(nfc.is_supported())
            self.assertEqual(
                [entry[0] for entry in nfc._load_drivers()],
                [nfc_ws1850s, nfc_pn532, nfc_mfrc522],
            )

    def test_i2c_reader_wins(self):
        self.assertEqual(self.pick(True, False), (WS1850S, "RFID Unit 2 (I2C)", True))
        self.assertEqual(self.pick(True, True), (WS1850S, "RFID Unit 2 (I2C)", True))

    def test_spi_reader_when_i2c_is_silent(self):
        self.assertEqual(self.pick(False, True), (MFRC522, "RC522 (SPI)", True))

    def test_spi_is_not_touched_when_i2c_answers(self):
        def boom():
            raise AssertionError("SPI probed")

        with mock.patch.object(nfc_ws1850s, "is_present", lambda: True), \
                mock.patch.object(nfc_mfrc522, "is_present", boom):
            self.assertTrue(nfc.is_available())
            self.assertIs(type(nfc.open_reader()), WS1850S)

    def test_nothing_plugged_in(self):
        self.assertEqual(self.pick(False, False), (WS1850S, None, False))
        with mock.patch.object(nfc_ws1850s, "is_present", lambda: False), \
                mock.patch.object(nfc_mfrc522, "is_present", lambda: False):
            with self.assertRaises(NFCNotFound):
                nfc.open_reader().init()

    def test_a_probe_that_blows_up_is_a_reader_not_there(self):
        def boom():
            raise OSError("bus")

        with mock.patch.object(nfc_ws1850s, "is_present", boom), \
                mock.patch.object(nfc_mfrc522, "is_present", boom):
            self.assertFalse(nfc.is_available())

    def test_end_to_end_over_the_board_bus(self):
        with BoardBus(SimSpiBus(SimChip(SimCard()))) as bus:
            self.assertEqual(nfc.reader_name(), "RC522 (SPI)")
            link = NFC()
            self.assertIs(type(link.reader), MFRC522)
            link.init()
            link.field(True)
            link.poll()
            link.write_record(b"over spi")
            link.deinit()
            self.assertEqual(bus.rst.level, 0)


class Rc522InterchangeTest(unittest.TestCase):
    """A card written on one reader reads on the other"""

    PAYLOAD = bytes(range(1, 62))

    def links(self, card):
        return NFC(WS1850S(i2c=SimChip(card))), NFC(MFRC522(bus=SimSpiBus(SimChip(card))))

    def move(self, card, writer, reader):
        writer.init()
        writer.field(True)
        writer.poll()
        writer.write_record(self.PAYLOAD)
        writer.deinit()
        card.reset()  # carried from one reader to the other
        reader.init()
        reader.field(True)
        reader.poll()
        self.assertEqual(reader.read(0, 4), b"KRN1")
        self.assertEqual(reader.read_record(), self.PAYLOAD)
        reader.deinit()

    def test_both_ways_on_both_families(self):
        for make in (SimCard, lambda: tags.ntag(tags.NTAG213_CC, 45)):
            card = make()
            i2c, spi = self.links(card)
            self.move(card, i2c, spi)
            card = make()
            i2c, spi = self.links(card)
            self.move(card, spi, i2c)

    def test_same_bytes_on_the_card(self):
        images = []
        for build in (lambda c: NFC(WS1850S(i2c=SimChip(c))),
                      lambda c: NFC(MFRC522(bus=SimSpiBus(SimChip(c))))):
            card = SimCard()
            link = build(card)
            link.init()
            link.field(True)
            link.poll()
            link.write_record(self.PAYLOAD)
            link.deinit()
            images.append([bytes(block) for block in card.blocks])
        self.assertEqual(images[0], images[1])


class Rc522DiagnosticTest(unittest.TestCase):
    """test_nfc_rc522.run(), the way it is called on the board"""

    def run_diag(self, bus, call="run", **options):
        output = io.StringIO()
        options.setdefault("seconds", 0)
        with BoardBus(bus), contextlib.redirect_stdout(output):
            result = getattr(diag, call)(**options)
        self.assertFalse(bus.chip.field_on, "the field must be off when it returns")
        self.assertEqual(bus.rst.level, 0, "the chip must be powered down")
        return result, output.getvalue()

    def test_pass(self):
        result, text = self.run_diag(SimSpiBus(SimChip(SimCard()), wake=6))
        self.assertTrue(result["ok"], text)
        for line in ("SPI:     open", "RESET:   silent with RST low", "VERSION: 0x92, NXP",
                     "READER:  ready, field off", "FIELD:   antenna drivers on",
                     "POLL:", "CARD:    MIFARE Classic, UID 04a1b2c3", "RECORD:  none"):
            self.assertIn(line, text)

    def test_clone_goes_on(self):
        result, text = self.run_diag(SimSpiBus(SimChip(SimCard()), version=0x3F))
        self.assertTrue(result["ok"], text)
        self.assertIn("VERSION: 0x3f, unknown", text)

    def test_spi_does_not_open(self):
        output = io.StringIO()
        with mock.patch.object(nfc_mfrc522, "_board_bus", None), \
                mock.patch.dict(sys.modules, {"machine": None}), \
                contextlib.redirect_stdout(output):
            result = diag.run(seconds=0)
        self.assertEqual((result["ok"], result["stage"]), (False, "SPI"))

    def test_nothing_on_the_header(self):
        for idle in (0x00, 0xFF):
            bus = SimSpiBus(idle=idle)
            bus.chip.present = False
            with mock.patch.object(diag, "_WAKE_LIMIT_MS", 5):
                result, text = self.run_diag(bus)
            self.assertEqual((result["ok"], result["stage"]), (False, "RESET"), text)
            self.assertIn("MISO reads 0x%02x" % idle, text)

    def test_reset_wire_missing(self):
        class NoReset(SimSpiBus):
            """RST never reaches the chip, which stays awake"""

            def write_readinto(self, tx, rx):
                level, self.rst.level = self.rst.level, 1
                try:
                    super().write_readinto(tx, rx)
                finally:
                    self.rst.level = level

        result, text = self.run_diag(NoReset(SimChip(SimCard())))
        self.assertEqual((result["ok"], result["stage"]), (False, "RESET"), text)
        self.assertIn("does not", text)

    def test_slow_oscillator_is_reported(self):
        bus = SimSpiBus(SimChip(SimCard()), wake=2 * nfc_mfrc522._WAKE_TIMEOUT_MS + 20)
        result, text = self.run_diag(bus)
        self.assertIn("OVER the driver limit", text)
        self.assertEqual((result["ok"], result["stage"]), (False, "READER"), text)

    def test_antenna_drivers_do_not_turn_on(self):
        class NoField(SimChip):
            def _write(self, reg, value):
                super()._write(reg, value & 0xFC if reg == 0x14 else value)

        result, text = self.run_diag(SimSpiBus(NoField(SimCard())))
        self.assertEqual((result["ok"], result["stage"]), (False, "FIELD"), text)

    def test_no_card(self):
        result, text = self.run_diag(SimSpiBus(SimChip(None)))
        self.assertEqual((result["ok"], result["stage"]), (False, "CARD"), text)

    def test_card_with_other_keys(self):
        result, text = self.run_diag(SimSpiBus(SimChip(SimCard(key=b"\x00" * 6))))
        self.assertEqual((result["ok"], result["stage"]), (False, "READ"), text)

    def test_roundtrip_writes_a_test_record(self):
        card = SimCard()
        result, text = self.run_diag(SimSpiBus(SimChip(card)), call="roundtrip")
        self.assertTrue(result["ok"], text)
        self.assertIn("read back OK", text)
        # and never over a record that is already there
        result, text = self.run_diag(SimSpiBus(SimChip(card)), call="roundtrip")
        self.assertEqual((result["ok"], result["stage"]), (False, "WRITE"), text)
        self.assertIn("refused", text)


@unittest.skipUnless(cards.READY, "needs embit, cryptography and the app's modules")
class Rc522JavacardDiagnosticTest(unittest.TestCase):
    """test_nfc_javacard falls back to the RC522 when the I2C reader is silent"""

    def test_runs_over_spi(self):
        jc = cards.base.diag
        bus = SimSpiBus(SimChip(IsoDepCard(cards.base.Applet())))
        output = io.StringIO()
        with BoardBus(bus), \
                mock.patch.object(jc.nfc_ws1850s, "is_present", lambda: False), \
                contextlib.redirect_stdout(output):
            result = jc.run(rounds=1, seconds=0, modes=("es",))
        self.assertTrue(result["ok"], output.getvalue())
        self.assertIn("RC522 on SPI", output.getvalue())
        self.assertFalse(bus.chip.field_on)
        self.assertEqual(bus.rst.level, 0)

    def test_chip_timer_is_stretched(self):
        bus = SimSpiBus()
        reader = cards.base.diag.DiagReaderSPI(bus=bus)
        reader.init()
        self.assertEqual(
            [bus.chip.reg[r] for r in (0x2A, 0x2B, 0x2C, 0x2D)], [0x8F, 0xFF, 0xFF, 0xFF]
        )
        reader.deinit()


# ---------- The suites written for the I2C reader, over SPI ----------
#
# They build their reader as WS1850S(i2c=chip) or through setup(). Both names
# are swapped for the SPI reader while a class below runs, so every test in
# them drives the real MFRC522 driver through the SPI framing.


class _OverSpi:
    module = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._saved = {
            name: getattr(cls.module, name)
            for name in ("WS1850S", "setup")
            if hasattr(cls.module, name)
        }
        cls.module.WS1850S = over_spi
        if "setup" in cls._saved:
            cls.module.setup = cls._setup

    @staticmethod
    def _setup(card):
        chip = SimChip(card)
        link = NFC(over_spi(chip))
        link.init()
        link.field(True)
        return chip, link

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._saved.items():
            setattr(cls.module, name, value)
        super().tearDownClass()


class SpiReaderTest(_OverSpi, tags.ReaderTest):
    module = tags


class SpiSelectTest(_OverSpi, tags.SelectTest):
    module = tags


class SpiRecordTest(_OverSpi, tags.RecordTest):
    module = tags


class SpiSeedFlowTest(_OverSpi, seeds.SeedFlowTest):
    module = seeds


class SpiSettingsTest(_OverSpi, seeds.SettingsTest):
    module = seeds


class SpiSmartcardKeystoreTest(_OverSpi, cards.SmartcardKeystoreTest):
    module = cards


class SpiConnectionTest(_OverSpi, cards.ConnectionTest):
    module = cards


if __name__ == "__main__":
    unittest.main()
