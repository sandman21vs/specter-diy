"""Leitor NFC RFID-RC522 (NXP MFRC522) por SPI, para a Waveshare ESP32-P4 4.3-C.

O WS1850S do RFID Unit 2 e compativel em registradores com o MFRC522, entao a
logica do chip -- init, transceive, calc_crc, authenticate, prazos -- e a de
nfc_ws1850s.py, herdada. Aqui fica so o que muda: o barramento, a presenca, o
reset por hardware e o desligamento.

Ligacao, pela placa adaptadora TapHad. Ela e passiva: cada pino do modulo vai
direto a um pino do conector de 40 pinos, tudo em 3,3 V.

    SCK   GPIO31        RST   GPIO28
    MOSI  GPIO29        IRQ   GPIO49 (roteado, nao usado)
    MISO  GPIO30        3.3V  3V3
    SDA   GPIO50 (SS)   GND   GND

Nenhum desses pinos tem outro uso no port, e o SPI2 nao tem dono, entao aqui
serve um machine.SPI comum -- ao contrario do I2C, que e do p4board. O chip
select e um GPIO qualquer, dirigido por software. O IRQ fica solto: o leitor e
consultado, como o WS1850S.

SPI. Modo 0, MSB primeiro. O primeiro byte de cada transacao e o endereco,
(reg << 1) & 0x7E, com o bit 7 em 1 para ler. Numa escrita todo byte seguinte
cai no mesmo registrador; numa leitura cada byte enviado e o endereco do
proximo byte recebido, e um zero fecha a sequencia.

Reset e desligamento. O pino RST do modulo e o NRSTPD, ativo em nivel baixo.
init() da um pulso nele e espera o oscilador; deinit() o deixa em nivel baixo
(hard power-down), em vez do soft power-down por CommandReg. Motivos: com
NRSTPD baixo o oscilador e os drivers da antena estao desligados pelo proprio
chip, sem depender de registrador nenhum, e todo init() parte do estado de
reset, que e o unico em que os clones se comportam igual. O custo e o tempo de
partida do cristal a cada init().

Presenca. O MFRC522 original devolve 0x91 ou 0x92 em VersionReg, mas os clones
devolvem outros valores, entao a presenca e o mesmo eco de registrador do
WS1850S e a versao so aparece no diagnostico (test_nfc_rc522.py). O SPI nao tem
ACK: sem modulo as leituras voltam o que o MISO solto tiver, e e o eco que
separa isso de um leitor.

Nada disto foi medido na placa ainda. O que depende de medicao esta marcado.
"""

import time

from nfc import NFCError, NFCNotFound
from nfc_ws1850s import _REG_T_RELOAD_L, WS1850S

SPI_ID = 1  # SPI2_HOST
PIN_SCK = 31
PIN_MOSI = 29
PIN_MISO = 30
PIN_SS = 50
PIN_RST = 28
PIN_IRQ = 49  # so documentado: fica como entrada, sem uso

# Conservador de proposito. O MFRC522 vai a 10 MHz, mas os fios da TapHad e do
# modulo nao foram medidos; subir so depois de medir que funciona.
SPI_BAUDRATE = 1000000

REG_VERSION = 0x37

# Pulso de reset: o chip pede 100 ns, o sleep_ms nao da menos que 1 ms.
_RESET_PULSE_MS = 2
# Partida depois de soltar o NRSTPD: 37,74 us do chip mais o cristal, que o
# datasheet nao limita. 50 ms e o que as bibliotecas do MFRC522 esperam as
# cegas; aqui e o teto de uma espera que termina no primeiro eco. A MEDIR: o
# diagnostico mostra quanto o modulo levou de verdade.
_WAKE_TIMEOUT_MS = 50

# Maior leitura numa transacao: a FIFO inteira
_MAX_READ = 64


class _BoardBus:
    """O SPI2 nos pinos da TapHad, mais o chip select e o reset.

    O driver so usa spi.write_readinto(), cs.value() e rst.value(), para que
    outro barramento com a mesma forma sirva no lugar.
    """

    def __init__(self):
        import machine

        Pin = machine.Pin
        # Antes do SPI: o chip nao pode ver clock com o select em nivel baixo
        self.cs = Pin(PIN_SS, Pin.OUT, value=1)
        self.rst = Pin(PIN_RST, Pin.OUT, value=0)
        self.spi = machine.SPI(
            SPI_ID,
            baudrate=SPI_BAUDRATE,
            polarity=0,
            phase=0,
            bits=8,
            firstbit=machine.SPI.MSB,
            sck=Pin(PIN_SCK),
            mosi=Pin(PIN_MOSI),
            miso=Pin(PIN_MISO),
        )


# Um barramento so, aberto na primeira vez e mantido: a presenca e consultada a
# cada menu, e abrir e fechar o SPI a cada vez nao compra nada.
_board_bus = None
# O leitor que esta de pe. Enquanto houver um, perguntar pela presenca nao pode
# resetar o chip debaixo dele.
_active = None


def _open_bus():
    global _board_bus
    if _board_bus is None:
        _board_bus = _BoardBus()
    return _board_bus


def is_present():
    """True se ha um leitor respondendo no SPI.

    Mais caro que o ACK do I2C: tira o chip do power-down, espera o eco e o
    desliga de novo. Com modulo sao poucos ms; sem modulo custa o teto de
    partida inteiro, entao nfc.is_available() so chega aqui quando o WS1850S
    nao respondeu.
    """
    try:
        if _active is not None and _active.ready and _active.bus is _board_bus:
            return _active._alive()
        reader = MFRC522()
        try:
            reader._attach()
            return True
        except NFCError:
            return False
        finally:
            reader._power_down()
    except Exception:
        return False


class MFRC522(WS1850S):
    """MFRC522 em SPI: a logica de registradores do WS1850S, outro barramento"""

    # Teto de consultas por ms de prazo pedido. Uma leitura de registrador sao
    # 16 bits a 1 MHz, 16 us so de fio, entao 100 por ms nunca e atingido antes
    # do relogio. O I2C usa 20, que aqui poderia cortar um prazo longo de
    # ISO-DEP pela metade. A MEDIR: o diagnostico mostra o tempo por consulta.
    _POLLS_PER_MS = 100

    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus

    # ---------- Registradores ----------

    def _transfer(self, tx):
        """Uma transacao: select em baixo, os bytes, select em alto"""
        bus = self.bus
        rx = bytearray(len(tx))
        try:
            bus.cs.value(0)
            try:
                bus.spi.write_readinto(tx, rx)
            finally:
                bus.cs.value(1)
        except Exception:
            raise NFCError("SPI transfer failed")
        return rx

    def _raw(self, payload):
        """Escreve o byte do registrador seguido dos dados"""
        self._transfer(bytes([(payload[0] << 1) & 0x7E]) + bytes(payload[1:]))

    def _read(self, reg, length=1):
        """Le de um registrador. Ler FIFODataReg repetidamente esvazia a FIFO."""
        if not 0 < length <= _MAX_READ:
            raise NFCError("Bad read length")
        addr = 0x80 | ((reg << 1) & 0x7E)
        # O byte i da resposta e o registrador enderecado pelo byte i-1
        return bytes(self._transfer(bytes([addr]) * length + b"\x00")[1:])

    # ---------- Ciclo de vida ----------

    def _attach(self):
        """Abre o barramento, reseta o chip e espera ele responder"""
        global _active
        if self.bus is None:
            try:
                self.bus = _open_bus()
            except Exception:
                # sem machine, ou o SPI2 nao abriu nesses pinos
                raise NFCNotFound("No SPI bus")
        try:
            self.bus.cs.value(1)
            self.bus.rst.value(0)
            time.sleep_ms(_RESET_PULSE_MS)
            self.bus.rst.value(1)
        except Exception:
            raise NFCNotFound("No SPI bus")

        # O oscilador leva um tempo que depende do cristal. Pergunta a cada ms
        # ate o eco voltar, com teto.
        for _ in range(_WAKE_TIMEOUT_MS):
            time.sleep_ms(1)
            try:
                self._probe()
            except NFCNotFound:
                continue
            _active = self
            return
        raise NFCNotFound("No NFC reader")

    def _power_down(self):
        """NRSTPD em nivel baixo: oscilador e antena desligados. Nunca levanta."""
        global _active
        if _active is self:
            _active = None
        try:
            self.bus.rst.value(0)
        except Exception:
            pass

    def init(self):
        """Sobe o leitor com o campo desligado. Idempotente."""
        try:
            super().init()
        except NFCError:
            # nao deixa o chip acordado atras de um init que falhou
            self._power_down()
            raise

    def deinit(self):
        """Desliga o campo e o chip. Nunca levanta excecao."""
        super().deinit()
        self._power_down()

    def _alive(self):
        """O eco de presenca num leitor em uso, sem reset e sem deixar rastro"""
        try:
            reload = self._byte(_REG_T_RELOAD_L)
            self._probe()
            self._write(_REG_T_RELOAD_L, reload)
            return True
        except NFCError:
            return False

    def version(self):
        """VersionReg: 0x91 e 0x92 no MFRC522 da NXP, outros valores em clones"""
        return self._byte(REG_VERSION)
