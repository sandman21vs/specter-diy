# Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3-C
#
# O ESP-IDF gera imagem para silicio v0.x/v1.x OU v3.x, nunca os dois: com
# ESP32P4_SELECTS_REV_LESS_V3 o teto e v1.99, sem ele o piso e v3.0. A placa
# testada aqui e v1.3 (chip_revision 103), mas placas mais novas ja chegam com
# v3.x (issue #21). Por isso o board tem dois builds:
#
#   padrao         sdkconfig.p4_pre_rev3 entra e define REV_MIN_0 (v0.0-v1.99)
#   -DP4_REV3=1    o fragmento fica de fora e vale o REV_MIN_300 do
#                  sdkconfig.p4 (v3.0-v3.99), mais sdkconfig.rev3 deste board
#
# tools/build.sh escolhe pelo P4_REV3=1 do ambiente.
#
# Sem variante de WiFi de proposito: a placa carrega um ESP32-C6, mas o alvo e
# um dispositivo airgapped e o radio fica fora do build.

set(IDF_TARGET esp32p4)

# O sdkconfig nao aceita caminho relativo ao board, entao o da tabela de
# particoes e gerado aqui, absoluto, como mais um fragmento de defaults.
set(_P4_PARTITIONS_SDKCONFIG ${CMAKE_BINARY_DIR}/sdkconfig.partitions)
file(WRITE ${_P4_PARTITIONS_SDKCONFIG}
    "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=\"${CMAKE_CURRENT_LIST_DIR}/partitions.csv\"\n")

if(P4_REV3)
    set(_P4_REV_SDKCONFIG ${CMAKE_CURRENT_LIST_DIR}/sdkconfig.rev3)
else()
    set(_P4_REV_SDKCONFIG boards/sdkconfig.p4_pre_rev3)
endif()

set(SDKCONFIG_DEFAULTS
    boards/sdkconfig.base
    boards/sdkconfig.p4
    ${_P4_REV_SDKCONFIG}
    ${CMAKE_CURRENT_LIST_DIR}/sdkconfig.board
    ${_P4_PARTITIONS_SDKCONFIG}
)

set(MICROPY_FROZEN_MANIFEST ${CMAKE_CURRENT_LIST_DIR}/manifest.py)
