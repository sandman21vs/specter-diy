"""The two seed flows end to end: GUI answers in, bytes on a simulated card out.

    python3 -m unittest discover -s test/tests_native -p "test_nfc_seed.py"

Needs the `embit` and `cryptography` packages; skipped without them.
"""
import asyncio
import os
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(ROOT, "src"))
sys.path.append(os.path.join(ROOT, "ports", "esp32p4", "lib"))

if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000)
if not hasattr(asyncio, "sleep_ms"):
    asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000)


class Progress:
    """Stands in for gui.screens.Progress, which needs LVGL"""

    def __init__(self, title, message, button_text="Cancel"):
        self.title = title
        self.waiting = True


try:
    import aes_shim
    import embit.bip39  # noqa: F401

    if "gui.screens" not in sys.modules:
        sys.modules.setdefault("gui", types.ModuleType("gui"))
        sys.modules["gui.screens"] = types.ModuleType("gui.screens")
    saved_progress = getattr(sys.modules["gui.screens"], "Progress", None)
    sys.modules["gui.screens"].Progress = Progress
    sys.modules.setdefault("ucryptolib", aes_shim)
    sys.modules.setdefault("rng", types.ModuleType("rng"))
    if not hasattr(sys.modules["rng"], "get_random_bytes"):
        sys.modules["rng"].get_random_bytes = os.urandom

    import kef
    import nfc
    from nfc import seed
    from nfc_sim import SimCard, SimChip
    from nfc_ws1850s import WS1850S

    kef.cryptolib = aes_shim
    seed.Progress = Progress
    if saved_progress is not None:
        sys.modules["gui.screens"].Progress = saved_progress
    READY = True
except ImportError:
    READY = False

WORDS12 = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
WORDS24 = (
    "brass creek fuel snack era success impulse dirt caution purity lottery lizard "
    "boil festival neither case swift smooth range mail gravity sample never ivory"
)
# root fingerprint of WORDS12 with no passphrase, a value published in BIP84
FINGERPRINT12 = "73c5da0a"


class FakeGUI:
    """Answers the screens from a script and records what was shown"""

    def __init__(self, prompts=(), inputs=(), cancel_tap=False):
        self.prompts = list(prompts)
        self.inputs = list(inputs)
        self.cancel_tap = cancel_tap
        self.shown = []
        self.taps = 0
        self.on_tap = None

    async def prompt(self, title, msg, popup=False):
        self.shown.append(("prompt", title, msg))
        return self.prompts.pop(0)

    async def get_input(self, title="", note="", suggestion=""):
        self.shown.append(("input", title, note))
        return self.inputs.pop(0)

    async def alert(self, title, msg, button_text="OK", note=None):
        self.shown.append(("alert", title, msg))

    async def load_screen(self, scr):
        self.shown.append(("screen", scr.title, ""))
        self.taps += 1
        if self.cancel_tap:
            scr.waiting = False
        if self.on_tap:
            self.on_tap()

    def show_loader(self, text="", title=""):
        pass

    def titles(self, kind):
        return [title for k, title, _ in self.shown if k == kind]


@unittest.skipUnless(READY, "needs the 'embit' and 'cryptography' packages")
class SeedFlowTest(unittest.TestCase):
    def setUp(self):
        self.chip = SimChip(SimCard())
        self._open_reader = nfc.open_reader
        nfc.open_reader = lambda: WS1850S(i2c=self.chip)
        # keep PBKDF2 short; the real count is covered in test_kef
        self._iterations = kef.DEFAULT_ITERATIONS
        kef.encrypt.__defaults__ = (10000, None)

    def tearDown(self):
        nfc.open_reader = self._open_reader
        kef.encrypt.__defaults__ = (self._iterations, None)

    def save(self, mnemonic, gui):
        return asyncio.run(seed.save_mnemonic(gui, mnemonic))

    def load(self, gui):
        return asyncio.run(seed.load_mnemonic(gui))

    def card_record(self):
        link = nfc.NFC(WS1850S(i2c=self.chip))
        link.init()
        link.field(True)
        link.poll()
        try:
            return link.read_record()
        finally:
            link.deinit()

    def test_backup_id_is_the_bare_fingerprint(self):
        self.assertEqual(seed.backup_id(WORDS12), FINGERPRINT12)

    def test_save_then_load(self):
        for words in (WORDS12, WORDS24):
            self.chip.card = SimCard()
            gui = FakeGUI(prompts=[True], inputs=["correct horse", "correct horse"])
            self.assertTrue(self.save(words, gui))
            self.assertFalse(self.chip.field_on)

            envelope = self.card_record()
            id_, version, iterations, _ = kef.unwrap(envelope)
            self.assertEqual(version, 20)
            self.assertEqual(id_.decode(), seed.backup_id(words))
            # the words never reach the card, sealed or not
            self.assertEqual(kef.decrypt(envelope, "correct horse"),
                             embit.bip39.mnemonic_to_bytes(words))

            gui = FakeGUI(prompts=[True], inputs=["correct horse"])
            self.assertEqual(self.load(gui), words)
            self.assertFalse(self.chip.field_on)
            self.assertIn("Fingerprint: " + seed.backup_id(words), gui.shown[-1][2])

    def test_real_iteration_count_is_written(self):
        kef.encrypt.__defaults__ = (self._iterations, None)
        gui = FakeGUI(prompts=[True], inputs=["pw", "pw"])
        self.assertTrue(self.save(WORDS12, gui))
        self.assertEqual(kef.unwrap(self.card_record())[2], 100000)

    def test_save_declined_or_cancelled(self):
        for gui in (
            FakeGUI(prompts=[False]),
            FakeGUI(prompts=[True], inputs=[None]),
            FakeGUI(prompts=[True], inputs=["pw", None]),
            FakeGUI(prompts=[True], inputs=["pw", "pw"], cancel_tap=True),
        ):
            self.assertFalse(self.save(WORDS12, gui))
            self.assertEqual(self.chip.card.writes, [])
            self.assertFalse(self.chip.field_on)

    def test_password_has_to_be_typed_twice_the_same(self):
        gui = FakeGUI(prompts=[True], inputs=["", "one", "two", "three", "three"])
        self.assertTrue(self.save(WORDS12, gui))
        self.assertEqual(gui.titles("alert")[:2], ["Empty password", "Passwords differ"])
        self.assertEqual(kef.decrypt(self.card_record(), "three"),
                         embit.bip39.mnemonic_to_bytes(WORDS12))

    def test_overwrite_is_asked_and_can_be_refused(self):
        self.assertTrue(self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"])))
        first = self.card_record()
        self.chip.card.writes.clear()

        gui = FakeGUI(prompts=[True, False], inputs=["b", "b"])
        self.assertFalse(self.save(WORDS24, gui))
        self.assertEqual(gui.titles("prompt")[-1], "Overwrite this card?")
        self.assertEqual(self.chip.card.writes, [])
        self.assertEqual(self.card_record(), first)

        gui = FakeGUI(prompts=[True, True], inputs=["b", "b"])
        self.assertTrue(self.save(WORDS24, gui))
        self.assertEqual(self.load(FakeGUI(prompts=[True], inputs=["b"])), WORDS24)

    def test_card_removed_before_the_write(self):
        self.assertTrue(self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"])))

        gui = FakeGUI(prompts=[True, True], inputs=["b", "b"])
        original = gui.prompt

        async def prompt(title, msg, popup=False):
            if title == "Overwrite this card?":
                self.chip.card = None
            return await original(title, msg, popup)

        gui.prompt = prompt
        with self.assertRaises(seed.NFCStorageError):
            self.save(WORDS24, gui)
        self.assertFalse(self.chip.field_on)

    def test_write_that_does_not_stick_is_reported(self):
        class Forgetful(SimCard):
            def exchange(self, frame, bits, encrypted):
                reply = super().exchange(frame, bits, encrypted)
                if self.writes and self.writes[-1] == 4:
                    self.blocks[4][0] ^= 0xFF
                    self.writes.append(-1)
                return reply

        self.chip.card = Forgetful()
        with self.assertRaises(seed.NFCStorageError):
            self.save(WORDS24, FakeGUI(prompts=[True], inputs=["a", "a"]))
        self.assertFalse(self.chip.field_on)

    def test_card_too_small(self):
        self.chip.card = SimCard(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00, ul_pages=16)
        with self.assertRaises(seed.NFCStorageError) as raised:
            self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"]))
        self.assertIn("too small", str(raised.exception))

    def test_no_reader(self):
        self.chip.present = False
        with self.assertRaises(seed.NFCStorageError):
            self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"]))
        with self.assertRaises(seed.NFCStorageError):
            self.load(FakeGUI())

    def test_load_wrong_password(self):
        self.assertTrue(self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"])))
        with self.assertRaises(seed.NFCStorageError) as raised:
            self.load(FakeGUI(inputs=["b"]))
        self.assertIn("Wrong password", str(raised.exception))

    def test_load_cancelled(self):
        self.assertTrue(self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"])))
        self.assertIsNone(self.load(FakeGUI(cancel_tap=True)))
        self.assertIsNone(self.load(FakeGUI(inputs=[None])))
        self.assertIsNone(self.load(FakeGUI(inputs=[""])))
        self.assertIsNone(self.load(FakeGUI(prompts=[False], inputs=["a"])))
        self.assertFalse(self.chip.field_on)

    def test_load_blank_card(self):
        with self.assertRaises(seed.NFCStorageError):
            self.load(FakeGUI())
        self.assertFalse(self.chip.field_on)

    def test_load_waits_for_the_card(self):
        self.assertTrue(self.save(WORDS12, FakeGUI(prompts=[True], inputs=["a", "a"])))
        card, self.chip.card = self.chip.card, None
        gui = FakeGUI(prompts=[True], inputs=["a"])

        async def run():
            task = asyncio.ensure_future(seed.load_mnemonic(gui))
            await asyncio.sleep(0.4)  # a few empty polls
            self.assertFalse(task.done())
            self.assertTrue(self.chip.field_on)
            self.chip.card = card
            return await task

        self.assertEqual(asyncio.run(run()), WORDS12)

    def test_load_refuses_what_is_not_a_seed(self):
        def plant(payload, record_type=nfc.RECORD_KEF):
            link = nfc.NFC(WS1850S(i2c=self.chip))
            link.init()
            link.field(True)
            link.poll()
            link.write_record(payload, record_type)
            link.deinit()

        # a record that is not an envelope at all
        plant(b"not an envelope")
        with self.assertRaises(seed.NFCStorageError):
            self.load(FakeGUI())
        # a valid envelope around something that is not BIP39 entropy
        for plain in (b"x" * 17, b"x" * 15, b"wpkh(xpub...)", b"x" * 36):
            plant(kef.encrypt("planted", "a", plain, 10000))
            with self.assertRaises(seed.NFCStorageError) as raised:
                self.load(FakeGUI(inputs=["a"]))
            self.assertIn("recovery phrase", str(raised.exception))
        # a seed envelope filed under another record type is not offered
        plant(kef.encrypt("planted", "a", bytes(16), 10000), nfc.RECORD_DESCRIPTOR)
        with self.assertRaises(seed.NFCStorageError):
            self.load(FakeGUI())

    def test_reads_a_card_written_by_krux(self):
        # GCM_ENCRYPTED_KEF from the Krux test suite, on a card
        envelope = (
            b"\x07test ID\x14\x00\x00\nOR\xa1\x93l>2q \x9e\x9dd\xbf\xb7vo]]\x8aO"
            b"\x90\x8e\x86\xe784L\x02]\x8f\xedT"
        )
        link = nfc.NFC(WS1850S(i2c=self.chip))
        link.init()
        link.field(True)
        link.poll()
        link.write_record(envelope)
        link.deinit()
        gui = FakeGUI(prompts=[True], inputs=["test key"])
        self.assertEqual(
            self.load(gui),
            "crush inherit small egg include title slogan mom remain blouse boost bonus",
        )
        self.assertIn("test ID", gui.shown[-1][2])


if __name__ == "__main__":
    unittest.main()
