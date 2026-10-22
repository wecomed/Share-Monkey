"""One input-sharing process per login, using an OS-released file lock."""
import os
from pathlib import Path
import sys
import time


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path=None):
        if path is None:
            base = Path(os.environ.get('LOCALAPPDATA', Path.home())) if sys.platform == 'win32' else Path.home() / 'Library' / 'Application Support'
            path = base / 'ShareMouse Lab' / 'client.lock'
        self.path = Path(path)
        self.handle = None
        self.request_path = self.path.with_name('show-window.request')
        self.last_request = self._read_request()

    def _read_request(self):
        try:
            return self.request_path.read_text(encoding='ascii')
        except OSError:
            return ''

    def request_show(self):
        """Reopening a resident app brings its existing window to the front."""
        try:
            self.request_path.write_text(str(time.time_ns()), encoding='ascii')
            return True
        except OSError:
            return False

    def consume_show_request(self):
        value = self._read_request()
        if value and value != self.last_request:
            self.last_request = value
            return True
        return False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if sys.platform == 'win32':
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning('Share Monkey 已在运行，可从菜单栏或任务栏通知区打开。升级时请先退出旧版本。') from exc
        self.handle = handle
        return self

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None
