"""Balanced Mac visibility and Windows cursor leases with crash recovery."""
import atexit
import os
import subprocess
import sys
import threading
import time


class MacCursorVisibility:
    POLL_INTERVAL = .05

    def __init__(self, api=None, on_failure=lambda reason: None):
        self.api = api
        self.on_failure = on_failure
        self._hidden = False
        self._display = None
        self._watch_position = False
        self._anchor = None
        self._closed = False
        self._stopped = threading.Event()
        self._monitor = None
        self._lock = threading.RLock()

    def start(self):
        pass

    def _api(self):
        if self.api is None:
            import Quartz
            self.api = Quartz
        return self.api

    def hide(self, watch_position=False):
        with self._lock:
            if self._closed:
                raise RuntimeError('Mac 光标服务已关闭')
            api = self._api()
            anchor = self._position() if watch_position else None
            if not self._hidden:
                display = api.CGMainDisplayID()
                if api.CGDisplayHideCursor(display) != api.kCGErrorSuccess:
                    raise RuntimeError('无法隐藏 Mac 指针')
                self._hidden, self._display = True, display
            self._watch_position, self._anchor = bool(watch_position), anchor
            if watch_position and self._monitor is None:
                self._monitor = threading.Thread(target=self._watch,
                                                 name='mac-idle-cursor', daemon=True)
                self._monitor.start()

    def _position(self):
        api = self._api()
        point = api.CGEventGetLocation(api.CGEventCreate(None))
        return point.x, point.y

    def _watch_once(self):
        with self._lock:
            if (not self._closed and self._hidden and self._watch_position
                    and self._position() != self._anchor):
                # Inactive receivers inject no motion. A changed position means
                # the local user took over; only the next explicit hide may
                # start another hidden period.
                self.show()

    def _watch(self):
        while not self._stopped.wait(self.POLL_INTERVAL):
            try:
                self._watch_once()
            except Exception as exc:
                try:
                    self.show()
                except Exception:
                    pass
                try:
                    self.on_failure('Mac 光标保护已结束：' + str(exc))
                except Exception:
                    pass
                return

    def show(self):
        with self._lock:
            self._watch_position = False
            if not self._hidden:
                return
            api = self._api()
            if api.CGDisplayShowCursor(self._display) != api.kCGErrorSuccess:
                raise RuntimeError('无法恢复 Mac 指针')
            self._hidden = False

    def close(self):
        with self._lock:
            self._closed = True
            self._watch_position = False
        self._stopped.set()
        try:
            self.show()
        finally:
            if self._monitor is not None and self._monitor is not threading.current_thread():
                self._monitor.join(timeout=.2)


class WindowsCursorVisibility:
    HEARTBEAT = .2
    REPLY_TIMEOUT = 1.2
    COMMAND_TIMEOUT = .75

    def __init__(self, on_failure=lambda reason: None, process_factory=None, recovery=None):
        self.on_failure = on_failure
        self._factory = process_factory or self._spawn
        self._recovery = recovery or self._restore_native
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._process = None
        self._reader = None
        self._ready = False
        self._closing = False
        self._closed = False
        self._failure = None
        self._sequence = 0
        self._acknowledged = 0
        self._hidden = False
        self._last_reply = time.monotonic()
        self._stopped = threading.Event()
        atexit.register(self.close)

    @staticmethod
    def _spawn():
        if sys.platform != 'win32':
            raise RuntimeError('Windows 光标服务需要完整的 64 位 Windows 客户端')
        command = ([sys.executable, '--cursor-guard'] if getattr(sys, 'frozen', False) else
                   [sys.executable, '-m', 'sharemouse.cursor_guard'])
        return subprocess.Popen(
            command + ['--parent', str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    @staticmethod
    def _restore_native():
        from .cursor_guard import MagnificationApi
        api = MagnificationApi()
        api.close()  # Show(TRUE) in a fresh runtime also works across threads.

    def start(self):
        with self._start_lock:
            with self._condition:
                if self._closed or self._closing:
                    raise RuntimeError('Windows 光标服务已关闭')
                if self._failure:
                    raise RuntimeError(str(self._failure))
                if self._ready:
                    return
            try:
                process = self._factory()
                with self._condition:
                    cancelled = self._closed or self._closing
                    if not cancelled:
                        self._process = process
                if cancelled:
                    try:
                        process.stdin.close()
                        process.wait(timeout=.5)
                    except Exception:
                        process.terminate()
                        process.wait(timeout=.5)
                    finally:
                        for pipe in (process.stdin, process.stdout):
                            try:
                                pipe.close()
                            except Exception:
                                pass
                    raise RuntimeError('Windows 光标服务启动已取消')
                self._reader = threading.Thread(target=self._read,
                                                name='cursor-guard-monitor', daemon=True)
                self._reader.start()
                deadline = time.monotonic() + 3
                with self._condition:
                    while not self._ready and self._failure is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Windows 光标服务启动超时')
                        self._condition.wait(remaining)
                    if self._failure:
                        raise RuntimeError(str(self._failure))
                threading.Thread(target=self._heartbeat,
                                 name='cursor-guard-heartbeat', daemon=True).start()
            except Exception as exc:
                self._fail(exc)
                raise RuntimeError('Windows 光标服务启动失败：' + str(exc)) from exc

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(256)
                if not line:
                    break
                text = line.decode('ascii').strip()
                with self._condition:
                    self._last_reply = time.monotonic()
                    if text == 'READY':
                        self._ready = True
                    elif text.startswith('OK '):
                        parts = text.split()
                        if len(parts) != 3 or parts[2] not in ('H', 'S', 'P', 'Q'):
                            raise RuntimeError('Windows 光标服务确认无效')
                        self._acknowledged = max(self._acknowledged, int(parts[1]))
                        if self._failure is None and not self._closing:
                            if parts[2] == 'H':
                                # An older TAKEOVER may already be in the pipe
                                # when a new explicit hide is requested.
                                self._hidden = True
                            elif parts[2] in ('S', 'Q'):
                                self._hidden = False
                    elif text == 'TAKEOVER':
                        # Ordinary heartbeats must never hide it again. Only an
                        # explicit future hide command starts another lease.
                        self._hidden = False
                    elif text == 'EXPIRED':
                        raise TimeoutError('Windows 光标守护心跳超时，已恢复本机指针')
                    else:
                        raise RuntimeError('Windows 光标服务回复无效')
                    self._condition.notify_all()
        except Exception as exc:
            self._fail(exc)
            return
        with self._condition:
            closing = self._closing or self._closed
        if not closing:
            self._fail(RuntimeError('Windows 光标服务意外退出，已恢复本机指针'))

    def _send(self, command):
        with self._write_lock:
            with self._condition:
                if self._failure:
                    raise RuntimeError(str(self._failure))
                if self._closed:
                    raise RuntimeError('Windows 光标服务已关闭')
                self._sequence += 1
                sequence = self._sequence
            command = command.replace('{seq}', str(sequence))
            data = (command + '\n').encode('ascii')
            if self._process.stdin.write(data) != len(data):
                raise BrokenPipeError('Windows 光标服务命令未完整写入')
            self._process.stdin.flush()
            return sequence

    def _request(self, command):
        self.start()
        try:
            sequence = self._send(command)
            deadline = time.monotonic() + self.COMMAND_TIMEOUT
            with self._condition:
                while self._acknowledged < sequence:
                    if self._failure:
                        raise RuntimeError(str(self._failure))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Windows 光标切换超时，已恢复本机指针')
                    self._condition.wait(remaining)
        except Exception as exc:
            self._fail(exc)
            raise

    def hide(self, watch_position=False):
        with self._condition:
            self._hidden = True
            # An idle helper has not needed heartbeats. Give this new H command
            # its own reply window before the heartbeat monitor checks staleness.
            self._last_reply = time.monotonic()
        self._request('H {seq} ' + ('1' if watch_position else '0'))

    def show(self):
        with self._condition:
            self._hidden = False
            started = self._process is not None
        if started:
            self._request('S {seq}')

    def _heartbeat(self):
        while not self._stopped.wait(self.HEARTBEAT):
            try:
                with self._condition:
                    if self._closing or self._closed:
                        return
                    hidden = self._hidden
                    stale = time.monotonic() - self._last_reply > self.REPLY_TIMEOUT
                if hidden:
                    if stale:
                        raise TimeoutError('Windows 光标守护无响应，已恢复本机指针')
                    self._send('P {seq}')
            except Exception as exc:
                self._fail(exc)
                return

    def _recover(self):
        try:
            self._recovery()
        except Exception:
            # The helper retains its own independent EOF / lease recovery.
            pass

    def _fail(self, error):
        with self._condition:
            if self._failure is not None or self._closing or self._closed:
                return
            self._failure = error
            self._hidden = False
            self._condition.notify_all()
        self._stopped.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=.5)
            except Exception:
                pass
        self._recover()
        try:
            self.on_failure('Windows 光标保护已恢复本机指针：' + str(error))
        except Exception:
            pass

    def close(self):
        with self._condition:
            if self._closing or self._closed:
                return
            self._closing = True
            self._hidden = False
        self._stopped.set()
        process = self._process
        if process is not None:
            try:
                self._send('Q {seq}')
                process.stdin.close()
                process.wait(timeout=.5)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=.5)
                except Exception:
                    pass
            finally:
                self._recover()
                for pipe in (process.stdin, process.stdout):
                    try:
                        pipe.close()
                    except Exception:
                        pass
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        atexit.unregister(self.close)


class NullCursorVisibility:
    def start(self):
        pass

    def hide(self, watch_position=False):
        pass

    def show(self):
        pass

    def close(self):
        pass


def create_cursor_visibility(on_failure=lambda reason: None):
    if sys.platform == 'win32':
        return WindowsCursorVisibility(on_failure)
    if sys.platform == 'darwin':
        return MacCursorVisibility(on_failure=on_failure)
    return NullCursorVisibility()
