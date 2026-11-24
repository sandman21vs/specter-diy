"""Teste do uscard (SEC1210) no computador, sem placa nem hat.

    python3 ports/esp32p4/test_uscard_host.py

Troca `machine.UART` por um SEC1210 simulado com um cartao T=1 (ou T=0) atras
dele e exercita o que o hardware real exige do driver: o enquadramento serial do
CCID, os comandos de slot e energia, e o protocolo T=1 com encadeamento nos dois
sentidos, pedido de mais tempo (WTX), pedido de retransmissao e mudanca de IFS.

Nao substitui o teste no hardware: valida a logica do protocolo, nao os niveis
eletricos, os tempos reais nem as particularidades do firmware do SEC1210.
"""

import os
import sys
import time
import types

# --- ambiente MicroPython minimo ---------------------------------------------

time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_add = lambda a, b: a + b
time.ticks_diff = lambda a, b: a - b
time.sleep_ms = lambda ms: time.sleep(ms / 1000)

ATR_T1 = bytes.fromhex("3BF81300008131FE454A434F5076323431B7")  # JCOP, IFSC 254
ATR_T0 = bytes.fromhex("3B6800000073C84013009000")


def lrc(data):
    value = 0
    for byte in data:
        value ^= byte
    return value


class FakeCardT1:
    """Lado do cartao no T=1. O APDU de comando escolhe o comportamento."""

    def __init__(self):
        self.ifsd = 32
        self.recv_seq = 0
        self.send_seq = 0
        self.incoming = b""
        self.outgoing = []
        self.asked_wtx = False
        self.asked_resend = False
        self.asked_ifs = False
        self.last = None

    def block(self, pcb, info=b""):
        body = bytes([0, pcb, len(info)]) + info
        self.last = body + bytes([lrc(body)])
        return self.last

    def answer(self, apdu):
        ins = apdu[1]
        if ins == 0x01:  # eco
            return apdu[5:] + b"\x90\x00"
        if ins == 0x02:  # resposta longa: encadeamento do cartao para o host
            return bytes(range(256)) * 3 + b"\x90\x00"
        if ins == 0x03:  # pede mais tempo antes de responder
            return b"slow" + b"\x90\x00"
        if ins == 0x04:  # pede retransmissao uma vez
            return b"again" + b"\x90\x00"
        if ins == 0x05:  # muda o IFS antes de responder
            return b"ifs" + b"\x90\x00"
        return b"\x6D\x00"

    def next_response_block(self):
        chunk = self.outgoing.pop(0)
        pcb = (self.send_seq << 6) | (0x20 if self.outgoing else 0)
        self.send_seq ^= 1
        return self.block(pcb, chunk)

    def start_response(self, apdu):
        data = self.answer(apdu)
        self.outgoing = [data[i : i + self.ifsd] for i in range(0, len(data), self.ifsd)]
        return self.next_response_block()

    def handle(self, raw):
        assert lrc(raw[:-1]) == raw[-1], "host sent a bad LRC"
        pcb, info = raw[1], raw[3:-1]
        assert len(info) == raw[2]
        if pcb == 0xC1:  # S(IFS request) do host
            self.ifsd = info[0]
            return self.block(0xE1, info)
        if pcb in (0xE3, 0xE1):  # resposta do host a um bloco S nosso
            return self.start_response(self.apdu)
        if pcb & 0xC0 == 0x80:  # bloco R do host
            if ((pcb >> 4) & 1) == self.send_seq and self.outgoing:
                return self.next_response_block()
            return self.last  # retransmissao
        assert pcb & 0x80 == 0, "unexpected block from host: %02x" % pcb
        if ((pcb >> 6) & 1) != self.recv_seq:
            return self.block(0x80 | (self.recv_seq << 4) | 0x02)
        ins = (self.incoming + info)[1] if len(self.incoming + info) > 1 else 0
        if ins == 0x04 and not self.asked_resend and not pcb & 0x20:
            self.asked_resend = True
            return self.block(0x80 | (self.recv_seq << 4))  # "repita"
        self.recv_seq ^= 1
        self.incoming += info
        if pcb & 0x20:
            return self.block(0x80 | (self.recv_seq << 4))  # confirma o pedaco
        self.apdu, self.incoming = self.incoming, b""
        if self.apdu[1] == 0x03 and not self.asked_wtx:
            self.asked_wtx = True
            return self.block(0xC3, b"\x02")
        if self.apdu[1] == 0x05 and not self.asked_ifs:
            self.asked_ifs = True
            return self.block(0xC1, b"\x10")
        return self.start_response(self.apdu)


class FakeCardT0:
    def handle(self, tpdu):
        if tpdu[1] == 0xC0:  # GET RESPONSE
            return b"t0-data!"[: tpdu[4]] + b"\x90\x00"
        if tpdu[1] == 0x01:
            return b"\x61\x08"
        if tpdu[1] == 0x02:
            return b"\x6C\x03" if tpdu[4] != 3 else b"abc\x90\x00"
        return b"\x6D\x00"


class FakeSEC1210:
    """Lado do leitor: quadros 03 06 <CCID> <LRC> a 115200 8N2."""

    present = True
    card = None
    atr = ATR_T1
    silent = False
    noise = True
    voltage = 1  # bPowerSelect que o cartao aceita: 1 = 5 V, 2 = 3 V, 3 = 1,8 V
    detect_after = 0  # quantas consultas de status respondem "vazio" antes de ver o cartao

    def __init__(self, *args, **kwargs):
        assert kwargs["baudrate"] == 115200 and kwargs["stop"] == 2
        assert kwargs["tx"] == 21 and kwargs["rx"] == 22
        self.rx = b""
        self.max_wait = 0

    def frame(self, message):
        body = bytes([0x03, 0x06]) + message
        return body + bytes([lrc(body)])

    def reply(self, msg_type, seq, data=b"", status=0, error=0):
        header = bytes([msg_type]) + len(data).to_bytes(4, "little") + bytes([0, seq, status, error, 0])
        return self.frame(header + data)

    def write(self, frame):
        cls = type(self)
        if cls.silent:
            return len(frame)
        assert frame[0] == 0x03 and frame[1] == 0x06 and lrc(frame[:-1]) == frame[-1]
        message = frame[2:-1]
        msg_type, seq = message[0], message[6]
        length = int.from_bytes(message[1:5], "little")
        data = message[10:]
        assert len(data) == length
        icc = 0 if cls.present else 2
        out = b""
        if cls.noise:
            out += bytes([0x50, 0x03])  # notificacao de troca de cartao, fora de quadro
        if msg_type == 0x65:
            if cls.detect_after > 0:
                cls.detect_after -= 1
                out += self.reply(0x81, seq, status=2)  # ainda nao detectou o cartao
            else:
                out += self.reply(0x81, seq, status=icc if cls.card or not cls.present else 1)
        elif msg_type == 0x62:
            self.power_selects = getattr(self, "power_selects", []) + [message[7]]
            if cls.present and message[7] != cls.voltage:
                # Como o SEC1210 real: tensao errada (ou "automatica") da erro.
                out += self.reply(0x80, seq, status=0x41, error=0xFB)
            elif not cls.present:
                out += self.reply(0x80, seq, status=0x42, error=0xFE)
            else:
                cls.card = FakeCardT1() if cls.atr is ATR_T1 else FakeCardT0()
                out += self.reply(0x80, seq, cls.atr)
        elif msg_type == 0x63:
            cls.card = None
            out += self.reply(0x81, seq, status=1 if cls.present else 2)
        elif msg_type == 0x6F:
            self.max_wait = max(self.max_wait, message[7])
            if cls.noise:
                out += self.reply(0x80, seq, status=0x80)  # extensao de tempo
            out += self.reply(0x80, seq, cls.card.handle(data))
        self.rx += out
        return len(frame)

    def read(self, count=None):
        if not self.rx:
            return None
        count = len(self.rx) if count is None else count
        data, self.rx = self.rx[:count], self.rx[count:]
        return data


machine = types.ModuleType("machine")
machine.UART = FakeSEC1210
sys.modules["machine"] = machine
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import uscard  # noqa: E402

uscard._STATUS_TIMEOUT_MS = 50
uscard._READER_RETRY_MS = 100
uscard._SETTLE_MS = 400
uscard._SETTLE_POLL_MS = 10


def check(name, condition):
    print("%-58s %s" % (name, "OK" if condition else "FAIL"))
    if not condition:
        raise SystemExit(1)


def fresh():
    uscard._sec1210 = None
    FakeSEC1210.card = None
    return uscard.Reader(name="test").createConnection()


def main():
    conn = fresh()
    check("card detected", conn.isCardInserted())
    conn.connect(conn.T1_protocol)
    check("ATR returned", conn.getATR() == ATR_T1)
    check("IFSC taken from the ATR (254)", conn._ifsc == 254)
    check("card accepted our IFSD of 254", FakeSEC1210.card.ifsd == 254)

    check("short APDU echoed", conn.transmit(b"\x80\x01\x00\x00\x03abc") == b"abc\x90\x00")

    big = bytes(range(250))
    apdu = b"\x80\x01\x00\x00\xfa" + big + b"\x00" * 300  # maior que um bloco
    check("command chained host -> card", conn.transmit(apdu)[-2:] == b"\x90\x00")

    long_answer = conn.transmit(b"\x80\x02\x00\x00\x00")
    check("response chained card -> host (770 bytes)", long_answer == bytes(range(256)) * 3 + b"\x90\x00")

    check("waiting time extension (WTX) honoured", conn.transmit(b"\x80\x03\x00\x00\x00") == b"slow\x90\x00")
    check("WTX multiplier passed to the reader", uscard._sec1210._uart.max_wait == 2)
    check("retransmission request honoured", conn.transmit(b"\x80\x04\x00\x00\x00") == b"again\x90\x00")
    check("IFS change from the card honoured", conn.transmit(b"\x80\x05\x00\x00\x00") == b"ifs\x90\x00")
    check("IFSC updated to 16", conn._ifsc == 16)
    check("still in sync after all of that", conn.transmit(b"\x80\x01\x00\x00\x02hi") == b"hi\x90\x00")
    check("unknown instruction returns the card's SW", conn.transmit(b"\x80\x7f\x00\x00\x00") == b"\x6d\x00")

    conn.disconnect()
    try:
        conn.transmit(b"\x80\x01\x00\x00\x00")
        check("transmit after disconnect raises", False)
    except uscard.NoCardException:
        check("transmit after disconnect raises", True)

    # Partida a frio: o leitor responde, mas so ve o cartao algumas consultas depois.
    FakeSEC1210.detect_after = 8
    conn = fresh()
    check("cold start: card found once the reader detects it", conn.isCardInserted())
    check("cold start: it took the reader's 8 empty answers", FakeSEC1210.detect_after == 0)

    FakeSEC1210.present = False
    conn = fresh()
    started = time.monotonic()
    check("no card: isCardInserted is False", not conn.isCardInserted())
    check("no card: waited for the settle window once", 0.3 < time.monotonic() - started < 1.0)
    started = time.monotonic()
    check("no card: later polls answer at once", not conn.isCardInserted() and time.monotonic() - started < 0.1)
    try:
        conn.connect(conn.T1_protocol)
        check("no card: connect raises NoCardException", False)
    except uscard.NoCardException:
        check("no card: connect raises NoCardException", True)
    FakeSEC1210.present = True

    FakeSEC1210.voltage = 2
    conn = fresh()
    conn.connect(conn.T1_protocol)
    check("3 V card: falls back from 5 V to 3 V", uscard._sec1210._uart.power_selects == [1, 2])
    check("3 V card: works after the fallback", conn.transmit(b"\x80\x01\x00\x00\x02ok") == b"ok\x90\x00")
    conn.disconnect()
    FakeSEC1210.voltage = 1

    FakeSEC1210.atr = ATR_T0
    conn = fresh()
    conn.connect()
    check("T=0 card recognised from the ATR", conn._protocol == 0)
    check("T=0: 61xx followed by GET RESPONSE", conn.transmit(b"\x00\x01\x00\x00") == b"t0-data!\x90\x00")
    check("T=0: 6Cxx retried with the right Le", conn.transmit(b"\x00\x02\x00\x00\x00") == b"abc\x90\x00")
    FakeSEC1210.atr = ATR_T1

    FakeSEC1210.silent = True
    conn = fresh()
    started = time.monotonic()
    check("no hat: isCardInserted is False", not conn.isCardInserted())
    first = time.monotonic() - started
    started = time.monotonic()
    for _ in range(20):
        conn.isCardInserted()
    check("no hat: repeated polls do not each wait a timeout", time.monotonic() - started < first + 0.15)
    FakeSEC1210.silent = False

    print("all good")


if __name__ == "__main__":
    main()
