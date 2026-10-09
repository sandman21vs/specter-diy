import lvgl as lv


class Modal(lv.obj):
    """mbox with semi-transparent background"""

    DIALOG_WIDTH = 400
    DIALOG_MARGIN = 24
    DIALOG_MIN_HEIGHT = 160

    def __init__(self, parent, *args, **kwargs):
        # Create a base object for the modal background
        super().__init__(parent, *args, **kwargs)

        # Create a full-screen background
        self.modal_style = lv.style_t()
        self.modal_style.init()
        self.modal_style.set_bg_color(lv.color_hex(0x000000))
        self.modal_style.set_bg_opa(lv.OPA._50)
        self.modal_style.set_border_width(0)
        self.modal_style.set_outline_width(0)
        self.modal_style.set_shadow_width(0)
        self.modal_style.set_radius(0)
        self.modal_style.set_pad_all(0)
        self.add_style(self.modal_style, 0)
        self.remove_flag(lv.obj.FLAG.SCROLLABLE)
        self.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        self.set_pos(0, 0)
        self.set_size(parent.get_width(), parent.get_height())

        self.mbox_style = lv.style_t()
        self.mbox_style.init()
        self.mbox_style.set_bg_color(lv.color_hex(0x202020))
        self.mbox_style.set_bg_opa(255)
        self.mbox_style.set_border_width(0)
        self.mbox_style.set_outline_width(0)
        self.mbox_style.set_shadow_width(0)
        self.mbox_style.set_pad_all(20)
        self.mbox_style.set_radius(8)

        self.mbox = lv.obj(self)
        self.mbox.add_style(self.mbox_style, 0)
        self.mbox.remove_flag(lv.obj.FLAG.SCROLLABLE)
        self.mbox.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        self.label = lv.label(self.mbox)
        self.label.set_long_mode(lv.label.LONG_MODE.WRAP)
        self.label.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        self.label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)

    def set_text(self, text):
        # Newly created objects have not resolved their requested size yet.
        # Resolve the backdrop before using its bounds to constrain the dialog.
        self.update_layout()
        parent_width = self.get_width()
        parent_height = self.get_height()
        dialog_width = min(self.DIALOG_WIDTH, parent_width - 2 * self.DIALOG_MARGIN)
        dialog_width = max(1, dialog_width)

        # Set the wrapping width before asking LVGL for the text height. LVGL
        # defers layout, so get_height() immediately after set_text() can still
        # return the dimensions from the previous message.
        self.mbox.set_width(dialog_width)
        self.label.set_width(max(1, dialog_width - 40))
        self.label.set_text(text)
        self.label.update_layout()

        content_height = self.label.get_height()
        max_dialog_height = max(1, parent_height - 2 * self.DIALOG_MARGIN)
        dialog_height = min(max(content_height + 40, self.DIALOG_MIN_HEIGHT),
                            max_dialog_height)
        self.mbox.set_height(dialog_height)

        if content_height + 40 > max_dialog_height:
            # Long messages remain reachable in a bounded dialog instead of
            # being drawn off-screen.
            self.mbox.add_flag(lv.obj.FLAG.SCROLLABLE)
            self.mbox.set_scroll_dir(lv.DIR.VER)
            self.label.align(lv.ALIGN.TOP_MID, 0, 0)
        else:
            self.mbox.remove_flag(lv.obj.FLAG.SCROLLABLE)
            self.label.center()

        self.mbox.align(lv.ALIGN.CENTER, 0, 0)
