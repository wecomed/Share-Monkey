"""Windows cursor lease process; every exit path restores the system cursor.

The public Magnification API affects the cursor across input threads without
replacing any system cursor resource. A separate process owns the hide lease so
parent crashes, EOF and missed heartbeats have a bounded recovery path.
"""
import argparse
import ctypes
import queue
import sys
import threading
import time


class MagnificationApi:
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('Cursor magnification API is Windows-only')
        from ctypes import wintypes as W
        self._point = W.POINT
        self.mag = ctypes.WinDLL('Magnification', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.mag.MagInitialize.argtypes = []
        self.mag.MagInitialize.restype = W.BOOL
        self.mag.MagUninitialize.argtypes = []
        self.mag.MagUninitialize.restype = W.BOOL
        self.mag.MagShowSystemCursor.argtypes = [W.BOOL]
        self.mag.MagShowSystemCursor.restype = W.BOOL
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
        self.user.GetCursorPos.restype = W.BOOL
        self.initialized = bool(self.mag.MagInitialize())
        if not self.initialized:
            raise OSError('无法初始化 Windows 光标服务，请使用 64 位 Windows 客户端')

    def show(self, visible):
        if not self.mag.MagShowSystemCursor(bool(visible)):
            raise OSError('Windows 无法切换系统光标显示状态')

    def position(self):
        point = self._point()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise OSError('Windows 无法读取光标位置')
        return point.x, point.y

    def close(self):
        if self.initialized:
            try:
                self.show(True)
            finally:
                self.mag.MagUninitialize()
                self.initialized = False


class ParentProcess:
    def __init__(self, pid):
        from ctypes import wintypes as W
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self.kernel.WaitForSingleObject.restype = W.DWORD
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CloseHandle.restype = W.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not self.handle:
            raise OSError('Cannot monitor the Share Monkey parent process')

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0x102  # WAIT_TIMEOUT

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsPipes:
    """Use inherited OS handles even when pythonw sets sys.stdout to None."""
    def __init__(self):
        from ctypes import wintypes as W
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.GetStdHandle.argtypes = [W.DWORD]
        self.kernel.GetStdHandle.restype = W.HANDLE
        self.kernel.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD), W.LPVOID]
        self.kernel.ReadFile.restype = W.BOOL
        self.kernel.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, ctypes.POINTER(W.DWORD), W.LPVOID]
        self.kernel.WriteFile.restype = W.BOOL
        self.input = self.kernel.GetStdHandle(0xFFFFFFF6)  # STD_INPUT_HANDLE
        self.output = self.kernel.GetStdHandle(0xFFFFFFF5)  # STD_OUTPUT_HANDLE
        invalid = ctypes.c_void_p(-1).value
        if self.input in (None, invalid) or self.output in (None, invalid):
            raise OSError('Cursor guard requires private inherited pipes')
        self._count = W.DWORD

    def read(self):
        data = ctypes.create_string_buffer(256)
        count = self._count()
        if not self.kernel.ReadFile(self.input, data, 256, ctypes.byref(count), None):
            return b''
        return data.raw[:count.value]

    def write(self, line):
        data = (line + '\n').encode('ascii', 'replace')
        count = self._count()
        if not self.kernel.WriteFile(self.output, data, len(data), ctypes.byref(count), None) or count.value != len(data):
            raise BrokenPipeError('Cursor guard parent pipe closed')


class GuardSession:
    """Pure state machine, tested with a fake API and clock; no native side effects."""
    def __init__(self, api, parent_alive, reply, clock=time.monotonic, lease=1.):
        self.api, self.parent_alive, self.reply = api, parent_alive, reply
        self.clock, self.lease = clock, lease
        self.hidden = False
        self.watch_position = False
        self.anchor = None
        self.heartbeat = clock()

    def command(self, line):
        parts = line.split()
        if not parts or len(parts) > 3 or not self.parent_alive():
            return False
        command = parts[0]
        if command == 'P' and len(parts) == 2:
            self.heartbeat = self.clock()
            self.reply('OK ' + parts[1] + ' P')
        elif command == 'H' and len(parts) == 3 and parts[2] in ('0', '1'):
            self.heartbeat = self.clock()
            self.anchor = self.api.position() if parts[2] == '1' else None
            self.watch_position = parts[2] == '1'
            self.api.show(False)
            self.hidden = True
            self.reply('OK ' + parts[1] + ' H')
        elif command in ('S', 'Q') and len(parts) == 2:
            self.api.show(True)
            self.hidden = False
            self.reply('OK ' + parts[1] + ' ' + command)
            if command == 'Q':
                return False
        else:
            return False
        return True

    def tick(self):
        if not self.parent_alive():
            return False
        if self.hidden and self.clock() - self.heartbeat > self.lease:
            self.api.show(True)
            self.hidden = False
            self.reply('EXPIRED')
        elif self.hidden and self.watch_position and self.api.position() != self.anchor:
            self.api.show(True)
            self.hidden = False
            self.reply('TAKEOVER')
        return True

    def close(self):
        self.api.show(True)
        self.hidden = False


def run_guard(api, parent_alive, reader, reply, clock=time.monotonic, lease=1.):
    commands = queue.Queue(maxsize=64)
    stopped = threading.Event()

    def receive():
        buffered = b''
        try:
            while not stopped.is_set():
                part = reader()
                if not part:
                    break
                buffered += part
                if len(buffered) > 512:
                    break
                while b'\n' in buffered:
                    line, buffered = buffered.split(b'\n', 1)
                    commands.put(line.decode('ascii'), timeout=.1)
        except Exception:
            pass
        finally:
            stopped.set()

    session = GuardSession(api, parent_alive, reply, clock, lease)
    threading.Thread(target=receive, name='cursor-guard-pipe', daemon=True).start()
    try:
        reply('READY')
        while True:
            if not session.tick():
                break
            try:
                command = commands.get(timeout=.025)
            except queue.Empty:
                if stopped.is_set():
                    break
                continue
            if not session.command(command):
                break
    finally:
        stopped.set()
        session.close()


def main(argv=None):
    if sys.platform != 'win32':
        raise SystemExit('This cursor guard only runs on Windows')
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args(argv)
    parent = ParentProcess(args.parent)
    api = None
    try:
        pipes = WindowsPipes()
        api = MagnificationApi()
        run_guard(api, parent.alive, pipes.read, pipes.write)
    finally:
        if api is not None:
            api.close()
        parent.close()


if __name__ == '__main__':
    main()
