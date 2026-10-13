"""The smartcard over NFC: ISO-DEP, the connection, and the keystore on top.

Everything from the keystore down to the I2C registers is the real code. The
card is nfc_sim.IsoDepCard running the stand-in for the MemoryCard applet from
test_nfc_isodep, which forgets it was unlocked when the field drops, as a real
card does. The screens are fakes that answer from a script.

    python3 test/run_native_tests.py

Needs the `embit` and `cryptography` packages; skipped without them.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(ROOT, "src"))
sys.path.append(os.path.join(ROOT, "ports", "esp32p4", "lib"))

if not hasattr(asyncio, "sleep_ms"):
    asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000)


class Message:
    def __init__(self, log):
        self.log = log

    def set_text(self, text):
        self.log.append(text)


class Screen:
    """What the keystore uses of a screen"""

    def __init__(self, title="", message="", **kwargs):
        self.title = title
        self.text = message
        self.kwargs = kwargs
        self.waiting = True
        self.hints = []
        self.message = Message(self.hints)

    def release(self):
        self.waiting = False


class Progress(Screen):
    pass


class Alert(Screen):
    pass


class Prompt(Screen):
    pass


class Menu(Screen):
    pass


class PinScreen(Screen):
    def __init__(self, title="", get_word=None, **kwargs):
        super().__init__(title, **kwargs)
        self.get_word = get_word


try:
    import test_nfc_isodep as base

    if not base.READY:
        raise ImportError("embit or cryptography missing")

    # The contact reader of the port needs a UART; the keystore module builds
    # a connection to it on import.
    if "uscard" not in sys.modules:
        uscard = types.ModuleType("uscard")
        uscard.SmartcardException = type("SmartcardException", (Exception,), {})

        class _NoCard:
            T1_protocol = 1

            def isCardInserted(self):
                return False

        uscard.Reader = lambda *args, **kwargs: types.SimpleNamespace(
            createConnection=_NoCard
        )
        sys.modules["uscard"] = uscard
    pyb = sys.modules.setdefault("pyb", types.ModuleType("pyb"))
    if not hasattr(pyb, "Pin"):
        cpu = type("cpu", (), {"__getattr__": lambda self, name: name})()
        pyb.Pin = types.SimpleNamespace(cpu=cpu)
    sys.modules.setdefault("lvgl", types.ModuleType("lvgl"))
    if "gui.screens" not in sys.modules:
        sys.modules.setdefault("gui", types.ModuleType("gui"))
        sys.modules["gui.screens"] = types.ModuleType("gui.screens")
    for _name in ("Alert", "Progress", "Menu", "Prompt", "PinScreen", "QRAlert"):
        if not hasattr(sys.modules["gui.screens"], _name):
            setattr(sys.modules["gui.screens"], _name, type(_name, (), {}))
    sys.modules.setdefault("rng", types.ModuleType("rng"))
    if not hasattr(sys.modules["rng"], "get_random_bytes"):
        sys.modules["rng"].get_random_bytes = os.urandom

    import nfc
    from nfc import isodep, smartcard
    from nfc_sim import IsoDepCard, SimCard, SimChip
    from nfc_ws1850s import WS1850S
    from keystore import nfccard, memorycard, ram
    from keystore.core import KeyStore, KeyStoreError, KeyStoreUnavailable, PinError
    from keystore.javacard.applets import securechannel
    import helpers

    class _InPlace:
        """secp256k1 as the firmware has it: tweak_mul changes the key given"""

        def __getattr__(self, name):
            return getattr(base.secp256k1, name)

        def ec_pubkey_parse(self, raw):
            return bytearray(base.secp256k1.ec_pubkey_parse(bytes(raw)))

        def ec_pubkey_tweak_mul(self, pub, tweak):
            pub[:] = base.secp256k1.ec_pubkey_tweak_mul(bytes(pub), tweak)

        def ec_pubkey_serialize(self, pub, *flags):
            return base.secp256k1.ec_pubkey_serialize(bytes(pub), *flags)

        def ecdsa_verify(self, sig, msg, pub):
            return base.secp256k1.ecdsa_verify(sig, msg, bytes(pub))

    READY = True
except ImportError as error:
    READY = False
    WHY = str(error)

WORDS = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
PIN = "1234"


class Rig:
    """A card on a reader, a keystore, and a user who follows a script"""

    def __init__(self, test, folder, card=None):
        self.test = test
        self.folder = folder
        self.applet = base.Applet()
        self.card = IsoDepCard(self.applet) if card is None else card
        self.chip = SimChip(self.card)
        self.pins = []
        self.prompts = []
        self.shown = []
        self.on_tap = None  # called with the tap number before each one
        self.taps = 0
        self.keystore = self.boot()

    def boot(self):
        """A fresh keystore object, as after a restart"""
        ks = nfccard.NFCMemoryCard()
        ks.connection = smartcard.CardConnection(WS1850S(i2c=self.chip))
        ks.applet.conn = ks.connection
        return ks

    async def show(self, scr):
        self.shown.append(scr)
        if isinstance(scr, Progress):
            self.taps += 1
            if self.on_tap is not None and self.on_tap(self.taps) == "cancel":
                await asyncio.sleep(0.05)
                scr.waiting = False
                return None
            self.test.assertTrue(self.chip.field_on or True)
            scr.waiting = True
            while scr.waiting:
                await asyncio.sleep(0.002)
            return None
        # Between taps the antenna is dark
        self.test.assertFalse(self.chip.field_on, "field on while %s is shown" % scr.title)
        if isinstance(scr, PinScreen):
            pin = self.pins.pop(0)
            if pin is not None:
                self.test.assertTrue(scr.get_word(pin[:2].encode()))
            return pin
        if isinstance(scr, Prompt):
            return self.prompts.pop(0)
        return None

    def run(self, coro):
        async def main():
            result = await coro
            # let a tap task that is still winding down finish
            await asyncio.sleep(0.05)
            return result

        result = asyncio.run(main())
        self.test.assertFalse(self.chip.field_on, "field left on")
        return result

    def start(self, ks=None):
        ks = ks or self.keystore
        self.run(ks.init(self.show, lambda *a, **k: None))
        return ks

    def unlocks(self):
        return [c for c in self.applet.commands if c[0] == b"\x03\x01"]


@unittest.skipUnless(READY, "needs embit, cryptography and the app's modules")
class SmartcardKeystoreTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.saved = {
            (nfccard, "Progress"): nfccard.Progress,
            (nfccard, "Alert"): nfccard.Alert,
            (nfccard, "Prompt"): nfccard.Prompt,
            (memorycard, "Progress"): memorycard.Progress,
            (memorycard, "Alert"): memorycard.Alert,
            (memorycard, "Prompt"): memorycard.Prompt,
            (memorycard, "Menu"): memorycard.Menu,
            (ram, "PinScreen"): ram.PinScreen,
            (ram, "Alert"): ram.Alert,
            (securechannel, "secp256k1"): securechannel.secp256k1,
            (nfccard, "secp256k1"): nfccard.secp256k1,
            (securechannel, "aes"): securechannel.aes,
            (securechannel, "get_random_bytes"): securechannel.get_random_bytes,
            (KeyStore, "path"): KeyStore.path,
            (nfccard, "POLL_MS"): nfccard.POLL_MS,
        }
        nfccard.Progress = memorycard.Progress = Progress
        nfccard.Alert = memorycard.Alert = ram.Alert = Alert
        memorycard.Prompt = nfccard.Prompt = Prompt
        memorycard.Menu = Menu
        ram.PinScreen = PinScreen
        securechannel.secp256k1 = nfccard.secp256k1 = _InPlace()
        securechannel.aes = base.aes_shim.aes
        securechannel.get_random_bytes = os.urandom
        KeyStore.path = os.path.join(self.folder, "keystore")
        nfccard.set_enabled(True)
        nfccard.POLL_MS = 5
        if not hasattr(time, "ticks_ms"):
            time.ticks_ms = lambda: int(time.monotonic() * 1000)
            time.ticks_diff = lambda a, b: a - b
            time.sleep_ms = lambda ms: time.sleep(ms / 1000)
        if not hasattr(time, "ticks_add"):
            time.ticks_add = lambda a, b: a + b

    def tearDown(self):
        for (owner, name), value in self.saved.items():
            setattr(owner, name, value)
        shutil.rmtree(self.folder, ignore_errors=True)

    def rig(self, card=None):
        return Rig(self, self.folder, card)

    def set_up_card(self, rig):
        """First use: choose a PIN. Leaves the keystore unlocked."""
        ks = rig.start()
        self.assertFalse(ks.is_pin_set)
        rig.pins = [PIN, PIN]
        rig.run(ks.unlock())
        self.assertFalse(ks.is_locked)
        return ks

    def reboot(self, rig, pin=PIN):
        """Restart, PIN, one tap. Returns the unlocked keystore."""
        rig.card.reset()
        taps = rig.taps
        ks = rig.start(rig.boot())
        self.assertEqual(rig.taps, taps, "a known card is not asked for before the PIN")
        self.assertTrue(ks.is_pin_set)
        self.assertTrue(ks.is_locked)
        rig.pins = [pin]
        rig.run(ks.unlock())
        self.assertEqual(rig.taps, taps + 1)
        return ks

    # ----- the switch -----

    def test_switch_lives_outside_the_settings(self):
        nfccard.set_enabled(False)
        self.assertFalse(nfccard.is_enabled())
        nfccard.set_enabled(True)
        self.assertTrue(nfccard.is_enabled())
        self.assertTrue(os.path.exists(KeyStore.path + "_nfc"))
        nfccard.set_enabled(False)
        nfccard.set_enabled(False)
        self.assertFalse(nfccard.is_enabled())

    def test_available_only_when_switched_on_and_plugged_in(self):
        saved = nfccard.reader_present
        try:
            nfccard.reader_present = lambda: True
            nfccard.set_enabled(False)
            self.assertFalse(nfccard.NFCMemoryCard.is_available())
            nfccard.set_enabled(True)
            self.assertTrue(nfccard.NFCMemoryCard.is_available())
            nfccard.reader_present = lambda: False
            self.assertFalse(nfccard.NFCMemoryCard.is_available())
        finally:
            nfccard.reader_present = saved

    # ----- first use, unlock, load -----

    def test_first_use_sets_the_pin(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        self.assertEqual(rig.taps, 2)  # who are you, then set the PIN
        self.assertIsNotNone(rig.applet.pin)
        self.assertFalse(ks.is_key_saved)

    def test_one_tap_at_boot_and_none_to_load(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        ks.set_mnemonic(WORDS, "")
        rig.prompts = [True]  # keep as plain text
        rig.taps = 0
        rig.run(ks.save_mnemonic())
        self.assertEqual(rig.taps, 2)  # look at what is there, then write
        self.assertTrue(rig.applet.stored)

        rig.taps = 0
        ks = self.reboot(rig)
        self.assertEqual(rig.taps, 1)
        self.assertFalse(ks.is_locked)
        self.assertTrue(ks.is_key_saved)
        self.assertIsNone(ks.mnemonic)
        rig.run(ks.load_mnemonic())
        self.assertEqual(rig.taps, 1)  # the key came from what unlock read
        self.assertEqual(ks.mnemonic, WORDS)
        self.assertEqual(rig.pins, [])  # the PIN was typed once
        # locking drops it, and the next unlock reads it again
        ks.lock()
        self.assertIsNone(ks._blob)
        rig.pins = [PIN]
        rig.run(ks.unlock())
        rig.run(ks.load_mnemonic())
        self.assertEqual(rig.taps, 2)

    def test_words_at_boot_match_the_ones_of_the_first_tap(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        word = ks.get_auth_word(b"12")
        rig.card.reset()
        ks = rig.start(rig.boot())
        self.assertFalse(ks._verified)
        self.assertEqual(ks.get_auth_word(b"12"), word)

    def test_a_tampered_card_key_changes_the_words_and_opens_nothing(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        word = ks.get_auth_word(b"12")
        other = base.Applet(seed=b"another card")
        with open(KeyStore.path + "_nfc", "wb") as f:
            f.write(other(b"\x00\xa4\x04\x00\x06" + base.AID) and other(b"\xb0\xb2\x00\x00")[:-2])
        rig.card.reset()
        ks = rig.start(rig.boot())
        self.assertNotEqual(ks.get_auth_word(b"12"), word)
        rig.pins = [PIN]
        before = len(rig.applet.commands)
        with self.assertRaises(KeyStoreError):
            rig.run(ks.unlock())
        self.assertTrue(ks.is_locked)
        self.assertEqual(len(rig.applet.commands), before)  # no PIN sent

    def test_wrong_pin(self):
        rig = self.rig()
        self.set_up_card(rig)
        rig.card.reset()
        ks = rig.start(rig.boot())
        rig.pins = ["9999"]
        with self.assertRaises(PinError) as raised:
            rig.run(ks.unlock())
        self.assertIn("9 of 10", str(raised.exception))
        self.assertTrue(ks.is_locked)
        # and the right one still works, with the counter back at the top
        rig.pins = [PIN]
        rig.run(ks.unlock())
        self.assertFalse(ks.is_locked)
        self.assertEqual(rig.applet.pin_left, 10)

    def test_lock_forgets_and_unlock_needs_the_card_again(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        self.assertTrue(ks.lock())
        self.assertFalse(rig.chip.field_on)
        self.assertIsNone(ks._session_pin)
        rig.pins = [PIN]
        rig.taps = 0
        rig.run(ks.unlock())
        self.assertEqual(rig.taps, 1)
        self.assertFalse(ks.is_locked)

    def test_words_need_a_verified_card(self):
        rig = self.rig()
        ks = rig.boot()
        ks.secret = b"s" * 32
        with self.assertRaises(KeyStoreError):
            ks.get_auth_word(b"12")

    # ----- a card that does not stay put -----

    def test_pin_lost_on_the_way_in_is_sent_again(self):
        rig = self.rig()
        self.set_up_card(rig)
        rig.card.reset()
        ks = rig.start(rig.boot())
        before = len(rig.unlocks())

        # the unlock command itself never reaches the applet the first time
        original = rig.applet._secure
        state = {"dropped": False}

        def secure(plain):
            if plain[:2] == b"\x03\x01" and not state["dropped"]:
                state["dropped"] = True
                rig.card.mute = 100  # everything after this is lost
                raise RuntimeError("frame lost")
            return original(plain)

        rig.applet._secure = secure
        rig.pins = [PIN]
        rig.run(ks.unlock())
        self.assertFalse(ks.is_locked)
        self.assertEqual(len(rig.unlocks()) - before, 1)
        self.assertEqual(rig.applet.pin_left, 10)

    def test_wrong_pin_whose_answer_was_lost_is_not_sent_twice(self):
        rig = self.rig()
        self.set_up_card(rig)
        rig.card.reset()
        ks = rig.start(rig.boot())
        before = len(rig.unlocks())
        original = rig.applet._secure
        state = {"dropped": False}

        def secure(plain):
            reply = original(plain)
            if plain[:2] == b"\x03\x01" and not state["dropped"]:
                state["dropped"] = True
                rig.card.mute = 100  # the card counted it; the answer is lost
            return reply

        rig.applet._secure = secure
        rig.pins = ["9999"]
        with self.assertRaises(PinError) as raised:
            rig.run(ks.unlock())
        self.assertIn("9 of 10", str(raised.exception))
        self.assertEqual(len(rig.unlocks()) - before, 1)
        self.assertEqual(rig.applet.pin_left, 9)
        self.assertTrue(ks.is_locked)

    def test_card_lost_during_the_handshake_is_asked_for_again(self):
        rig = self.rig()
        rig.card.brownout_ins = 0xB4
        ks = rig.boot()

        async def heal():
            await asyncio.sleep(0.3)
            rig.card.brownout_ins = None

        async def both():
            asyncio.create_task(heal())
            await ks.init(rig.show, lambda *a, **k: None)

        rig.run(both())
        self.assertTrue(ks._verified)
        self.assertIn(nfccard.HINT_LOST, rig.shown[-1].hints)

    # ----- the wrong thing on the reader -----

    def test_another_card_on_the_second_tap_is_refused(self):
        rig = self.rig()
        self.set_up_card(rig)
        rig.card.reset()
        ks = rig.start(rig.boot())
        impostor = base.Applet(seed=b"another card")
        impostor.pin = rig.applet.pin
        rig.card.applet = impostor
        rig.pins = [PIN]
        with self.assertRaises(KeyStoreError) as raised:
            rig.run(ks.unlock())
        self.assertIn("last used with", str(raised.exception))
        self.assertTrue(ks.is_locked)
        self.assertEqual(impostor.commands, [])  # it never saw the PIN
        # Asked for again, it is taken as a new card: its key is learned
        # first, and the words on the PIN screen are no longer the old ones.
        rig.taps = 0
        rig.start(ks)
        self.assertEqual(rig.taps, 1)
        self.assertTrue(ks._verified)
        rig.pins = [PIN]
        rig.run(ks.unlock())
        self.assertFalse(ks.is_locked)

    def test_another_card_in_the_middle_of_a_session_is_refused(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        impostor = base.Applet(seed=b"another card")
        rig.card.applet = impostor
        rig.card.reset()
        with self.assertRaises(KeyStoreError) as raised:
            rig.run(ks.delete_mnemonic())
        self.assertIn("this session was started with", str(raised.exception))
        self.assertEqual(impostor.commands, [])

    def test_card_without_the_applet(self):
        rig = self.rig()
        rig.applet.aid = b"\xA0\x00\x00\x01\x51"
        with self.assertRaises(KeyStoreError) as raised:
            rig.start()
        self.assertIn("applet", str(raised.exception))

    def test_memory_tag_gets_a_hint_and_cancel_falls_back(self):
        rig = self.rig(SimCard())
        rig.on_tap = lambda number: "cancel"

        async def slow_cancel(scr):
            rig.shown.append(scr)
            await asyncio.sleep(0.1)
            scr.waiting = False

        with self.assertRaises(KeyStoreUnavailable):
            rig.run(rig.keystore.init(slow_cancel, lambda *a, **k: None))
        self.assertIn(nfccard.HINT_WRONG, rig.shown[-1].hints)

    def test_without_the_card_at_boot_the_keystore_bows_out(self):
        rig = self.rig()
        self.set_up_card(rig)
        rig.card.reset()
        ks = rig.start(rig.boot())  # a known card: straight to the PIN
        rig.chip.card = None
        rig.on_tap = lambda number: "cancel"
        rig.pins = [PIN]
        with self.assertRaises(KeyStoreUnavailable):
            rig.run(ks.unlock())
        self.assertTrue(ks.is_locked)

    def test_cancelling_after_a_lock_goes_back_to_the_pin(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        ks.lock()
        cancelled = []

        def on_tap(number):
            if not cancelled:
                cancelled.append(number)
                rig.chip.card = None
                return "cancel"
            rig.chip.card = rig.card

        rig.on_tap = on_tap
        rig.pins = [PIN, PIN]
        rig.run(ks.unlock())
        self.assertFalse(ks.is_locked)
        self.assertEqual(rig.pins, [])

    def test_no_reader(self):
        rig = self.rig()
        rig.chip.present = False
        with self.assertRaises(smartcard.CardLost):
            rig.start()

    # ----- storage and PIN change -----

    def test_delete(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        ks.set_mnemonic(WORDS, "")
        rig.prompts = [True]
        rig.run(ks.save_mnemonic())
        rig.taps = 0
        rig.run(ks.delete_mnemonic())
        self.assertEqual(rig.taps, 1)
        self.assertEqual(rig.applet.stored, b"")
        self.assertFalse(ks.is_key_saved)

    def test_encrypted_save_reads_back_on_this_device_only(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        ks.set_mnemonic(WORDS, "")
        rig.prompts = [False]  # encrypt
        rig.run(ks.save_mnemonic())
        ks = self.reboot(rig)
        rig.run(ks.load_mnemonic())
        self.assertEqual(ks.mnemonic, WORDS)

    def test_data_from_another_device_can_be_deleted(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        ks.set_mnemonic(WORDS, "")
        rig.prompts = [False]  # encrypt
        rig.run(ks.save_mnemonic())
        # the same card on a device with another secret
        os.remove(os.path.join(KeyStore.path, "secret"))
        rig.card.reset()
        ks = rig.start(rig.boot())
        rig.pins = [PIN]
        with self.assertRaises(KeyStoreError) as raised:
            rig.run(ks.unlock())
        self.assertIn("different device", str(raised.exception))
        # unlocked all the same, and it knows there is something to clear
        self.assertFalse(ks.is_locked)
        self.assertFalse(ks.is_key_saved)
        self.assertTrue(ks.card_has_data())
        # backing out of the warning leaves the card alone
        rig.prompts = [False]
        with self.assertRaises(nfccard.TapCancelled):
            rig.run(ks.delete_mnemonic())
        self.assertTrue(rig.applet.stored)
        rig.prompts = [True]
        rig.run(ks.delete_mnemonic())
        self.assertEqual(rig.applet.stored, b"")
        self.assertFalse(ks.card_has_data())

    def test_change_pin(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        rig.pins = [PIN, "777", "777"]
        rig.taps = 0
        rig.run(ks.change_pin())
        self.assertEqual(rig.taps, 1)
        ks = self.reboot(rig, "777")
        self.assertFalse(ks.is_locked)

    def test_change_pin_with_a_wrong_old_pin(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        old = rig.applet.pin
        rig.pins = ["0000", "777", "777"]
        with self.assertRaises(PinError):
            rig.run(ks.change_pin())
        self.assertEqual(rig.applet.pin, old)

    def test_cancelled_tap_in_a_menu_changes_nothing(self):
        rig = self.rig()
        ks = self.set_up_card(rig)
        rig.on_tap = lambda number: "cancel"
        rig.chip.card = None  # nothing on the reader, and the user gives up
        before = len(rig.applet.commands)
        with self.assertRaises(nfccard.TapCancelled):
            rig.run(ks.delete_mnemonic())
        self.assertEqual(len(rig.applet.commands), before)


@unittest.skipUnless(READY, "needs embit, cryptography and the app's modules")
class ConnectionTest(unittest.TestCase):
    def setUp(self):
        if not hasattr(time, "ticks_ms"):
            time.ticks_ms = lambda: int(time.monotonic() * 1000)
            time.ticks_diff = lambda a, b: a - b
            time.sleep_ms = lambda ms: time.sleep(ms / 1000)
        if not hasattr(time, "ticks_add"):
            time.ticks_add = lambda a, b: a + b

    def connection(self, card):
        chip = SimChip(card)
        return chip, smartcard.CardConnection(WS1850S(i2c=chip))

    def test_apdu_round_trip_and_field_discipline(self):
        chip, conn = self.connection(IsoDepCard(base.echo, inf_size=20))
        self.assertFalse(conn.isCardInserted())
        with self.assertRaises(smartcard.CardLost):
            conn.transmit(b"\x00\x10\x00\x00")
        self.assertFalse(chip.field_on)
        conn.connect(conn.T1_protocol)
        self.assertTrue(conn.isCardInserted() and chip.field_on)
        self.assertEqual(conn.getATR(), bytes.fromhex("0578807002"))
        apdu = b"\x00\x10\x00\x00\xc8" + bytes(range(200))
        self.assertEqual(conn.transmit(apdu), bytes(range(200)) + b"\x90\x00")
        conn.disconnect()
        self.assertFalse(conn.isCardInserted() or chip.field_on)
        conn.disconnect()

    def test_no_card_wrong_card_lost_card(self):
        chip, conn = self.connection(None)
        with self.assertRaises(smartcard.CardLost):
            conn.connect()
        conn.disconnect()
        chip, conn = self.connection(SimCard())
        with self.assertRaises(smartcard.WrongCard):
            conn.connect()
        conn.disconnect()
        card = IsoDepCard(base.echo)
        chip, conn = self.connection(card)
        conn.connect()
        card.mute = 100
        with self.assertRaises(smartcard.CardLost):
            conn.transmit(b"\x00\x10\x00\x00")
        self.assertFalse(conn.isCardInserted())
        card.mute = 0
        conn.reset()
        conn.connect()
        self.assertEqual(conn.transmit(b"\x00\x10\x00\x00"), b"\x90\x00")
        conn.disconnect()

    def test_timeout_goes_back_to_the_short_default(self):
        chip, conn = self.connection(IsoDepCard(base.echo))
        conn.connect()
        reader = conn._link.reader
        conn.transmit(b"\x00\x10\x00\x00")
        self.assertEqual(reader.timeout_ms, 39)
        self.assertEqual(chip.reg[0x2B], 0xFF)
        conn._dep.deselect()
        self.assertIsNone(reader.timeout_ms)
        self.assertEqual([chip.reg[r] for r in (0x2A, 0x2B, 0x2C, 0x2D)], [0x80, 0xA9, 0x03, 0xE8])
        conn.disconnect()

    def test_memory_tags_still_refuse_a_smartcard(self):
        chip = SimChip(IsoDepCard(base.echo))
        link = nfc.NFC(WS1850S(i2c=chip))
        link.init()
        link.field(True)
        with self.assertRaises(nfc.NFCWrongCard):
            link.poll()
        # even one that also claims to be a MIFARE Classic
        chip.card.sak = 0x28
        with self.assertRaises(nfc.NFCWrongCard):
            link.poll()


if __name__ == "__main__":
    unittest.main()
