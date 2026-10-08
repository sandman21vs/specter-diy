"""`uscard` para a Waveshare ESP32-P4 4.3-C com o Smartcard Hat SEC1210.

O hat (3rdIteration/seedsigner, electronics/SmartcardHat) usa um Microchip
SEC1210-URT: um leitor de smartcard que fala CCID sobre UART. O SEC1210 cuida da
parte eletrica do cartao -- alimentacao, clock, reset, deteccao e a negociacao
de parametros a partir do ATR -- e entrega ao host os blocos do cartao (nivel
TPDU). Aqui ficam tres camadas:

  1. o enquadramento serial do CCID ("GemPC Twin"): 03 06 <CCID> <LRC>;
  2. os comandos CCID: estado do slot, liga/desliga o cartao, troca de bloco;
  3. o protocolo T=1 do cartao (blocos I/R/S), e T=0 para cartoes antigos.

Ligacao. O hat foi feito para o conector de um Raspberry Pi, onde o UART cai nos
pinos 8 e 10 -- na Waveshare, GPIO37 e GPIO38, que sao o console e a gravacao de
firmware. Por isso o hat e usado com os dois sinais desviados para GPIO21 (TX do
P4, RXD do SEC1210) e GPIO22 (RX do P4, TXD do SEC1210). O hat tira 5 V do
proprio conector.

Referencia do protocolo serial: ccid_serial.c do driver CCID de Ludovic
Rousseau, que suporta este leitor como "SEC1210URT": 115200 baud, 8 bits, dois
stop bits, sem eco.

A API espelha f469-disco/libs/unix/uscard.py, que e a que o keystore do Specter
usa: Reader.createConnection(), e na conexao isCardInserted(), connect(),
disconnect(), getATR() e transmit(apdu) -> dados + SW1 SW2.
"""

import time

import machine

# Pinos do hat modificado. TX/RX do ponto de vista do P4.
UART_ID = 1
TX_PIN = 21
RX_PIN = 22

_SYNC = 0x03
_CTRL_ACK = 0x06
_CTRL_NAK = 0x15
_NOTIFY_SLOT_CHANGE = 0x50

_PC_ICC_POWER_ON = 0x62
_PC_ICC_POWER_OFF = 0x63
_PC_GET_SLOT_STATUS = 0x65
_PC_XFR_BLOCK = 0x6F

# bStatus: bits 0-1 dizem o estado do cartao, bits 6-7 o resultado do comando.
_ICC_ABSENT = 2
_CMD_FAILED = 1
_CMD_TIME_EXTENSION = 2

# Quanto esperar por uma resposta do leitor. Um comando de status volta em
# milissegundos; uma operacao criptografica no cartao pode levar segundos.
_STATUS_TIMEOUT_MS = 250
_POWER_TIMEOUT_MS = 3000
_XFR_TIMEOUT_MS = 20000
# Sem hat ligado cada tentativa custa um timeout. O Specter consulta a presenca
# do cartao em lacos, entao depois de uma falha nao insistimos por este tempo.
_READER_RETRY_MS = 2000

_T1_MAX_RETRIES = 3


class SmartcardException(Exception):
    pass


class CardConnectionException(SmartcardException):
    pass


class NoCardException(SmartcardException):
    pass


_debug = False


def enableDebug(*args, **kwargs):
    """Imprime no console cada comando CCID e cada resposta, em hexadecimal."""
    global _debug
    _debug = True


def disableDebug(*args, **kwargs):
    global _debug
    _debug = False


def _trace(direction, data):
    import binascii

    print("uscard", direction, binascii.hexlify(data).decode())


def _lrc(data):
    value = 0
    for byte in data:
        value ^= byte
    return value


class _SEC1210:
    """Comandos CCID sobre a serial do SEC1210."""

    def __init__(self):
        self._uart = None
        self._seq = 0
        self._absent_until = None

    def _open(self):
        if self._uart is None:
            self._uart = machine.UART(
                UART_ID,
                baudrate=115200,
                bits=8,
                parity=None,
                stop=2,
                tx=TX_PIN,
                rx=RX_PIN,
                timeout=20,
                rxbuf=1024,
            )
        return self._uart

    def _read(self, count, deadline):
        uart = self._uart
        data = b""
        while len(data) < count:
            chunk = uart.read(count - len(data))
            if chunk:
                data += chunk
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise CardConnectionException("card reader timeout")
        return data

    def command(self, message_type, data=b"", params=b"\x00\x00\x00", timeout_ms=_XFR_TIMEOUT_MS):
        """Manda um comando CCID e devolve (bStatus, bError, dados)."""
        if self._absent_until is not None:
            if time.ticks_diff(self._absent_until, time.ticks_ms()) > 0:
                raise CardConnectionException("card reader not responding")
            self._absent_until = None
        uart = self._open()
        self._seq = (self._seq + 1) & 0xFF
        seq = self._seq
        message = (
            bytes([message_type])
            + len(data).to_bytes(4, "little")
            + bytes([0, seq])
            + params
            + data
        )
        frame = bytes([_SYNC, _CTRL_ACK]) + message
        frame += bytes([_lrc(frame)])
        stale = uart.read()  # descarta notificacoes antigas de troca de cartao
        if _debug:
            if stale:
                _trace("stale", stale)
            _trace(">", message)
        uart.write(frame)
        try:
            return self._response(seq, time.ticks_add(time.ticks_ms(), timeout_ms))
        except CardConnectionException:
            self._absent_until = time.ticks_add(time.ticks_ms(), _READER_RETRY_MS)
            raise

    def _response(self, seq, deadline):
        while True:
            first = self._read(1, deadline)[0]
            if first == _NOTIFY_SLOT_CHANGE:
                # RDR_to_PC_NotifySlotChange: um byte de estado, fora de quadro.
                self._read(1, deadline)
                continue
            if first != _SYNC:
                # Pedido de tempo do T=0 (0x80..0xFF) ou lixo de linha.
                continue
            control = self._read(1, deadline)[0]
            if control == _CTRL_NAK:
                self._read(1, deadline)
                raise CardConnectionException("card reader rejected the frame")
            if control != _CTRL_ACK:
                continue
            header = self._read(10, deadline)
            length = int.from_bytes(header[1:5], "little")
            if length > 600:
                raise CardConnectionException("card reader sent a bad length")
            data = self._read(length, deadline)
            check = self._read(1, deadline)[0]
            if _lrc(bytes([_SYNC, _CTRL_ACK]) + header + data) != check:
                raise CardConnectionException("card reader checksum error")
            if header[6] != seq:
                continue
            if _debug:
                _trace("<", header + data)
            status = header[7]
            if (status >> 6) == _CMD_TIME_EXTENSION:
                # O leitor avisa que o cartao pediu mais tempo e responde depois.
                continue
            return status, header[8], data

    def card_present(self):
        status, _, _ = self.command(_PC_GET_SLOT_STATUS, timeout_ms=_STATUS_TIMEOUT_MS)
        return (status & 0x03) != _ICC_ABSENT

    def power_on(self):
        # O SEC1210 nao escolhe a tensao sozinho: bPowerSelect = 0 ("automatico")
        # volta erro de hardware 0xFB. Como o driver CCID faz por padrao,
        # tentamos 5 V, depois 3 V, depois 1,8 V, desligando entre as tentativas.
        error = 0
        for power_select in (1, 2, 3):
            status, error, atr = self.command(
                _PC_ICC_POWER_ON, params=bytes([power_select, 0, 0]),
                timeout_ms=_POWER_TIMEOUT_MS,
            )
            if (status & 0x03) == _ICC_ABSENT:
                raise NoCardException("no card in the reader")
            if (status >> 6) != _CMD_FAILED and atr:
                return bytes(atr)
            self.power_off()
            time.sleep_ms(20)
        raise CardConnectionException("card did not answer to reset (error 0x%02x)" % error)

    def power_off(self):
        try:
            self.command(_PC_ICC_POWER_OFF, timeout_ms=_STATUS_TIMEOUT_MS)
        except SmartcardException:
            pass

    def exchange(self, block, wait_multiplier=0):
        """Manda um bloco ao cartao e devolve o que ele respondeu."""
        status, error, data = self.command(
            _PC_XFR_BLOCK, block, bytes([wait_multiplier, 0, 0])
        )
        if (status & 0x03) == _ICC_ABSENT:
            raise NoCardException("card removed")
        if (status >> 6) == _CMD_FAILED:
            raise CardConnectionException("card exchange failed (error 0x%02x)" % error)
        return bytes(data)


def _parse_atr(atr):
    """Devolve (protocolo, IFSC, usa_crc) a partir do ATR."""
    if len(atr) < 2:
        raise CardConnectionException("ATR too short")
    protocol = 0
    first_protocol = None
    ifsc = 32
    use_crc = False
    index = 1
    y = atr[1] >> 4
    block = 1
    while True:
        has_ta, has_tb, has_tc, has_td = y & 1, y & 2, y & 4, y & 8
        if has_ta:
            index += 1
            # TA apos a primeira indicacao de T=1: tamanho maximo de bloco do cartao.
            if block > 2 and protocol == 1 and index < len(atr):
                ifsc = atr[index]
        if has_tb:
            index += 1
        if has_tc:
            index += 1
            if block > 2 and protocol == 1 and index < len(atr):
                use_crc = bool(atr[index] & 1)
        if not has_td:
            break
        index += 1
        if index >= len(atr):
            break
        protocol = atr[index] & 0x0F
        if first_protocol is None:
            first_protocol = protocol
        y = atr[index] >> 4
        block += 1
    if first_protocol is None:
        first_protocol = 0
    if not 1 <= ifsc <= 254:
        ifsc = 32
    return first_protocol, ifsc, use_crc


class CardConnection:
    T0_protocol = 2
    T1_protocol = 1

    def __init__(self, reader):
        self._reader = reader
        self._atr = None
        self._protocol = None
        self._ifsc = 32
        self._send_seq = 0
        self._recv_seq = 0

    def isCardInserted(self):
        try:
            present = self._reader.card_present()
        except SmartcardException:
            present = False
        if not present:
            self._atr = None
        return present

    def connect(self, protocol=None):
        atr = self._reader.power_on()
        card_protocol, ifsc, use_crc = _parse_atr(atr)
        if card_protocol not in (0, 1):
            self._reader.power_off()
            raise CardConnectionException("unsupported card protocol T=%d" % card_protocol)
        if card_protocol == 1 and use_crc:
            self._reader.power_off()
            raise CardConnectionException("T=1 cards with CRC are not supported")
        self._atr = atr
        self._protocol = card_protocol
        self._ifsc = ifsc
        self._send_seq = 0
        self._recv_seq = 0
        if card_protocol == 1:
            self._negotiate_ifsd()

    def disconnect(self):
        if self._atr is not None:
            self._reader.power_off()
        self._atr = None

    def getATR(self):
        if self._atr is None:
            raise NoCardException("card is not connected")
        return self._atr

    def transmit(self, data):
        if self._atr is None:
            raise NoCardException("card is not connected")
        apdu = bytes(data)
        if self._protocol == 1:
            return self._transmit_t1(apdu)
        return self._transmit_t0(apdu)

    # --------------------------------------------------------------- T=1 ----
    #
    # Bloco: NAD PCB LEN INF... LRC.  PCB: I = 0 N(S) M 0 0000; R = 1 0 0 N(R)
    # 00 EE; S = 1 1 r tttttt (r = resposta).

    def _t1_block(self, pcb, info=b""):
        block = bytes([0x00, pcb, len(info)]) + info
        return block + bytes([_lrc(block)])

    def _t1_exchange(self, block, wait_multiplier=0):
        response = self._reader.exchange(block, wait_multiplier)
        if len(response) < 4 or len(response) != response[2] + 4:
            raise CardConnectionException("malformed T=1 block")
        if _lrc(response[:-1]) != response[-1]:
            raise CardConnectionException("T=1 checksum error")
        return response[1], response[3:-1]

    def _negotiate_ifsd(self):
        # Diz ao cartao que aceitamos blocos de ate 254 bytes. Cartao que nao
        # responde fica com os 32 do padrao; nao e motivo para falhar.
        try:
            pcb, info = self._t1_exchange(self._t1_block(0xC1, b"\xFE"))
        except CardConnectionException:
            return
        if pcb != 0xE1 or info != b"\xFE":
            return

    def _transmit_t1(self, apdu):
        result = b""
        offset = 0
        chunk = apdu[: self._ifsc]
        more = len(chunk) < len(apdu)
        # `pending` e o ultimo bloco I ou R que mandamos: e o que se repete se
        # o cartao pedir retransmissao. Blocos S nao entram aqui.
        pending = self._t1_block((self._send_seq << 6) | (0x20 if more else 0), chunk)
        awaiting_ack = True  # o bloco I em `pending` ainda nao foi confirmado
        block = pending
        wait_multiplier = 0
        retries = 0
        while True:
            pcb, info = self._t1_exchange(block, wait_multiplier)
            wait_multiplier = 0
            if pcb == 0xC3:
                # S(WTX request): o cartao pede mais tempo. Confirmamos, e o
                # multiplicador vale para a espera seguinte do leitor.
                wait_multiplier = info[0] if info else 1
                block = self._t1_block(0xE3, info)
            elif pcb == 0xC1:
                # S(IFS request): o cartao mudou o tamanho de bloco que aceita.
                if info and 1 <= info[0] <= 254:
                    self._ifsc = info[0]
                block = self._t1_block(0xE1, info)
            elif pcb & 0xC0 == 0xC0:
                raise CardConnectionException("unexpected T=1 S-block 0x%02x" % pcb)
            elif pcb & 0x80:
                # Bloco R. N(R) diferente do nosso N(S) confirma o pedaco
                # encadeado; igual, pede a repeticao do ultimo bloco.
                if awaiting_ack and more and ((pcb >> 4) & 1) != self._send_seq:
                    self._send_seq ^= 1
                    offset += len(chunk)
                    chunk = apdu[offset : offset + self._ifsc]
                    more = offset + len(chunk) < len(apdu)
                    pending = self._t1_block(
                        (self._send_seq << 6) | (0x20 if more else 0), chunk
                    )
                    retries = 0
                else:
                    retries += 1
                    if retries > _T1_MAX_RETRIES:
                        raise CardConnectionException("T=1 too many retransmissions")
                block = pending
            else:
                # Bloco I: a resposta, inteira ou em partes.
                if awaiting_ack:
                    self._send_seq ^= 1
                    awaiting_ack = False
                if ((pcb >> 6) & 1) != self._recv_seq:
                    raise CardConnectionException("T=1 sequence error")
                self._recv_seq ^= 1
                result += info
                if not pcb & 0x20:
                    return result
                # Ha mais: confirma com um bloco R pedindo o proximo.
                pending = self._t1_block(0x80 | (self._recv_seq << 4))
                block = pending
                retries = 0

    # --------------------------------------------------------------- T=0 ----
    #
    # No nivel TPDU o leitor trata os bytes de procedimento; ao host cabe pedir
    # a resposta (GET RESPONSE em 61xx) e corrigir o Le (6Cxx).

    def _transmit_t0(self, apdu):
        if len(apdu) < 4:
            raise CardConnectionException("APDU too short")
        if len(apdu) == 4:
            tpdu = apdu + b"\x00"
        elif len(apdu) == 5:
            tpdu = apdu
        else:
            lc = apdu[4]
            # Caso 4 (dados + Le): o Le nao vai no TPDU do T=0.
            tpdu = apdu[: 5 + lc]
        response = self._reader.exchange(tpdu)
        result = b""
        while True:
            if len(response) < 2:
                raise CardConnectionException("T=0 response too short")
            sw1, sw2 = response[-2], response[-1]
            result += response[:-2]
            if sw1 == 0x61:
                response = self._reader.exchange(bytes([apdu[0], 0xC0, 0x00, 0x00, sw2]))
            elif sw1 == 0x6C and len(tpdu) == 5:
                tpdu = tpdu[:4] + bytes([sw2])
                response = self._reader.exchange(tpdu)
            else:
                return result + bytes([sw1, sw2])


_sec1210 = None


class Reader:
    def __init__(self, *args, **kwargs):
        # Os pinos que o Specter passa sao os do leitor do Shield no STM32; aqui
        # o leitor e o SEC1210 no UART definido no topo deste arquivo.
        self.name = kwargs.get("name", "SEC1210 smartcard hat")

    def createConnection(self):
        global _sec1210
        if _sec1210 is None:
            _sec1210 = _SEC1210()
        return CardConnection(_sec1210)
