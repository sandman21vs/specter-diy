/**
 * @file moddisplay.c
 * @brief Modulos `display` e `lvgl` do MicroPython para a Waveshare 4.3-C.
 *
 * Modelado a partir de f469-disco/usermods/udisplay_f469/display.c. O app do
 * Specter usa apenas display.init() e display.update(); o resto existe para
 * paridade com a Discovery.
 *
 * Como no original, este arquivo inclui lv_mpy.c -- o binding LVGL gerado --
 * e registra os dois modulos.
 */

#include "lv_p4_hal.h"
#include "lvgl.h"
#include "esp_timer.h"
#include "p4board.h"
#include "py/builtin.h"
#include "py/obj.h"
#include "py/runtime.h"

/* O binding vem ANTES do codigo deste arquivo de proposito. O makeqstrdefs.py
 * divide a saida do pre-processador por arquivo de origem e, ao voltar para
 * moddisplay.c depois de um #include de outro .c, sobrescreve o que ja tinha
 * coletado. Com o lv_mpy.c no fim, todos os MP_QSTR_ do modulo display se
 * perdiam, e so os nomes que por acaso existiam em outros modulos funcionavam. */
/* Shim de compatibilidade para o binding gerado.
 *
 * lv_mpy.c chama mp_obj_int_to_bytes_impl(), removida do py/objint.h do
 * MicroPython. O binding e codigo gerado por lv_binding_micropython e vive num
 * submodulo, entao a correcao fica aqui em vez de editar o gerado. Mesma
 * quebra de API que atingiu o secp256k1-embedded; ver
 * reports/lv-binding-micropython-master-api.md.
 *
 * A funcao antiga tratava apenas long ints; mp_obj_int_to_bytes trata os dois
 * casos, entao e um superconjunto. overflow_check fica off para preservar o
 * comportamento anterior.
 */
#include "py/objint.h"

static inline void mp_obj_int_to_bytes_impl(mp_obj_t self_in, bool big_endian,
    size_t len, byte *buf) {
    mp_obj_int_to_bytes(self_in, len, buf, big_endian, false, false);
}

#include "lv_mpy.c"

static uint32_t update_calls;
static uint64_t update_us;

static mp_obj_t display_init(void) {
    /* O app chama display.init() duas vezes (main.py e gui/core.py), como no
     * F469, onde a segunda chamada nao faz nada. Aqui cada chamada criava outro
     * lv_display e outro indev: duas telas renderizadas a cada quadro (UI
     * lenta) e dois leitores do mesmo touch (cada toque contava duas vezes). */
    static bool initialized = false;
    if (initialized) {
        return mp_const_none;
    }
    lv_init();
    tft_init();
    touchpad_init();
    initialized = true;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(display_init_obj, display_init);

static mp_obj_t display_update(mp_obj_t dt_obj) {
    /* O relogio do LVGL e o esp_timer (lv_tick_set_cb em tft_init); dt fica
     * so pela compatibilidade com a API do F469. */
    (void)dt_obj;
    int64_t started = esp_timer_get_time();
    lv_task_handler();
    update_us += (uint64_t)(esp_timer_get_time() - started);
    update_calls++;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(display_update_obj, display_update);

/* display.stats(reset=True) -> (updates, update_us, frames, areas, wait_us,
 * present_us). update_us inclui wait_us e present_us; o resto e desenho e
 * logica do LVGL. Diagnostico apenas. */
static mp_obj_t display_stats(size_t n_args, const mp_obj_t *args) {
    bool reset = n_args == 0 || mp_obj_is_true(args[0]);
    lv_p4_stats_t hal;
    lv_p4_stats(&hal, reset);
    mp_obj_t items[6] = {
        mp_obj_new_int_from_uint(update_calls),
        mp_obj_new_int_from_ull(update_us),
        mp_obj_new_int_from_uint(hal.frames),
        mp_obj_new_int_from_uint(hal.areas),
        mp_obj_new_int_from_ull(hal.wait_us),
        mp_obj_new_int_from_ull(hal.present_us),
    };
    if (reset) {
        update_calls = 0;
        update_us = 0;
    }
    return mp_obj_new_tuple(6, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(display_stats_obj, 0, 1, display_stats);

/* udisplay.image_rgb565(img, buf, w, h): mostra num lv.image um buffer RGB565
 * que muda de conteudo -- a previa da camera. O descritor e montado aqui
 * porque o lv_image_header_t tem campos de bits, que o binding nao expoe bem.
 *
 * Um descritor so, para uma imagem por vez. O buffer tem de continuar valido
 * enquanto a imagem existir; o da camera nunca e liberado. */
static lv_image_dsc_t rgb565_image;

static mp_obj_t display_image_rgb565(size_t n_args, const mp_obj_t *args) {
    lv_obj_t *image = mp_to_lv(args[0]);
    mp_buffer_info_t info;
    mp_get_buffer_raise(args[1], &info, MP_BUFFER_READ);
    mp_int_t width = mp_obj_get_int(args[2]);
    mp_int_t height = mp_obj_get_int(args[3]);
    if (width <= 0 || height <= 0 || (size_t)(width * height * 2) > info.len) {
        mp_raise_ValueError(MP_ERROR_TEXT("buffer smaller than image"));
    }
    lv_image_cache_drop(&rgb565_image);
    rgb565_image = (lv_image_dsc_t){0};
    rgb565_image.header.magic = LV_IMAGE_HEADER_MAGIC;
    rgb565_image.header.cf = LV_COLOR_FORMAT_RGB565;
    rgb565_image.header.w = (uint32_t)width;
    rgb565_image.header.h = (uint32_t)height;
    rgb565_image.header.stride = (uint32_t)width * 2u;
    rgb565_image.data_size = (uint32_t)(width * height * 2);
    rgb565_image.data = info.buf;
    lv_image_set_src(image, &rgb565_image);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(display_image_rgb565_obj, 4, 4, display_image_rgb565);

/* udisplay.image_changed(img): o conteudo do buffer mudou; redesenhar. */
static mp_obj_t display_image_changed(mp_obj_t image_obj) {
    lv_image_cache_drop(&rgb565_image);
    lv_obj_invalidate(mp_to_lv(image_obj));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(display_image_changed_obj, display_image_changed);

static mp_obj_t display_on(void) {
    p4board_display_enabled(true);
    p4board_backlight(100);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(display_on_obj, display_on);

static mp_obj_t display_off(void) {
    p4board_backlight(0);
    p4board_display_enabled(false);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(display_off_obj, display_off);

static mp_obj_t display_backlight(mp_obj_t percent_obj) {
    mp_int_t percent = mp_obj_get_int(percent_obj);
    if (percent < 0 || percent > 100) {
        mp_raise_ValueError(MP_ERROR_TEXT("backlight must be 0..100"));
    }
    p4board_backlight((uint32_t)percent);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(display_backlight_obj, display_backlight);

static mp_obj_t display_set_rotation(mp_obj_t rot_obj) {
    /* O painel e fixo em retrato 480x800 e o driver DPI nao gira o
     * framebuffer. Aceitar 0 mantem compatibilidade com o app; qualquer outra
     * coisa e recusada em vez de silenciosamente ignorada. */
    if (mp_obj_get_int(rot_obj) != 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("only rotation 0 is supported"));
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(display_set_rotation_obj, display_set_rotation);

static const mp_rom_map_elem_t display_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),     MP_ROM_QSTR(MP_QSTR_udisplay) },
    { MP_ROM_QSTR(MP_QSTR_init),         MP_ROM_PTR(&display_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_update),       MP_ROM_PTR(&display_update_obj) },
    { MP_ROM_QSTR(MP_QSTR_stats),        MP_ROM_PTR(&display_stats_obj) },
    { MP_ROM_QSTR(MP_QSTR_image_rgb565), MP_ROM_PTR(&display_image_rgb565_obj) },
    { MP_ROM_QSTR(MP_QSTR_image_changed), MP_ROM_PTR(&display_image_changed_obj) },
    { MP_ROM_QSTR(MP_QSTR_on),           MP_ROM_PTR(&display_on_obj) },
    { MP_ROM_QSTR(MP_QSTR_off),          MP_ROM_PTR(&display_off_obj) },
    { MP_ROM_QSTR(MP_QSTR_backlight),    MP_ROM_PTR(&display_backlight_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_rotation), MP_ROM_PTR(&display_set_rotation_obj) },
};
static MP_DEFINE_CONST_DICT(display_module_globals, display_module_globals_table);

const mp_obj_module_t display_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&display_module_globals,
};


MP_REGISTER_MODULE(MP_QSTR_udisplay, display_user_cmodule);
MP_REGISTER_MODULE(MP_QSTR_lvgl, mp_module_lvgl);
