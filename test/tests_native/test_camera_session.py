"""A camera scan starts from a clean session.

The camera keeps streaming between scans, so without a flush the first scan
after a pause decodes a frame from the previous session (PR #16). These tests
cover the Python side of the fix: CameraUART.flush() in the ESP32-P4 pyb shim,
and QRHost calling it whenever a camera scan starts. The frame queue itself is
C code that only runs on the board.

    python3 -m unittest discover -s test/tests_native -p "test_camera_session.py"
"""
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")


def _load(name, path, modules):
    """Imports a file under `name` with `modules` standing in for its imports"""
    with mock.patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _load_pyb():
    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value
    return _load(
        "esp32p4_pyb",
        os.path.join(ROOT, "ports", "esp32p4", "lib", "pyb.py"),
        {"machine": mock.MagicMock(), "micropython": micropython},
    )


def _load_qr():
    """hosts/qr.py with the GUI, storage and the other hosts stubbed out"""
    core = types.ModuleType("hosts.core")

    class Host:
        def __init__(self, *args, **kwargs):
            pass

    class HostError(Exception):
        pass

    core.Host, core.HostError = Host, HostError
    package = types.ModuleType("hosts")
    package.__path__ = [os.path.join(ROOT, "src", "hosts")]
    package.core = core

    decorators = types.ModuleType("gui.decorators")
    decorators.on_release = lambda func: func
    stubs = {
        name: mock.MagicMock()
        for name in (
            "pyb", "platform", "lvgl", "gui", "gui.common", "gui.screens",
            "gui.screens.settings", "helpers", "microur", "microur.decoder",
            "microur.util",
        )
    }
    stubs.update({"hosts": package, "hosts.core": core, "gui.decorators": decorators})
    return _load("hosts.qr", os.path.join(ROOT, "src", "hosts", "qr.py"), stubs)


pyb = _load_pyb()
qr = _load_qr()


class FakeCamera(types.ModuleType):
    """The `camera` module: records calls, scan() returns what it is given"""

    def __init__(self, scans=()):
        super().__init__("camera")
        self.calls = []
        self.scans = list(scans)

    def init(self):
        self.calls.append("init")

    def scan(self):
        self.calls.append("scan")
        return self.scans.pop(0) if self.scans else None

    def flush(self):
        self.calls.append("flush")

    def deinit(self):
        self.calls.append("deinit")


class CameraUARTFlushTest(unittest.TestCase):
    def test_drops_a_result_not_read_yet(self):
        camera = FakeCamera(scans=[b"old seed"])
        uart = pyb.CameraUART()
        with mock.patch.dict(sys.modules, {"camera": camera}):
            self.assertEqual(uart.any(), len(b"old seed\r"))
            uart.flush()
            self.assertEqual(uart.read(), None)
            # the next scan goes back to the camera, nothing comes from memory
            self.assertEqual(uart.any(), 0)
        self.assertEqual(camera.calls, ["init", "scan", "flush", "scan"])

    def test_flushes_the_camera_once_started(self):
        camera = FakeCamera()
        uart = pyb.CameraUART()
        with mock.patch.dict(sys.modules, {"camera": camera}):
            uart.any()
            uart.flush()
        self.assertEqual(camera.calls, ["init", "scan", "flush"])

    def test_does_not_start_the_camera(self):
        camera = FakeCamera()
        uart = pyb.CameraUART()
        with mock.patch.dict(sys.modules, {"camera": camera}):
            uart.flush()
        self.assertEqual(camera.calls, [])

    def test_after_deinit_the_camera_is_left_alone(self):
        camera = FakeCamera()
        uart = pyb.CameraUART()
        with mock.patch.dict(sys.modules, {"camera": camera}):
            uart.any()
            uart.deinit()
            uart.flush()
        self.assertEqual(camera.calls, ["init", "scan", "deinit"])

    def test_a_camera_error_is_not_raised(self):
        camera = FakeCamera()

        def broken():
            raise OSError("camera gone")

        camera.flush = broken
        uart = pyb.CameraUART()
        with mock.patch.dict(sys.modules, {"camera": camera}):
            uart.any()
            uart._pending = b"old\r"
            uart.flush()
        self.assertEqual(uart.read(), None)


class StartScannerTest(unittest.TestCase):
    """QRHost._start_scanner() on a camera and on a serial scanner"""

    def host(self, uart, trigger=None):
        host = qr.QRHost.__new__(qr.QRHost)
        host.uart = uart
        host.trigger = trigger
        host._start_scan = mock.Mock()
        return host

    def test_camera_scan_starts_clean(self):
        uart = mock.Mock(is_camera=True)
        host = self.host(uart)
        host._start_scanner()
        # read() empties what came in, then flush() drops the camera's frames
        self.assertEqual([call[0] for call in uart.method_calls], ["read", "flush"])
        host._start_scan.assert_not_called()

    def test_camera_scans_in_a_row_each_start_clean(self):
        camera = FakeCamera(scans=[b"first"])
        uart = pyb.CameraUART()
        host = self.host(uart)
        with mock.patch.dict(sys.modules, {"camera": camera}):
            host._start_scanner()
            self.assertTrue(uart.any())
            # cancelled before the result was read
            host._start_scanner()
            self.assertEqual(uart.any(), 0)
        self.assertEqual(camera.calls.count("flush"), 1)  # the first had no camera yet

    def test_serial_scanner_is_not_flushed(self):
        uart = mock.Mock(spec=["read", "write", "any"])
        host = self.host(uart)
        host._start_scanner()
        host._start_scan.assert_called_once_with(1)

    def test_serial_scanner_with_trigger(self):
        uart = mock.Mock(spec=["read", "write", "any"])
        trigger = mock.Mock()
        host = self.host(uart, trigger=trigger)
        host._start_scanner()
        trigger.off.assert_called_once_with()
        host._start_scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
