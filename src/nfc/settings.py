"""The NFC entry of the communication settings: a switch and a reader test.

NFC is off until it is switched on here. Off, no menu offers a card, the reader
is never brought up and the antenna stays dark - the only thing still allowed
to touch the reader is the test below, which the user starts by hand.
"""
from binascii import hexlify

from . import (
    CLASSIC,
    HEADER_LEN,
    NFC,
    NFCError,
    NFCNotFound,
    RECORD_KEF,
    is_available,
    parse_header,
)
from .seed import wait_for_card

WIRING = "SDA to GPIO7, SCL to GPIO8,\nVCC to 3V3 (not 5 V), GND to GND."


def _describe_record(nfc):
    """One line on what the selected card holds. Reads, never writes."""
    try:
        header = nfc.read(0, HEADER_LEN)
    except NFCError:
        return (
            "The card could not be read.\n"
            "A card formatted by a phone (NDEF)\n"
            "has to be erased with a tag tool first."
        )
    try:
        parse_header(header, nfc.tag.capacity)
    except NFCError:
        return "The card is blank."
    if header[4] == RECORD_KEF:
        return "The card holds an encrypted key."
    return "The card holds a record that is not a key."


async def test_reader(gui):
    """Checks the bus, the reader and, if one is offered, a card"""
    if not is_available():
        await gui.alert(
            "No NFC reader found",
            "Nothing answers on the I2C bus.\n\nCheck the four wires:\n" + WIRING,
        )
        return False
    nfc = NFC()
    try:
        try:
            nfc.init()
        except NFCError:
            await gui.alert(
                "NFC reader error",
                "Something answers on the bus, but it does not behave "
                "like an RFID Unit 2 (WS1850S).",
            )
            return False
        try:
            tag = await wait_for_card(
                gui,
                nfc,
                "The NFC reader works",
                "Hold a card against it to test reading,\nor cancel to stop here.",
            )
            if tag is None:
                return True
            message = "%s\nUID: %s\nRoom for %d bytes\n\n%s" % (
                "MIFARE Classic" if tag.kind == CLASSIC else "Ultralight / NTAG",
                hexlify(tag.uid).decode(),
                tag.capacity - HEADER_LEN,
                _describe_record(nfc),
            )
        except NFCError:
            await gui.alert("NFC reader error", "The reader stopped answering.")
            return False
    finally:
        nfc.deinit()
    await gui.alert("Reader and card work", message)
    return True


async def settings_menu(gui, enabled, save, smartcard=None):
    """Shows the NFC settings. save(bool) stores the switch.

    smartcard is (is_on, set_on) for using the Specter smartcard over NFC. It
    is a switch of its own, kept outside the settings, because it is read at
    boot before the settings can be decrypted.
    """
    while True:
        buttons = [
            (None, "NFC is ON" if enabled else "NFC is OFF"),
            (1, "Disable NFC" if enabled else "Enable NFC"),
            (2, "Test the reader"),
        ]
        if smartcard is not None:
            on = smartcard[0]()
            buttons += [
                (None, "Smartcard over NFC is ON" if on else "Smartcard over NFC is OFF"),
                (3, "Stop using the smartcard over NFC" if on else "Use the smartcard over NFC"),
            ]
        item = await gui.menu(
            buttons,
            title="NFC card reader",
            note="M5Stack RFID Unit 2 on GPIO7 (SDA) and GPIO8 (SCL)",
            last=(255, None),
        )
        if item == 255:
            return enabled
        if item == 2:
            await test_reader(gui)
        elif item == 3:
            if on:
                smartcard[1](False)
            elif await gui.prompt(
                "Use the smartcard over NFC?",
                "The Specter smartcard (JavaCard) can be read by the NFC reader "
                "instead of the contact reader.\n\n"
                "From the next start the device asks for the card: hold it "
                "against the reader, type the PIN, hold it once more.\n\n"
                "The card has to rest on the right spot of the reader "
                "and stay still. This is experimental.",
            ):
                smartcard[1](True)
                await gui.alert(
                    "Smartcard over NFC",
                    "It takes effect the next time the device starts.",
                )
        elif item == 1:
            if not enabled and not await gui.prompt(
                "Enable NFC?",
                "With a reader plugged in, the device can save the key to an "
                "NFC card, encrypted with a password, and load it back.\n\n"
                "NFC is a radio. It is on only while a screen asks for a card.\n\n"
                "This is experimental.",
            ):
                continue
            enabled = not enabled
            save(enabled)
