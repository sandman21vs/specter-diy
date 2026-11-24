idf_component_register(
    SRCS "p4camera.c"
    INCLUDE_DIRS "."
    REQUIRES esp_video esp_driver_i2c esp_timer k_quirc
)
