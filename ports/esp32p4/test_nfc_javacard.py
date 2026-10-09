"""Diagnostico: o JavaCard do Specter (applet MemoryCard) por NFC, no WS1850S.

    mpremote cp ports/esp32p4/test_nfc_javacard.py :/test_nfc_javacard.py
    mpremote exec "import test_nfc_javacard; test_nfc_javacard.run()"

Com o JavaCard parado sobre o leitor. Responde duas perguntas antes de existir
um transporte NFC para o keystore:

  1. o campo do RFID2 a 3,3 V sustenta o cartao durante as operacoes de curva
     eliptica do canal seguro, ou ele reinicia no meio?
  2. o applet responde ao SELECT pela interface sem contato?

Nao muda nada no firmware nem no cartao: usa o driver e a selecao que ja estao
congelados (nfc, nfc_ws1850s) e traz aqui o que falta -- aceitar SAK 0x20, RATS,
ISO-DEP (blocos I com encadeamento, R e S(WTX)) e o aperto de mao do canal
seguro. No cartao so le: SELECT, chave publica, abertura de canal e PIN_STATUS.
Nenhuma tentativa de PIN e gasta.

O cartao so responde em algumas posicoes sobre o leitor. Para achar uma, rode
go() num terminal e mova o cartao olhando a saida: quando o sinal fica firme
por alguns segundos o diagnostico comeca sozinho.

    mpremote exec "import test_nfc_javacard; test_nfc_javacard.go()"

Opcoes de run():
    rounds=10            aberturas de canal por modo
    modes=("es", "ee")   "es" e o que o keystore usa; "ee" faz o cartao gerar
                         uma chave a mais por abertura, e o pior caso de consumo
    trace=True           mostra cada quadro trocado
    boost=True           experimental: ganho de recepcao e corrente de antena no
                         maximo (registradores do MFRC522; o WS1850S pode ignorar)

O cartao e tratado como entrada hostil: tamanho de ATS, de quadro e de
resposta, numero de WTX, retransmissoes e tempo por APDU tem teto fixo.
"""

import binascii
import hashlib
import hmac
import os
import time

import nfc
import nfc_ws1850s
import secp256k1
from ucryptolib import aes

AID = b"\xB0\x0B\x51\x11\xCB\x01"  # MemoryCardApplet, src/keystore/javacard/applets

# ---------------------------------------------------------------- limites ----

_FSD = 64  # maior quadro que cabe na FIFO do leitor, CRC incluido
_FSDI = 5  # o codigo de 64 bytes no RATS
_MAX_ATS = 20
_FSC_TABLE = (16, 24, 32, 40, 48, 64, 96, 128, 256)
_FWT_MAX_MS = 4949  # FWI 14, o maior que a norma permite
_TIMEOUT_MARGIN_MS = 20  # folga sobre o FWT: o leitor e consultado por I2C
_SELECT_TIMEOUT_MS = 30
_MAX_APDU = 261
_MAX_RESPONSE = 258  # APDU curto: 256 de dados mais o status word
_MAX_WTX = 100  # pedidos de mais tempo aceitos por APDU
_MAX_RETRIES = 2  # blocos invalidos ou perdidos seguidos
_APDU_BUDGET_MS = 15000

_MAC = 14
_AES_CBC = 2


def _hex(data):
    return binascii.hexlify(bytes(data)).decode()


# ----------------------------------------------------------------- leitor ----


class Timeout(nfc.NFCError):
    """O prazo acabou sem nenhum quadro do cartao"""


class DiagReader(nfc_ws1850s.WS1850S):
    """O driver do firmware com prazo ajustavel por troca.

    O driver espera 25 ms, pelo timer do chip. Aqui o prazo e do relogio do P4;
    o timer do chip e ignorado e ainda por cima esticado, para o caso de o
    WS1850S, ao contrario do MFRC522, parar de receber quando ele dispara.
    """

    deadline_ms = _SELECT_TIMEOUT_MS

    def init(self):
        was_ready = self.ready
        super().init()
        if not was_ready:
            # Timer do chip no mais longo que ele tem (prescaler 0xFFF, 604 us
            # por tick, ~40 s), para ele nunca disparar antes do nosso prazo.
            for reg, value in ((0x2A, 0x8F), (0x2B, 0xFF), (0x2C, 0xFF), (0x2D, 0xFF)):
                self._write(reg, value)

    def _wait_irq(self, mask, timeout_ms):
        start = time.ticks_ms()
        while True:
            if self._byte(0x04) & mask:  # ComIrqReg
                return
            if time.ticks_diff(time.ticks_ms(), start) > self.deadline_ms:
                raise Timeout("No answer from card")


# ---------------------------------------------------------------- ISO-DEP ----


class IsoDepError(nfc.NFCError):
    """O cartao saiu do protocolo ou passou de um limite"""


def parse_ats(ats):
    """Valida um ATS (sem CRC), devolve (fsc, fwi, sfgi, ta1, tc1, historicos)"""
    ats = bytes(ats)
    if not 1 <= len(ats) <= _MAX_ATS or ats[0] != len(ats):
        raise IsoDepError("Bad ATS length")
    fsci, fwi, sfgi, ta1, tc1 = 2, 4, 0, 0, 0x02
    pos = 1
    if len(ats) > 1:
        t0 = ats[1]
        fsci = t0 & 0x0F
        pos = 2
        # Cada bit de T0 anuncia um byte; confere que ele existe antes de ler
        if pos + bin(t0 & 0x70).count("1") > len(ats):
            raise IsoDepError("Truncated ATS")
        if t0 & 0x10:
            ta1 = ats[pos]
            pos += 1
        if t0 & 0x20:
            fwi, sfgi = ats[pos] >> 4, ats[pos] & 0x0F
            pos += 1
        if t0 & 0x40:
            tc1 = ats[pos]
            pos += 1
    # 15 e reservado nos dois campos; a norma manda usar o padrao
    if fwi == 15:
        fwi = 4
    if sfgi == 15:
        sfgi = 0
    return _FSC_TABLE[min(fsci, 8)], fwi, sfgi, ta1, tc1, ats[pos:]


def _fwt_ms(fwi):
    # FWT = 256 * 16 / fc * 2^FWI = 302 us * 2^FWI
    return (302 * (1 << fwi)) // 1000 + 1


class IsoDep:
    """O minimo de ISO 14443-4 do lado do leitor, a 106 kbit/s, sem CID"""

    def __init__(self, reader, trace=False):
        self.reader = reader
        self.trace = trace
        self.active = False
        self.bn = 0
        self.fsc = 32
        self.fwt_ms = _fwt_ms(4)
        # contadores, para o relatorio
        self.wtx = 0
        self.max_wtxm = 0
        self.timeouts = 0
        self.garbled = 0
        self.resent = 0

    def _frame(self, block, timeout_ms):
        self.reader.deadline_ms = timeout_ms + _TIMEOUT_MARGIN_MS
        if self.trace:
            print("    >> %s" % _hex(block))
        try:
            reply = self.reader.transceive_crc(block, _FSD)
        except nfc.NFCError as error:
            if self.trace:
                print("    << (%s)" % error)
            raise
        if self.trace:
            print("    << %s" % _hex(reply))
        return reply

    def activate(self):
        """RATS. Devolve o ATS cru; fsc e fwt_ms ficam ajustados."""
        self.active = False
        self.bn = 0
        ats = self._frame(bytes([0xE0, _FSDI << 4]), _SELECT_TIMEOUT_MS)
        fsc, fwi, sfgi, _, _, _ = parse_ats(ats)
        # Nunca mandamos mais do que cabe na nossa propria FIFO
        self.fsc = min(fsc, _FSD)
        self.fwt_ms = _fwt_ms(fwi)
        # SFGT: o tempo que o cartao pede antes do primeiro bloco
        time.sleep_ms(min(_fwt_ms(sfgi), _FWT_MAX_MS))
        self.active = True
        return ats

    def deselect(self):
        """S(DESELECT). Nunca levanta excecao."""
        self.active = False
        try:
            self._frame(b"\xC2", _SELECT_TIMEOUT_MS)
        except nfc.NFCError:
            pass

    def _iblock(self, apdu, offset):
        chunk = apdu[offset : offset + self.fsc - 3]  # PCB e CRC ocupam 3
        more = offset + len(chunk) < len(apdu)
        pcb = 0x02 | (0x10 if more else 0) | self.bn
        return bytes([pcb]) + chunk, len(chunk), more

    def exchange(self, apdu):
        """Manda um APDU, devolve a resposta inteira com o status word"""
        apdu = bytes(apdu)
        if not self.active or not 4 <= len(apdu) <= _MAX_APDU:
            raise IsoDepError("Bad APDU or card not activated")
        try:
            return self._exchange(apdu)
        except nfc.NFCError:
            self.active = False
            raise

    def _exchange(self, apdu):
        offset = 0
        block, sent, more = self._iblock(apdu, 0)
        last = block  # o que uma retransmissao repete
        receiving = False  # a resposta comecou; `last` e um R(ACK)
        out = bytearray()
        wtx = 0
        errors = 0
        timeout = self.fwt_ms
        deadline = time.ticks_add(time.ticks_ms(), _APDU_BUDGET_MS)

        while True:
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                raise IsoDepError("APDU took too long")
            try:
                reply = self._frame(block, timeout)
            except nfc.NFCError as error:
                if isinstance(error, Timeout):
                    self.timeouts += 1
                else:
                    self.garbled += 1
                errors += 1
                if errors > _MAX_RETRIES:
                    if isinstance(error, Timeout):
                        raise Timeout("card stopped answering")
                    raise IsoDepError("no valid block: %s" % error)
                # Regras 4 e 5: repete o R(ACK) se o cartao estava encadeando,
                # senao pede com R(NAK) que ele repita
                block = last if receiving else bytes([0xB2 | self.bn])
                timeout = self.fwt_ms
                continue

            pcb, inf = reply[0], reply[1:]
            timeout = self.fwt_ms

            if pcb == 0xF2:  # S(WTX): o cartao pede mais tempo
                wtxm = inf[0] & 0x3F if len(inf) == 1 else 0
                wtx += 1
                if not 1 <= wtxm <= 59 or wtx > _MAX_WTX:
                    raise IsoDepError("Bad or too many WTX")
                self.wtx += 1
                self.max_wtxm = max(self.max_wtxm, wtxm)
                block = bytes([0xF2, wtxm])
                timeout = min(self.fwt_ms * wtxm, _FWT_MAX_MS)
                errors = 0

            elif pcb & 0xFE == 0xA2:  # R(ACK)
                if inf or receiving:
                    raise IsoDepError("Unexpected R(ACK)")
                if pcb & 1 == self.bn:
                    # Regra 7: o pedaco encadeado chegou, segue o proximo
                    if not more:
                        raise IsoDepError("R(ACK) for a final block")
                    self.bn ^= 1
                    offset += sent
                    block, sent, more = self._iblock(apdu, offset)
                    last = block
                    errors = 0
                else:
                    # Regra 6: o cartao nao recebeu o ultimo bloco I
                    errors += 1
                    if errors > _MAX_RETRIES:
                        raise IsoDepError("Too many retransmissions")
                    self.resent += 1
                    block = last

            elif pcb & 0xEE == 0x02:  # bloco I, sem CID e sem NAD
                if more or pcb & 1 != self.bn:
                    raise IsoDepError("I-block out of sequence")
                self.bn ^= 1
                # Tamanho total escolhido pelo cartao: limita antes de juntar
                if len(out) + len(inf) > _MAX_RESPONSE:
                    raise IsoDepError("Response too long")
                out += inf
                if not pcb & 0x10:
                    if len(out) < 2:
                        raise IsoDepError("Response without status word")
                    return bytes(out)
                # Um pedaco vazio nao avanca nada e encadearia para sempre
                if not inf:
                    raise IsoDepError("Empty chained block")
                receiving = True
                block = last = bytes([0xA2 | self.bn])
                errors = 0

            else:
                raise IsoDepError("Unexpected block 0x%02x" % pcb)


# ------------------------------------------------------------ canal seguro ----
#
# O mesmo aperto de mao de src/keystore/javacard/applets/securechannel.py,
# repetido aqui para nao importar o pacote keystore, que abre a UART do hat.


class CardStatus(Exception):
    """O cartao respondeu, mas com um status word de erro"""

    def __init__(self, sw):
        super().__init__("SW " + _hex(sw))
        self.sw = bytes(sw)


class ChannelError(Exception):
    """A resposta do cartao nao confere: HMAC, assinatura ou padding"""


def _apdu(dep, apdu):
    reply = dep.exchange(apdu)
    if reply[-2:] != b"\x90\x00":
        raise CardStatus(reply[-2:])
    return reply[:-2]


def _lv(data):
    return bytes([len(data)]) + data


def _mac(key, data):
    h = hmac.new(key, digestmod="sha256")
    h.update(data)
    return h.digest()[:_MAC]


def _parse_sig(raw):
    """DER, aceitando o zero a esquerda que o cartao deixa em 1 de cada ~128"""
    try:
        return secp256k1.ecdsa_signature_parse_der(raw)
    except ValueError:
        pass
    if len(raw) < 8 or raw[0] != 0x30 or raw[1] != len(raw) - 2 or raw[2] != 0x02:
        raise ChannelError("signature encoding")
    s_tag = 4 + raw[3]
    if len(raw) < s_tag + 2 or raw[s_tag] != 0x02 or s_tag + 2 + raw[s_tag + 1] != len(raw):
        raise ChannelError("signature encoding")
    r = bytes(raw[4:s_tag]).lstrip(b"\x00")
    s = bytes(raw[s_tag + 2 :]).lstrip(b"\x00")
    if len(r) > 32 or len(s) > 32:
        raise ChannelError("signature encoding")
    return secp256k1.ecdsa_signature_parse_compact(
        b"\x00" * (32 - len(r)) + r + b"\x00" * (32 - len(s)) + s
    )


class Channel:
    """Uma abertura de canal seguro, com a etapa atual em `stage`"""

    def __init__(self, dep, card_pub):
        self.dep = dep
        self.card_pub = card_pub  # 65 bytes, como o cartao mandou
        self.stage = ""
        self.card_ms = 0  # tempo do APDU de abertura: e o cartao calculando

    def _ecdh_x(self, pub_raw, secret):
        pub = secp256k1.ec_pubkey_parse(pub_raw)
        # no firmware altera `pub` no lugar; no embit de desktop devolve outro
        pub = secp256k1.ec_pubkey_tweak_mul(pub, secret) or pub
        return secp256k1.ec_pubkey_serialize(pub)[1:33]

    def open(self, mode):
        self.stage = "host key"
        # os.urandom basta para um diagnostico; o firmware usa rng.get_random_bytes
        secret = os.urandom(32)
        host_pub = secp256k1.ec_pubkey_serialize(
            secp256k1.ec_pubkey_create(secret), secp256k1.EC_UNCOMPRESSED
        )
        started = time.ticks_ms()
        if mode == "ee":
            self.stage = "OPEN_EE apdu"
            reply = _apdu(self.dep, b"\xB0\xB5\x00\x00" + _lv(host_pub))
            self.card_ms = time.ticks_diff(time.ticks_ms(), started)
            self.stage = "OPEN_EE reply"
            if len(reply) < 65 + _MAC + 8:
                raise ChannelError("short reply")
            signed, mac, sig = reply[:65], reply[65 : 65 + _MAC], reply[65 + _MAC :]
            shared = hashlib.sha256(self._ecdh_x(signed, secret)).digest()
        else:
            self.stage = "OPEN_SE apdu"
            reply = _apdu(self.dep, b"\xB0\xB4\x00\x00" + _lv(host_pub))
            self.card_ms = time.ticks_diff(time.ticks_ms(), started)
            self.stage = "OPEN_SE reply"
            if len(reply) < 32 + _MAC + 8:
                raise ChannelError("short reply")
            signed, mac, sig = reply[:32], reply[32 : 32 + _MAC], reply[32 + _MAC :]
            shared = hashlib.sha256(self._ecdh_x(self.card_pub, secret) + signed).digest()

        self.host_aes = hashlib.sha256(b"host_aes" + shared).digest()
        self.card_aes = hashlib.sha256(b"card_aes" + shared).digest()
        self.host_mac = hashlib.sha256(b"host_mac" + shared).digest()
        self.card_mac = hashlib.sha256(b"card_mac" + shared).digest()

        self.stage = "handshake HMAC"
        if _mac(self.card_mac, signed) != mac:
            raise ChannelError("wrong HMAC")
        self.stage = "handshake signature"
        sig = secp256k1.ecdsa_signature_normalize(_parse_sig(sig))
        digest = hashlib.sha256(signed + mac).digest()
        if not secp256k1.ecdsa_verify(sig, digest, secp256k1.ec_pubkey_parse(self.card_pub)):
            raise ChannelError("invalid signature")

    def pin_status(self):
        """PIN_STATUS pelo canal, com IV 0: prova que as chaves batem"""
        self.stage = "PIN_STATUS apdu"
        iv = bytes(16)
        plain = b"\x03\x00\x80" + bytes(13)
        ct = aes(self.host_aes, _AES_CBC, iv).encrypt(plain)
        reply = _apdu(self.dep, b"\xB0\xB6\x00\x00" + _lv(ct + _mac(self.host_mac, iv + ct)))
        self.stage = "PIN_STATUS reply"
        ct, mac = reply[:-_MAC], reply[-_MAC:]
        if not ct or len(ct) % 16 or _mac(self.card_mac, iv + ct) != mac:
            raise ChannelError("wrong HMAC")
        plain = aes(self.card_aes, _AES_CBC, iv).decrypt(ct).rstrip(b"\x00")
        if plain[-1:] != b"\x80":
            raise ChannelError("wrong padding")
        if plain[:2] != b"\x90\x00":
            raise CardStatus(plain[:2])
        return plain[2:-1]


# ------------------------------------------------------------- diagnostico ----


def _open_reader(boost):
    if not nfc_ws1850s.is_present():
        print("BUS:     nothing answers at 0x%02x on GPIO7 (SDA) / GPIO8 (SCL)"
              % nfc_ws1850s.WS1850S_ADDR)
        return None
    link = nfc.NFC(DiagReader())
    try:
        link.init()
    except nfc.NFCError as error:
        print("READER:  not a WS1850S, or it does not reset: %s" % error)
        return None
    reader = link.reader
    if boost:
        # RFCfgReg: RxGain 48 dB. GsNReg / CWGsPReg: condutancia maxima dos
        # drivers da antena.
        for reg, value in ((0x26, 0x70), (0x27, 0xF8), (0x28, 0x3F)):
            reader._write(reg, value)
    print("READER:  ready. RFCfg=%02x GsN=%02x CWGsP=%02x%s"
          % (reader._byte(0x26), reader._byte(0x27), reader._byte(0x28),
             " (boost)" if boost else ""))
    return link


def _select(link):
    """WUPA, anticolisao e SELECT. Devolve (atqa, uid, sak) de qualquer cartao."""
    reader = link.reader
    reader.deadline_ms = _SELECT_TIMEOUT_MS
    atqa, _ = reader.transceive(b"\x52", 7, 2)
    if len(atqa) != 2:
        raise nfc.NFCError("Bad ATQA")
    uid, sak = link._cascade(nfc.CMD_SEL_CL1)
    if sak & nfc.SAK_CASCADE_BIT:
        if uid[0] != nfc.CASCADE_TAG:
            raise nfc.NFCError("Unsupported UID")
        head = uid[1:4]
        uid, sak = link._cascade(nfc.CMD_SEL_CL2)
        if sak & nfc.SAK_CASCADE_BIT:
            raise nfc.NFCError("Unsupported UID")
        uid = head + uid
    return atqa, uid, sak


def _session(link, dep, seconds, quiet=False):
    """Do campo ligado ate o applet selecionado. Devolve a etapa onde parou,
    ou None se chegou ao fim."""
    reader = link.reader
    reader.field(True)
    time.sleep_ms(50)  # o cartao leva alguns ms para subir depois do campo

    if not quiet:
        print("CARD:    hold the JavaCard on the reader (%d s)..." % seconds)
    deadline = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while True:
        try:
            atqa, uid, sak = _select(link)
            break
        except nfc.NFCError:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                print("CARD:    none found")
                return "CARD"
            time.sleep_ms(150)
    if not quiet:
        print("CARD:    UID %s, ATQA %s, SAK 0x%02x" % (_hex(uid), _hex(atqa), sak))
    if not sak & 0x20:
        print("CARD:    SAK without the ISO 14443-4 bit: not a JavaCard")
        return "CARD"

    try:
        ats = dep.activate()
    except nfc.NFCError as error:
        print("RATS:    failed: %s" % error)
        return "RATS"
    if not quiet:
        fsc, fwi, sfgi, ta1, tc1, hist = parse_ats(ats)
        print("RATS:    ATS %s" % _hex(ats))
        print("         FSC %d (using %d), FWI %d (FWT %d ms), SFGI %d, TA1 0x%02x,"
              % (fsc, dep.fsc, fwi, dep.fwt_ms, sfgi, ta1))
        print("         CID %s, NAD %s, historical %s"
              % ("yes" if tc1 & 2 else "no", "yes" if tc1 & 1 else "no", _hex(hist) or "-"))

    try:
        started = time.ticks_ms()
        reply = dep.exchange(b"\x00\xA4\x04\x00" + _lv(AID))
    except nfc.NFCError as error:
        print("SELECT:  no answer to the first APDU: %s" % error)
        return "SELECT"
    sw = reply[-2:]
    if not quiet or sw != b"\x90\x00":
        print("SELECT:  AID %s -> SW %s (%d ms)"
              % (_hex(AID), _hex(sw), time.ticks_diff(time.ticks_ms(), started)))
    if sw != b"\x90\x00":
        print("         the applet did not answer over contactless.")
        try:
            # O aplicativo padrao responde em qualquer JavaCard: separa "o
            # cartao nao fala APDU por NFC" de "este applet nao esta acessivel"
            reply = dep.exchange(b"\x00\xA4\x04\x00\x00")
            print("         default SELECT -> SW %s, %d bytes: APDUs do work over NFC"
                  % (_hex(reply[-2:]), len(reply) - 2))
        except nfc.NFCError as error:
            print("         default SELECT failed too: %s" % error)
        return "SELECT"
    return None


def _probe(link, dep):
    """Depois de uma falha, sem desligar o campo: em que estado o cartao ficou?

    Um cartao em sessao ISO-DEP ignora WUPA. Se ele responde, perdeu a sessao
    sozinho, e com o campo ligado o tempo todo isso e reinicio por falta de
    energia.
    """
    reader = link.reader
    reader.deadline_ms = _SELECT_TIMEOUT_MS
    try:
        # Um ATQA sao dois bytes inteiros. Com a antena no maximo o receptor
        # devolve ruido de poucos bits, que nao pode contar como cartao.
        atqa, bits = reader.transceive(b"\x52", 7, 2)
        if len(atqa) == 2 and not bits:
            return "RESET"
    except nfc.NFCError:
        pass
    try:
        reader.transceive_crc(bytes([0xB2 | dep.bn]), _FSD)
        return "ALIVE"
    except Timeout:
        return "SILENT"
    except nfc.NFCError:
        return "ALIVE"


def find(seconds=120, hold=15, boost=False):
    """Mostra ao vivo se o cartao responde, para achar a posicao.

    Cada tentativa e uma selecao completa mais RATS, com o cartao desligado
    entre uma e outra. Devolve True depois de `hold` seguidas com sucesso.
    """
    link = _open_reader(boost)
    if link is None:
        return False
    reader = link.reader
    dep = IsoDep(reader)
    streak = 0
    window = ""
    try:
        reader.field(True)
        time.sleep_ms(100)
        print("FIND:    move the card slowly over the reader; stop where the bar fills")
        deadline = time.ticks_add(time.ticks_ms(), seconds * 1000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                _, _, sak = _select(link)
                if not sak & 0x20:
                    raise nfc.NFCError("not ISO-DEP")
                dep.activate()
                streak += 1
                window += "#"
            except nfc.NFCError:
                streak = 0
                window += "-"
            reader.field(False)
            time.sleep_ms(40)
            reader.field(True)
            time.sleep_ms(60)
            if streak >= hold:
                print("FIND:    [%s] good spot. KEEP THE CARD EXACTLY THERE." % window)
                return True
            if len(window) >= 5:
                print("         [%s] %d in a row" % (window, streak))
                window = ""
        print("FIND:    no stable position found")
        return False
    finally:
        reader.deadline_ms = _SELECT_TIMEOUT_MS
        link.deinit()


def go(seconds=120, **options):
    """find() e, achada a posicao, run()"""
    if not find(seconds, boost=options.get("boost", False)):
        return {"ok": False, "stage": "FIND"}
    options.setdefault("seconds", 5)
    return run(**options)


_PROBE_TEXT = {
    "RESET": "card answers WUPA again: it lost the session with the field on (power reset)",
    "ALIVE": "card still in session and answering: protocol problem, not power",
    "SILENT": "card silent: removed, still busy, or hung",
}


def run(rounds=10, modes=("es", "ee"), trace=False, boost=False, seconds=15):
    """Devolve um dict com o resumo; "ok" e True so se tudo passou."""
    result = {"ok": False, "stage": "BUS", "passed": {}, "rounds": rounds, "probes": []}
    link = _open_reader(boost)
    if link is None:
        return result
    dep = IsoDep(link.reader, trace)
    try:
        result["stage"] = _session(link, dep, seconds)
        if result["stage"]:
            return result

        result["stage"] = "GET_PUBKEY"
        try:
            started = time.ticks_ms()
            card_pub = _apdu(dep, b"\xB0\xB2\x00\x00")
            secp256k1.ec_pubkey_parse(card_pub)
        except (nfc.NFCError, CardStatus, ValueError) as error:
            print("PUBKEY:  failed: %s" % error)
            return result
        print("PUBKEY:  %s... (%d ms)"
              % (_hex(card_pub[:9]), time.ticks_diff(time.ticks_ms(), started)))

        result["stage"] = "CHANNEL"
        for mode in modes:
            result["passed"][mode] = _rounds(link, dep, card_pub, mode, rounds, result)
            if result["stage"] != "CHANNEL":
                break
        else:
            result["stage"] = None
        _verdict(result, dep)
        return result
    finally:
        dep.deselect()
        link.reader.deadline_ms = _SELECT_TIMEOUT_MS
        link.deinit()


def _rounds(link, dep, card_pub, mode, rounds, result):
    """Abre o canal `rounds` vezes. Devolve quantas passaram."""
    passed = 0
    failed_in_a_row = 0
    times = []
    print("CHANNEL: mode %s, %d rounds" % (mode, rounds))
    for number in range(1, rounds + 1):
        channel = Channel(dep, card_pub)
        wtx = dep.wtx
        lost = dep.timeouts + dep.garbled + dep.resent
        started = time.ticks_ms()
        try:
            channel.open(mode)
            status = channel.pin_status()
        except (nfc.NFCError, CardStatus, ChannelError, ValueError) as error:
            failed_in_a_row += 1
            print("  %s %2d: FAILED at %s after %d ms: %s"
                  % (mode, number, channel.stage,
                     time.ticks_diff(time.ticks_ms(), started), error))
            if isinstance(error, nfc.NFCError):
                state = _probe(link, dep)
                result["probes"].append(state)
                print("         %s" % _PROBE_TEXT[state])
            # Recomeca do zero: campo desligado, cartao frio, nova sessao
            link.reader.field(False)
            time.sleep_ms(200)
            stage = _session(link, dep, 3, quiet=True)
            if stage:
                print("         could not start a new session (stopped at %s)" % stage)
                result["stage"] = "RECOVERY"
                break
            if isinstance(error, Timeout) and state == "SILENT":
                # Mudo com o campo ligado e de volta depois de um ciclo de
                # campo: o cartao nao saiu do lugar, ele se calou sob carga.
                result["probes"].append("REVIVED")
                print("         it came back after a field cycle: the card went mute under")
                print("         load without being moved, which points to power")
            if failed_in_a_row >= 3:
                print("         three failures in a row, giving up on this mode")
                break
            continue
        failed_in_a_row = 0
        passed += 1
        times.append(channel.card_ms)
        print("  %s %2d: ok, card %d ms, total %d ms, %d WTX, %d lost frames, PIN status %s"
              % (mode, number, channel.card_ms, time.ticks_diff(time.ticks_ms(), started),
                 dep.wtx - wtx, dep.timeouts + dep.garbled + dep.resent - lost, _hex(status)))
    if times:
        print("  %s: %d/%d passed, card time min %d / avg %d / max %d ms"
              % (mode, passed, rounds, min(times), sum(times) // len(times), max(times)))
    else:
        print("  %s: 0/%d passed" % (mode, rounds))
    return passed


def _verdict(result, dep):
    total = sum(result["passed"].values())
    wanted = result["rounds"] * len(result["passed"])
    lost = dep.timeouts + dep.garbled + dep.resent
    print("LINK:    %d WTX (largest multiplier %d), %d timeouts, %d bad frames, %d resends"
          % (dep.wtx, dep.max_wtxm, dep.timeouts, dep.garbled, dep.resent))
    if result["stage"] is None and total == wanted:
        result["ok"] = True
        if lost:
            print("RESULT:  PASS, but %d frames needed a retry: the link is marginal" % lost)
        else:
            print("RESULT:  PASS. The reader powers the card and the applet answers.")
    elif "RESET" in result["probes"] or "REVIVED" in result["probes"]:
        print("RESULT:  FAIL, %d/%d. The card dropped out with the field on: POWER."
              % (total, wanted))
        print("         Try run(boost=True) and moving the card; if it still resets,")
        print("         this reader can not run the card.")
    else:
        print("RESULT:  FAIL, %d/%d, with no sign of a card reset. Run again with"
              % (total, wanted))
        print("         trace=True and send the output.")
