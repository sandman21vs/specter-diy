"""The smartcard keystore over NFC: same card, same applet, no card slot.

A contact card stays in its slot, so MemoryCard talks to it whenever it likes.
An NFC card is held to the reader by hand, and with a small antenna it has to
be held in the right place. So here the card is asked for only when it is
really needed, a screen says so, and the RF field is on only while that screen
is up. Each of those moments is one "tap":

    boot      the PIN is typed, then one tap unlocks the card and reads
              what it stores
    load key  nothing: the key was read at boot
    storage   one tap for each thing done to the card

The anti-phishing words on the PIN screen are made from the card's public key,
and at boot the card is not there yet. So the key of the card last used is
remembered in flash. The tap that follows proves the card owns it - no other
card can open the secure channel for that key - and a card that does not is
refused before it sees the PIN. Only a card this device has never seen costs a
tap of its own, before the PIN screen, to learn its key.

What the card stores is read once, at unlock, and kept in RAM until the device
is locked, so "Load key from smartcard" does not need the card again.

The card forgets it was unlocked as soon as it leaves the field, so each later
tap has to unlock it again. The PIN that unlocked the device is kept in RAM for
that, until the device is locked: an unlocked device may use the card, as it
may with the contact reader, where the card sits unlocked in its slot.

A tap can fail halfway - the card slips, or the field is too weak for it at
that spot. The screen then asks for the card again and the step is repeated,
with one exception that is never repeated blindly: the PIN. Before it is sent
a second time the card's attempt counter is read, and if it went down the
first try did arrive and was wrong.
"""
import asyncio
import hmac
import os

import platform
import secp256k1
from embit import bip39
from gui.screens import Alert, Progress, Prompt
from helpers import tagged_hash
from nfc import is_available as reader_present
from nfc.smartcard import CardConnection, CardLost, WrongCard

from platform import CriticalErrorWipeImmediately

from .core import KeyStore, KeyStoreError, KeyStoreUnavailable, PinError
from .javacard.applets.applet import AppletException, ISOException
from .javacard.applets.securechannel import SecureChannelError, SecureError
from .memorycard import MemoryCard
from .ram import RAMKeyStore

# How often an empty field is asked again
POLL_MS = 150

HINT = "Hold the card against the NFC reader\nand keep it still."
HINT_FOUND = "Card found.\nKeep it still..."
HINT_LOST = "The card moved away.\nHold it against the reader again\nand keep it still."
HINT_WRONG = "This is not a smartcard.\nHold the Specter smartcard\nagainst the reader."


class TapCancelled(KeyStoreError):
    NAME = "Smartcard"

    def __init__(self):
        super().__init__("Cancelled.")


def _flag_path():
    # Next to the keystore folder, not in the settings: those are encrypted
    # with a key that exists only after the keystore is unlocked, and this has
    # to be read before a keystore is even chosen.
    return KeyStore.path + "_nfc"


def is_enabled():
    """True when the smartcard is to be used over NFC.

    The file that says so also remembers the public key of the card last
    used. Neither is a secret and neither is authenticated: the switch only
    makes the boot ask for a card, and a key that was tampered with changes
    the anti-phishing words and then fails the secure channel.
    """
    try:
        os.stat(_flag_path())
        return True
    except Exception:
        return False


def set_enabled(enabled):
    if enabled:
        if not is_enabled():
            with open(_flag_path(), "wb") as f:
                f.write(b"1")
    else:
        try:
            os.remove(_flag_path())
        except OSError:
            pass


def _hint(scr, text):
    try:
        scr.message.set_text(text)
    except Exception:
        pass


class NFCMemoryCard(MemoryCard):
    NAME = "Smartcard"
    NOTE = """Saves encryption key and Bitcoin key on a PIN-protected smartcard, read over NFC.
The card is asked for when the device is unlocked and when its storage is changed."""

    def __init__(self):
        # before the parent builds the applet around self.connection
        self.connection = CardConnection()
        super().__init__()
        # The card relocks itself when it leaves the field; whether the device
        # is locked is kept here.
        self._locked_here = True
        # the card proved it owns the public key the words are made from
        self._verified = False
        # its key came from flash; the next tap has to prove it
        self._known = False
        self._recalled = False
        # what the card stores, as read at unlock
        self._blob = None
        self._has_data = False
        # PIN for the operation in progress
        self._pin = None
        # PIN that unlocked the device, until it is locked again
        self._session_pin = None

    def _recall_card(self):
        """Takes the key of the card last used from flash, if there is one.

        Once: a card that turned out not to own it is learned afresh.
        """
        if self._recalled:
            return
        self._recalled = True
        try:
            with open(_flag_path(), "rb") as f:
                raw = f.read()
            if len(raw) != 65:
                return
            self.applet.sc.card_pubkey = secp256k1.ec_pubkey_parse(raw)
        except Exception:
            return
        # A card is remembered only after its PIN was used, and what it
        # really says is read at the tap.
        self.applet._pin_status = self.applet.PIN_LOCKED
        self._known = True

    def _remember_card(self):
        if not is_enabled():
            return
        raw = secp256k1.ec_pubkey_serialize(
            self.applet.card_pubkey, secp256k1.EC_UNCOMPRESSED
        )
        try:
            with open(_flag_path(), "rb") as f:
                if f.read() == raw:
                    return
            with open(_flag_path(), "wb") as f:
                f.write(raw)
        except OSError:
            pass

    @classmethod
    def is_available(cls):
        return is_enabled() and reader_present()

    # ---------- One tap ----------

    def _start_session(self):
        """Applet, secure channel and PIN state, with the card in the field"""
        applet = self.applet
        applet.sc.is_open = False
        try:
            applet.select()
        except ISOException:
            raise KeyStoreError("This card does not have the Specter applet.")
        try:
            # With the key of the first tap remembered, this is also what
            # refuses any other card: it can not sign for that key.
            applet.open_secure_channel()
        except (SecureChannelError, ValueError):
            if self._verified:
                raise KeyStoreError(
                    "This is not the card this session was started with."
                )
            applet.sc.card_pubkey = None
            if self._known:
                # Forgotten for now, so the next try learns this card's key
                # first and shows the words that go with it.
                self._known = False
                applet._pin_status = None
                raise KeyStoreError(
                    "This is not the smartcard this device was last used with.\n\n"
                    "To use it instead, hold it to the reader again "
                    "and check the words on the PIN screen."
                )
            raise KeyStoreError("The card failed to prove its identity.")
        self._verified = True
        self.connected = True
        applet.get_pin_status()
        # Not relied upon, but not assumed either: a card that stayed unlocked
        # while the device is locked gets locked now.
        if self._locked_here and applet._pin_status == applet.PIN_UNLOCKED:
            applet.lock()

    def _release(self):
        """Field off. The next request opens a new secure channel."""
        self.applet.sc.is_open = False
        self.connection.disconnect()

    async def _tap_task(self, scr, outcome, done):
        conn = self.connection
        try:
            conn.open()
            while scr.waiting and not done:
                try:
                    conn.connect()
                except WrongCard:
                    _hint(scr, HINT_WRONG)
                    await asyncio.sleep_ms(POLL_MS)
                    continue
                except CardLost:
                    await asyncio.sleep_ms(POLL_MS)
                    continue
                _hint(scr, HINT_FOUND)
                # let the screen draw: what follows blocks for over a second
                await asyncio.sleep_ms(30)
                try:
                    self._start_session()
                except (CardLost, AppletException):
                    conn.reset()
                    _hint(scr, HINT_LOST)
                    await asyncio.sleep_ms(POLL_MS)
                    continue
                outcome.append(True)
                break
        except Exception as e:
            outcome.append(e)
        # The screen may not be on display yet, and showing it arms it again:
        # keep releasing until the caller has it back.
        while not done:
            scr.release()
            await asyncio.sleep_ms(10)

    async def _tap(self, title):
        """Asks for the card and returns with a session open.

        The caller ends it with _release(). Raises TapCancelled if the user
        gives up, with the field off.
        """
        scr = Progress(title, HINT, button_text="Cancel")
        outcome = []
        done = []
        asyncio.create_task(self._tap_task(scr, outcome, done))
        await self.show(scr)
        done.append(True)
        if not outcome:
            self._release()
            raise TapCancelled()
        if outcome[0] is not True:
            self._release()
            raise outcome[0]

    async def _do(self, fn=None, need_pin=True, title="Hold the smartcard to the reader"):
        """Runs fn with the card in the field, unlocked first if need_pin.

        Asks for the card, and for the PIN if the card wants one and none was
        typed for this operation yet. Starts over when the card is lost.
        """
        if need_pin and self._pin is None:
            self._pin = self._session_pin
        # A card with a PIN is locked every time it enters the field, so ask
        # for the PIN first and save a tap that would only find that out.
        if need_pin and self._pin is None and self._verified and self.is_pin_set:
            self._pin = await self.get_pin(with_cancel=True)
            if self._pin is None:
                raise TapCancelled()
        before = None
        while True:
            await self._tap(title)
            try:
                if need_pin and self.applet.is_locked:
                    if self._pin is None:
                        # the PIN is typed with the card away
                        self._release()
                        pin = await self.get_pin(with_cancel=True)
                        if pin is None:
                            raise TapCancelled()
                        self._pin = pin
                        continue
                    left = self.applet._pin_attempts_left
                    if before is not None and left < before:
                        # The card counted the try whose answer was lost: it
                        # arrived and it was wrong. Do not spend another.
                        raise PinError(
                            "Invalid PIN!\n%d of %d attempts left..."
                            % (left, self.pin_attempts_max)
                        )
                    before = left
                    # Only the card: what it stores is read where it is
                    # wanted, so that unreadable data can still be replaced
                    # or deleted.
                    self._card_unlock(self._pin)
                return fn() if fn is not None else None
            except PinError:
                self._pin = None
                self._session_pin = None
                raise
            except (CardLost, AppletException):
                title = "Hold the smartcard to the reader again"
            finally:
                self._release()

    # ---------- What MemoryCard does with a card always present ----------

    @property
    def is_locked(self):
        if self.applet._pin_status == self.applet.PIN_UNSET:
            return False
        return self._locked_here

    def get_auth_word(self, pin_part):
        # The channel is closed while the PIN is typed. What matters is that
        # it was opened once with this key, so the card can not lie about it.
        # A key recalled from flash will do: the tap that follows refuses a
        # card that does not own it.
        if not (self._verified or self._known):
            raise KeyStoreError("The card was not verified.")
        key = tagged_hash("auth", self.secret + self.applet.card_pubkey)
        h = hmac.new(key, pin_part, digestmod="sha256").digest()
        word_number = int.from_bytes(h[:2], "big") % len(bip39.WORDLIST)
        return bip39.WORDLIST[word_number]

    def lock(self):
        """Locks the device. The card is not there to be told, and does not
        need to be: it locked itself when it left the field."""
        self._locked_here = True
        self._blob = None
        self._pin = None
        self._session_pin = None
        return self.is_locked

    def _card_unlock(self, pin):
        """Unlocks the card and nothing else. PinError if the PIN is wrong."""
        try:
            self.applet.unlock(pin)
        except SecureError as e:
            if str(e) == "0502":  # wrong PIN
                raise PinError(
                    "Invalid PIN!\n%d of %d attempts left..."
                    % (self.pin_attempts_left, self.pin_attempts_max)
                )
            elif str(e) == "0503":  # bricked
                # wipe is happening automatically on this exception
                raise CriticalErrorWipeImmediately("No more PIN attempts!\nWipe!")
            raise e

    def _unlock(self, pin):
        self._card_unlock(pin)
        self._opened(pin)

    def _opened(self, pin):
        """The card took the PIN: the device is unlocked"""
        self._locked_here = False
        self._session_pin = pin
        self._remember_card()
        # May raise for data written by another device. The device is
        # unlocked all the same: the PIN was right.
        self.check_saved()

    def check_saved(self):
        self._blob = None
        self._is_key_saved = False
        data = self.applet.get_secret()
        self._blob = data
        self._has_data = bool(data)
        if len(data):
            # raises if the data was written by another device
            d, _ = self.parse_data(data)
            self._is_key_saved = "entropy" in d

    def card_usable(self):
        return True

    def card_has_data(self):
        # Also data this device can not read: a card written by another
        # device, encrypted, would otherwise be impossible to clear here.
        return self._has_data or self.is_key_saved

    async def check_card(self, check_pin=False):
        await self._do(need_pin=check_pin)

    async def init(self, show_fn, show_loader):
        self.show_loader = show_loader
        self.show = show_fn
        platform.maybe_mkdir(self.path)
        self.load_secret(self.path)
        if not self._verified and not self._known:
            self._recall_card()
        if not self._verified and not self._known:
            # a card never seen: its key first, for the words
            try:
                await self._do(need_pin=False)
            except TapCancelled:
                raise KeyStoreUnavailable("No smartcard.")
        await RAMKeyStore.init(self, show_fn, show_loader)

    async def unlock(self):
        try:
            while True:
                if not self.is_pin_set:
                    pin = await self.setup_pin()
                    await self._do(lambda: self._set_pin(pin), need_pin=False)
                if not self.is_locked:
                    return
                pin = self._pin = await self.get_pin()

                def unlocked():
                    # A remembered card that has no PIN after all: back to
                    # the top, to choose one.
                    if self.is_pin_set:
                        self._opened(pin)

                try:
                    await self._do(unlocked, title="Hold the smartcard to the reader to unlock")
                except TapCancelled:
                    # No card was shown since the device started: the user may
                    # not have it, so let the next keystore take over. After
                    # that, cancelling only goes back to the PIN screen.
                    if not self._verified:
                        raise KeyStoreUnavailable("No smartcard.")
                self._pin = None
        finally:
            self._pin = None

    async def change_pin(self):
        old_pin = await self.get_pin(title="First enter your old PIN code", with_cancel=True)
        if old_pin is None:
            return
        new_pin = await self.setup_pin()

        def change():
            try:
                self.applet.change_pin(old_pin, new_pin)
            except (CardLost, AppletException):
                # Not repeated: a second try with the old PIN would cost an
                # attempt if the first one did go through.
                raise KeyStoreError(
                    "The card moved away while the PIN was being changed.\n\n"
                    "It holds either the old PIN or the new one."
                )

        self._pin = old_pin
        try:
            await self._do(change)
        finally:
            self._pin = None
        self._session_pin = new_pin
        await self.show(
            Alert("Success!", "PIN code is successfully changed!", button_text="OK")
        )

    async def _get_mnemonic(self):
        if self._blob is None:
            try:
                await self._do(self.check_saved, title="Hold the smartcard to the reader to load the key")
            finally:
                self._pin = None
        if not self._is_key_saved:
            raise KeyStoreError("Key is not saved")
        d, _ = self.parse_data(self._blob)
        return bip39.mnemonic_from_bytes(d["entropy"])

    async def save_mnemonic(self):
        entropy = bip39.mnemonic_to_bytes(self.mnemonic)
        try:
            info = await self._do(self.get_secret_info)
            encrypt = await self.ask_how_to_save(info)
            if encrypt is None:
                return
            d = self.serialize_data({"entropy": entropy}, encrypt=encrypt)

            def write():
                self.applet.save_secret(d)
                self.check_saved()

            await self._do(write, title="Hold the smartcard again to save")
            # check it's ok
            stored, _ = self.parse_data(self._blob)
            if stored.get("entropy") != entropy:
                raise KeyStoreError("The card did not keep the key.")
        finally:
            self._pin = None
        await self.show(
            Alert(
                "Success!",
                "Your key is stored on the smartcard now.",
                button_text="OK",
            )
        )

    async def delete_mnemonic(self):
        if self._has_data and not self.is_key_saved:
            if not await self.show(Prompt(
                "Delete unreadable data?",
                "\nThe card holds data this device can not read. "
                "It was most likely saved, encrypted, by another device.\n\n"
                "If it is a key and that device is gone, "
                "this is the only copy.\n\nDelete it anyway?",
                warning="Irreversibly delete the data on the card",
            )):
                raise TapCancelled()
        try:
            await self._do(lambda: self.applet.save_secret(b""))
        finally:
            self._pin = None
        self._is_key_saved = False
        self._has_data = False
        self._blob = b""

    async def switch_card(self):
        self._session_pin = None
        self._pin = await self.get_pin(with_cancel=True)
        if self._pin is None:
            return
        try:
            # the PIN of the current card, checked by the card itself
            await self._do(title="Hold the current smartcard to the reader")
        finally:
            self._pin = None
        self.lock()
        self._verified = False
        self._known = False
        self._has_data = False
        self._is_key_saved = False
        self._userkey = None
        self.applet.sc.card_pubkey = None
        self.applet._pin_status = None
        await self.show(Alert("Please swap the card", "Now you can hold another card to the reader and set it up.", button_text="Continue"))
        await self._do(need_pin=False, title="Hold the new smartcard to the reader")
        await self.unlock()

    async def show_card_info(self):
        try:
            info = await self._do(self.get_secret_info)
        finally:
            self._pin = None
        self._has_data = info[0]
        await self.show_info(info)
