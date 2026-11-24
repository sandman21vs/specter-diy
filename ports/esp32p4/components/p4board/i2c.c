/**
 * @file i2c.c
 * @brief Raw access to the board I2C bus for external modules.
 *
 * The board has one I2C bus on GPIO 8/7, already owned by the GT911 touch and
 * shared with the camera SCCB. An external module on the same pins -- today the
 * M5Stack RFID Unit 2 (WS1850S, address 0x28) -- cannot open a second master
 * on them: machine.I2C would fail with the port already acquired. So the bus is
 * lent out here instead, one device address at a time.
 *
 * Only one external device handle is kept. Asking for a different address
 * replaces it, which is all a single reader needs and keeps the driver from
 * accumulating handles nobody releases.
 */

#include "driver/i2c_master.h"
#include "p4board.h"

#define EXTERNAL_I2C_FREQUENCY_HZ 400000U
#define EXTERNAL_I2C_TIMEOUT_MS   100

static i2c_master_bus_handle_t device_bus;
static i2c_master_dev_handle_t device;
static uint8_t device_address;

static esp_err_t select_device(uint8_t address) {
    i2c_master_bus_handle_t bus = p4board_i2c_bus();
    if (!bus) {
        /* The touch deinit deleted the bus and took our handle with it. */
        device = NULL;
        device_bus = NULL;
        return ESP_ERR_INVALID_STATE;
    }
    if (device && device_bus == bus && device_address == address) {
        return ESP_OK;
    }
    if (device && device_bus == bus) {
        i2c_master_bus_rm_device(device);
    }
    device = NULL;

    const i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = EXTERNAL_I2C_FREQUENCY_HZ,
    };
    esp_err_t result = i2c_master_bus_add_device(bus, &config, &device);
    if (result != ESP_OK) {
        device = NULL;
        return result;
    }
    device_bus = bus;
    device_address = address;
    return ESP_OK;
}

void p4board_i2c_release(void) {
    if (device && device_bus && device_bus == p4board_i2c_bus()) {
        i2c_master_bus_rm_device(device);
    }
    device = NULL;
    device_bus = NULL;
    device_address = 0;
}

esp_err_t p4board_i2c_probe(uint8_t address) {
    i2c_master_bus_handle_t bus = p4board_i2c_bus();
    if (!bus) {
        return ESP_ERR_INVALID_STATE;
    }
    return i2c_master_probe(bus, address, EXTERNAL_I2C_TIMEOUT_MS);
}

esp_err_t p4board_i2c_write(uint8_t address, const uint8_t *data, size_t size) {
    esp_err_t result = select_device(address);
    if (result != ESP_OK) {
        return result;
    }
    return i2c_master_transmit(device, data, size, EXTERNAL_I2C_TIMEOUT_MS);
}

esp_err_t p4board_i2c_read(uint8_t address, uint8_t *data, size_t size) {
    esp_err_t result = select_device(address);
    if (result != ESP_OK) {
        return result;
    }
    return i2c_master_receive(device, data, size, EXTERNAL_I2C_TIMEOUT_MS);
}
