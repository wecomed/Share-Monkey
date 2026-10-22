"""A scoped Windows power request: allow display-off, retain shared input.

Does not change a power plan or override explicit Sleep/lid-close. Modern
Standby on battery can end requests; the session still reconnects on resume.
"""
import ctypes
import sys
import time


class _Detailed(ctypes.Structure):
    _fields_ = [('module', ctypes.c_void_p), ('reason_id', ctypes.c_uint32),
                ('count', ctypes.c_uint32), ('strings', ctypes.POINTER(ctypes.c_wchar_p))]


class _Reason(ctypes.Union):
    _fields_ = [('detailed', _Detailed), ('simple', ctypes.c_wchar_p)]


class ReasonContext(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('flags', ctypes.c_uint32), ('reason', _Reason)]


class ConnectionPower:
    def __init__(self, api=None, clock=time.monotonic):
        self.api = api
        self.clock = clock
        self.handle = None
        self.requests = []
        self.last_error = None
        self._next_refresh = 0

    def set_active(self, active):
        if not active:
            self.close()
            return
        if self.clock() < self._next_refresh:
            return
        self.close()
        self._next_refresh = self.clock() + 30
        try:
            if self.api is None:
                self.api = ctypes.WinDLL('kernel32', use_last_error=True)
                self.api.PowerCreateRequest.argtypes = [ctypes.POINTER(ReasonContext)]
                self.api.PowerCreateRequest.restype = ctypes.c_void_p
                for name in ('PowerSetRequest', 'PowerClearRequest'):
                    function = getattr(self.api, name)
                    function.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    function.restype = ctypes.c_int
                self.api.CloseHandle.argtypes = [ctypes.c_void_p]
                self.api.CloseHandle.restype = ctypes.c_int
            reason = ReasonContext(0, 1)
            reason.reason.simple = 'Share Monkey 正在保持键鼠共享连接，允许关闭屏幕'
            handle = self.api.PowerCreateRequest(ctypes.byref(reason))
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise OSError('无法创建连接电源请求')
            self.handle = handle
            # SystemRequired and ExecutionRequired, never DisplayRequired.
            for kind in (1, 3):
                if self.api.PowerSetRequest(handle, kind):
                    self.requests.append(kind)
            if not self.requests:
                raise OSError('系统未接受连接电源请求')
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            self.close(reset_timer=False)

    def close(self, reset_timer=True):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                for kind in self.requests:
                    self.api.PowerClearRequest(handle, kind)
            finally:
                self.requests.clear()
                self.api.CloseHandle(handle)
        if reset_timer:
            self._next_refresh = 0


class NoPowerRequest:
    def set_active(self, active):
        pass

    def close(self):
        pass


def create_connection_power():
    return ConnectionPower() if sys.platform == 'win32' else NoPowerRequest()
