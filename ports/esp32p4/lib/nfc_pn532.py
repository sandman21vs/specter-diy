"""Leitor NFC PN532 por I2C, para a Waveshare ESP32-P4 4.3-C.

O modulo vermelho "PN532 NFC RFID V3" (desenho da Elechouse e seus clones). Vai
no mesmo barramento do RFID Unit 2 -- GPIO7 (SDA) e GPIO8 (SCL), emprestado
pelo p4board -- e responde no endereco 0x24. A chave do modulo precisa estar em
I2C, e a alimentacao em 3,3 V: os pull-ups dele puxam SDA e SCL para o proprio
VCC, e esse barramento e o do touch e da camera.

O PN532 nao e um MFRC522 com outro barramento. Entre o host e o radio ha um
firmware, e fala-se com ele por comandos em quadros:

    host    00 00 FF LEN LCS D4 cmd ... DCS 00
    chip    00 00 FF 00 FF 00                       (ACK)
    chip    00 00 FF LEN LCS D5 cmd+1 ... DCS 00

Em I2C toda leitura comeca por um byte de estado, 01 quando ha um quadro
pronto, e o quadro tem de ser lido de uma vez.

Esse firmware sabe selecionar cartoes e falar ISO-DEP sozinho. Aqui ele nao e
usado para isso: o leitor entrega quadros crus, como os outros dois, e a
selecao, o MIFARE e o ISO-DEP continuam sendo os de src/nfc, com os limites que
ja estao la. Do firmware usam-se tres coisas:

    InCommunicateThru    troca um quadro cru com o cartao
    Read/WriteRegister   os registradores do radio (o CIU, um PN512: o mesmo
                         mapa do MFRC522) para bits finais, CRC e crypto1
    RFConfiguration      campo e prazo de resposta

O CRC_A e calculado aqui, em vez de pelo chip, para o quadro que sai ser
exatamente o que a camada de cima montou.

Traducao do que a libnfc faz em pn53x.c para quadros crus (LGPL; aqui so a
sequencia de comandos, reescrita).
"""

import time

from nfc import FIFO_SIZE, NFCError, NFCNotFound, NFCSizeError, NFCTimeout, Reader

PN532_ADDR = 0x24

# Comandos do firmware
_CMD_FIRMWARE_VERSION = 0x02
_CMD_READ_REGISTER = 0x06
_CMD_WRITE_REGISTER = 0x08
_CMD_SET_PARAMETERS = 0x12
_CMD_SAM_CONFIGURATION = 0x14
_CMD_RF_CONFIGURATION = 0x32
_CMD_COMMUNICATE_THRU = 0x42

_HOST_TO_CHIP = 0xD4
_CHIP_TO_HOST = 0xD5
_ACK = b"\x00\x00\xff\x00\xff\x00"
_IC_PN532 = 0x32

# Registradores do CIU. E o mapa do MFRC522 em 0x63xx: a pagina 0 fica em
# 0x6330 + reg, as paginas 1 e 2 em 0x6300 + (reg - 0x10).
_CIU_TX_MODE = 0x6302
_CIU_RX_MODE = 0x6303
_CIU_TX_AUTO = 0x6305
_CIU_MANUAL_RCV = 0x630D
_CIU_COMMAND = 0x6331
_CIU_STATUS2 = 0x6338
_CIU_FIFO_DATA = 0x6339
_CIU_FIFO_LEVEL = 0x633A
_CIU_CONTROL = 0x633C
_CIU_BIT_FRAMING = 0x633D

_PCD_IDLE = 0x00
_PCD_AUTHENT = 0x0E
_PICC_AUTH_KEY_A = 0x60
# Chave A de fabrica, como em nfc_ws1850s.py: a protecao e a senha do KEF.
_MF_DEFAULT_KEY = b"\xff\xff\xff\xff\xff\xff"

# Erros de InCommunicateThru, nos seis bits baixos do byte de estado
_ERR_TIMEOUT = 0x01

# Prazo de resposta do cartao: o firmware aceita 100 us * 2^(n-1), n de 1 a
# 16. O padrao aqui e n = 10, 51,2 ms: cobre a escrita de um bloco MIFARE e
# nao deixa um cartao mudo prender uma leitura.
_DEFAULT_TIMEOUT_CODE = 0x0A
_MAX_TIMEOUT_CODE = 0x10  # 3,28 s

# Quanto o host espera pelo firmware alem do prazo que deu ao cartao
_COMMAND_MARGIN_MS = 60
# Um comando sem radio (registradores, configuracao) responde em poucos ms
_LOCAL_TIMEOUT_MS = 60
# Teto de consultas ao byte de estado, para que nem um relogio parado gire
# para sempre
_MAX_POLLS = 6000

# Maior resposta lida: estado, quadro, e os dados de um InCommunicateThru com
# a FIFO cheia
_MAX_DATA = FIFO_SIZE + 4


def crc_a(data):
    """CRC_A da ISO 14443-3, byte baixo primeiro"""
    crc = 0x6363
    for byte in data:
        byte ^= crc & 0xFF
        byte = (byte ^ (byte << 4)) & 0xFF
        crc = (crc >> 8) ^ (byte << 8) ^ (byte << 3) ^ (byte >> 4)
    crc &= 0xFFFF
    return bytes([crc & 0xFF, crc >> 8])


def _timeout_code(timeout_ms):
    """O menor codigo de prazo do firmware que cobre timeout_ms"""
    code = 1
    while code < _MAX_TIMEOUT_CODE and (1 << (code - 1)) < timeout_ms * 10:
        code += 1
    return code


def _timeout_ms(code):
    return ((1 << (code - 1)) + 9) // 10


class _BoardBus:
    """O barramento I2C da placa, emprestado pelo p4board"""

    def __init__(self):
        import p4board

        self._board = p4board

    def writeto(self, addr, data):
        self._board.i2c_writeto(addr, data)

    def readfrom(self, addr, length):
        return self._board.i2c_readfrom(addr, length)


def is_present(addr=PN532_ADDR):
    """True se ha um dispositivo respondendo no endereco do leitor"""
    try:
        import p4board

        return p4board.i2c_probe(addr)
    except Exception:
        return False


class PN532(Reader):
    """PN532 em I2C, entregando quadros crus"""

    def __init__(self, i2c=None, addr=PN532_ADDR):
        self.i2c = i2c
        self.addr = addr
        self.ready = False
        self.crypto_on = False
        self.timeout_ms = None  # None: o prazo padrao
        self._code = None  # codigo de prazo que esta no chip
        self._tx_bits = None  # CIU_BitFraming que esta no chip

    # ---------- Quadros do firmware ----------

    def _wait_ready(self, timeout_ms):
        """Espera o byte de estado dizer que ha um quadro para ler"""
        start = time.ticks_ms()
        for _ in range(_MAX_POLLS):
            try:
                status = self.i2c.readfrom(self.addr, 1)
                if status and status[0] & 0x01:
                    return
            except Exception:
                # ocupado, o chip pode nem dar ACK ao endereco
                pass
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                break
            time.sleep_ms(1)
        raise NFCError("Reader does not answer")

    def _read_frame(self, max_data):
        """Le um quadro inteiro, devolve o que vem entre LCS e DCS"""
        try:
            raw = self.i2c.readfrom(self.addr, 1 + 6 + max_data + 2)
        except Exception:
            raise NFCError("I2C read failed")
        if raw is None or len(raw) < 7 or not raw[0] & 0x01:
            raise NFCError("I2C short read")
        raw = bytes(raw[1:])
        if raw[:3] != b"\x00\x00\xff":
            raise NFCError("Bad frame")
        length = raw[3]
        if (length + raw[4]) & 0xFF:
            raise NFCError("Bad frame length")
        # Um numero que vem do chip: limita antes de usar como tamanho
        if length > max_data or len(raw) < 5 + length + 1:
            raise NFCSizeError("Oversized frame")
        body = raw[5 : 5 + length]
        if (sum(body) + raw[5 + length]) & 0xFF:
            raise NFCError("Bad frame checksum")
        return body

    def _command(self, command, params=b"", max_reply=8, timeout_ms=_LOCAL_TIMEOUT_MS):
        """Manda um comando ao firmware, devolve os dados da resposta"""
        if self.i2c is None:
            raise NFCError("No I2C bus")
        body = bytes([_HOST_TO_CHIP, command]) + bytes(params)
        if len(body) > 254:
            raise NFCSizeError("Command too long")
        frame = (
            b"\x00\x00\xff"
            + bytes([len(body), (-len(body)) & 0xFF])
            + body
            + bytes([(-sum(body)) & 0xFF, 0x00])
        )
        try:
            self.i2c.writeto(self.addr, frame)
        except Exception:
            raise NFCError("I2C write failed")

        # Primeiro o ACK, que so diz que o quadro chegou inteiro
        self._wait_ready(_LOCAL_TIMEOUT_MS)
        try:
            ack = self.i2c.readfrom(self.addr, 1 + len(_ACK))
        except Exception:
            raise NFCError("I2C read failed")
        if ack is None or bytes(ack[1:]) != _ACK:
            raise NFCError("Command not acknowledged")

        self._wait_ready(timeout_ms)
        body = self._read_frame(max_reply + 2)
        # Um quadro de erro do firmware tem um byte so, 0x7F
        if len(body) < 2 or body[0] != _CHIP_TO_HOST or body[1] != command + 1:
            raise NFCError("Unexpected answer")
        return body[2:]

    # ---------- Registradores do radio ----------

    def _write_regs(self, pairs):
        """Escreve registradores do CIU, em ordem, num comando so"""
        params = bytearray()
        for reg, value in pairs:
            params += bytes([reg >> 8, reg & 0xFF, value])
        self._command(_CMD_WRITE_REGISTER, params)

    def _read_reg(self, reg):
        reply = self._command(_CMD_READ_REGISTER, bytes([reg >> 8, reg & 0xFF]))
        if len(reply) != 1:
            raise NFCError("Bad register read")
        return reply[0]

    def _set_timeout_code(self, code):
        if code != self._code:
            # item 2: RFU, prazo do ATR_RES (nao usado), prazo de resposta
            self._command(_CMD_RF_CONFIGURATION, bytes([0x02, 0x00, 0x0B, code]))
            self._code = code

    def _code_now(self):
        if self.timeout_ms is None:
            return _DEFAULT_TIMEOUT_CODE
        return _timeout_code(self.timeout_ms)

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
        try:
            # O primeiro comando tambem acorda o chip, e pode se perder
            try:
                version = self._command(_CMD_FIRMWARE_VERSION, max_reply=4)
            except NFCError:
                time.sleep_ms(5)
                version = self._command(_CMD_FIRMWARE_VERSION, max_reply=4)
            if len(version) != 4 or version[0] != _IC_PN532:
                raise NFCError("Not a PN532")

            # Modo normal, sem SAM e sem o pino de IRQ
            self._command(_CMD_SAM_CONFIGURATION, b"\x01\x00\x00")
            # Nada automatico: nem RATS, nem ATR_RES. Quem ativa o cartao e a
            # camada de cima.
            self._command(_CMD_SET_PARAMETERS, b"\x00")
        except NFCError:
            raise NFCNotFound("No NFC reader")

        self._code = None
        self._tx_bits = None
        self.timeout_ms = None
        self.crypto_on = False
        self.ready = True
        try:
            self.field(False)
            # Leitor ISO 14443A a 106 kbit/s, quadros crus: o chip nao poe nem
            # confere CRC, e a paridade continua com ele.
            self._write_regs(
                (
                    (_CIU_CONTROL, 0x10),  # iniciador
                    (_CIU_STATUS2, 0x00),  # crypto1 de uma sessao que ficou
                    (_CIU_TX_MODE, 0x00),
                    (_CIU_RX_MODE, 0x00),
                    (_CIU_TX_AUTO, 0x40),  # forca 100% ASK
                    (_CIU_MANUAL_RCV, 0x00),
                    (_CIU_BIT_FRAMING, 0x00),
                )
            )
            self._tx_bits = 0
            self._set_timeout_code(_DEFAULT_TIMEOUT_CODE)
        except NFCError:
            self.ready = False
            raise

    def deinit(self):
        """Desliga o campo e esquece o leitor. Nunca levanta excecao."""
        if self.ready:
            try:
                self._command(_CMD_RF_CONFIGURATION, b"\x01\x00")
            except NFCError:
                pass
        self.ready = False
        self.crypto_on = False

    def field(self, on):
        """Liga ou desliga a antena"""
        if not self.ready:
            raise NFCError("Reader not ready")
        self._command(_CMD_RF_CONFIGURATION, bytes([0x01, 0x01 if on else 0x00]))
        if not on:
            self.crypto_on = False

    def set_timeout(self, timeout_ms=None):
        """Prazo de resposta das proximas trocas. None volta ao padrao."""
        if not self.ready:
            raise NFCError("Reader not ready")
        if timeout_ms is not None:
            timeout_ms = max(1, int(timeout_ms))
        self.timeout_ms = timeout_ms

    def clear_crypto(self):
        """Encerra uma sessao crypto1 se houver. Nunca levanta excecao."""
        if self.crypto_on:
            try:
                status = self._read_reg(_CIU_STATUS2)
                self._write_regs(((_CIU_STATUS2, status & ~0x08 & 0xFF),))
            except NFCError:
                pass
            self.crypto_on = False

    # ---------- Troca de quadros ----------

    def transceive(self, send, tx_last_bits=0, recv_size=0):
        """Troca um quadro, devolve (resposta, rx_last_bits).

        recv_size e a maior resposta aceita; uma maior e recusada em vez de
        truncada. 0 quer dizer que nao se espera resposta.
        """
        if not self.ready or not send or len(send) > FIFO_SIZE or tx_last_bits > 7:
            raise NFCSizeError("Bad frame")

        if tx_last_bits != self._tx_bits:
            self._tx_bits = None
            self._write_regs(((_CIU_BIT_FRAMING, tx_last_bits),))
            self._tx_bits = tx_last_bits
        code = self._code_now()
        self._set_timeout_code(code)

        reply = self._command(
            _CMD_COMMUNICATE_THRU,
            send,
            max_reply=_MAX_DATA,
            timeout_ms=_timeout_ms(code) + _COMMAND_MARGIN_MS,
        )
        if not reply:
            raise NFCError("Bad answer")
        error = reply[0] & 0x3F
        if error == _ERR_TIMEOUT:
            if not recv_size:
                return b"", 0
            raise NFCTimeout("No answer from tag")
        if error:
            raise NFCError("Reader error")
        if not recv_size:
            return b"", 0

        data = bytes(reply[1:])
        # Quem escolhe este tamanho e o cartao
        if len(data) > FIFO_SIZE:
            raise NFCError("Oversized reply")
        if len(data) > recv_size:
            raise NFCSizeError("Reply does not fit")
        # Os bits validos do ultimo byte so interessam nos quadros curtos sem
        # CRC: o ATQA e o ACK de quatro bits. Nos outros poupa uma ida ao chip.
        bits = 0
        if len(data) < 3:
            bits = self._read_reg(_CIU_CONTROL) & 0x07
        return data, bits

    def calc_crc(self, data):
        """Calcula um CRC_A"""
        if not self.ready or not data or len(data) > FIFO_SIZE:
            raise NFCSizeError("Bad CRC input")
        return crc_a(data)

    # ---------- MIFARE Classic ----------

    def authenticate(self, uid, block):
        """Abre uma sessao crypto1 num setor com a chave A de fabrica.

        Pelo comando MFAuthent do proprio CIU, escrito nos registradores como
        se faz no MFRC522: assim o firmware nao precisa conhecer o cartao, que
        foi selecionado por quadros crus.
        """
        if not self.ready or len(uid) < 4:
            raise NFCError("Reader not ready")
        frame = bytes([_PICC_AUTH_KEY_A, block]) + _MF_DEFAULT_KEY + bytes(uid[-4:])
        self._write_regs(
            ((_CIU_COMMAND, _PCD_IDLE), (_CIU_FIFO_LEVEL, 0x80))
            + tuple((_CIU_FIFO_DATA, byte) for byte in frame)
            + ((_CIU_COMMAND, _PCD_AUTHENT),)
        )
        # Crypto1On em Status2 e o unico sinal confiavel de sucesso
        on = False
        try:
            start = time.ticks_ms()
            for _ in range(_MAX_POLLS):
                if self._read_reg(_CIU_STATUS2) & 0x08:
                    on = True
                    break
                if time.ticks_diff(time.ticks_ms(), start) > _LOCAL_TIMEOUT_MS:
                    break
        finally:
            self.crypto_on = on
            self._write_regs(((_CIU_COMMAND, _PCD_IDLE),))
        if not on:
            raise NFCError("Authentication failed")

    def version(self):
        """(IC, versao, revisao, suporte) do firmware"""
        return tuple(self._command(_CMD_FIRMWARE_VERSION, max_reply=4))
