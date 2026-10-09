import lvgl as lv
from .progress import Progress

# Square because QR codes are square; the camera driver crops the centre of
# the frame to this size.
PREVIEW_SIZE = 440


class CameraScanProgress(Progress):
    """
    Progress screen for boards that read QR codes with a camera instead of a
    serial scanner: a live preview of what the camera sees, with the message
    and the progress below it. Without the preview the user has nothing to aim
    with, since a camera has no aiming light.
    """

    def __init__(self, title, message, button_text="Cancel"):
        super().__init__(title, message, button_text=button_text)
        import camera
        import display

        self._camera = camera
        self._display = display

        self.spinner.add_flag(lv.obj.FLAG.HIDDEN)
        self.preview = lv.image(self)
        buffer = camera.preview(PREVIEW_SIZE, PREVIEW_SIZE)
        display.image_rgb565(self.preview, buffer, PREVIEW_SIZE, PREVIEW_SIZE)
        self.preview.align_to(self.title, lv.ALIGN.OUT_BOTTOM_MID, 0, 20)

        self.page.align_to(self.preview, lv.ALIGN.OUT_BOTTOM_MID, 0, 15)
        self.message.align(lv.ALIGN.TOP_MID, 0, 0)
        self.progress.align_to(self.message, lv.ALIGN.OUT_BOTTOM_MID, 0, 15)

        self._frames = camera.preview_frames()
        self.add_event_cb(self._on_delete, lv.EVENT.DELETE, None)

    def set_progress(self, val):
        super().set_progress(val)
        # Redraw only when the camera produced a new frame.
        frames = self._camera.preview_frames()
        if frames != self._frames:
            self._frames = frames
            self._display.image_changed(self.preview)

    def _on_delete(self, event):
        self._camera.preview_off()
