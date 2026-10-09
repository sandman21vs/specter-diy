"""Leitor NFC WS1850S (M5Stack RFID Unit 2) para a Waveshare ESP32-P4 4.3-C.

O WS1850S e compativel em registradores com o NXP MFRC522, mas nao e a mesma
peca: ele nao devolve os valores de VersionReg do MFRC522, entao a presenca e
testada escrevendo num registrador e lendo de volta.

Ligacao. O modulo fala I2C no endereco 0x28 e entra no barramento I2C da placa,
GPIO7 (SDA) e GPIO8 (SCL), o mesmo do touch GT911 e da camera. Esse barramento
ja tem dono -- o modulo C p4board -- e abrir um machine.I2C nos mesmos pinos
falha. Por isso as transferencias passam por p4board.i2c_writeto() e
p4board.i2c_readfrom(), que emprestam o barramento. Alimentacao em 3,3 V: o
WS1850S e uma peca de 3,3 V, igual a logica do P4.

So este arquivo conhece o chip. Selecao de cartao, enderecamento MIFARE e
registros ficam em src/nfc, que conversa pela interface nfc.Reader.

Tudo aqui tem limite. Tamanho de quadro, lacos de espera e leituras da FIFO tem
teto fixo, porque do outro lado da antena esta um dispositivo que outra pessoa
construiu.

Traducao de odudex/Kern components/nfc/src/pcd_ws1850s.c (branch
nfc-card-storage) e de krux/nfc_ws1850s.py, ambos MIT.
"""

import time

from nfc import FIFO_SIZE, NFCError, NFCNotFound, NFCSizeError, NFCTimeout, Reader

WS1850S_ADDR = 0x28

# Registradores (compativeis com o MFRC522)
_REG_COMMAND = 0x01
_REG_COM_IRQ = 0x04
_REG_DIV_IRQ = 0x05
_REG_ERROR = 0x06
_REG_STATUS2 = 0x08
_REG_FIFO_DATA = 0x09
_REG_FIFO_LEVEL = 0x0A
_REG_CONTROL = 0x0C
_REG_BIT_FRAMING = 0x0D
_REG_MODE = 0x11
_REG_TX_CONTROL = 0x14
_REG_TX_ASK = 0x15
_REG_CRC_RESULT_H = 0x21
_REG_CRC_RESULT_L = 0x22
_REG_T_MODE = 0x2A
_REG_T_PRESCALER = 0x2B
_REG_T_RELOAD_H = 0x2C
_REG_T_RELOAD_L = 0x2D

# Comandos do leitor
_PCD_IDLE = 0x00
_PCD_CALC_CRC = 0x03
_PCD_TRANSCEIVE = 0x0C
_PCD_AUTHENT = 0x0E
_PCD_RESET = 0x0F

_PICC_AUTH_KEY_A = 0x60
# Chave A de fabrica. A protecao e a senha do KEF, nao a chave do setor: o
# cartao continua legivel por qualquer leitor, e o que se le e texto cifrado.
_MF_DEFAULT_KEY = b"\xff\xff\xff\xff\xff\xff"

# Qualquer bit fatal de ErrorReg quer dizer que o quadro e lixo. CRCErr fica de
# fora porque um quadro sem CRC o deixa ligado legitimamente.
_ERR_FATAL_MASK = 0x1B  # BufferOvfl | Coll | Parity | Protocol

_IRQ_TIMER = 0x01
_IRQ_IDLE = 0x10
_IRQ_RX = 0x20

# Uma troca tem tres limites: o timer do proprio chip (25 ms), um prazo de
# relogio caso o chip pare de responder, e um teto de consultas para que nem um
# relogio parado consiga girar para sempre.
_EXCHANGE_TIMEOUT_MS = 60
_CRC_TIMEOUT_MS = 20
_MAX_POLLS = 4000

# Com um prazo pedido por set_timeout() o timer do chip sai do caminho: fica no
# mais longo que ele tem (prescaler 0xFFF, 604 us por tick, ~40 s) e quem conta
# o tempo e o relogio do P4. Foi assim que o JavaCard foi validado na placa.
_TIMER_STOCK = ((_REG_T_MODE, 0x80), (_REG_T_PRESCALER, 0xA9),
                (_REG_T_RELOAD_H, 0x03), (_REG_T_RELOAD_L, 0xE8))
_TIMER_LONG = ((_REG_T_MODE, 0x8F), (_REG_T_PRESCALER, 0xFF),
               (_REG_T_RELOAD_H, 0xFF), (_REG_T_RELOAD_L, 0xFF))
# Teto de qualquer prazo pedido: o maior FWT da ISO 14443-4, com folga.
_MAX_TIMEOUT_MS = 5100
# Folga sobre o prazo pedido: o leitor e consultado por I2C, nao por interrupcao.
_TIMEOUT_MARGIN_MS = 20


class _BoardBus:
    """O barramento I2C da placa, emprestado pelo p4board.

    Tem a forma de um machine.I2C no que o driver usa, para que um barramento
    proprio (machine.I2C ou SoftI2C em outros pinos) sirva no lugar.
    """

    def __init__(self):
        import p4board

        self._board = p4board

    def writeto(self, addr, data):
        self._board.i2c_writeto(addr, data)

    def readfrom(self, addr, length):
        return self._board.i2c_readfrom(addr, length)


def is_present(addr=WS1850S_ADDR):
    """True se ha um dispositivo respondendo no endereco do leitor.

    So um ACK no barramento: barato o bastante para decidir, a cada vez que um
    menu e montado, se as opcoes de NFC aparecem.
    """
    try:
        import p4board

        return p4board.i2c_probe(addr)
    except Exception:
        return False


class WS1850S(Reader):
    """Leitor compativel com MFRC522 em I2C"""

    def __init__(self, i2c=None, addr=WS1850S_ADDR):
        self.i2c = i2c
        self.addr = addr
        self.ready = False
        self.crypto_on = False
        self.timeout_ms = None  # None: os 25 ms do timer do chip

    # ---------- Registradores ----------

    def _raw(self, payload):
        """Escreve o byte do registrador seguido dos dados.

        Os dois precisam ir na mesma transacao: o chip nao auto-incrementa,
        entao todo byte depois do endereco cai no mesmo registrador.
        """
        try:
            self.i2c.writeto(self.addr, payload)
        except Exception:
            raise NFCError("I2C write failed")

    def _write(self, reg, val):
        self._raw(bytes([reg, val]))

    def _read(self, reg, length=1):
        """Le de um registrador. Ler FIFODataReg repetidamente esvazia a FIFO."""
        try:
            self.i2c.writeto(self.addr, bytes([reg]))
            data = self.i2c.readfrom(self.addr, length)
        except Exception:
            raise NFCError("I2C read failed")
        if data is None or len(data) != length:
            raise NFCError("I2C short read")
        return data

    def _byte(self, reg):
        return self._read(reg)[0]

    def _mask(self, reg, mask, on):
        """Liga ou desliga os bits mascarados de um registrador"""
        val = self._byte(reg)
        self._write(reg, val | mask if on else val & (~mask & 0xFF))

    # ---------- Ciclo de vida ----------

    def init(self):
        """Sobe o leitor com o campo desligado. Idempotente."""
        if self.ready:
            return
        if self.i2c is None:
            try:
                self.i2c = _BoardBus()
            except ImportError:
                raise NFCNotFound("No I2C bus")

        # VersionReg nao serve aqui, entao escreve dois padroes num registrador
        # inofensivo e le de volta. Sem modulo a transferencia falha.
        for pattern in (0x55, 0xAA):
            try:
                self._write(_REG_T_RELOAD_L, pattern)
                if self._byte(_REG_T_RELOAD_L) != pattern:
                    raise NFCError("No echo")
            except NFCError:
                raise NFCNotFound("No NFC reader")

        self._write(_REG_COMMAND, _PCD_RESET)
        for _ in range(10):
            time.sleep_ms(5)
            try:
                if not self._byte(_REG_COMMAND) & 0x10:
                    break
            except NFCError:
                pass
        else:
            raise NFCError("Reader reset timed out")

        # Timer: TAuto, prescaler 0xA9 -> 40 kHz, reload 1000 -> 25 ms por
        # troca. E o que impede um cartao mudo de travar uma leitura.
        for reg, val in _TIMER_STOCK + (
            (_REG_TX_ASK, 0x40),  # forca 100% ASK
            (_REG_MODE, 0x3D),  # preset do CRC 0x6363
        ):
            self._write(reg, val)
        self.timeout_ms = None

        self.ready = True
        self.crypto_on = False
        # Sobe com a antena apagada; quem chama a acende de proposito.
        try:
            self.field(False)
        except NFCError:
            self.ready = False
            raise

    def deinit(self):
        """Desliga o campo e esquece o leitor. Nunca levanta excecao."""
        if self.ready:
            try:
                self._mask(_REG_TX_CONTROL, 0x03, False)
            except NFCError:
                pass
        self.ready = False
        self.crypto_on = False

    def field(self, on):
        """Liga ou desliga a antena"""
        if not self.ready:
            raise NFCError("Reader not ready")
        self._mask(_REG_TX_CONTROL, 0x03, on)

    def set_timeout(self, timeout_ms=None):
        """Prazo de resposta das proximas trocas. None volta aos 25 ms."""
        if not self.ready:
            raise NFCError("Reader not ready")
        if timeout_ms is not None:
            timeout_ms = max(1, min(int(timeout_ms), _MAX_TIMEOUT_MS))
        # So mexe nos registradores quando muda de modo, nao a cada quadro
        if (timeout_ms is None) != (self.timeout_ms is None):
            for reg, val in _TIMER_STOCK if timeout_ms is None else _TIMER_LONG:
                self._write(reg, val)
        self.timeout_ms = timeout_ms

    def clear_crypto(self):
        """Encerra uma sessao crypto1 se houver. Nunca levanta excecao."""
        if self.crypto_on:
            try:
                self._mask(_REG_STATUS2, 0x08, False)
            except NFCError:
                pass
            self.crypto_on = False

    # ---------- Troca de quadros ----------

    def _wait_irq(self, mask, timeout_ms):
        """Espera um bit de IRQ, o timer do leitor ou o prazo"""
        polls = _MAX_POLLS
        custom = self.timeout_ms is not None
        if custom:
            timeout_ms = self.timeout_ms + _TIMEOUT_MARGIN_MS
            # o teto de consultas acompanha o prazo, mas continua existindo
            polls += timeout_ms * 20
        start = time.ticks_ms()
        for _ in range(polls):
            irq = self._byte(_REG_COM_IRQ)
            if irq & mask:
                return
            if irq & _IRQ_TIMER and not custom:
                break
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                break
        raise NFCTimeout("No answer from tag")

    def transceive(self, send, tx_last_bits=0, recv_size=0):
        """Troca um quadro, devolve (resposta, rx_last_bits).

        recv_size e a maior resposta aceita; uma maior e recusada em vez de
        truncada, porque truncar deixaria o cartao nos dessincronizar. 0 quer
        dizer que nao se espera resposta.
        """
        if not self.ready or not send or len(send) > FIFO_SIZE or tx_last_bits > 7:
            raise NFCSizeError("Bad frame")

        self._write(_REG_COMMAND, _PCD_IDLE)
        self._write(_REG_COM_IRQ, 0x7F)  # limpa as IRQs
        self._write(_REG_FIFO_LEVEL, 0x80)  # esvazia a FIFO
        self._raw(bytes([_REG_FIFO_DATA]) + bytes(send))
        self._write(_REG_BIT_FRAMING, tx_last_bits)
        self._write(_REG_COMMAND, _PCD_TRANSCEIVE)
        self._mask(_REG_BIT_FRAMING, 0x80, True)  # StartSend
        try:
            self._wait_irq(_IRQ_RX | _IRQ_IDLE, _EXCHANGE_TIMEOUT_MS)
        finally:
            self._mask(_REG_BIT_FRAMING, 0x80, False)

        # Colisao so acontece com mais de um cartao no campo; pedimos um cartao
        # so em vez de resolver.
        if self._byte(_REG_ERROR) & _ERR_FATAL_MASK:
            raise NFCError("Reader error")

        if not recv_size:
            self._write(_REG_COMMAND, _PCD_IDLE)
            return b"", 0

        # Quem escolhe este numero e o cartao. Copia-lo para um buffer menor e
        # o estouro classico do MFRC522, entao recusa em vez de truncar.
        level = self._byte(_REG_FIFO_LEVEL)
        if level > FIFO_SIZE:
            raise NFCError("Oversized reply")
        if level > recv_size:
            raise NFCSizeError("Reply does not fit")

        data = self._read(_REG_FIFO_DATA, level) if level else b""
        # RxLastBits tem tres bits; mascara antes de virar aritmetica
        return bytes(data), self._byte(_REG_CONTROL) & 0x07

    def calc_crc(self, data):
        """Calcula um CRC_A com o coprocessador do proprio leitor"""
        if not self.ready or not data or len(data) > FIFO_SIZE:
            raise NFCSizeError("Bad CRC input")

        self._write(_REG_COMMAND, _PCD_IDLE)
        self._write(_REG_DIV_IRQ, 0x04)  # limpa CRCIRq
        self._write(_REG_FIFO_LEVEL, 0x80)
        self._raw(bytes([_REG_FIFO_DATA]) + bytes(data))
        self._write(_REG_COMMAND, _PCD_CALC_CRC)

        start = time.ticks_ms()
        for _ in range(_MAX_POLLS):
            if self._byte(_REG_DIV_IRQ) & 0x04:
                self._write(_REG_COMMAND, _PCD_IDLE)
                return bytes(
                    [self._byte(_REG_CRC_RESULT_L), self._byte(_REG_CRC_RESULT_H)]
                )
            if time.ticks_diff(time.ticks_ms(), start) > _CRC_TIMEOUT_MS:
                break
        self._write(_REG_COMMAND, _PCD_IDLE)
        raise NFCError("CRC timed out")

    # ---------- MIFARE Classic ----------

    def authenticate(self, uid, block):
        """Abre uma sessao crypto1 num setor com a chave A de fabrica"""
        if not self.ready or len(uid) < 4:
            raise NFCError("Reader not ready")
        self._write(_REG_COMMAND, _PCD_IDLE)
        self._write(_REG_COM_IRQ, 0x7F)
        self._write(_REG_FIFO_LEVEL, 0x80)
        # O Classic autentica com os quatro ultimos bytes do UID: o UID inteiro
        # nos cartoes de UID simples, o final nos de UID duplo.
        self._raw(
            bytes([_REG_FIFO_DATA, _PICC_AUTH_KEY_A, block])
            + _MF_DEFAULT_KEY
            + bytes(uid[-4:])
        )
        self._write(_REG_COMMAND, _PCD_AUTHENT)
        try:
            self._wait_irq(_IRQ_IDLE, _EXCHANGE_TIMEOUT_MS)
        except NFCError:
            self.crypto_on = False
            self._write(_REG_COMMAND, _PCD_IDLE)
            raise

        # Crypto1On em Status2Reg e o unico sinal confiavel de sucesso
        if not self._byte(_REG_STATUS2) & 0x08:
            self.crypto_on = False
            raise NFCError("Authentication failed")
        self.crypto_on = True
