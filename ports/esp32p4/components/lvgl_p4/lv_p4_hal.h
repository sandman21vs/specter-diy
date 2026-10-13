/**
 * @file lv_p4_hal.h
 * @brief LVGL display and input hooks for the Waveshare ESP32-P4 4.3-C.
 */

#ifndef LV_P4_HAL_H
#define LV_P4_HAL_H

#include <stdbool.h>
#include <stdint.h>

void tft_init(void);
void touchpad_init(void);

/* Contadores de desempenho, acumulados desde o ultimo reset. */
typedef struct {
    uint32_t frames;        /* quadros apresentados ao painel */
    uint32_t areas;         /* areas redesenhadas (flushes) */
    uint64_t wait_us;       /* tempo esperando o painel liberar o buffer */
    uint64_t present_us;    /* write-back do cache + pedido de troca */
} lv_p4_stats_t;

void lv_p4_stats(lv_p4_stats_t *out, bool reset);

#endif  // LV_P4_HAL_H
