import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sharemouse.display import primary_windows_scale


class WindowsScaleTests(unittest.TestCase):
    def test_prefers_dpi_aware_window_api(self):
        user32 = Mock()
        user32.GetDpiForWindow.return_value = 192
        with patch.object(ctypes, 'windll', SimpleNamespace(user32=user32), create=True):
            self.assertEqual(primary_windows_scale(123), 2)
        user32.GetDpiForWindow.assert_called_once_with(123)
        user32.MonitorFromPoint.assert_not_called()

    def test_reads_effective_dpi_on_primary_monitor(self):
        def get_dpi(monitor, kind, x, y):
            self.assertEqual((monitor, kind), (1234, 0))
            ctypes.cast(x, ctypes.POINTER(ctypes.c_uint)).contents.value = 144
            ctypes.cast(y, ctypes.POINTER(ctypes.c_uint)).contents.value = 144
            return 0
        user32 = Mock()
        user32.MonitorFromPoint.return_value = 1234
        shcore = SimpleNamespace(GetDpiForMonitor=Mock(side_effect=get_dpi))
        with patch.object(ctypes, 'windll', SimpleNamespace(user32=user32, shcore=shcore), create=True):
            self.assertEqual(primary_windows_scale(), 1.5)
        user32.GetDpiForSystem.assert_not_called()

    def test_falls_back_to_system_dpi_then_unscaled(self):
        user32 = Mock()
        user32.GetDpiForSystem.return_value = 192
        dlls = SimpleNamespace(user32=user32, shcore=SimpleNamespace())
        with patch.object(ctypes, 'windll', dlls, create=True):
            self.assertEqual(primary_windows_scale(), 2)
            user32.GetDpiForSystem.side_effect = OSError('unavailable')
            self.assertEqual(primary_windows_scale(), 1)
