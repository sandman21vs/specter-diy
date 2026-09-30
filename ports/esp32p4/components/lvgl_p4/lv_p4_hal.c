/**
 * @file lv_p4_hal.c
 * @brief LVGL bound to the p4board panel and GT911 touch.
 *
 * Modelado a partir de f469-disco/usermods/udisplay_f469/lv_stm_hal/, que faz
 * o mesmo para a Discovery F469 em 73 linhas.
 *
 * O LVGL usa os dois framebuffers RGB565 do painel em modo FULL. Cada quadro
 * e terminado por inteiro num buffer livre; o flush espera o callback de fim
 * de quadro do IDF que libera o framebuffer anterior. Assim
 * a CPU nunca escreve no buffer que o DPI esta varrendo.
 */

#include "lv_p4_hal.h"

#include "board_config.h"
#include "esp_log.h"
#include "lv_conf.h"
#include "lvgl.h"
#include "p4board.h"

static const char *TAG = "lv-p4";
static bool flush_error_reported;
static bool invalid_area_reported;

static void tft_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map);
static void touchpad_read(lv_indev_t *indev, lv_indev_data_t *data);

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
    size_t nbytes = (size_t)P4BOARD_LCD_WIDTH * P4BOARD_LCD_HEIGHT * 2u;

    lv_display_t *disp = lv_display_create(P4BOARD_LCD_WIDTH, P4BOARD_LCD_HEIGHT);
    lv_display_set_flush_cb(disp, tft_flush);
    /* Start by drawing into fb1; fb0 is the initial scanout buffer. */
    lv_display_set_buffers(disp, framebuffer1, framebuffer0, nbytes,
        LV_DISPLAY_RENDER_MODE_FULL);

    p4board_backlight(100);
    ESP_LOGI(TAG, "LVGL on %ux%u RGB565, double-buffered full render",
        P4BOARD_LCD_WIDTH, P4BOARD_LCD_HEIGHT);
}

static void tft_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map) {
    if (!px_map || area->x1 != 0 || area->y1 != 0 ||
        area->x2 != P4BOARD_LCD_WIDTH - 1 ||
        area->y2 != P4BOARD_LCD_HEIGHT - 1) {
        if (!invalid_area_reported) {
            ESP_LOGE(TAG, "FULL render produced an invalid flush area");
            invalid_area_reported = true;
        }
        lv_display_flush_ready(disp);
        return;
    }

    /* The IDF DPI driver accepts panel framebuffer pointers and synchronizes
     * the complete dirty area out of cache. The BSP queues the frame at a
     * completed-frame boundary, then waits until the old scanout buffer is
     * released before LVGL may use it for the next render. */
    esp_err_t result = p4board_present_framebuffer((const uint16_t *)px_map);
    if (result != ESP_OK && !flush_error_reported) {
        ESP_LOGE(TAG, "framebuffer presentation failed: %s", esp_err_to_name(result));
        flush_error_reported = true;
    }
    lv_display_flush_ready(disp);
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

static void touchpad_read(lv_indev_t *indev, lv_indev_data_t *data) {
    (void)indev;
    /* O LVGL espera a ultima posicao conhecida junto com o estado RELEASED,
     * senao um toque que termina fora do widget cancela o clique. */
    static int32_t last_x = 0;
    static int32_t last_y = 0;

    p4board_touch_point_t points[P4BOARD_TOUCH_MAX_POINTS];
    uint8_t count = 0;

    if (p4board_touch_read(points, P4BOARD_TOUCH_MAX_POINTS, &count) == ESP_OK
        && count > 0) {
        last_x = points[0].x;
        last_y = points[0].y;
        data->state = LV_INDEV_STATE_PRESSED;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
    data->point.x = last_x;
    data->point.y = last_y;
}
