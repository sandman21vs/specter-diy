"""Teste do leitor NFC (M5Stack RFID Unit 2, WS1850S) na placa.

    mpremote cp ports/esp32p4/test_nfc.py :/test_nfc.py
    mpremote exec "import test_nfc; test_nfc.run()"

Mostra em que etapa a conversa para: barramento, leitor, cartao ou registro.
run() so le. roundtrip() ESCREVE um registro de teste no cartao, le de volta e
compara -- use num cartao que nao guarde nada.
"""

import binascii
import time

import nfc
import nfc_ws1850s


def _open():
    if not nfc_ws1850s.is_present():
        print("BUS:    nothing answers at 0x%02x on GPIO7 (SDA) / GPIO8 (SCL)"
              % nfc_ws1850s.WS1850S_ADDR)
        print("        check the four wires and that the module has 3.3 V")
        return None
    print("BUS:    device at 0x%02x" % nfc_ws1850s.WS1850S_ADDR)

    link = nfc.NFC()
    try:
        link.init()
    except nfc.NFCError as error:
        print("READER: not a WS1850S, or it does not reset: %s" % error)
        return None
    print("READER: ready, field off")
    return link


def _wait_for_card(link, seconds):
    link.field(True)
    print("CARD:   hold one on the reader (%d s)..." % seconds)
    deadline = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        try:
            tag = link.poll()
        except nfc.NFCNotFound:
            time.sleep_ms(150)
            continue
        kind = "MIFARE Classic" if tag.kind == nfc.CLASSIC else "Ultralight / NTAG"
        print("CARD:   %s, UID %s, %d usable bytes"
              % (kind, binascii.hexlify(tag.uid).decode(), tag.capacity))
        return tag
    print("CARD:   none found. Unsupported cards (DESFire, phones, bank cards)")
    print("        read as an empty field on purpose.")
    return None


def run(seconds=15):
    link = _open()
    if link is None:
        return False
    try:
        if _wait_for_card(link, seconds) is None:
            return False
        try:
            header = link.read(0, nfc.HEADER_LEN)
        except nfc.NFCError as error:
            print("READ:   failed: %s" % error)
            print("        a Classic card formatted as NDEF no longer has the")
            print("        factory keys; erase it with a tag tool first")
            return False
        print("READ:   %s" % binascii.hexlify(header).decode())
        try:
            length = nfc.parse_header(header, link.tag.capacity)
            print("RECORD: type %d, %d bytes" % (header[4], length))
        except nfc.NFCError:
            print("RECORD: none (blank card, or not written by Specter/Kern/Krux)")
        return True
    finally:
        link.deinit()


def roundtrip(seconds=15):
    link = _open()
    if link is None:
        return False
    try:
        if _wait_for_card(link, seconds) is None:
            return False
        if link.has_record():
            print("WRITE:  refused, the card already holds a record")
            return False
        payload = bytes(range(61))  # the size of a 24 word backup
        started = time.ticks_ms()
        # A reserved type no loader accepts, so a test card is never offered
        # as a key
        link.write_record(payload, nfc.RECORD_DATUM)
        link.poll()
        ok = link.read_record(nfc.RECORD_DATUM) == payload
        print("WRITE:  %s in %d ms"
              % ("read back OK" if ok else "READ BACK DIFFERS",
                 time.ticks_diff(time.ticks_ms(), started)))
        return ok
    except nfc.NFCError as error:
        print("WRITE:  failed: %s" % error)
        return False
    finally:
        link.deinit()
