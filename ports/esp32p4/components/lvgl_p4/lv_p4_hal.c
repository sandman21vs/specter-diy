/**
 * @file lv_p4_hal.c
 * @brief LVGL bound to the p4board panel and GT911 touch.
 *
 * Modelado a partir de f469-disco/usermods/udisplay_f469/lv_stm_hal/, que faz
 * o mesmo para a Discovery F469 em 73 linhas.
 *
 * O LVGL desenha em modo DIRECT nos dois framebuffers RGB565 do painel: so as
 * areas que mudaram sao renderizadas, e o proprio LVGL copia essas areas para
 * o outro buffer antes do quadro seguinte (refr_sync_areas). A troca de buffer
 * e assincrona -- o flush so pede a troca e volta -- e a espera pelo fim do
 * scanout acontece no flush_wait_cb, que o LVGL chama antes de escrever de novo
 * no buffer que estava na tela. Assim a CPU nunca escreve no buffer que o DPI
 * esta varrendo, e o app nao fica bloqueado esperando o painel.
 */

#include "lv_p4_hal.h"

#include "board_config.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "lv_conf.h"
#include "lvgl.h"
#include "p4board.h"

static const char *TAG = "lv-p4";
static bool flush_error_reported;
static bool invalid_area_reported;
static uint16_t *lvgl_framebuffers[2];
static lv_p4_stats_t stats;

void lv_p4_stats(lv_p4_stats_t *out, bool reset) {
    if (out) {
        *out = stats;
    }
    if (reset) {
        stats = (lv_p4_stats_t){0};
    }
}

static void tft_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map);
static void tft_flush_wait(lv_display_t *disp);
static void touchpad_read(lv_indev_t *indev, lv_indev_data_t *data);

/* Relogio real para o LVGL. Antes cada display.update(dt) somava dt fixo, entao
 * qualquer atraso no loop deixava as animacoes mais lentas em vez de pular
 * quadros. */
static uint32_t tick_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void fail_closed(const char *what, esp_err_t result) {
    if (!flush_error_reported) {
        ESP_LOGE(TAG, "%s failed: %s; restarting safely", what, esp_err_to_name(result));
        flush_error_reported = true;
    }
    /* A failed handoff does not prove either scanout buffer is writable.
     * Restart instead of rotating buffers or hanging in LVGL's wait loop.
     * This only resets the CPU; persistent storage is not erased. */
    esp_restart();
}

void tft_init(void) {
    if (p4board_display_init() != ESP_OK) {
        ESP_LOGE(TAG, "panel init failed; LVGL will have nothing to draw on");
        return;
    }

    uint16_t *framebuffer0 = NULL;
    uint16_t *framebuffer1 = NULL;
    if (p4board_framebuffers(&framebuffer0, &framebuffer1) != ESP_OK) {
        ESP_LOGE(TAG, "panel framebuffers unavailable; LVGL will have nothing to draw on");
        return;
    }
    lvgl_framebuffers[0] = framebuffer0;
    lvgl_framebuffers[1] = framebuffer1;
    size_t nbytes = (size_t)P4BOARD_LCD_WIDTH * P4BOARD_LCD_HEIGHT * 2u;

    lv_tick_set_cb(tick_ms);

    lv_display_t *disp = lv_display_create(P4BOARD_LCD_WIDTH, P4BOARD_LCD_HEIGHT);
    lv_display_set_flush_cb(disp, tft_flush);
    lv_display_set_flush_wait_cb(disp, tft_flush_wait);
    /* Start by drawing into fb1; fb0 is the initial scanout buffer. */
    lv_display_set_buffers(disp, framebuffer1, framebuffer0, nbytes,
        LV_DISPLAY_RENDER_MODE_DIRECT);

    p4board_backlight(100);
    ESP_LOGI(TAG, "LVGL on %ux%u RGB565, double-buffered direct render",
        P4BOARD_LCD_WIDTH, P4BOARD_LCD_HEIGHT);
}

static void tft_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map) {
    (void)area;
    stats.areas++;
    if (px_map != (uint8_t *)lvgl_framebuffers[0]
        && px_map != (uint8_t *)lvgl_framebuffers[1]) {
        if (!invalid_area_reported) {
            ESP_LOGE(TAG, "DIRECT render flushed an unknown buffer");
            invalid_area_reported = true;
        }
        /* Unknown buffer ownership must never be handed back to LVGL. */
        esp_restart();
        return;
    }

    /* Em DIRECT o LVGL chama o flush uma vez por area alterada, todas no mesmo
     * buffer. So a ultima fecha o quadro; as anteriores nao tem o que fazer. */
    if (!lv_display_flush_is_last(disp)) {
        lv_display_flush_ready(disp);
        return;
    }

    /* draw_bitmap com o proprio framebuffer faz o write-back do cache e marca
     * o buffer para o proximo scanout. A tela inteira entra no write-back
     * porque as copias de sincronizacao do LVGL tambem sujaram o cache. */
    int64_t started = esp_timer_get_time();
    esp_err_t result = p4board_present_begin((const uint16_t *)px_map);
    stats.present_us += (uint64_t)(esp_timer_get_time() - started);
    stats.frames++;
    if (result != ESP_OK) {
        fail_closed("framebuffer presentation", result);
        return;
    }
    /* Sem flush_ready aqui: o LVGL chama tft_flush_wait() antes de voltar a
     * escrever no buffer que estava na tela. */
}

static void tft_flush_wait(lv_display_t *disp) {
    (void)disp;
    int64_t started = esp_timer_get_time();
    esp_err_t result = p4board_present_finish();
    stats.wait_us += (uint64_t)(esp_timer_get_time() - started);
    if (result != ESP_OK) {
        fail_closed("framebuffer handoff", result);
    }
}

void touchpad_init(void) {
    if (p4board_touch_init() != ESP_OK) {
        ESP_LOGE(TAG, "touch init failed; the UI will be display-only");
        return;
    }
    lv_indev_t *indev = lv_indev_create();
    lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev, touchpad_read);
    ESP_LOGI(TAG, "GT911 registered at 0x%02x", p4board_touch_address());
}

/* Um "soltou" so vale depois de ficar este tempo sem novo toque. O GT911 as
 * vezes reporta zero pontos no meio de um toque leve; sem esse filtro o LVGL
 * ve press-release-press e conta um clique duplo. */
#define TOUCH_RELEASE_DEBOUNCE_MS 40
/* Com o dedo na tela o GT911 entrega um quadro a cada ~10 ms. Se nada chegar
 * por bem mais que isso, o quadro de "soltou" se perdeu (erro de I2C, por
 * exemplo) e o toque nao pode ficar preso como pressionado. */
#define TOUCH_STALE_MS 250

static void touchpad_read(lv_indev_t *indev, lv_indev_data_t *data) {
    (void)indev;
    /* O LVGL espera a ultima posicao conhecida junto com o estado RELEASED,
     * senao um toque que termina fora do widget cancela o clique. */
    static int32_t last_x = 0;
    static int32_t last_y = 0;
    static bool pressed = false;
    static uint32_t release_tick = 0;
    static bool release_pending = false;
    static uint32_t last_frame_tick = 0;

    p4board_touch_point_t points[P4BOARD_TOUCH_MAX_POINTS];
    uint8_t count = 0;
    esp_err_t result = p4board_touch_read(points, P4BOARD_TOUCH_MAX_POINTS, &count);
    uint32_t now = lv_tick_get();

    if (result == ESP_OK) {
        last_frame_tick = now;
        if (count > 0) {
            last_x = points[0].x;
            last_y = points[0].y;
            pressed = true;
            release_pending = false;
        } else if (pressed && !release_pending) {
            release_pending = true;
            release_tick = now;
        }
    }
    /* ESP_ERR_NOT_FINISHED (sem quadro novo) e erros de leitura nao dizem nada
     * sobre o dedo: mantem o estado anterior. */

    if (pressed && release_pending
        && lv_tick_elaps(release_tick) >= TOUCH_RELEASE_DEBOUNCE_MS) {
        pressed = false;
        release_pending = false;
    }
    if (pressed && lv_tick_elaps(last_frame_tick) >= TOUCH_STALE_MS) {
        pressed = false;
        release_pending = false;
    }

    data->state = pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
    data->point.x = last_x;
    data->point.y = last_y;
}
