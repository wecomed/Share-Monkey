"""Checked Windows cursor positioning; importing this module never moves input.

SetCursorPos success means Windows accepted the cursor position, not that a
physical display has presented it. The caller sends its processed ACK only after
this method succeeds and must update its position cache afterwards.
"""
import math
import sys


def _user32():
    if sys.platform != 'win32':
        raise OSError('Windows 指针接口只能在 Windows 上启动')
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    api.SetCursorPos.restype = wintypes.BOOL
    context = getattr(api, 'SetThreadDpiAwarenessContext', None)
    if context is not None:
        context.argtypes = (ctypes.c_void_p,)
        context.restype = ctypes.c_void_p
    return api, ctypes.get_last_error


class WindowsPointer:
    def __init__(self, api=None):
        if api is None:
            self.api, self._last_error = _user32()
        else:
            self.api = api
            self._last_error = getattr(api, 'last_error', lambda: 0)
        self._dpi = getattr(self.api, 'SetThreadDpiAwarenessContext', None)

    def _failure(self, operation):
        code = self._last_error()
        return OSError(code, f'Windows 指针操作失败：{operation}（Win32 {code}）')

    def move_to(self, position):
        try:
            raw_x, raw_y = position
            x, y = float(raw_x), float(raw_y)
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError
            x, y = round(x), round(y)
            if not all(-(2 ** 31) <= value < 2 ** 31 for value in (x, y)):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Windows 鼠标位置无效') from exc
        previous = None
        if self._dpi is not None:
            # PER_MONITOR_AWARE_V2 applies physical coordinates on the engine's
            # worker even if a GUI library changed its thread's DPI context.
            previous = self._dpi(-4)
            if not previous:
                raise self._failure('SetThreadDpiAwarenessContext')
        succeeded = False
        try:
            if not self.api.SetCursorPos(x, y):
                raise self._failure('SetCursorPos')
            succeeded = True
        finally:
            if previous is not None and not self._dpi(previous) and succeeded:
                raise self._failure('恢复线程 DPI 状态')
        return x, y

    __call__ = move_to
