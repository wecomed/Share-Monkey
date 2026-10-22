"""Display geometry in the same coordinate space as native input events."""
import sys
from .core import Screen


def enable_dpi_awareness():
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def primary_screen(window=None):
    if sys.platform == 'darwin':
        import Quartz
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return Screen(round(bounds.size.width), round(bounds.size.height))
    if sys.platform == 'win32':
        import ctypes
        return Screen(ctypes.windll.user32.GetSystemMetrics(0),
                      ctypes.windll.user32.GetSystemMetrics(1), primary_windows_scale(window))
    raise RuntimeError('仅支持 Mac 和 Windows')


def primary_windows_scale(window=None):
    """Windows cursor coordinates are physical pixels; Quartz uses points."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    if window is not None:
        try:
            get_window_dpi = user32.GetDpiForWindow
            get_window_dpi.argtypes = [wintypes.HWND]
            get_window_dpi.restype = wintypes.UINT
            dpi = get_window_dpi(window)
            if dpi:
                return max(.5, min(8, dpi / 96))
        except (AttributeError, OSError):
            pass
    try:
        monitor_from_point = user32.MonitorFromPoint
        monitor_from_point.argtypes = [wintypes.POINT, wintypes.DWORD]
        monitor_from_point.restype = wintypes.HANDLE
        monitor = monitor_from_point(wintypes.POINT(0, 0), 1)
        get_dpi = ctypes.windll.shcore.GetDpiForMonitor
        get_dpi.argtypes = [wintypes.HANDLE, ctypes.c_int,
                           ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        get_dpi.restype = ctypes.c_long
        x, y = wintypes.UINT(), wintypes.UINT()
        if get_dpi(monitor, 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return max(.5, min(8, x.value / 96))
    except (AttributeError, OSError):
        pass
    try:
        return max(.5, min(8, user32.GetDpiForSystem() / 96))
    except (AttributeError, OSError):
        return 1.0
