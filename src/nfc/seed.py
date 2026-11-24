"""Seed backups on NFC cards: save the loaded key to a card, load one back.

What goes on the card is a KEF envelope around the BIP39 entropy - the bytes of
a Compact SeedQR - so the seed never crosses the antenna in the clear. It is
converted and encrypted before the reader is touched, and nothing here writes a
mnemonic to a card unsealed. The same card reads on Kern and on the Krux fork
that share the format.

The RF field is on only while a "hold a card" screen is up, and every path out
of these functions drops it.
"""
import asyncio
from binascii import hexlify

from embit import bip32, bip39
from errors import BaseError
from gui.screens import Progress

import kef
from . import NFC, NFCError, NFCNotFound, NFCSizeError, NFCWrongCard, RECORD_KEF

# How often an empty field is asked again. Each look is a handful of I2C
# transfers and a 25 ms wait for a card that is not there.
POLL_MS = 150

# BIP39 entropy sizes, 12 to 24 words
ENTROPY_SIZES = (16, 20, 24, 28, 32)

CARD_HINT = "Hold the card against the NFC reader\nand keep it still."
WRONG_CARD_HINT = "This card can not hold a backup.\nUse a MIFARE Classic or NTAG card."


class NFCStorageError(BaseError):
    NAME = "NFC card"


def backup_id(mnemonic):
    """Label and salt of the envelope: the root fingerprint of the bare seed.

    Without the passphrase on purpose. The passphrase is not on the card, so
    this is the fingerprint the card gives back when it is loaded - and it
    keeps a passphrase wallet's fingerprint off a medium anyone can read.
    """
    root = bip32.HDKey.from_seed(bip39.mnemonic_to_seed(mnemonic))
    return hexlify(root.child(0).fingerprint).decode()


async def wait_for_card(gui, nfc, title, message=CARD_HINT):
    """Energizes the field and waits for one card. None if the user cancels."""
    scr = Progress(title, message, button_text="Cancel")
    await gui.load_screen(scr)
    nfc.field(True)
    while scr.waiting:
        try:
            return nfc.poll()
        except NFCWrongCard:
            # a smartcard, or a family that is not a backup card
            try:
                scr.message.set_text(WRONG_CARD_HINT)
            except Exception:
                pass
        except NFCNotFound:
            pass
        await asyncio.sleep_ms(POLL_MS)
    return None


def _open():
    nfc = NFC()
    try:
        nfc.init()
    except NFCError:
        nfc.deinit()
        raise NFCStorageError("NFC reader is not responding.\nCheck the cable.")
    return nfc


async def _new_password(gui):
    """Asks for the encryption password twice. None if the user cancels."""
    while True:
        password = await gui.get_input(
            title="Password for the card:",
            note="It encrypts the key on the card.\n"
            "Without it the card can not be read back.",
        )
        if password is None:
            return None
        if not password:
            await gui.alert("Empty password", "The card needs a password.")
            continue
        again = await gui.get_input(
            title="Repeat the password:",
            note="Type the same password once more.",
        )
        if again is None:
            return None
        if again == password:
            return password
        await gui.alert("Passwords differ", "The two passwords are not the same.\nTry again.")


async def save_mnemonic(gui, mnemonic):
    """Encrypts the recovery phrase and writes it to a card. True when written."""
    if not await gui.prompt(
        "Save key to NFC card?",
        "The recovery phrase is encrypted with a password "
        "and written to the card.\n\n"
        "Anyone holding the card can copy it and try passwords "
        "for as long as they like, so pick a strong one.\n\n"
        "This is experimental. Keep another backup.",
    ):
        return False
    password = await _new_password(gui)
    if password is None:
        return False

    gui.show_loader(title="Encrypting...")
    id_ = backup_id(mnemonic)
    envelope = kef.encrypt(id_, password, bip39.mnemonic_to_bytes(mnemonic))

    nfc = _open()
    try:
        if await wait_for_card(gui, nfc, "Save key to NFC card") is None:
            return False
        if nfc.has_record():
            if not await gui.prompt(
                "Overwrite this card?",
                "The card already holds a record.\nWriting the key replaces it.",
            ):
                return False
            # Select again rather than trusting the earlier look: the question
            # was on screen, and the card only had to drift a centimetre.
            nfc.poll()
        nfc.write_record(envelope, RECORD_KEF)
        # A write is only as good as what reads back
        nfc.poll()
        if nfc.read_record(RECORD_KEF) != envelope:
            raise NFCError("Read back differs")
    except NFCSizeError:
        raise NFCStorageError("The card is too small for this key.")
    except NFCError:
        raise NFCStorageError(
            "Could not write the card.\n\n"
            "Keep it still on the reader and try again. "
            "A card formatted by a phone has to be erased first."
        )
    finally:
        nfc.deinit()
    await gui.alert(
        "Success!",
        "The encrypted key is on the card.\n\nBackup ID: %s" % id_,
    )
    return True


async def load_mnemonic(gui):
    """Reads a card, decrypts it and returns the recovery phrase, or None."""
    nfc = _open()
    try:
        if await wait_for_card(gui, nfc, "Load key from NFC card") is None:
            return None
        envelope = nfc.read_record(RECORD_KEF)
    except (NFCNotFound, NFCSizeError):
        raise NFCStorageError("No encrypted key on this card.")
    except NFCError:
        raise NFCStorageError(
            "Could not read the card.\n\nKeep it still on the reader and try again."
        )
    finally:
        nfc.deinit()

    try:
        id_ = kef.unwrap(envelope)[0].decode()
    except kef.KEFError:
        raise NFCStorageError("No encrypted key on this card.")

    password = await gui.get_input(
        title="Password of the card:",
        note="Backup ID: %s" % id_,
    )
    if not password:
        return None

    gui.show_loader(title="Decrypting...")
    try:
        entropy = kef.decrypt(envelope, password)
    except kef.KEFError:
        raise NFCStorageError("Wrong password, or the card is damaged.")

    # Decrypting does not make these bytes ours: a planted card could have been
    # encrypted with a password its author chose. One narrow gate - raw BIP39
    # entropy, which is the only thing ever written to a card.
    if len(entropy) not in ENTROPY_SIZES:
        raise NFCStorageError("The card does not hold a recovery phrase.")
    mnemonic = bip39.mnemonic_from_bytes(entropy)
    if not bip39.mnemonic_is_valid(mnemonic):
        raise NFCStorageError("The card does not hold a recovery phrase.")

    # What is left is whose seed it is, and only the user can answer that
    if not await gui.prompt(
        "Load this key?",
        "Fingerprint: %s\n\nBackup ID on the card: %s" % (backup_id(mnemonic), id_),
    ):
        return None
    return mnemonic
