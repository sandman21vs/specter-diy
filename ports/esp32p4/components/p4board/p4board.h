/**
 * @file p4board.h
 * @brief Board support surface exposed to the MicroPython module.
 */

#ifndef P4BOARD_H
#define P4BOARD_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* Display */
esp_err_t p4board_display_init(void);
void p4board_display_deinit(void);
esp_err_t p4board_backlight(uint32_t percent);
esp_err_t p4board_display_enabled(bool enabled);
uint16_t *p4board_framebuffer(void);
esp_err_t p4board_framebuffers(uint16_t **fb0, uint16_t **fb1);
esp_err_t p4board_present_framebuffer(const uint16_t *framebuffer);
/* Assincrono: begin pede a troca e volta; finish espera ate o buffer anterior
 * sair do scanout. Entre os dois, o buffer anterior NAO pode ser escrito. */
esp_err_t p4board_present_begin(const uint16_t *framebuffer);
esp_err_t p4board_present_finish(void);
esp_err_t p4board_flush(uint16_t y, uint16_t height);

/* Touch */
typedef struct {
    uint16_t x;
    uint16_t y;
    uint16_t size;
    uint8_t id;
} p4board_touch_point_t;

esp_err_t p4board_touch_init(void);
void p4board_touch_deinit(void);
esp_err_t p4board_touch_read(p4board_touch_point_t *points, uint8_t capacity,
    uint8_t *count);
uint8_t p4board_touch_address(void);

/**
 * @brief I2C bus shared by the GT911 touch and the camera SCCB.
 *
 * On this board both sit on GPIO 8/7. Creating a second master bus on the same
 * pins fails, so the camera must reuse this handle rather than open its own.
 * Returns NULL until p4board_touch_init() has run.
 */
void *p4board_i2c_bus(void);

/* Radio co-processor */
esp_err_t p4board_radio_off(void);

#endif  // P4BOARD_H
