import subprocess
import sys
import unittest
from unittest.mock import patch

from sharemouse.windows_pointer import WindowsPointer


class FakeUser32:
    def __init__(self):
        self.calls = []
        self.position_success = True
        self.dpi_success = True
        self.restore_success = True

    def SetThreadDpiAwarenessContext(self, value):
        self.calls.append(('dpi', value))
        return (123 if self.dpi_success else 0) if value == -4 else (456 if self.restore_success else 0)

    def SetCursorPos(self, x, y):
        self.calls.append(('position', x, y))
        return self.position_success

    def last_error(self):
        return 5


class WindowsPointerTests(unittest.TestCase):
    def test_import_does_not_load_user32_or_move_mouse(self):
        subprocess.run([sys.executable, '-c',
                        'import sys; import sharemouse.windows_pointer; '
                        'assert "ctypes" not in sys.modules'], check=True)

    def test_success_uses_physical_dpi_context_and_restores_original(self):
        api = FakeUser32()
        pointer = WindowsPointer(api)
        self.assertEqual(pointer.move_to((100.6, 200.2)), (101, 200))
        self.assertEqual(api.calls, [('dpi', -4), ('position', 101, 200), ('dpi', 123)])

    def test_setcursorpos_false_raises_instead_of_reporting_processed(self):
        api = FakeUser32()
        api.position_success = False
        processed = []
        try:
            processed.append(WindowsPointer(api).move_to((10, 20)))
        except OSError as exc:
            self.assertEqual(exc.errno, 5)
            self.assertIn('SetCursorPos', str(exc))
        else:
            self.fail('failed SetCursorPos must not be treated as processed')
        self.assertFalse(processed)
        self.assertEqual(api.calls[-1], ('dpi', 123))

    def test_dpi_failure_does_not_attempt_position_and_restore_failure_is_reported(self):
        api = FakeUser32()
        api.dpi_success = False
        with self.assertRaisesRegex(OSError, 'SetThreadDpiAwarenessContext'):
            WindowsPointer(api).move_to((10, 20))
        self.assertEqual(api.calls, [('dpi', -4)])
        api = FakeUser32()
        api.restore_success = False
        with self.assertRaisesRegex(OSError, 'DPI'):
            WindowsPointer(api).move_to((10, 20))

    def test_invalid_positions_never_call_native_api(self):
        api = FakeUser32()
        for position in ((float('nan'), 0), (0, float('inf')), (2 ** 31, 0),
                         (0, -2 ** 31 - 1), None, (1,), ('bad', 2)):
            with self.subTest(position=position), self.assertRaises(ValueError):
                WindowsPointer(api).move_to(position)
        self.assertFalse(api.calls)

    def test_legacy_api_without_thread_dpi_function_still_checks_setcursorpos(self):
        class MinimalApi:
            def SetCursorPos(self, x, y):
                return False

        with self.assertRaisesRegex(OSError, 'SetCursorPos'):
            WindowsPointer(MinimalApi()).move_to((10, 20))


if __name__ == '__main__':
    unittest.main()
