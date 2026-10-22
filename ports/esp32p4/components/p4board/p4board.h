/**
 * @file p4board.h
 * @brief Board support surface exposed to the MicroPython module.
 */

#ifndef P4BOARD_H
#define P4BOARD_H

#include <stdbool.h>
#include <stddef.h>
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

/**
 * @brief Raw transfers on the board I2C bus for an external module.
 *
 * For devices plugged on GPIO 8/7 next to the touch, such as an NFC reader.
 * They fail with ESP_ERR_INVALID_STATE until p4board_touch_init() has run.
 * p4board_i2c_probe() only checks for an ACK and logs nothing when the address
 * is empty, so it is the one to use for "is the module there".
 */
esp_err_t p4board_i2c_probe(uint8_t address);
esp_err_t p4board_i2c_write(uint8_t address, const uint8_t *data, size_t size);
esp_err_t p4board_i2c_read(uint8_t address, uint8_t *data, size_t size);
void p4board_i2c_release(void);

/* Radio co-processor */
esp_err_t p4board_radio_off(void);

#endif  // P4BOARD_H
