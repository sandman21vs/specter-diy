/*
 * SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
 * SPDX-FileCopyrightText: 2026 Specter contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * ST7701 MIPI DSI panel and backlight for the Waveshare 4.3-C board.
 *
 * Adapted from miketlk/specter-bootloader @ port_esp32-p4,
 * platforms/esp32-p4-wifi6-touch-lcd/lcd-4p3/board_display.c, which in turn
 * adapts Waveshare's Apache-2.0 ESP32-P4-WIFI6-Touch-LCD-4.3 example. The
 * controller command sequence below is copied verbatim from that source: it is
 * validated on this exact panel, and the values are opaque magic numbers where
 * a transcription slip is silent and fatal.
 *
 * Changes from the original: the Specter bootloader HAL entry points are
 * replaced by the p4board_* surface consumed by the MicroPython module, and
 * the SPECTER_* configuration macros are renamed P4BOARD_*. The deliberate
 * property of the original is preserved -- no managed esp_lcd_st7701 component
 * is required, so this builds on ESP-IDF 5.5.x without the component registry.
 */

#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "board_config.h"
#include "driver/ledc.h"
#include "esp_attr.h"
#include "esp_cache.h"
#include "esp_heap_caps.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_ldo_regulator.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "p4board.h"

#define ARRAY_SIZE(values) (sizeof(values) / sizeof((values)[0]))
#define BACKLIGHT_DUTY_MAX 1023U
#define DISPLAY_EVENT_TIMEOUT_MS 100U

typedef struct st7701_init_command {
  uint8_t command;
  uint8_t data[16];
  uint8_t data_size;
  uint16_t delay_ms;
} st7701_init_command_t;

static const st7701_init_command_t st7701_init_commands[] = {
    {0xff, {0x77, 0x01, 0x00, 0x00, 0x13}, 5, 0},
    {0xef, {0x08}, 1, 0},
    {0xff, {0x77, 0x01, 0x00, 0x00, 0x10}, 5, 0},
    {0xc0, {0x63, 0x00}, 2, 0},
    {0xc1, {0x0d, 0x02}, 2, 0},
    {0xc2, {0x17, 0x08}, 2, 0},
    {0xcc, {0x10}, 1, 0},
    {0xb0,
     {0x40, 0xc9, 0x94, 0x0e, 0x10, 0x05, 0x0b, 0x09, 0x08, 0x26, 0x04, 0x52,
      0x10, 0x69, 0x6b, 0x69},
     16,
     0},
    {0xb1,
     {0x40, 0xd2, 0x98, 0x0c, 0x92, 0x07, 0x09, 0x08, 0x07, 0x25, 0x02, 0x0e,
      0x0c, 0x6e, 0x78, 0x55},
     16,
     0},
    {0xff, {0x77, 0x01, 0x00, 0x00, 0x11}, 5, 0},
    {0xb0, {0x5d}, 1, 0},
    {0xb1, {0x4e}, 1, 0},
    {0xb2, {0x87}, 1, 0},
    {0xb3, {0x80}, 1, 0},
    {0xb5, {0x4e}, 1, 0},
    {0xb7, {0x85}, 1, 0},
    {0xb8, {0x21}, 1, 0},
    {0xb9, {0x10, 0x1f}, 2, 0},
    {0xbb, {0x03}, 1, 0},
    {0xbc, {0x00}, 1, 0},
    {0xc1, {0x78}, 1, 0},
    {0xc2, {0x78}, 1, 0},
    {0xd0, {0x88}, 1, 0},
    {0xe0, {0x00, 0x3a, 0x02}, 3, 0},
    {0xe1,
     {0x04, 0xa0, 0x00, 0xa0, 0x05, 0xa0, 0x00, 0xa0, 0x00, 0x40, 0x40},
     11,
     0},
    {0xe2,
     {0x30, 0x00, 0x40, 0x40, 0x32, 0xa0, 0x00, 0xa0, 0x00, 0xa0, 0x00, 0xa0,
      0x00},
     13,
     0},
    {0xe3, {0x00, 0x00, 0x33, 0x33}, 4, 0},
    {0xe4, {0x44, 0x44}, 2, 0},
    {0xe5,
     {0x09, 0x2e, 0xa0, 0xa0, 0x0b, 0x30, 0xa0, 0xa0, 0x05, 0x2a, 0xa0, 0xa0,
      0x07, 0x2c, 0xa0, 0xa0},
     16,
     0},
    {0xe6, {0x00, 0x00, 0x33, 0x33}, 4, 0},
    {0xe7, {0x44, 0x44}, 2, 0},
    {0xe8,
     {0x08, 0x2d, 0xa0, 0xa0, 0x0a, 0x2f, 0xa0, 0xa0, 0x04, 0x29, 0xa0, 0xa0,
      0x06, 0x2b, 0xa0, 0xa0},
     16,
     0},
    {0xeb, {0x00, 0x00, 0x4e, 0x4e, 0x00, 0x00, 0x00}, 7, 0},
    {0xec, {0x08, 0x01}, 2, 0},
    {0xed,
     {0xb0, 0x2b, 0x98, 0xa4, 0x56, 0x7f, 0xff, 0xff, 0xff, 0xff, 0xf7, 0x65,
      0x4a, 0x89, 0xb2, 0x0b},
     16,
     0},
    {0xef, {0x08, 0x08, 0x08, 0x45, 0x3f, 0x54}, 6, 0},
    {0xff, {0x77, 0x01, 0x00, 0x00, 0x00}, 5, 0},
    {0x3a, {0x55}, 1, 0},
    {0x11, {0}, 0, 120},
    {0x29, {0}, 0, 0},
};

static const char *TAG = "p4board-display";
static esp_ldo_channel_handle_t dsi_ldo;
static esp_lcd_dsi_bus_handle_t dsi_bus;
static esp_lcd_panel_io_handle_t panel_io;
static esp_lcd_panel_handle_t dpi_panel;
static uint16_t *lcd_framebuffers[2];
static uint16_t *drawing_framebuffer;
static SemaphoreHandle_t frame_complete_semaphore;
static uint8_t displayed_framebuffer;
/* Troca pedida por present_begin() e ainda nao confirmada por present_finish(). */
static uint8_t pending_framebuffer;
static bool present_pending;
/* Quantos fins de quadro present_finish() precisa ver (1 ou 2, ver begin). */
static uint8_t present_events_needed;
/* Nucleo em que o ISR de fim de quadro roda; -1 ate o primeiro quadro. */
static volatile int display_isr_core = -1;
static portMUX_TYPE present_lock = portMUX_INITIALIZER_UNLOCKED;
static bool framebuffer_state_known;
static bool lvgl_owns_framebuffers;
static bool display_timeout_reported;
static bool backlight_timer_initialized;
static bool backlight_channel_initialized;

/* These callbacks run from the MIPI-DSI bridge ISR. They only wake the task
 * that is waiting for a safe frame boundary; LVGL is called from its normal
 * MicroPython update task, never from interrupt context. */
static bool IRAM_ATTR display_on_frame_complete(esp_lcd_panel_handle_t panel,
    esp_lcd_dpi_panel_event_data_t *event_data, void *user_ctx) {
    (void)panel;
    (void)event_data;
    (void)user_ctx;
    display_isr_core = xPortGetCoreID();
    BaseType_t task_woken = pdFALSE;
    if (frame_complete_semaphore) {
        xSemaphoreGiveFromISR(frame_complete_semaphore, &task_woken);
    }
    return task_woken == pdTRUE;
}

static esp_err_t wait_for_display_event(SemaphoreHandle_t semaphore,
    const char *event_name) {
    if (xSemaphoreTake(semaphore, pdMS_TO_TICKS(DISPLAY_EVENT_TIMEOUT_MS)) != pdTRUE) {
        if (!display_timeout_reported) {
            ESP_LOGW(TAG, "timed out waiting for display %s", event_name);
            display_timeout_reported = true;
        }
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

esp_err_t p4board_backlight(uint32_t percent) {
    if (percent > 100U) {
        return ESP_ERR_INVALID_ARG;
    }
    uint32_t duty = (BACKLIGHT_DUTY_MAX * percent) / 100U;
    esp_err_t result = ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
    if (result == ESP_OK) {
        result = ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    }
    return result;
}

static esp_err_t init_backlight(void) {
    ledc_timer_config_t timer_config = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_1,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    esp_err_t result = ledc_timer_config(&timer_config);
    if (result != ESP_OK) {
        return result;
    }
    backlight_timer_initialized = true;
    ledc_channel_config_t channel_config = {
        .gpio_num = P4BOARD_LCD_BACKLIGHT_GPIO,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_1,
        .duty = 0,
        .hpoint = 0,
        .flags.output_invert = P4BOARD_LCD_BACKLIGHT_INVERTED,
    };
    result = ledc_channel_config(&channel_config);
    backlight_channel_initialized = (result == ESP_OK);
    return result;
}

static esp_err_t reset_lcd(void) {
    gpio_config_t reset_config = {
        .pin_bit_mask = 1ULL << P4BOARD_LCD_RESET_GPIO,
        .mode = GPIO_MODE_OUTPUT,
    };
    esp_err_t result = gpio_config(&reset_config);
    if (result != ESP_OK) {
        return result;
    }
    int active = P4BOARD_LCD_RESET_ACTIVE_HIGH ? 1 : 0;
    /* Idle, assert, release -- 10 ms each, as in the validated original. */
    const int levels[] = { !active, active, !active };
    for (size_t i = 0; i < ARRAY_SIZE(levels); ++i) {
        result = gpio_set_level(P4BOARD_LCD_RESET_GPIO, levels[i]);
        if (result != ESP_OK) {
            return result;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return ESP_OK;
}

static esp_err_t send_st7701_init(void) {
    for (size_t index = 0; index < ARRAY_SIZE(st7701_init_commands); ++index) {
        const st7701_init_command_t *item = &st7701_init_commands[index];
        esp_err_t result = esp_lcd_panel_io_tx_param(panel_io, item->command,
            item->data_size ? item->data : NULL, item->data_size);
        if (result != ESP_OK) {
            return result;
        }
        if (item->delay_ms) {
            vTaskDelay(pdMS_TO_TICKS(item->delay_ms));
        }
    }
    return ESP_OK;
}

esp_err_t p4board_display_init(void) {
    if (dpi_panel) {
        return ESP_OK;
    }
    display_timeout_reported = false;
    esp_err_t result = init_backlight();
    if (result == ESP_OK) {
        result = p4board_backlight(0);
    }
    if (result != ESP_OK) {
        goto fail;
    }

    esp_ldo_channel_config_t ldo_config = {
        .chan_id = P4BOARD_LCD_DSI_LDO_CHANNEL,
        .voltage_mv = P4BOARD_LCD_DSI_LDO_MV,
    };
    if ((result = esp_ldo_acquire_channel(&ldo_config, &dsi_ldo)) != ESP_OK) {
        goto fail;
    }

    esp_lcd_dsi_bus_config_t bus_config = {
        .bus_id = 0,
        .num_data_lanes = P4BOARD_LCD_DSI_LANES,
        .phy_clk_src = MIPI_DSI_PHY_CLK_SRC_DEFAULT,
        .lane_bit_rate_mbps = P4BOARD_LCD_DSI_LANE_BITRATE_MBPS,
    };
    if ((result = esp_lcd_new_dsi_bus(&bus_config, &dsi_bus)) != ESP_OK) {
        goto fail;
    }

    esp_lcd_dbi_io_config_t io_config = {
        .virtual_channel = 0,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    if ((result = esp_lcd_new_panel_io_dbi(dsi_bus, &io_config, &panel_io)) != ESP_OK) {
        goto fail;
    }

    esp_lcd_dpi_panel_config_t panel_config = {
        .virtual_channel = 0,
        .dpi_clk_src = MIPI_DSI_DPI_CLK_SRC_DEFAULT,
        .dpi_clock_freq_mhz = P4BOARD_LCD_DPI_CLOCK_MHZ,
        .in_color_format = LCD_COLOR_FMT_RGB565,
        .num_fbs = 2,
        .video_timing = {
            .h_size = P4BOARD_LCD_WIDTH,
            .v_size = P4BOARD_LCD_HEIGHT,
            .hsync_back_porch = P4BOARD_LCD_HSYNC_BACK_PORCH,
            .hsync_pulse_width = P4BOARD_LCD_HSYNC_PULSE_WIDTH,
            .hsync_front_porch = P4BOARD_LCD_HSYNC_FRONT_PORCH,
            .vsync_back_porch = P4BOARD_LCD_VSYNC_BACK_PORCH,
            .vsync_pulse_width = P4BOARD_LCD_VSYNC_PULSE_WIDTH,
            .vsync_front_porch = P4BOARD_LCD_VSYNC_FRONT_PORCH,
        },
        /* LVGL and the panel share the two scanout buffers directly. No
         * framebuffer-copy engine is needed for this path. */
        .flags.use_dma2d = false,
    };

    /* Keep frame completions as a count: two DMA completions may arrive
     * before the waiting task runs, and both are needed for a safe handoff. */
    frame_complete_semaphore = xSemaphoreCreateCounting(UINT32_MAX, 0);
    if (!frame_complete_semaphore) {
        result = ESP_ERR_NO_MEM;
        goto fail;
    }

    if ((result = esp_lcd_new_panel_dpi(dsi_bus, &panel_config, &dpi_panel)) != ESP_OK) {
        goto fail;
    }

    const esp_lcd_dpi_panel_event_callbacks_t display_callbacks = {
        .on_frame_buf_complete = display_on_frame_complete,
    };
    if ((result = esp_lcd_dpi_panel_register_event_callbacks(dpi_panel,
        &display_callbacks, NULL)) != ESP_OK) {
        goto fail;
    }

    void *framebuffer0 = NULL;
    void *framebuffer1 = NULL;
    if ((result = reset_lcd()) != ESP_OK ||
        (result = send_st7701_init()) != ESP_OK ||
        (result = esp_lcd_panel_init(dpi_panel)) != ESP_OK ||
        (result = esp_lcd_dpi_panel_get_frame_buffer(dpi_panel, 2,
            &framebuffer0, &framebuffer1)) != ESP_OK) {
        goto fail;
    }

    lcd_framebuffers[0] = framebuffer0;
    lcd_framebuffers[1] = framebuffer1;
    displayed_framebuffer = 0;
    framebuffer_state_known = true;
    lvgl_owns_framebuffers = false;

    ESP_LOGI(TAG, "ST7701 %ux%u active: DSI=%u lanes at %u Mbps, LDO=%d/%d mV",
        P4BOARD_LCD_WIDTH, P4BOARD_LCD_HEIGHT, P4BOARD_LCD_DSI_LANES,
        P4BOARD_LCD_DSI_LANE_BITRATE_MBPS, P4BOARD_LCD_DSI_LDO_CHANNEL,
        P4BOARD_LCD_DSI_LDO_MV);
    return ESP_OK;

fail:
    ESP_LOGE(TAG, "display init failed: %s", esp_err_to_name(result));
    p4board_display_deinit();
    return result;
}

void p4board_display_deinit(void) {
    if (backlight_channel_initialized) {
        p4board_backlight(0);
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
        gpio_reset_pin(P4BOARD_LCD_BACKLIGHT_GPIO);
        backlight_channel_initialized = false;
    }
    if (backlight_timer_initialized) {
        ledc_timer_pause(LEDC_LOW_SPEED_MODE, LEDC_TIMER_1);
        ledc_timer_config_t timer_config = {
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .timer_num = LEDC_TIMER_1,
            .deconfigure = true,
        };
        ledc_timer_config(&timer_config);
        backlight_timer_initialized = false;
    }
    if (dpi_panel) {
        esp_lcd_panel_del(dpi_panel);
        dpi_panel = NULL;
    }
    lcd_framebuffers[0] = NULL;
    lcd_framebuffers[1] = NULL;
    displayed_framebuffer = 0;
    framebuffer_state_known = false;
    present_pending = false;
    lvgl_owns_framebuffers = false;
    if (drawing_framebuffer) {
        heap_caps_free(drawing_framebuffer);
        drawing_framebuffer = NULL;
    }
    if (panel_io) {
        esp_lcd_panel_io_del(panel_io);
        panel_io = NULL;
    }
    if (dsi_bus) {
        esp_lcd_del_dsi_bus(dsi_bus);
        dsi_bus = NULL;
    }
    if (dsi_ldo) {
        esp_ldo_release_channel(dsi_ldo);
        dsi_ldo = NULL;
    }
    if (frame_complete_semaphore) {
        vSemaphoreDelete(frame_complete_semaphore);
        frame_complete_semaphore = NULL;
    }
    gpio_reset_pin(P4BOARD_LCD_RESET_GPIO);
}

uint16_t *p4board_framebuffer(void) {
    /* The public MicroPython framebuffer is off-screen. The LVGL renderer uses
     * the two panel-owned buffers through p4board_framebuffers(). Allocate
     * this compatibility buffer only when a caller requests it, so the normal
     * LVGL UI does not reserve an unused third frame in PSRAM. */
    if (!dpi_panel) {
        return NULL;
    }
    if (!drawing_framebuffer) {
        drawing_framebuffer = heap_caps_calloc(1,
            (size_t)P4BOARD_LCD_WIDTH * P4BOARD_LCD_HEIGHT * sizeof(uint16_t),
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    return drawing_framebuffer;
}

esp_err_t p4board_framebuffers(uint16_t **fb0, uint16_t **fb1) {
    if (!fb0 || !fb1) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!dpi_panel || !lcd_framebuffers[0] || !lcd_framebuffers[1]) {
        return ESP_ERR_INVALID_STATE;
    }
    *fb0 = lcd_framebuffers[0];
    *fb1 = lcd_framebuffers[1];
    /* LVGL owns both buffers until display deinit. Legacy flush must not
     * change its scanout selection or overwrite its next render target. */
    lvgl_owns_framebuffers = true;
    return ESP_OK;
}

esp_err_t p4board_present_begin(const uint16_t *framebuffer) {
    if (!dpi_panel || !framebuffer || !frame_complete_semaphore) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!framebuffer_state_known || present_pending) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t framebuffer_index;
    if (framebuffer == lcd_framebuffers[0]) {
        framebuffer_index = 0;
    } else if (framebuffer == lcd_framebuffers[1]) {
        framebuffer_index = 1;
    } else {
        return ESP_ERR_INVALID_ARG;
    }

    /* Write the whole frame back from cache before the DMA can see it. Done
     * here, outside the critical section below, because it is the slow part. */
    size_t framebuffer_size = (size_t)P4BOARD_LCD_WIDTH * P4BOARD_LCD_HEIGHT
        * sizeof(uint16_t);
    esp_err_t result = esp_cache_msync((void *)framebuffer, framebuffer_size,
        ESP_CACHE_MSYNC_FLAG_DIR_C2M | ESP_CACHE_MSYNC_FLAG_UNALIGNED);
    if (result != ESP_OK) {
        return result;
    }

    /* The IDF frame ISR reads cur_fb_index, restarts the DMA on that buffer,
     * and only then reports the completion. If it can interleave with the
     * switch below, an ISR already past its read reports a completion while
     * the old buffer has been restarted -- so two completions are needed.
     *
     * When the ISR runs on this core, doing the drain and the switch with this
     * core's interrupts masked rules that interleaving out: every completion
     * counted afterwards comes from an ISR that read the new index, and it
     * fires exactly when the old buffer's last transfer ended. One completion
     * is then enough, which saves a whole panel frame (16.7 ms) per present.
     * On any other core we keep the conservative two. */
    bool same_core = display_isr_core == (int)xPortGetCoreID();
    present_events_needed = same_core ? 1 : 2;

    /* Once a switch is requested, neither the requested buffer nor the prior
     * scanout buffer is safe to infer after an error or timeout. */
    framebuffer_state_known = false;
    portENTER_CRITICAL(&present_lock);
    /* Start from a clean event count. Any completion posted after this point
     * is retained individually, even if multiple frames finish before the
     * task wakes up. */
    while (xSemaphoreTakeFromISR(frame_complete_semaphore, NULL) == pdTRUE) {
    }
    /* A one-line area: draw_bitmap only selects the buffer and writes back
     * that line, the full write-back already happened above. */
    result = esp_lcd_panel_draw_bitmap(dpi_panel, 0, 0,
        P4BOARD_LCD_WIDTH, 1, framebuffer);
    portEXIT_CRITICAL(&present_lock);
    if (result != ESP_OK) {
        return result;
    }
    pending_framebuffer = framebuffer_index;
    present_pending = true;
    return ESP_OK;
}

esp_err_t p4board_present_finish(void) {
    if (!present_pending) {
        return ESP_OK;
    }
    present_pending = false;
    /* The IDF ISR snapshots cur_fb_index before restarting DMA. An ISR already
     * in flight may report the old selection, so wait for both the selection
     * boundary and the following completion before reusing the previous
     * scanout buffer. The semaphore counts completions since present_begin(),
     * so if the caller did other work in between, these return immediately. */
    esp_err_t result = wait_for_display_event(frame_complete_semaphore, "frame selection");
    if (result == ESP_OK && present_events_needed > 1) {
        result = wait_for_display_event(frame_complete_semaphore,
            "frame-buffer completion");
    }
    if (result == ESP_OK) {
        displayed_framebuffer = pending_framebuffer;
        framebuffer_state_known = true;
    }
    return result;
}

esp_err_t p4board_present_framebuffer(const uint16_t *framebuffer) {
    esp_err_t result = p4board_present_begin(framebuffer);
    if (result != ESP_OK) {
        return result;
    }
    return p4board_present_finish();
}

esp_err_t p4board_display_enabled(bool enabled) {
    if (!panel_io) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 0x29 = display on, 0x28 = display off. */
    return esp_lcd_panel_io_tx_param(panel_io, enabled ? 0x29 : 0x28, NULL, 0);
}

esp_err_t p4board_flush(uint16_t y, uint16_t height) {
    if (lvgl_owns_framebuffers) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!dpi_panel || !drawing_framebuffer || !lcd_framebuffers[0] || !lcd_framebuffers[1]) {
        return ESP_ERR_INVALID_STATE;
    }
    /* A timed-out present may already have switched the panel. Do not derive
     * a writable back buffer from a stale displayed_framebuffer value. */
    if (!framebuffer_state_known) {
        return ESP_ERR_INVALID_STATE;
    }
    if (y >= P4BOARD_LCD_HEIGHT || height > P4BOARD_LCD_HEIGHT - y) {
        return ESP_ERR_INVALID_ARG;
    }
    /* Keep the legacy framebuffer API tear-free too. The caller's off-screen
     * image is copied to the buffer that is not being scanned, then presented
     * as one complete frame so both panel buffers stay consistent. */
    uint8_t next_framebuffer = displayed_framebuffer ^ 1u;
    size_t framebuffer_size = (size_t)P4BOARD_LCD_WIDTH * P4BOARD_LCD_HEIGHT
        * sizeof(uint16_t);
    memcpy(lcd_framebuffers[next_framebuffer], drawing_framebuffer,
        framebuffer_size);
    return p4board_present_framebuffer(lcd_framebuffers[next_framebuffer]);
}
