"""A contactless smartcard behind the interface the JavaCard applets expect.

keystore.javacard talks to a card through a connection object - connect(),
transmit(apdu), disconnect() - that until now only the contact reader provided.
This is the same thing over NFC, so the applets and the secure channel run
unchanged on top of it.

One difference shapes everything above: a contact card sits in its slot for the
whole session, an NFC card is held to the reader for a few seconds at a time.
So a connection here is short. It is opened when a screen asks for the card,
used, and closed, and the RF field is on only in between.
"""
from errors import BaseError

from . import NFC, NFCError, NFCTimeout
from .isodep import IsoDep, IsoDepError


class CardLost(BaseError):
    """The card is not in the field, or left it in the middle of something"""

    NAME = "NFC smartcard"


class WrongCard(CardLost):
    """A card is in the field, but it is not a smartcard"""


class CardConnection:
    """Has the shape of uscard.CardConnection in what the applets use"""

    T0_protocol = 2
    T1_protocol = 1

    def __init__(self, reader=None):
        self._reader = reader  # None: the reader of this board
        self._link = None
        self._dep = None

    def open(self):
        """Brings the reader up and turns the field on. Idempotent."""
        if self._link is None:
            link = NFC(self._reader)
            try:
                link.init()
                link.reader.field(True)
            except NFCError:
                link.deinit()
                raise CardLost("NFC reader is not responding.\nCheck the cable.")
            self._link = link
            self._dep = IsoDep(link)

    def isCardInserted(self):
        """True while a session with a card is up"""
        return self._dep is not None and self._dep.active

    def connect(self, protocol=None):
        """Selects the card held to the reader. CardLost if there is none."""
        self.open()
        try:
            self._dep.connect()
        except NFCTimeout:
            raise CardLost("No card")
        except IsoDepError:
            raise WrongCard("Not a smartcard")
        except NFCError:
            # half an answer: a card at the edge of the field
            raise CardLost("No card")

    def reset(self):
        """Powers the card down and up again, ending any session with it.

        A card that lost power in the middle of a command stays mute until the
        field goes away, so this is what makes it selectable again.
        """
        if self._link is None:
            return
        self._dep.active = False
        try:
            reader = self._link.reader
            reader.set_timeout(None)
            reader.field(False)
            reader.field(True)
        except NFCError:
            self.disconnect()

    def disconnect(self):
        """Ends the session and turns the field off. Never raises."""
        link, self._link = self._link, None
        dep, self._dep = self._dep, None
        if dep is not None:
            dep.deselect()
        if link is not None:
            try:
                link.reader.field(False)
            except NFCError:
                pass
            link.reader.deinit()

    def getATR(self):
        if not self.isCardInserted():
            raise CardLost("No card")
        return self._dep.ats

    def transmit(self, apdu):
        """Sends one APDU, returns the response with its status word"""
        if not self.isCardInserted():
            raise CardLost("No card")
        try:
            return self._dep.exchange(apdu)
        except NFCError:
            raise CardLost("The card was moved away")
