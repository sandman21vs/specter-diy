// Hook de partida do board, chamado pelo app_main do MicroPython antes de
// criar a mp_task (MICROPY_BOARD_STARTUP em mpconfigboard.h).

#include "sdkconfig.h"
#include "p4board.h"

#if CONFIG_ESP32P4_REV_MIN_FULL >= 300
#include "soc/hp_system_reg.h"
#include "soc/soc.h"
#endif

void boardctrl_startup(void);

// No ESP32-P4 v3.x um load ou store do core pela PSRAM pode passar do bus
// timeout (65535 ciclos, o maximo do registrador). O acesso terminaria, mas o
// timeout o transforma em access fault, e como o erro de barramento e
// assincrono nesse core nao ha como repetir. O Kern viu isso numa wave_43
// v3.2 no boot e no scan de QR com a PSRAM a 200 MHz e resolveu desligando o
// timeout (odudex/Kern 3ad82dd); fazemos o mesmo. Em v0.x/v1.x vira no-op.
static void p4board_bus_timeout_disable(void) {
#if CONFIG_ESP32P4_REV_MIN_FULL >= 300
    REG_CLR_BIT(HP_SYSTEM_CORE_DBUS_TIMEOUT_REG, HP_SYSTEM_CORE_DBUS_TIMEOUT_EN);
    REG_CLR_BIT(HP_SYSTEM_CORE_IBUS_TIMEOUT_REG, HP_SYSTEM_CORE_IBUS_TIMEOUT_EN);
#endif
}

void p4board_startup(void) {
    p4board_bus_timeout_disable();
    // Partida padrao do port esp32: NVS e deteccao do tamanho da flash.
    boardctrl_startup();
}
