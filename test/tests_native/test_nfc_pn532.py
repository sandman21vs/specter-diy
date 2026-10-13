"""The PN532 driver: firmware frames over I2C, raw frames to the card.

nfc_sim.SimPN532 plays the PN532's firmware in front of the same simulated
radio and cards the other readers use, and the real driver runs against it.
The tag, record, seed and smartcard suites written for the first reader then
run again, unchanged, through a PN532.

    python3 -m unittest discover -s test/tests_native -p "test_nfc_pn532.py"
"""
import contextlib
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

import nfc
import nfc_mfrc522
import nfc_pn532
import nfc_ws1850s
import test_nfc as tags
import test_nfc_seed as seeds
import test_nfc_smartcard as cards
from nfc import NFC, NFCError, NFCNotFound, NFCSizeError, NFCTimeout
from nfc_pn532 import PN532
from nfc_sim import IsoDepCard, SimCard, SimChip, SimPN532, crc_a


def over_pn532(i2c=None):
    """Stands in for WS1850S(i2c=chip): the same radio, inside a PN532"""
    return PN532(i2c=SimPN532(i2c))


def pn532_setup(card, **options):
    bus = SimPN532(SimChip(card), **options)
    link = NFC(PN532(i2c=bus))
    link.init()
    link.field(True)
    return bus, link


def ntag(pages=45, cc=b"\xe1\x10\x12\x00"):
    return SimCard(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00, ul_pages=pages, ul_cc=cc)


class Pn532FramingTest(unittest.TestCase):
    def reader(self, **options):
        bus = SimPN532(**options)
        return bus, PN532(i2c=bus)

    def test_command_frame(self):
        bus, reader = self.reader()
        sent = []
        writeto = bus.writeto
        bus.writeto = lambda addr, data: (sent.append((addr, bytes(data))), writeto(addr, data))[1]
        self.assertEqual(reader.version(), (0x32, 0x01, 0x06, 0x07))
        # 00 00 FF, length and its complement, D4 02, checksum, 00
        self.assertEqual(sent, [(0x24, bytes.fromhex("0000ff02fed4022a00"))])

    def test_waits_for_the_status_byte(self):
        bus, reader = self.reader(busy=5)
        self.assertEqual(reader.version()[0], 0x32)

    def test_a_chip_that_never_gets_ready(self):
        bus, reader = self.reader(busy=10 ** 9)
        started = time.monotonic()
        with self.assertRaises(NFCError):
            reader.version()
        self.assertLess(time.monotonic() - started, 1)

    def test_frames_from_the_chip_are_checked(self):
        def corrupt(index, value):
            def mangle(frame):
                frame = bytearray(frame)
                frame[index] = value
                return bytes(frame)

            return mangle

        for mangle in (
            corrupt(2, 0xFE),  # start code
            corrupt(3, 0x07),  # length without its complement
            corrupt(4, 0x00),
            corrupt(5, 0xD4),  # direction
            corrupt(6, 0x09),  # answer to another command
            corrupt(-2, 0x00),  # data checksum
            lambda frame: frame[:6],
            lambda frame: b"\x00\x00\xff\x01\xff\x7f\x81\x00",  # firmware error frame
            # a length far past anything asked for
            lambda frame: b"\x00\x00\xff\xf0\x10" + bytes(0xF0) + b"\x00\x00",
        ):
            bus, reader = self.reader()
            bus.mangle = mangle
            with self.assertRaises(NFCError):
                reader.version()

    def test_no_acknowledge(self):
        bus, reader = self.reader()
        readfrom = bus.readfrom

        def no_ack(addr, length):
            data = readfrom(addr, length)
            return b"\x01\x00\x00\xff\xff\x00\x00" if length == 7 else data

        bus.readfrom = no_ack
        with self.assertRaises(NFCError):
            reader.version()

    def test_no_bus_and_no_module(self):
        with self.assertRaises(NFCError):
            PN532(i2c=None)._command(0x02)
        bus, reader = self.reader()
        bus.chip.present = False
        with self.assertRaises(NFCNotFound):
            reader.init()
        self.assertFalse(reader.ready)

    def test_crc_matches_the_coprocessor(self):
        for data in (b"\x50\x00", b"\x30\x04", bytes(range(62))):
            self.assertEqual(nfc_pn532.crc_a(data), crc_a(data))

    def test_timeout_codes(self):
        # 100 us * 2^(n-1), the smallest that covers the request
        for ms, code in ((1, 5), (25, 9), (39, 10), (52, 11), (100, 11), (500, 14), (3000, 16), (10 ** 6, 16)):
            self.assertEqual(nfc_pn532._timeout_code(ms), code, ms)


class Pn532LifecycleTest(unittest.TestCase):
    def test_init_leaves_the_field_off_and_nothing_automatic(self):
        bus = SimPN532(SimChip(SimCard()))
        bus.chip.reg[0x14] = 0x83  # left on by whoever was here before
        bus.chip.reg[0x08] = 0x08  # and with crypto1 running
        reader = PN532(i2c=bus)
        reader.init()
        self.assertTrue(reader.ready)
        self.assertFalse(bus.chip.field_on)
        self.assertFalse(bus.chip.crypto_on)
        self.assertEqual(bus.parameters, 0x00)  # no automatic RATS
        # raw frames: the chip neither adds nor checks a CRC
        self.assertEqual((bus.chip.reg[0x12], bus.chip.reg[0x13]), (0x00, 0x00))
        self.assertEqual(bus.chip.reg[0x15], 0x40)
        count = len(bus.commands)
        reader.init()
        self.assertEqual(len(bus.commands), count)

    def test_another_chip_at_the_address_is_refused(self):
        bus = SimPN532(ic=0x33)
        with self.assertRaises(NFCNotFound):
            PN532(i2c=bus).init()

    def test_deinit_drops_the_field_and_never_raises(self):
        bus, link = pn532_setup(SimCard())
        self.assertTrue(bus.chip.field_on)
        link.deinit()
        self.assertFalse(bus.chip.field_on)
        bus, link = pn532_setup(SimCard())
        bus.chip.present = False
        link.deinit()
        self.assertFalse(link.reader.ready)

    def test_module_pulled_midway(self):
        bus, link = pn532_setup(SimCard())
        bus.chip.present = False
        with self.assertRaises(NFCNotFound):
            link.poll()

    def test_presence_is_one_probe_of_the_board_bus(self):
        self.assertFalse(nfc_pn532.is_present())  # no p4board on this machine
        board = mock.Mock()
        board.i2c_probe.return_value = True
        with mock.patch.dict(sys.modules, {"p4board": board}):
            self.assertTrue(nfc_pn532.is_present())
            board.i2c_probe.assert_called_once_with(0x24)
            board.i2c_probe.side_effect = OSError("bus")
            self.assertFalse(nfc_pn532.is_present())


class Pn532ExchangeTest(unittest.TestCase):
    def test_short_frame_sets_the_bit_count_once(self):
        bus, link = pn532_setup(SimCard())
        reader = link.reader
        reply, bits = reader.transceive(b"\x52", 7, 2)
        self.assertEqual((reply, bits), (b"\x04\x00", 0))
        self.assertEqual(bus.chip.reg[0x0D] & 7, 7)
        writes = [c for c in bus.commands if c[0] == 0x08]
        reader.transceive(b"\x52", 7, 2)
        self.assertEqual([c for c in bus.commands if c[0] == 0x08], writes)
        reader.transceive(b"\x93\x20", 0, 5)
        self.assertEqual(bus.chip.reg[0x0D] & 7, 0)

    def test_silence_is_a_timeout(self):
        bus, link = pn532_setup(None)
        with self.assertRaises(NFCTimeout):
            link.reader.transceive(b"\x52", 7, 2)
        # and the success case of a frame that expects no answer
        self.assertEqual(link.reader.transceive(b"\x50\x00\x57\xcd"), (b"", 0))

    def test_reply_size_is_the_card_s_choice_and_is_bounded(self):
        class Loud(SimCard):
            size = 3

            def exchange(self, frame, bits, encrypted):
                return bytes(self.size), 0

        card = Loud()
        bus, link = pn532_setup(card)
        with self.assertRaises(NFCSizeError):
            link.reader.transceive(b"\x52", 7, 2)
        for size in (65, 200):
            card.size = size
            with self.assertRaises(NFCError):
                link.reader.transceive(b"\x30\x04", 0, 64)

    def test_bad_frames_never_reach_the_chip(self):
        bus, link = pn532_setup(SimCard())
        count = len(bus.commands)
        for send, bits in ((b"", 0), (bytes(65), 0), (b"\x52", 8)):
            with self.assertRaises(NFCSizeError):
                link.reader.transceive(send, bits, 2)
        with self.assertRaises(NFCSizeError):
            link.reader.calc_crc(bytes(65))
        self.assertEqual(len(bus.commands), count)

    def test_firmware_error_is_not_a_timeout(self):
        bus, link = pn532_setup(SimCard())
        bus._thru = lambda frame: b"\x05"  # framing error
        with self.assertRaises(NFCError) as raised:
            link.reader.transceive(b"\x52", 7, 2)
        self.assertNotIsInstance(raised.exception, NFCTimeout)

    def test_timeout_is_programmed_only_when_it_changes(self):
        bus, link = pn532_setup(SimCard())
        reader = link.reader
        reader.transceive(b"\x52", 7, 2)
        self.assertEqual(bus.thru_timeouts[-1], 0x0A)
        reader.set_timeout(39)
        configs = [c for c in bus.commands if c[0] == 0x32]
        reader.transceive(b"\x26", 7, 2)
        reader.transceive(b"\x52", 7, 2)
        self.assertEqual(bus.thru_timeouts[-2:], [0x0A, 0x0A])  # 51 ms covers 39
        self.assertEqual([c for c in bus.commands if c[0] == 0x32], configs)
        reader.set_timeout(1000)
        reader.transceive(b"\x26", 7, 2)
        self.assertEqual(bus.thru_timeouts[-1], 15)  # 1.6 s
        reader.set_timeout(None)
        reader.transceive(b"\x52", 7, 2)
        self.assertEqual(bus.thru_timeouts[-1], 0x0A)

    def test_classic_authentication_through_the_radio_registers(self):
        card = SimCard()
        bus, link = pn532_setup(card)
        link.poll()
        self.assertEqual(link.read(0, 16), bytes(16))
        self.assertTrue(link.reader.crypto_on and bus.chip.crypto_on)
        link.release()
        self.assertFalse(bus.chip.crypto_on)

    def test_wrong_key_is_an_error_and_leaves_crypto_off(self):
        bus, link = pn532_setup(SimCard(key=b"\xd3\xf7\xd3\xf7\xd3\xf7"))
        link.poll()
        started = time.monotonic()
        with self.assertRaises(NFCError):
            link.read(0, 16)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(link.reader.crypto_on)

    def test_record_roundtrip_on_both_families(self):
        for card in (SimCard(), ntag()):
            bus, link = pn532_setup(card)
            link.poll()
            link.write_record(bytes(range(61)))
            link.poll()
            self.assertEqual(link.read_record(), bytes(range(61)))
            link.deinit()
            self.assertFalse(bus.chip.field_on)

    def test_card_written_by_one_reader_reads_on_the_other(self):
        card = SimCard()
        first = NFC(nfc_ws1850s.WS1850S(i2c=SimChip(card)))
        second = NFC(PN532(i2c=SimPN532(SimChip(card))))
        for writer, reader in ((first, second), (second, first)):
            for link in (writer, reader):
                link.init()
                link.field(True)
                link.poll()
            payload = os.urandom(45)
            writer.write_record(payload)
            writer.deinit()
            card.reset()
            reader.poll()
            self.assertEqual(reader.read_record(), payload)
            reader.deinit()
            card.reset()


class Pn532SelectionTest(unittest.TestCase):
    """nfc.open_reader(): the PN532 when the first reader is not there"""

    def pick(self, unit2, pn532, rc522):
        with mock.patch.object(nfc_ws1850s, "is_present", lambda: unit2), \
                mock.patch.object(nfc_pn532, "is_present", lambda: pn532), \
                mock.patch.object(nfc_mfrc522, "is_present", lambda: rc522), \
                mock.patch.object(nfc, "_drivers", None):
            return type(nfc.open_reader()), nfc.reader_name()

    def test_order(self):
        self.assertEqual(self.pick(False, True, False), (PN532, "PN532 (I2C)"))
        self.assertEqual(self.pick(False, True, True), (PN532, "PN532 (I2C)"))
        self.assertEqual(self.pick(True, True, True)[1], "RFID Unit 2 (I2C)")
        self.assertEqual(self.pick(False, False, True)[1], "RC522 (SPI)")

    def test_spi_is_not_touched_when_the_pn532_answers(self):
        def boom():
            raise AssertionError("SPI probed")

        with mock.patch.object(nfc_ws1850s, "is_present", lambda: False), \
                mock.patch.object(nfc_pn532, "is_present", lambda: True), \
                mock.patch.object(nfc_mfrc522, "is_present", boom):
            self.assertIs(type(nfc.open_reader()), PN532)


@unittest.skipUnless(cards.base.READY, "needs the embit and cryptography packages")
class Pn532JavacardDiagnosticTest(unittest.TestCase):
    """test_nfc_javacard, the script run on the board, with a PN532 on the bus"""

    def run_diag(self, card, **options):
        jc = cards.base.diag
        bus = SimPN532(SimChip(card))
        output = io.StringIO()
        options.setdefault("rounds", 2)
        options.setdefault("seconds", 0)
        with mock.patch.object(jc.nfc_ws1850s, "is_present", lambda: False), \
                mock.patch.object(jc.nfc_pn532, "is_present", lambda: True), \
                mock.patch.object(jc, "DiagReaderPN532",
                                  lambda cls=jc.DiagReaderPN532: cls(i2c=bus)), \
                contextlib.redirect_stdout(output):
            result = jc.run(**options)
        self.assertFalse(bus.chip.field_on, "the field must be off when it returns")
        return result, output.getvalue()

    def test_pass(self):
        card = IsoDepCard(cards.base.Applet(), inf_size=20)
        card.wtx = 3
        result, text = self.run_diag(card)
        self.assertTrue(result["ok"], text)
        self.assertIn("PN532 at 0x24 on I2C", text)
        self.assertEqual(result["passed"], {"es": 2, "ee": 2})

    def test_boost_reaches_the_radio_registers(self):
        result, text = self.run_diag(IsoDepCard(cards.base.Applet()), boost=True, modes=("es",))
        self.assertTrue(result["ok"], text)
        self.assertIn("RFCfg=70 GsN=f8 CWGsP=3f (boost)", text)

    def test_card_dying_mid_handshake_is_still_called_power(self):
        card = IsoDepCard(cards.base.Applet())
        card.brownout_ins = 0xB4
        result, text = self.run_diag(card, modes=("es",))
        self.assertFalse(result["ok"])
        self.assertIn("RESET", result["probes"])
        self.assertIn("POWER", text)


# ---------- The suites written for the first reader, through a PN532 ----------


class _OverPn532:
    module = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._saved = {
            name: getattr(cls.module, name)
            for name in ("WS1850S", "setup")
            if hasattr(cls.module, name)
        }
        cls.module.WS1850S = over_pn532
        if "setup" in cls._saved:
            cls.module.setup = cls._setup

    @staticmethod
    def _setup(card):
        chip = SimChip(card)
        link = NFC(over_pn532(chip))
        link.init()
        link.field(True)
        return chip, link

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._saved.items():
            setattr(cls.module, name, value)
        super().tearDownClass()


class Pn532SelectTest(_OverPn532, tags.SelectTest):
    module = tags


class Pn532RecordTest(_OverPn532, tags.RecordTest):
    module = tags


class Pn532SeedFlowTest(_OverPn532, seeds.SeedFlowTest):
    module = seeds


class Pn532SettingsTest(_OverPn532, seeds.SettingsTest):
    module = seeds

    def test_something_else_on_the_bus(self):
        # a device that answers at the address and is not a PN532
        saved = nfc.open_reader
        nfc.open_reader = lambda: PN532(i2c=SimPN532(self.chip, ic=0x07))
        try:
            gui = seeds.FakeGUI()
            self.assertFalse(self.run_test(gui))
            self.assertEqual(gui.titles("alert"), ["NFC reader error"])
        finally:
            nfc.open_reader = saved


class Pn532SmartcardKeystoreTest(_OverPn532, cards.SmartcardKeystoreTest):
    module = cards


class Pn532ConnectionTest(_OverPn532, cards.ConnectionTest):
    module = cards

    def test_timeout_goes_back_to_the_short_default(self):
        chip = SimChip(IsoDepCard(cards.base.echo)) if cards.READY else None
        if chip is None:
            self.skipTest("needs embit and cryptography")
        bus = SimPN532(chip)
        conn = cards.smartcard.CardConnection(PN532(i2c=bus))
        conn.connect()
        reader = conn._link.reader
        conn.transmit(b"\x00\x10\x00\x00")
        self.assertEqual(reader.timeout_ms, 39)
        conn._dep.deselect()
        self.assertIsNone(reader.timeout_ms)
        reader.transceive(b"\x52", 7, 2)
        self.assertEqual(bus.thru_timeouts[-1], 0x0A)
        conn.disconnect()


if __name__ == "__main__":
    unittest.main()
