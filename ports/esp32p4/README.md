# Port do Specter DIY para ESP32-P4

Alvo: **Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C** (480x800 MIPI DSI ST7701,
touch GT911, câmera OV5647, 32 MB PSRAM, 16 MB de flash usada de 32 MB físicos).

Trabalho em andamento. Nada aqui é firmware utilizável ainda.

**Para compilar e gravar, siga o [README da raiz](../../README.md#build-and-flash).**

## Perfil de segurança

Enquanto durar o desenvolvimento: **Secure Boot desativado, flash encryption
desativada, nenhum eFuse queimado.** Toda gravação é reversível.

## Restrições que moldam o port

**MicroPython.** O board `ESP32_GENERIC_P4` existe apenas no `master` do
MicroPython; não está em v1.25.0 nem v1.26.0. O fork MicroPython do Specter
(`miketlk/micropython-specter-diy` @ `merge-with-upstream`) está em v1.25.0 e
traz somente `ESP32_GENERIC`. O baseline parte do `master` upstream.

**ESP-IDF.** As três fontes exigem versões incompatíveis:

| Projeto | ESP-IDF |
|---|---|
| MicroPython | 5.3–5.5.4 (recomendada 5.5.2) |
| `miketlk/specter-bootloader` | 5.5.5 pinado |
| `odudex/Kern` | 6.0.2 |

Por isso o código do Kern entra como **referência traduzida**, não como
componente plugado. Detalhes em `reports/micropython-p4-unreleased.md`.

## Procedência de cada driver

| Área | Origem | Situação |
|---|---|---|
| Painel ST7701 + timings | miketlk e Kern (idênticos) | validado no hardware |
| Backlight GPIO 26 (PWM, invertido) | ambos | validado |
| Touch GT911 (0x5D / 0x14) | ambos | validado |
| Reset do touch GPIO 23 | miketlk | divergente do Kern — ver reports |
| SDMMC 39–44 | miketlk | slot responde; falta testar com cartão |
| Rádio C6 em reset (GPIO 54) | Kern | adotado por ser airgapped |
| Câmera OV5647 / MIPI-CSI | Kern | pendente; depende de `esp_video` em 5.5.x |
| Decodificação de QR | Kern (`k_quirc`) | **bloqueado por licença** |

## Origem do app

O app Specter tem 11.184 linhas de Python e apenas 4 arquivos tocam hardware,
com 26 call sites no total:

| Arquivo | Call sites |
|---|---:|
| `src/platform.py` | 19 |
| `src/hosts/qr.py` | 4 |
| `src/hosts/usb.py` | 2 |
| `src/gui/tcp_gui.py` | 1 |

Existe precedente de shim: `f469-disco/libs/unix/pyb.py` finge o módulo `pyb`
inteiro em 117 linhas para o simulador rodar em Linux. O shim ESP32 segue esse
molde, mapeando para `machine.UART`, `machine.Pin` e `esp32`.

## Estrutura

```
boards/WAVESHARE_P4_43/   definição de board do MicroPython
components/               BSP revisado (display, touch, sd, câmera)
tools/                    scripts de build e gravação
```

## Créditos

Trabalho derivado de dois projetos MIT, ambos com revisão própria antes de uso:

- [`miketlk/specter-bootloader`](https://github.com/miketlk/specter-bootloader)
  @ `port_esp32-p4` — bootloader ESP32-P4, mapa de pinos, timings do painel
- [`odudex/Kern`](https://github.com/odudex/Kern) — BSP `wave_43`, pipeline de
  câmera, componente de SD, desligamento do rádio

Divergências e problemas encontrados nessas fontes estão documentados em
`reports/` na raiz deste repositório.

## Estado do baseline

Compilação **funcionando**. MicroPython `master` para `ESP32_GENERIC_P4`,
variante `PRE_REV3`, com ESP-IDF v5.5.5:

```
micropython.bin binary size 0x190e70 bytes.
Smallest app partition is 0x1f0000 bytes. 0x5f190 bytes (19%) free.
```

Gravado e **verificado na placa**. Banner do REPL:

```
MicroPython 8cf130db34-dirty on 2026-08-25; Generic ESP32P4 with pre revision 3 chip with ESP32-P4
>>> import sys; print(sys.implementation)
(name='micropython', version=(1, 30, 0, 'preview'), _machine='Generic ESP32P4 with pre
 revision 3 chip with ESP32-P4', _mpy=143110, _build='ESP32_GENERIC_P4-PRE_REV3', _thread='GIL')
```

Confirmado no hardware:

| Item | Resultado |
|---|---|
| Variante ativa | `ESP32_GENERIC_P4-PRE_REV3` (no próprio banner) |
| Clock | 360 MHz |
| PSRAM | `gc.mem_free()` = 33.091.696 B (~31,6 MB) |
| Filesystem | LFS montado, 7680 blocos de 4096 B (~30 MB) |
| Escrita/leitura | arquivo criado, lido e removido com sucesso |
| Partição da app | `('factory', 65536, 2031616)` |

**Atenção ao primeiro flash:** gravar por cima do layout do bootloader do
Specter deixa resíduo e o MicroPython acusa `filesystem appears to be
corrupted`. Um `esptool erase_flash` antes do primeiro flash resolve; depois
disso o boot fica limpo.

### Duas descobertas do baseline

**A placa exige `BOARD_VARIANT=PRE_REV3`.** A telemetria do mock firmware
reportou `chip_revision: 103`, que no encoding do ESP-IDF é major 1, minor 3, ou
seja **v1.3**. O `board.md` do MicroPython é explícito: revisões 0.x e 1.x
precisam das variantes `PRE_REV3`. O build padrão define
`CONFIG_ESP32P4_REV_MIN_300=y` e não sobe neste silício.

**ESP-IDF 5.5.5 funciona**, apesar de não constar na lista oficial do
MicroPython (5.3, 5.4, 5.4.1, 5.4.2, 5.5.1, 5.5.2, 5.5.4). Isso permite
reaproveitar o checkout já pinado pelo bootloader, sem uma segunda árvore de
ESP-IDF. O `tools/setup.sh` instala o toolchain em `deps/espressif`.

**Nenhuma variante de WiFi.** A placa tem um ESP32-C6, mas o alvo é airgapped.
O rádio fica fora do build e, adiante, será mantido em reset por hardware pelo
GPIO 54, como o Kern faz.

### Reproduzir

```sh
ports/esp32p4/tools/setup.sh      # uma vez: MicroPython, ESP-IDF e toolchain em deps/
ports/esp32p4/tools/build.sh
ports/esp32p4/tools/build.sh erase
ports/esp32p4/tools/build.sh flash
```

`idf.py flash` **não** funciona nesta combinação: o wrapper procura
`components/esptool_py/esptool.py`, que não existe mais (o esptool virou pacote
pip, v4.12.0). O `tools/build.sh flash` chama o módulo direto.

## Fase 2 — display e touch sob MicroPython

Board `WAVESHARE_P4_43` e módulo C `p4board`, **validados no hardware**.

### API

```python
import p4board, framebuf
p4board.init()                      # display + touch; returns True if touch initialized
g = framebuf.FrameBuffer(p4board.framebuffer(),
                         p4board.WIDTH, p4board.HEIGHT, framebuf.RGB565)
g.fill_rect(40, 60, 400, 90, 0xF800)
p4board.flush()                     # flush(y, height) is still supported; presents the full frame
p4board.backlight(100)              # 0..100
p4board.touch()                     # ((id, x, y, size), ...)
p4board.radio_off()                 # holds the ESP32-C6 in reset
```

`framebuffer()` returns a writable RGB565 `memoryview` outside the scanout
buffers. `flush()` copies the complete frame into the free buffer and waits for
a safe frame switch. LVGL uses the panel's two scanout buffers directly in FULL
render mode and waits for the frame-completion callback before reusing the
previous buffer.

### Verified on hardware

| Item | Resultado |
|---|---|
| `import p4board` | 480 x 800 |
| `p4board.init()` | `True` (display and touch) |
| `framebuffer()` | 768,000 bytes = 480 x 800 x 2, off-screen |
| Drawing + `flush()` | color-bar pattern visible on the panel |
| Touch | 819 points in 20 s at 50 Hz, covering x 4–475, y 8–797 |
| GT911 address | **0x14** (backup) |

### O endereço 0x14

Dirigimos o GPIO 23 como reset do touch (fonte: miketlk) **e** mantivemos a
sondagem dupla de endereço (fonte: Kern). O controlador subiu no endereço de
backup mesmo com o reset pulsado.

Ou seja, nesta unidade a sondagem dupla do Kern não é redundância defensiva —
**é o que faz o touch funcionar**. Uma implementação que fixasse 0x5D falharia.
Evidência completa em `reports/touch-reset-gpio-divergence.md`.

### Compilar e gravar

```sh
ports/esp32p4/tools/build.sh
ports/esp32p4/tools/build.sh flash
```

### Duas armadilhas do build

**Generator expressions não funcionam nos includes do usermod.** O MicroPython
achata os `target_include_directories` de um usermod na lista `INCLUDE_DIRS` do
componente, e o ESP-IDF então verifica que cada entrada é um diretório real. Um
`$<TARGET_PROPERTY:idf::driver,INTERFACE_INCLUDE_DIRECTORIES>` chega literal e o
build morre com *"is not a directory"*. O `components/p4board/micropython.cmake`
resolve os componentes para caminhos absolutos com `idf_component_get_property`.

**O `mpconfigboard.h` precisa desligar rádio explicitamente.** Sem
`MICROPY_PY_BLUETOOTH (0)` o build tenta compilar o NimBLE e falha por falta dos
headers. O `MICROPY_HW_ENABLE_UART_REPL (1)` também é obrigatório: o REPL desta
placa chega pela ponte CH343, não por USB nativo.

## Fase 3 — secp256k1

`secp256k1-embedded` compilado como usermod CMake e **verificado na placa**
contra constantes públicas.

| Teste | Resultado |
|---|---|
| Chave privada 1 → ponto gerador da curva | **OK** |
| ECDSA: assina, verifica, rejeita mensagem errada | **OK** |
| BIP340 vetor 0: pubkey x-only | **OK** |
| BIP340 vetor 0: assinatura Schnorr | **OK, idêntica byte a byte** |

A assinatura Schnorr gerada no ESP32-P4 confere com
`bip-0340/test-vectors.csv` do repositório `bitcoin/bips`, índice 0:

```
E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215
25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0
```

Isso exercita o caminho inteiro — campo 10x26 e escalar 8x32, ou seja, a
implementação de 32 bits — sobre RISC-V. Rodar:

```sh
mpremote cp ports/esp32p4/test_secp256k1.py :test_secp256k1.py
mpremote exec "import test_secp256k1; test_secp256k1.run()"
```

### Origem e correções

O submódulo aponta para `sandman21vs/secp256k1-embedded` @
`micropython-master-api`, um fork de `miketlk/secp256k1-embedded` @
`micropython-upgrade` com duas correções que precisamos fazer:

1. **APIs de inteiro do MicroPython.** `mp_obj_int_to_bytes_impl()` foi removida
   e `mp_binary_set_int()` mudou de assinatura. Substituídas por
   `mp_obj_int_to_bytes()`, que cobre small e long ints numa chamada.
2. **Qstrs perdidos.** O corpo do módulo está sob `#if MODULE_SECP256K1_ENABLED`,
   e o `usermod_gather_sources()` do MicroPython não propaga
   `INTERFACE_COMPILE_DEFINITIONS` ao passe de qstr — então o pré-processador vê
   um arquivo vazio e a compilação falha com `MP_QSTR_secp256k1 undeclared`.

As duas estão documentadas em `reports/` para reporte upstream. Voltar ao
repositório do miketlk é trocar `url` e `branch` no `.gitmodules`.

### Armadilha da API

`xonly_pubkey_from_pubkey()` devolve uma tupla cujo primeiro item é a struct
interna de 64 bytes do libsecp256k1, **não** a chave x-only serializada, e não
existe `xonly_pubkey_serialize`. Para a x-only, tire o byte de prefixo da pubkey
comprimida. Comparar a struct interna com o vetor BIP340 dá um falso negativo.

### Regeneração de qstr

A regra ninja de `genhdr/qstr.i.last` depende só dos `.c`. Mudar um header que
altera o pré-processamento não dispara regeneração — se uma correção em header
parecer não ter efeito, apague `build-W43/genhdr` antes de concluir qualquer
coisa.

## Fase 4 — camada de plataforma

Shims `pyb` e `sdram`, ramo ESP32 no `platform.py`, hashes de Bitcoin e TRNG.
Tudo **verificado na placa**.

| Item | Resultado |
|---|---|
| `sha256`, `sha512`, `ripemd160` | **OK** contra digests de `"abc"` |
| `hmac_sha512` | **OK** contra RFC 4231 caso 1 |
| `pbkdf2_hmac` / semente BIP39 | **OK** |
| TRNG (`os.urandom`) | 50,0% de bits em 1, 256 valores distintos |
| `platform.py` | importa, `esp32=True`, boot=`factory` |
| ramdisk em PSRAM | formata, monta, escreve, apaga |
| microSD | slot responde; sem cartão reporta ausente |

### Shims

`lib/pyb.py` mapeia a API `pyb` para `machine`. Segue o precedente do
`f469-disco/libs/unix/pyb.py`, que faz o mesmo em 117 linhas para o simulador.

O que **não existe** nesta placa vira objeto inerte que avisa uma vez no
console, em vez de falhar ou fingir em silêncio: `LED` (não há LEDs discretos),
`USB_VCP` (o REPL vem da ponte CH343, sem USB nativo) e UARTs sem pinos
mapeados, como a `"YB"` do ST-Link. `pyb.stub_report()` lista o que foi
exercitado sem hardware real.

`lib/sdram.py` cobre a PSRAM: `init()` é no-op porque o ESP-IDF já a inicializa,
`RAMDevice` é um block device em RAM para o `/ramdisk`, e o bloco pré-alocado de
1 MB espelha o do simulador.

### Mudanças no app

Duas, ambas mínimas:

**`src/config_default.py`** — `simulator` era `sys.platform != "pyboard"`, o que
classifica qualquer alvo novo como simulador. No ESP32 isso fazia o config
tentar criar `./fs` numa placa. Passou a usar a mesma expressão do
`platform.py`.

**`src/platform.py`** — ramo `esp32` ao lado do `simulator` que já existia:
imports (`stm` não existe), `/flash` e `/qspi` como diretórios do sistema de
arquivos interno, modo de boot pela partição em execução, e `usb_connected()`
retornando False.

### Duas decisões de honestidade

**Proteções de flash retornam `unknown`.** Secure Boot e flash encryption vivem
em eFuses, e o MicroPython não expõe leitura de eFuse. Reportar o que o perfil
de build pretendia seria afirmar em tempo de execução algo não verificado —
inaceitável num readout de segurança de carteira. Fica `unknown` até haver
leitura real.

**`wipe()` não apaga a flash nesta placa.** Em `/flash` e `/qspi`, que aqui são
diretórios e não volumes, os arquivos são removidos. Sobrescrever a partição com
bytes aleatórios exigiria desmontar a raiz de onde o próprio código roda.
Apagamento seguro precisa acontecer no bootloader. Está marcado como **não
implementado** no código, não silenciado.

### Hashes

O MicroPython traz md5, sha1 e sha256. Faltam sha512 (BIP32 usa HMAC-SHA512) e
ripemd160 (endereços), então o usermod `uhashlib` foi vendorizado de
`miketlk/f469-disco` @ `micropython-upgrade`, com duas correções locais: o macro
`STATIC` foi removido do MicroPython, e o guard `MODULE_HASHLIB_ENABLED`
precisou de valor padrão pelo mesmo motivo do secp256k1. O `hashlib` embutido
foi desligado no `mpconfigboard.h` para não disputar o nome.

Os fontes em `crypto/` vêm da linhagem trezor-crypto, licença BSD de três
cláusulas, com os avisos de copyright preservados.

### Atenção ao tamanho

```
micropython.bin binary size 0x1cabf0 bytes.
Smallest app partition is 0x1f0000 bytes. 0x25410 bytes (8%) free.
```

**8% de folga.** O app do Specter ainda não entrou. A tabela de partições vai
precisar de ajuste antes da Fase 5.

### Armadilha de build

Uma regeneração parcial de qstr produz `redeclaration of enumerator
'MP_QSTR_msg'`, com o qstr aparecendo no pool principal e no congelado. Apagar
só `genhdr` não basta; ao mexer em usermods, apague o diretório de build inteiro.

## Fase 5a — Bitcoin funcionando na placa

Tabela de partições própria, bibliotecas congeladas no firmware, e o **embit
derivando endereços Bitcoin corretos**.

### Vetores oficiais do BIP84

Os sete valores de `bitcoin/bips`, `bip-0084.mediawiki`, conferem na placa:

| Item | Resultado |
|---|---|
| `rootpriv` / `rootpub` (zprv/zpub) | **OK** |
| xpub da conta `m/84h/0h/0h` | **OK** |
| pubkey de `m/84h/0h/0h/0/0` | **OK** |
| Endereços `0/0`, `0/1`, `1/0` | **OK** |

```
bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu
```

Isso exercita tudo de uma vez: PBKDF2-HMAC-SHA512 do BIP39, HMAC-SHA512 e a
derivação do BIP32, secp256k1 para as chaves públicas, SHA256 mais RIPEMD160
para o hash do endereço, e a codificação bech32.

### Partições

A tabela padrão do MicroPython dava 0x1F0000 à app, e o firmware já ocupava 92%
disso **antes** do Specter entrar. `boards/WAVESHARE_P4_43/partitions.csv` sobe
para 4 MB e deixa o resto da janela de 16 MB como sistema de arquivos. Com tudo
congelado o binário está em 2,2 MB, 47% livre.

Mudar a tabela invalida o sistema de arquivos existente: faça `erase_flash`
antes do primeiro flash com o layout novo.

### Congelamento

`boards/WAVESHARE_P4_43/manifest.py` congela os shims, `embit`, `microur`,
`bcur`, `lvqr` e o `src/` inteiro.

**Não** congelamos `f469-disco/libs/common` de uma vez: ele vendoriza uma cópia
de `asyncio` da era MicroPython v1.10 que colide com a moderna vinda do
manifesto da porta —

```
error: redefinition of 'const_qstr_table_data_asyncio___init__'
```

A do MicroPython é mais nova e mantida, então é a que fica. O app importa
`asyncio` em cinco arquivos e não depende de particularidades da cópia antiga.

### O app já tenta subir

Com `src/` congelado, o `main.py` roda no boot e vai longe:

```
File "main.py", line 2, in <module>
File "specter.py", line 20, in <module>
File "hosts/core.py", line 5, in <module>
File "gui/core.py", line 1, in <module>
ImportError: no module named 'lvgl'
```

Ou seja, `platform.py`, `specter.py` e a camada de hosts carregam. O que falta é
exclusivamente o binding do LVGL. A falha cai no REPL, então a placa continua
utilizável.

## Fase 6 — câmera e leitura de QR

A placa tem uma OV5647 por MIPI-CSI. Ela substitui o leitor serial que o
Specter espera e que esta placa não tem.

| Item | Resultado na placa |
|---|---|
| Sensor | OV5647, detectado no SCCB |
| Captura | 1280x960 RGB565, **45,5 fps** |
| Decodificação | 640x480 em tons de cinza, **11,3 leituras/s** |
| QR de teste | lido corretamente em menos de 4 s |

### Como a câmera entra no app sem alterá-lo

O `QRHost` lê o scanner por `uart.any()` e `uart.read()`, esperando o payload
terminado em CR. Em vez de reescrever o host, o shim entrega uma **UART virtual
alimentada pela câmera** (`pyb.CameraUART`).

Isso preserva de graça toda a lógica de protocolo do `QRHost` — QR animado, UR,
BBQr, remontagem de partes. Ele não precisa saber de onde vieram os bytes.

A única mudança no app são três linhas em `hosts/qr.py`: `init()` pula a
configuração serial quando `uart.is_camera` é verdadeiro. Sem isso o host
gastaria timeouts sondando um scanner inexistente — e pior, se um QR estivesse
no campo de visão durante a sondagem, a resposta seria confundida com a de um
scanner.

### Decisões de implementação

**Formato enumerado, não exigido.** O `quirc` trabalha em luminância, então
`V4L2_PIX_FMT_GREY` seria ideal. O OV5647 não oferece GREY pelo pipeline CSI, e
um `S_FMT` recusado derruba a inicialização inteira. O código enumera o que o
driver oferece e desce a lista GREY → RGB565 → Bayer, guardando a escolha.

**Redução por 2 na conversão.** O sensor entrega 1280x960; decodificar 1,2
milhão de pixels custa caro sem melhorar acerto. A conversão para cinza já
reduz para 640x480, onde os módulos de um QR sobram.

**Luminância aproximada.** `(2R + 5G + B) / 8` em vez dos coeficientes ITU-R —
uma soma e um shift por pixel. O `quirc` precisa de contraste entre módulo
claro e escuro, não de fidelidade colorimétrica.

### Componentes ESP-IDF

`esp_video` é dependência gerenciada, e o MicroPython só lê `idf_component.yml`
de `ports/esp32/main/`. Por isso a câmera é um **componente ESP-IDF de verdade**
em `idf_components/`, trazido por `EXTRA_COMPONENT_DIRS` — o gerenciador lê o
manifesto de qualquer componente na árvore, não só o do main.

O `k_quirc` (MIT: quirc do Daniel Beer → OpenMV → Kern) entra como submódulo no
mesmo diretório, já que traz o próprio `CMakeLists.txt` de componente.

### Diagnóstico sem logs

Os `ESP_LOG` não chegam ao REPL cru do MicroPython. Sem isso a depuração é às
cegas, então `camera.stage()` reporta em que etapa a inicialização parou. Foi
ele que revelou a falha em `open`: o sensor respondia no I²C mas o dispositivo
V4L2 não existia, porque faltava `CONFIG_CAMERA_OV5647=y` e o driver do sensor
nem era compilado.

## NFC — backup da seed em cartão

Leitor **M5Stack RFID Unit 2** (WS1850S) no I²C da placa. Salva a seed cifrada
num cartão e carrega de volta. **Ainda não rodou na placa com leitor de
verdade**: passou nos testes contra leitor e cartões simulados, e sob o
MicroPython da porta unix, só isso.

### Camadas

| Arquivo | O que sabe |
|---|---|
| `components/p4board/i2c.c` | empresta o barramento I²C do touch |
| `lib/nfc_ws1850s.py` | o chip: registradores, quadros ISO14443A, CRC, crypto1 |
| `src/nfc/__init__.py` | o cartão: seleção, endereçamento linear, registro `KRN1` |
| `src/kef.py` | o envelope cifrado (KEF, o formato do Krux) |
| `src/nfc/seed.py` | os dois fluxos de tela: salvar e carregar |

O código do Kern (`components/nfc`, C sobre ESP-IDF 6) entra como referência
traduzida, como o resto do port. A tradução para MicroPython partiu do fork do
Krux, que já tinha a mesma pilha em Python.

### O barramento já tem dono

GPIO7/8 é o I²C do touch e da câmera, aberto pelo `p4board` em C. Um
`machine.I2C` nos mesmos pinos falha com a porta já adquirida. Por isso o
`p4board` ganhou três funções que emprestam o barramento:

```python
p4board.i2c_probe(0x28)             # True se alguém responde; não loga nada
p4board.i2c_writeto(0x28, b"...")
p4board.i2c_readfrom(0x28, 16)
```

`i2c_probe` é o que decide, a cada vez que um menu é montado, se as opções de
NFC aparecem. Sem o módulo ligado elas somem e nada de NFC roda.

### GCM sem GCM

O `cryptolib` do MicroPython só tem ECB e CBC. O KEF usa também CTR e GCM, e o
padrão do Kern e do Krux é GCM. Os dois modos são construídos em Python sobre a
cifra de um bloco em ECB: o contador do CTR e o GHASH do GCM. Um backup tem no
máximo três blocos, então o custo é irrelevante perto do PBKDF2.

Conferido de três jeitos: contra os vetores da suíte do Krux, contra envelopes
gerados pelo `kef.py` do Krux nas doze versões, e contra o AES-GCM do pacote
`cryptography`.

### Testar na placa

```sh
mpremote cp ports/esp32p4/test_nfc.py :/test_nfc.py
mpremote exec "import test_nfc; test_nfc.run()"        # só lê
mpremote exec "import test_nfc; test_nfc.roundtrip()"  # ESCREVE um registro de teste
```

### O que falta

- Rodar na placa: alcance em 3,3 V, tempo do PBKDF2 de 100.000 rodadas, e se o
  leitor convive com o touch e a câmera no mesmo barramento.
- Descritores de carteira no cartão (tipo 2 do formato), como o Kern faz.
- Apagar um cartão.

