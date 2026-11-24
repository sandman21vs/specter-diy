"""Teste do leitor NFC RFID-RC522 (MFRC522, SPI) na placa, pela TapHad.

    mpremote cp ports/esp32p4/test_nfc_rc522.py :/test_nfc_rc522.py
    mpremote exec "import test_nfc_rc522; test_nfc_rc522.run()"

Mostra em que etapa a conversa para, nesta ordem:

    SPI      o barramento abre nos pinos da TapHad
    RESET    o chip se cala com o RST em baixo e responde com ele em alto
    VERSION  o que VersionReg diz (so informa: clones nao sao recusados)
    READER   o driver do firmware sobe o leitor, com o campo desligado
    FIELD    os drivers da antena ligam
    POLL     quanto custa uma consulta ao chip (confere um teto do driver)
    CARD     um cartao responde
    READ     o comeco do cartao e lido
    RECORD   o que ha nele

run() so le. roundtrip() ESCREVE um registro de teste no cartao, le de volta e
compara -- use num cartao que nao guarde nada.

Usa os modulos congelados no firmware (nfc, nfc_mfrc522), entao o firmware
gravado precisa ser o que traz o driver.
"""

import binascii
import time

import nfc
import nfc_mfrc522

_PINS = "SCK GPIO%d, MOSI GPIO%d, MISO GPIO%d, SDA/SS GPIO%d, RST GPIO%d" % (
    nfc_mfrc522.PIN_SCK, nfc_mfrc522.PIN_MOSI, nfc_mfrc522.PIN_MISO,
    nfc_mfrc522.PIN_SS, nfc_mfrc522.PIN_RST)

# VersionReg dos chips que se conhece. Qualquer outro valor segue em frente.
_VERSIONS = {
    0x91: "NXP MFRC522 v1.0",
    0x92: "NXP MFRC522 v2.0",
    0x88: "Fudan FM17522 (clone)",
    0xB2: "Fudan FM17522E (clone)",
    0x12: "clone",
    0x82: "clone",
}

_WAKE_LIMIT_MS = 200  # bem acima do teto do driver, para medir quem passa dele
_POLL_SAMPLES = 200


def _fail(stage):
    return {"ok": False, "stage": stage}


def _open():
    """SPI, RESET e VERSION. Devolve (leitor, None) ou (None, etapa que falhou)."""
    reader = nfc_mfrc522.MFRC522()
    try:
        reader.bus = nfc_mfrc522._open_bus()
    except Exception as error:
        print("SPI:     could not open SPI%d on the TapHad pins: %r"
              % (nfc_mfrc522.SPI_ID + 1, error))
        return None, "SPI"
    bus = reader.bus
    print("SPI:     open at %d Hz, mode 0. %s" % (nfc_mfrc522.SPI_BAUDRATE, _PINS))

    # Com o NRSTPD em baixo o chip nao pode responder. Se responde, o fio do
    # RST nao chega nele.
    try:
        bus.rst.value(0)
        time.sleep_ms(5)
        asleep = set(reader.version() for _ in range(4))
    except nfc.NFCError as error:
        print("SPI:     transfer failed: %s" % error)
        return None, "SPI"
    idle = asleep.pop() if len(asleep) == 1 else None
    if idle is None:
        print("RESET:   MISO is not steady with RST low (floating line?), going on")
    elif idle not in (0x00, 0xFF):
        print("RESET:   the chip answers 0x%02x with RST low: RST (GPIO%d) does not"
              % (idle, nfc_mfrc522.PIN_RST))
        print("         reach the module")
        return None, "RESET"

    bus.rst.value(1)
    started = time.ticks_ms()
    while True:
        time.sleep_ms(1)
        waited = time.ticks_diff(time.ticks_ms(), started)
        try:
            reader._probe()
            break
        except nfc.NFCError:
            pass
        if waited > _WAKE_LIMIT_MS:
            bus.rst.value(0)
            print("RESET:   nothing answers %d ms after RST went high" % waited)
            if idle is not None:
                print("         MISO reads 0x%02x all the time" % idle)
            print("         check 3V3 and GND on the module, then the wires:")
            print("         " + _PINS)
            return None, "RESET"
    limit = nfc_mfrc522._WAKE_TIMEOUT_MS
    print("RESET:   silent with RST low, answers %d ms after RST high%s"
          % (waited, "" if waited < limit else
             " -- OVER the driver limit of %d ms, raise _WAKE_TIMEOUT_MS" % limit))

    version = reader.version()
    print("VERSION: 0x%02x, %s" % (version, _VERSIONS.get(version, "unknown, taken as a clone")))
    bus.rst.value(0)
    return reader, None


def _start():
    """Tudo ate o campo ligado. Devolve (link, None) ou (None, etapa)."""
    reader, stage = _open()
    if reader is None:
        return None, stage
    link = nfc.NFC(reader)
    try:
        link.init()
    except nfc.NFCError as error:
        print("READER:  the chip answers but the driver could not bring it up: %s" % error)
        return None, "READER"
    print("READER:  ready, field off (TxControl=%02x)" % reader._byte(0x14))

    try:
        link.field(True)
        control = reader._byte(0x14)
        if control & 0x03 != 0x03:
            print("FIELD:   antenna drivers did not turn on (TxControl=%02x)" % control)
            link.deinit()
            return None, "FIELD"
        print("FIELD:   antenna drivers on (TxControl=%02x). Only a card proves"
              % control)
        print("         there is RF: the chip cannot measure its own field")

        started = time.ticks_us()
        for _ in range(_POLL_SAMPLES):
            reader._byte(0x04)
        each = time.ticks_diff(time.ticks_us(), started) // _POLL_SAMPLES
        floor = 1000 // reader._POLLS_PER_MS
        print("POLL:    %d us per register read (the driver assumes at least %d)%s"
              % (each, floor, "" if each >= floor else " -- TOO FAST, raise _POLLS_PER_MS"))
    except nfc.NFCError as error:
        print("FIELD:   the reader stopped answering: %s" % error)
        link.deinit()
        return None, "FIELD"
    return link, None


def _wait_for_card(link, seconds):
    print("CARD:    hold one on the reader (%d s)..." % seconds)
    deadline = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while True:
        try:
            tag = link.poll()
        except nfc.NFCNotFound:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
            time.sleep_ms(150)
            continue
        kind = "MIFARE Classic" if tag.kind == nfc.CLASSIC else "Ultralight / NTAG"
        print("CARD:    %s, UID %s, %d usable bytes"
              % (kind, binascii.hexlify(tag.uid).decode(), tag.capacity))
        return tag
    print("CARD:    none found. If the reader got this far, suspect the antenna")
    print("         or the card. Unsupported cards (DESFire, phones, bank cards)")
    print("         read as an empty field on purpose.")
    return None


def run(seconds=15):
    link, stage = _start()
    if link is None:
        return _fail(stage)
    try:
        if _wait_for_card(link, seconds) is None:
            return _fail("CARD")
        try:
            header = link.read(0, nfc.HEADER_LEN)
        except nfc.NFCError as error:
            print("READ:    failed: %s" % error)
            print("         a Classic card formatted as NDEF no longer has the")
            print("         factory keys; erase it with a tag tool first")
            return _fail("READ")
        print("READ:    %s" % binascii.hexlify(header).decode())
        try:
            length = nfc.parse_header(header, link.tag.capacity)
            print("RECORD:  type %d, %d bytes" % (header[4], length))
        except nfc.NFCError:
            print("RECORD:  none (blank card, or not written by Specter/Kern/Krux)")
        return {"ok": True, "stage": None}
    finally:
        link.deinit()


def roundtrip(seconds=15):
    link, stage = _start()
    if link is None:
        return _fail(stage)
    try:
        if _wait_for_card(link, seconds) is None:
            return _fail("CARD")
        if link.has_record():
            print("WRITE:   refused, the card already holds a record")
            return _fail("WRITE")
        payload = bytes(range(61))  # the size of a 24 word backup
        started = time.ticks_ms()
        # A reserved type no loader accepts, so a test card is never offered
        # as a key
        link.write_record(payload, nfc.RECORD_DATUM)
        link.poll()
        ok = link.read_record(nfc.RECORD_DATUM) == payload
        print("WRITE:   %s in %d ms"
              % ("read back OK" if ok else "READ BACK DIFFERS",
                 time.ticks_diff(time.ticks_ms(), started)))
        return {"ok": True, "stage": None} if ok else _fail("WRITE")
    except nfc.NFCError as error:
        print("WRITE:   failed: %s" % error)
        return _fail("WRITE")
    finally:
        link.deinit()
