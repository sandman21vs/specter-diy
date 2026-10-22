"""ISO-DEP and the JavaCard diagnostic, against a simulated smartcard.

The code under test is ports/esp32p4/test_nfc_javacard.py, the script that is
run on the board. Here it talks to nfc_sim.IsoDepCard through the real driver,
with a stand-in for the MemoryCard applet doing the card's side of the secure
channel.

    python3 -m unittest discover -s test/tests_native -p "test_nfc_isodep.py"

Needs the `embit` and `cryptography` packages; skipped without them.
"""
import contextlib
import hashlib
import hmac
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

if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000)
if not hasattr(time, "ticks_add"):
    time.ticks_add = lambda a, b: a + b

_P = 2 ** 256 - 2 ** 32 - 977


def _add(a, b):
    """Affine addition on secp256k1; None is the point at infinity"""
    if a is None or b is None:
        return a or b
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        slope = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (slope * slope - a[0] - b[0]) % _P
    return x, (slope * (a[0] - x) - a[1]) % _P


class _Secp:
    """embit's secp256k1 plus ec_pubkey_tweak_mul.

    Under the test path embit falls back to its pure Python backend, which has
    no point multiplication by a scalar. The firmware's version changes the key
    in place; this one returns a new key, and the code under test takes both.
    """

    def __init__(self, backend):
        self._backend = backend

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def ec_pubkey_tweak_mul(self, pub, tweak):
        raw = self._backend.ec_pubkey_serialize(pub, self._backend.EC_UNCOMPRESSED)
        point = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
        result = None
        for bit in bin(int.from_bytes(tweak, "big"))[2:]:
            result = _add(result, result)
            if bit == "1":
                result = _add(result, point)
        return self._backend.ec_pubkey_parse(
            b"\x04" + result[0].to_bytes(32, "big") + result[1].to_bytes(32, "big")
        )


try:
    import aes_shim
    from embit.util import secp256k1

    secp256k1 = _Secp(secp256k1)
    sys.modules.setdefault("ucryptolib", aes_shim)
    sys.modules.setdefault("secp256k1", secp256k1)

    import nfc
    from nfc_sim import IsoDepCard, SimCard, SimChip

    # Loaded by path: the directory it lives in has a test_nfc.py of its own
    spec = importlib.util.spec_from_file_location(
        "nfc_javacard_diag", os.path.join(ROOT, "ports", "esp32p4", "test_nfc_javacard.py")
    )
    diag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag)
    # run_native_tests.py stubs both of these with fakes; the handshake needs
    # the real thing
    diag.secp256k1 = secp256k1
    diag.aes = aes_shim.aes
    READY = True
except ImportError:
    READY = False

AID = b"\xB0\x0B\x51\x11\xCB\x01"
OK = b"\x90\x00"


def mac(key, data):
    return hmac.new(key, data, "sha256").digest()[:14]


class Applet:
    """The card's side of the MemoryCard secure channel"""

    PIN_MAX = 10

    def __init__(self, aid=AID, seed=b"card static key"):
        self.aid = aid
        self.secret = hashlib.sha256(seed).digest()
        # what survives a power cycle
        self.pin = None
        self.pin_left = self.PIN_MAX
        self.stored = b""
        self.commands = []  # secure commands, in order: (code, data)
        self.reset()

    def reset(self):
        """Field lost: everything in RAM is gone"""
        self.selected = False
        self.keys = None
        self.iv = 0
        self.unlocked = False

    def _status(self):
        if self.pin is None:
            state = 0
        elif self.pin_left == 0:
            state = 3
        else:
            state = 2 if self.unlocked else 1
        return bytes([self.pin_left, self.PIN_MAX, state])

    def _secure(self, plain):
        """One command inside the channel. Returns status word and data."""
        code, data = plain[:2], plain[2:]
        self.commands.append((code, data))
        if code == b"\x00\x00":
            return OK + data
        if code == b"\x03\x00":
            return OK + self._status()
        if code == b"\x03\x04":  # set PIN
            if self.pin is not None:
                return b"\x05\x01"
            self.pin, self.unlocked = data, True
            return OK
        if code == b"\x03\x01":  # unlock
            if self.pin_left == 0:
                return b"\x05\x03"
            if data != self.pin:
                self.pin_left -= 1
                return b"\x05\x03" if self.pin_left == 0 else b"\x05\x02"
            self.pin_left, self.unlocked = self.PIN_MAX, True
            return OK
        if code == b"\x03\x02":
            self.unlocked = False
            return OK
        if code == b"\x03\x03":  # change PIN: len old, old, len new, new
            old, new = data[1:33], data[34:66]
            if not self.unlocked or old != self.pin:
                return b"\x05\x02"
            self.pin = new
            return OK
        if code in (b"\x05\x00", b"\x05\x01"):
            if self.pin is not None and not self.unlocked:
                return b"\x05\x04"
            if code == b"\x05\x01":
                self.stored = data
                return OK
            return OK + self.stored
        return b"\x6D\x00"

    def _x(self, host_pub, secret):
        point = secp256k1.ec_pubkey_parse(host_pub)
        point = secp256k1.ec_pubkey_tweak_mul(point, secret) or point
        return secp256k1.ec_pubkey_serialize(point)[1:33]

    def _finish(self, shared, signed):
        self.keys = {
            name: hashlib.sha256(name.encode() + shared).digest()
            for name in ("host_aes", "card_aes", "host_mac", "card_mac")
        }
        self.iv = 0
        tag = mac(self.keys["card_mac"], signed)
        sig = secp256k1.ecdsa_sign(hashlib.sha256(signed + tag).digest(), self.secret)
        return signed + tag + secp256k1.ecdsa_signature_serialize_der(sig) + OK

    def __call__(self, apdu):
        if apdu[:4] == b"\x00\xA4\x04\x00":
            self.selected = apdu[5:] == self.aid
            return OK if self.selected else b"\x6A\x82"
        if not self.selected or apdu[0] != 0xB0:
            return b"\x6D\x00"
        ins, data = apdu[1], apdu[5:]
        if ins == 0xB2:
            pub = secp256k1.ec_pubkey_create(self.secret)
            return secp256k1.ec_pubkey_serialize(pub, secp256k1.EC_UNCOMPRESSED) + OK
        if ins == 0xB4:
            nonce = os.urandom(32)
            shared = hashlib.sha256(self._x(data, self.secret) + nonce).digest()
            return self._finish(shared, nonce)
        if ins == 0xB5:
            ephemeral = os.urandom(32)
            pub = secp256k1.ec_pubkey_serialize(
                secp256k1.ec_pubkey_create(ephemeral), secp256k1.EC_UNCOMPRESSED
            )
            return self._finish(hashlib.sha256(self._x(data, ephemeral)).digest(), pub)
        if ins == 0xB6 and self.keys:
            iv = self.iv.to_bytes(16, "big")
            ct, tag = data[:-14], data[-14:]
            if mac(self.keys["host_mac"], iv + ct) != tag:
                return b"\x69\x82"
            plain = aes_shim.aes(self.keys["host_aes"], 2, iv).decrypt(ct)
            plain = plain[: plain.rindex(b"\x80")]
            answer = self._secure(plain)
            answer += b"\x80"
            answer += bytes(-len(answer) % 16)
            ct = aes_shim.aes(self.keys["card_aes"], 2, iv).encrypt(answer)
            self.iv += 1
            return ct + mac(self.keys["card_mac"], iv + ct) + OK
        return b"\x6D\x00"


def echo(apdu):
    """Answers with the command data, so both directions can be checked"""
    return apdu[5:] + OK


def activate(card):
    """A selected card with the protocol up"""
    chip = SimChip(card)
    link = nfc.NFC(diag.DiagReader(i2c=chip))
    link.init()
    link.reader.field(True)
    diag._select(link)
    dep = diag.IsoDep(link.reader)
    dep.activate()
    return link, dep


@unittest.skipUnless(READY, "needs the embit and cryptography packages")
class AtsTest(unittest.TestCase):
    def test_typical(self):
        fsc, fwi, sfgi, ta1, tc1, hist = diag.parse_ats(bytes.fromhex("0578807002"))
        self.assertEqual((fsc, fwi, sfgi, ta1, tc1, hist), (256, 7, 0, 0x80, 0x02, b""))

    def test_defaults_and_historical_bytes(self):
        self.assertEqual(diag.parse_ats(b"\x01")[:3], (32, 4, 0))
        self.assertEqual(diag.parse_ats(bytes.fromhex("04054a43"))[5], b"JC")

    def test_reserved_values_fall_back(self):
        fsc, fwi, sfgi = diag.parse_ats(bytes.fromhex("032fff"))[:3]
        self.assertEqual((fsc, fwi, sfgi), (256, 4, 0))

    def test_hostile(self):
        for ats in ("", "00", "0378", "0578", "027880", "0270", "16" + "00" * 21):
            with self.assertRaises(diag.IsoDepError, msg=ats):
                diag.parse_ats(bytes.fromhex(ats))


@unittest.skipUnless(READY, "needs the embit and cryptography packages")
class IsoDepTest(unittest.TestCase):
    def apdu(self, size):
        return b"\x00\x10\x00\x00" + bytes([size]) + bytes(range(size))

    def test_single_block(self):
        card = IsoDepCard(echo)
        _, dep = activate(card)
        self.assertEqual(dep.fsc, 64)
        self.assertEqual(dep.fwt_ms, 39)
        self.assertEqual(dep.exchange(self.apdu(8)), bytes(range(8)) + OK)
        self.assertEqual(dep.exchange(self.apdu(3)), bytes(range(3)) + OK)

    def test_chaining_both_ways(self):
        card = IsoDepCard(echo, inf_size=17)
        _, dep = activate(card)
        self.assertEqual(dep.exchange(self.apdu(200)), bytes(range(200)) + OK)
        self.assertEqual(card.apdus, [self.apdu(200)])
        # and the block numbers are still in step afterwards
        self.assertEqual(dep.exchange(self.apdu(1)), b"\x00" + OK)

    def test_small_card_frame(self):
        # FSCI 2: the card takes 32 byte frames, so 29 bytes of INF at a time
        card = IsoDepCard(echo, ats=bytes.fromhex("0572807002"))
        frames = []
        card.tamper = lambda block: block
        original = card.exchange
        card.exchange = lambda f, b, e: (frames.append(len(f)), original(f, b, e))[1]
        _, dep = activate(card)
        self.assertEqual(dep.fsc, 32)
        self.assertEqual(dep.exchange(self.apdu(100)), bytes(range(100)) + OK)
        self.assertLessEqual(max(frames), 32)

    def test_no_frame_exceeds_the_fifo(self):
        card = IsoDepCard(echo)
        sizes = []
        original = card.exchange
        card.exchange = lambda f, b, e: (sizes.append(len(f)), original(f, b, e))[1]
        _, dep = activate(card)
        dep.exchange(self.apdu(250))
        self.assertEqual(max(sizes), 64)

    def test_wtx(self):
        card = IsoDepCard(echo)
        card.wtx, card.wtxm = 5, 3
        _, dep = activate(card)
        self.assertEqual(dep.exchange(self.apdu(4)), bytes(range(4)) + OK)
        self.assertEqual((dep.wtx, dep.max_wtxm), (5, 3))

    def test_command_lost_on_the_way_in(self):
        card = IsoDepCard(echo)
        _, dep = activate(card)
        card.mute = 1
        self.assertEqual(dep.exchange(self.apdu(4)), bytes(range(4)) + OK)
        self.assertEqual(len(card.apdus), 1)
        self.assertEqual((dep.timeouts, dep.resent), (1, 1))

    def test_answer_lost_on_the_way_out(self):
        card = IsoDepCard(echo)
        _, dep = activate(card)
        dropped = []
        card.tamper = lambda block: block if dropped else dropped.append(1)
        self.assertEqual(dep.exchange(self.apdu(4)), bytes(range(4)) + OK)
        # the card ran the command once and repeated its answer
        self.assertEqual(len(card.apdus), 1)
        self.assertEqual((dep.timeouts, dep.resent), (1, 0))

    def test_lost_frames_in_the_middle_of_chains(self):
        card = IsoDepCard(echo, inf_size=20)
        _, dep = activate(card)
        count = [0]

        def lossy(block):
            count[0] += 1
            return None if count[0] in (2, 6) else block

        card.tamper = lossy
        self.assertEqual(dep.exchange(self.apdu(150)), bytes(range(150)) + OK)
        self.assertEqual(len(card.apdus), 1)

    def test_silent_card(self):
        card = IsoDepCard(echo)
        _, dep = activate(card)
        card.mute = 100
        with self.assertRaises(diag.Timeout):
            dep.exchange(self.apdu(4))
        self.assertFalse(dep.active)
        self.assertEqual(card.mute, 97)  # the block and two R(NAK), no more
        with self.assertRaises(diag.IsoDepError):
            dep.exchange(self.apdu(4))

    def hostile(self, tamper=None, **knobs):
        card = IsoDepCard(knobs.pop("applet", echo))
        for name, value in knobs.items():
            setattr(card, name, value)
        _, dep = activate(card)
        card.tamper = tamper
        started = time.monotonic()
        with self.assertRaises(nfc.NFCError):
            dep.exchange(self.apdu(100))
        self.assertFalse(dep.active)
        self.assertLess(time.monotonic() - started, 2)
        return card, dep

    def test_endless_wtx(self):
        _, dep = self.hostile(wtx=10 ** 6)
        self.assertEqual(dep.wtx, diag._MAX_WTX)

    def test_bad_wtx_multiplier(self):
        for wtxm in (0, 60, 63):
            self.hostile(wtx=1, wtxm=wtxm)
        self.hostile(lambda block: b"\xF2", wtx=1)
        self.hostile(lambda block: b"\xF2\x01\x01", wtx=1)

    def test_response_too_long(self):
        self.hostile(applet=lambda apdu: bytes(300) + OK)

    def test_endless_chain(self):
        # every block says another follows
        self.hostile(lambda block: bytes([block[0] | 0x10]) + block[1:]
                     if block[0] & 0xE2 == 0x02 else block)
        self.hostile(lambda block: bytes([block[0] | 0x10])
                     if block[0] & 0xE2 == 0x02 else block)

    def test_wrong_block_number(self):
        self.hostile(lambda block: bytes([block[0] ^ 1]) + block[1:])

    def test_reply_before_the_command_is_complete(self):
        self.hostile(lambda block: b"\x02\x90\x00")

    def test_ack_for_a_final_block(self):
        card = IsoDepCard(echo)
        _, dep = activate(card)
        card.tamper = lambda block: b"\xA2"
        with self.assertRaises(diag.IsoDepError):
            dep.exchange(self.apdu(4))

    def test_ack_that_never_matches(self):
        _, dep = self.hostile(lambda block: b"\xA3")
        self.assertEqual(dep.resent, diag._MAX_RETRIES)

    def test_unknown_blocks(self):
        for block in (b"\xC2", b"\x0A\x00", b"\xB2", b"\x06\x90\x00", b"\xFA\x01"):
            self.hostile(lambda _, block=block: block)

    def test_response_without_status_word(self):
        self.hostile(applet=lambda apdu: b"\x90")

    def test_oversized_frame(self):
        _, dep = self.hostile(lambda block: block + bytes(70))
        self.assertEqual(dep.garbled, diag._MAX_RETRIES + 1)

    def test_bad_ats_is_refused(self):
        for ats in ("0678807002", "02", "0578"):
            chip = SimChip(IsoDepCard(echo, ats=bytes.fromhex(ats)))
            link = nfc.NFC(diag.DiagReader(i2c=chip))
            link.init()
            link.reader.field(True)
            diag._select(link)
            dep = diag.IsoDep(link.reader)
            with self.assertRaises(nfc.NFCError):
                dep.activate()
            self.assertFalse(dep.active)

    def test_the_stock_tag_layer_still_refuses_the_card(self):
        chip = SimChip(IsoDepCard(echo))
        link = nfc.NFC(diag.DiagReader(i2c=chip))
        link.init()
        link.field(True)
        with self.assertRaises(nfc.NFCNotFound):
            link.poll()


@unittest.skipUnless(READY, "needs the embit and cryptography packages")
class SignatureTest(unittest.TestCase):
    def test_padded_der_is_accepted(self):
        secret = hashlib.sha256(b"k").digest()
        digest = hashlib.sha256(b"m").digest()
        compact = secp256k1.ecdsa_signature_serialize_compact(
            secp256k1.ecdsa_sign(digest, secret)
        )
        r, s = b"\x00\x00" + compact[:32], b"\x00" + compact[32:]
        body = b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
        sig = diag._parse_sig(b"\x30" + bytes([len(body)]) + body)
        self.assertEqual(secp256k1.ecdsa_signature_serialize_compact(sig), compact)

    def test_garbage_is_refused(self):
        for raw in (bytes(8), b"\x30\x06\x02\x01\x01\x03\x01\x01", bytes(72)):
            with self.assertRaises((diag.ChannelError, ValueError)):
                diag._parse_sig(raw)


@unittest.skipUnless(READY, "needs the embit and cryptography packages")
class DiagnosticTest(unittest.TestCase):
    """run() from end to end, the way it is called on the board"""

    def run_diag(self, card, present=True, **options):
        chip = SimChip(card)
        chip.present = present
        output = io.StringIO()
        options.setdefault("rounds", 3)
        options.setdefault("seconds", 0)
        with mock.patch.object(diag.nfc_ws1850s, "is_present", lambda: present), \
                mock.patch.object(diag, "DiagReader",
                                  lambda: type(self).reader(i2c=chip)), \
                contextlib.redirect_stdout(output):
            result = diag.run(**options)
        self.assertFalse(chip.field_on, "the field must be off when run() returns")
        return result, output.getvalue()

    @classmethod
    def setUpClass(cls):
        if READY:
            cls.reader = diag.DiagReader

    def test_pass(self):
        card = IsoDepCard(Applet())
        result, text = self.run_diag(card)
        self.assertTrue(result["ok"], text)
        self.assertEqual(result["passed"], {"es": 3, "ee": 3})
        self.assertIn("UID 043e196aa66e80", text)
        self.assertIn("SAK 0x20", text)
        self.assertIn("FWI 7 (FWT 39 ms)", text)
        self.assertIn("SW 9000", text)
        self.assertIn("PIN status 0a0a00", text)
        self.assertIn("RESULT:  PASS.", text)

    def test_pass_with_wtx_and_short_blocks(self):
        card = IsoDepCard(Applet(), inf_size=16)
        card.wtx = 4
        result, text = self.run_diag(card, modes=("es",), trace=True)
        self.assertTrue(result["ok"], text)
        self.assertIn(">> f201", text)

    def test_marginal_link_is_reported(self):
        card = IsoDepCard(Applet())
        count = [0]

        def lossy(block):
            count[0] += 1
            return None if count[0] == 5 else block

        card.tamper = lossy
        result, text = self.run_diag(card, modes=("es",))
        self.assertTrue(result["ok"], text)
        self.assertIn("marginal", text)

    def test_go_finds_the_card_then_runs(self):
        card = IsoDepCard(Applet())
        chip = SimChip(card)
        output = io.StringIO()
        with mock.patch.object(diag.nfc_ws1850s, "is_present", lambda: True), \
                mock.patch.object(diag, "DiagReader", lambda: type(self).reader(i2c=chip)), \
                contextlib.redirect_stdout(output):
            result = diag.go(seconds=10, rounds=1, modes=("es",))
        self.assertTrue(result["ok"], output.getvalue())
        self.assertIn("good spot", output.getvalue())
        self.assertFalse(chip.field_on)

    def test_find_gives_up_without_a_card(self):
        chip = SimChip(None)
        with mock.patch.object(diag.nfc_ws1850s, "is_present", lambda: True), \
                mock.patch.object(diag, "DiagReader", lambda: type(self).reader(i2c=chip)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(diag.find(seconds=1))
        self.assertFalse(chip.field_on)

    def test_chip_timer_is_stretched(self):
        chip = SimChip(None)
        reader = diag.DiagReader(i2c=chip)
        reader.init()
        self.assertEqual([chip.reg[r] for r in (0x2A, 0x2B, 0x2C, 0x2D)], [0x8F, 0xFF, 0xFF, 0xFF])

    def test_no_reader(self):
        result, text = self.run_diag(None, present=False)
        self.assertEqual((result["ok"], result["stage"]), (False, "BUS"))

    def test_no_card(self):
        result, text = self.run_diag(None)
        self.assertEqual((result["ok"], result["stage"]), (False, "CARD"))

    def test_memory_tag_is_not_a_javacard(self):
        result, text = self.run_diag(SimCard())
        self.assertEqual((result["ok"], result["stage"]), (False, "CARD"))
        self.assertIn("SAK 0x08", text)

    def test_applet_not_selectable(self):
        result, text = self.run_diag(IsoDepCard(Applet(aid=b"\xA0\x00\x00\x01\x51")))
        self.assertEqual((result["ok"], result["stage"]), (False, "SELECT"))
        self.assertIn("SW 6a82", text)
        self.assertIn("APDUs do work over NFC", text)

    def test_card_dead_at_the_first_apdu(self):
        card = IsoDepCard(Applet())
        card.brownout_ins = 0xA4
        result, text = self.run_diag(card)
        self.assertEqual((result["ok"], result["stage"]), (False, "SELECT"))

    def test_brownout_during_the_handshake(self):
        card = IsoDepCard(Applet())
        card.brownout_ins = 0xB4
        result, text = self.run_diag(card, modes=("es",))
        self.assertFalse(result["ok"])
        self.assertEqual(result["passed"], {"es": 0})
        self.assertEqual(result["probes"], ["RESET"] * 3)
        self.assertIn("FAILED at OPEN_SE apdu", text)
        self.assertIn("POWER", text)

    def test_brownout_only_in_the_heavier_mode(self):
        card = IsoDepCard(Applet())
        card.brownout_ins = 0xB5
        result, text = self.run_diag(card)
        self.assertFalse(result["ok"])
        self.assertEqual(result["passed"], {"es": 3, "ee": 0})
        self.assertIn("FAILED at OPEN_EE apdu", text)

    def test_wrong_card_key_is_not_blamed_on_power(self):
        applet = Applet()
        card = IsoDepCard(applet)
        original = applet._finish
        # signs with a key that is not the one the card published
        def forged(shared, signed):
            applet.secret, saved = hashlib.sha256(b"other").digest(), applet.secret
            try:
                return original(shared, signed)
            finally:
                applet.secret = saved

        applet._finish = forged
        result, text = self.run_diag(card, modes=("es",))
        self.assertFalse(result["ok"])
        self.assertEqual(result["probes"], [])
        self.assertIn("FAILED at handshake signature", text)
        self.assertNotIn("POWER", text)

    def test_card_that_goes_mute_until_the_field_cycles(self):
        # what a JCOP does after a supply glitch: no reset, no answer, until
        # the field is dropped and raised again
        card = IsoDepCard(Applet())
        mute = [False]
        reset = card.reset

        def field_dropped():
            mute[0] = False
            reset()

        def glitch(block):
            if card.apdus and card.apdus[-1][1] == 0xB4:
                mute[0] = True
            return None if mute[0] else block

        card.reset = field_dropped
        card.tamper = glitch
        result, text = self.run_diag(card, modes=("es",))
        self.assertFalse(result["ok"])
        self.assertEqual(result["probes"], ["SILENT", "REVIVED"] * 3)
        self.assertIn("POWER", text)

    def test_card_that_hangs(self):
        card = IsoDepCard(Applet())

        class Hang:
            armed = False

            def __call__(self, block):
                if len(card.apdus) >= 3:  # SELECT, GET_PUBKEY, then OPEN_SE
                    return None
                return block

        card.tamper = Hang()
        result, text = self.run_diag(card, modes=("es",), rounds=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["probes"], ["SILENT"])
        self.assertNotIn("POWER", text)


if __name__ == "__main__":
    unittest.main()
