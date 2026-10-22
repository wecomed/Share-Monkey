"""Desktop presentation state; native UI callbacks stay on the main thread."""
from dataclasses import asdict, dataclass, field, fields
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import sys
import time

from .discovery import Browser
from .display import primary_screen
from .engine import Engine
from .connection_power import create_connection_power


def settings_path():
    base = (Path(os.environ.get('LOCALAPPDATA', Path.home())) if sys.platform == 'win32'
            else Path.home() / 'Library' / 'Application Support')
    return base / 'Share Monkey' / 'settings.json'


@dataclass
class Settings:
    transport: str = 'lan'
    role: str = 'controller'
    side: str = 'right'
    speed: float = 1.5
    clipboard: bool = True
    files: bool = False
    mapping: bool = True
    manual: bool = False
    host: str = ''
    port: int = 24809
    auto_reconnect: bool = True
    keep_awake: bool = True
    device_id: str = field(default_factory=lambda: secrets.token_hex(6))

    @classmethod
    def load(cls, client, path=None):
        settings = cls(role=client.default_role, speed=1.5 if client.platform == 'darwin' else 1.)
        try:
            data = json.loads((path or settings_path()).read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                return settings
            for field in fields(settings):
                value = data.get(field.name)
                default = getattr(settings, field.name)
                if type(value) is type(default):
                    setattr(settings, field.name, value)
            if settings.role not in ('controller', 'receiver'):
                settings.role = client.default_role
            if settings.transport not in ('lan', 'bluetooth'):
                settings.transport = 'lan'
            if settings.side not in ('left', 'right'):
                settings.side = 'right'
            if not math.isfinite(settings.speed) or not .25 <= settings.speed <= 4:
                settings.speed = 1.5 if client.platform == 'darwin' else 1.
            if not 1024 <= settings.port <= 65535:
                settings.port = 24809
            if re.fullmatch('[0-9a-f]{12}', settings.device_id) is None:
                settings.device_id = secrets.token_hex(6)
        except (OSError, ValueError, TypeError):
            pass
        return settings

    def save(self, path=None):
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)


@dataclass(frozen=True)
class ConnectionRequest:
    controller: bool
    screen: object
    side: str
    password: str = field(repr=False)
    host: str
    port: int
    transport: str
    clipboard: bool
    files: bool
    mapping: bool


class Session:
    def __init__(self, client, changed=lambda: None, discovery=True, settings=None, persist=True,
                 clock=time.monotonic):
        self.client, self.changed = client, changed
        self.settings = settings or Settings.load(client)
        self.engine = self.browser = None
        self.peers = {}
        self.selected = ''
        self.status = self.ready_status()
        self.discovery_status = ''
        self.events = queue.Queue()
        self.generation = 0
        self.discovery_enabled = discovery
        self.closed = False
        self.persist = persist
        self.engine_generation = 0
        self._connected = False
        self.clock = clock
        self._request = None
        self._retiring = []
        self._retry_at = None
        self._retry_attempts = 0
        self._connected_since = None
        self._retry_caption = None
        self._peer_id = None
        self._screen_window = None
        self.power = create_connection_power()
        self.refresh_discovery()

    @property
    def connected(self):
        return bool(self.engine and self.engine.connected)

    def ready_status(self):
        return ('准备就绪 · 无需网络，使用蓝牙直连' if self.settings.transport == 'bluetooth'
                else '准备就绪 · 在同一网络中连接你的电脑')

    @property
    def running(self):
        return bool(self._request or (self.engine and not self.engine.stop_event.is_set()))

    @property
    def reconnecting(self):
        return bool(self._request and self._retry_at is not None)

    def refresh_discovery(self):
        self.generation += 1
        generation = self.generation
        if self.browser:
            self.browser.stop()
            self.browser = None
        self.peers = {}
        self.selected = ''
        controller = self.settings.role == 'controller'
        if self.settings.transport == 'bluetooth':
            self.discovery_status = ('先在此 Mac 点击“开启蓝牙等待”，再从 Windows 连接' if self.client.platform == 'darwin'
                                     else '先在 Mac 开启蓝牙等待，正在查找附近的 Share Monkey…')
            if self.client.platform == 'win32' and self.discovery_enabled and not self.closed:
                from .bluetooth_windows import WindowsBleBrowser
                self.browser = WindowsBleBrowser(
                    lambda peers: self.events.put(('peers', generation, peers)),
                    lambda error: self.events.put(('discovery', generation, error))).start()
            return
        self.discovery_status = ('正在查找附近的电脑…' if controller else
                                 '点击“开始等待”，另一台电脑就能发现这台设备')
        if controller and self.discovery_enabled and not self.settings.manual and not self.closed:
            self.browser = Browser(lambda peers: self.events.put(('peers', generation, peers)),
                                   lambda error: self.events.put(('discovery', generation, error))).start()

    def configure(self, **values):
        if self.running:
            raise ValueError('请先断开连接，再调整连接设置')
        for key in values:
            if key not in Settings.__dataclass_fields__:
                raise ValueError('未知设置：' + key)
        if 'transport' in values and values['transport'] not in ('lan', 'bluetooth'):
            raise ValueError('未知连接方式')
        for key, value in values.items():
            setattr(self.settings, key, value)
        if 'role' in values or 'manual' in values or 'transport' in values:
            self.refresh_discovery()
        if 'transport' in values:
            self.status = self.ready_status()
        self.changed()

    def connect(self, password, window=None):
        if self.closed or self.running:
            return
        settings = self.settings
        if len(password) < 4:
            raise ValueError('请输入至少 4 个字符的配对密码，4 位数字即可，两台电脑填写相同密码。')
        controller = settings.role == 'controller'
        bluetooth = settings.transport == 'bluetooth'
        host, port = '', 0
        if bluetooth:
            port = 0
            if self.client.platform == 'darwin':
                host = ''
            else:
                peer = self.peers.get(self.selected)
                if peer is None:
                    raise ValueError('请先在 Mac 点击“开启蓝牙等待”，再选择发现的 Mac。')
                host = peer.host
        else:
            host, port = settings.host.strip(), int(settings.port)
            if not 1024 <= port <= 65535:
                raise ValueError('端口范围为 1024–65535')
        if not bluetooth and not settings.manual:
            if controller:
                peer = self.peers.get(self.selected)
                if peer is None:
                    raise ValueError('请先在另一台电脑点击“开始等待”，再选择发现的设备。')
                host, port = peer.host, peer.port
            else:
                host = '0.0.0.0'
        elif not bluetooth and not controller:
            host = host or '0.0.0.0'
        if not host and not (bluetooth and self.client.platform == 'darwin'):
            raise ValueError('请填写对方的局域网 IP 地址。')
        self._request = ConnectionRequest(controller, primary_screen(window), settings.side,
                                          password, host, port, settings.transport,
                                          settings.clipboard, settings.files and not bluetooth,
                                          settings.mapping)
        self._screen_window = window
        self._peer_id = None
        if self.persist:
            try:
                self.settings.save()  # Save device identity, never the password.
            except OSError:
                pass
        self._retry_attempts = 0
        self._connected_since = None
        self._retiring = [engine for engine in self._retiring if not engine.quiescent]
        if self._retiring:
            self._retry_at = self.clock()
            self.status = '正在恢复本机键鼠，随后重新连接…'
        else:
            self._start_attempt(initial=True)
        self.power.set_active(settings.keep_awake)
        self.changed()

    def _start_attempt(self, initial=False):
        request = self._request
        if request is None or self.closed:
            return
        self.engine_generation += 1
        generation = self.engine_generation
        options = {'mouse_speed': self.settings.speed}
        options['device_id'] = self.settings.device_id
        if self._peer_id is not None:
            options['expected_peer_id'] = self._peer_id
        bluetooth = request.transport == 'bluetooth'
        if bluetooth:
            options['transport'] = 'bluetooth'
        engine = None
        try:
            screen = request.screen if initial else primary_screen(self._screen_window)
            host = request.host
            if bluetooth and self.client.platform == 'win32' and self._peer_id:
                matches = [peer for peer in self.peers.values()
                           if peer.name == 'SM Monkey ' + self._peer_id]
                if len(matches) == 1:
                    host = matches[0].host
            engine = Engine(request.controller, screen, request.side, request.password,
                            host, request.port,
                            lambda text: self.events.put(('status', generation, text)),
                            request.clipboard, request.files, request.mapping, **options)
            self.engine = engine
            self._retry_at = None
            engine.start()
        except Exception as exc:
            if engine is not None:
                engine.stop()
                self._retiring.append(engine)
            self.engine = None
            if initial or isinstance(exc, PermissionError):
                self._cancel_retry()
                if initial:
                    raise
                self.status = '无法恢复连接：' + str(exc)
            else:
                self._schedule_retry()
            return
        self.status = ('正在启动蓝牙等待…' if self.client.platform == 'darwin' else '正在连接 Mac 蓝牙…') if bluetooth else (
            '正在连接…' if request.controller else '正在启动接收…')

    def _cancel_retry(self):
        self._request = None  # Credentials live only in this session, never Settings.
        self._retry_at = None
        self._retry_attempts = 0
        self._connected_since = None
        self._peer_id = None
        self._screen_window = None
        self.power.close()

    def _schedule_retry(self):
        self._retry_attempts += 1
        delay = min(15, 2 ** min(self._retry_attempts - 1, 4))
        self._retry_at = self.clock() + delay
        self._retry_caption = None

    def _poll_connection(self):
        dirty = False
        self._retiring = [engine for engine in self._retiring if not engine.quiescent]
        engine = self.engine
        if engine is not None and engine.stop_event.is_set():
            self.engine = None
            self._retiring.append(engine)
            self.engine_generation += 1  # Ignore late status from retired workers.
            if self._request:
                if not self.settings.auto_reconnect or not engine.retryable:
                    if engine.last_error:
                        self.status = engine.last_error
                    self._cancel_retry()
                else:
                    self._schedule_retry()
            dirty = True
        if self.connected:
            peer_id = getattr(self.engine, 'peer_id', None)
            if isinstance(peer_id, str) and re.fullmatch('[0-9a-f]{12}', peer_id):
                self._peer_id = peer_id
            if self._connected_since is None:
                self._connected_since = self.clock()
            elif self.clock() - self._connected_since >= 5:
                self._retry_attempts = 0
        else:
            self._connected_since = None
        if self._request and self._retry_at is not None:
            remaining = max(0, math.ceil(self._retry_at - self.clock()))
            caption = (f'连接暂时中断 · {remaining} 秒后自动重连 · 可点击“断开”停止'
                       if remaining else '正在恢复连接 · 等待旧连接释放…')
            if caption != self._retry_caption:
                self._retry_caption = self.status = caption
                dirty = True
            if remaining == 0 and not self._retiring:
                self._start_attempt()
                dirty = True
        self.power.set_active(bool(self._request and self.settings.keep_awake))
        return dirty

    def set_speed(self, value):
        value = float(value)
        if not math.isfinite(value) or not .25 <= value <= 4:
            raise ValueError('鼠标速度范围为 25%–400%')
        self.settings.speed = value
        if self.engine:
            self.engine.set_mouse_speed(value)
        self.changed()

    def disconnect(self):
        self._cancel_retry()
        self.engine_generation += 1
        if self.engine:
            self.engine.stop()
            self._retiring.append(self.engine)
            self.engine = None
        # Discard status messages from the engine that was just stopped.
        self.poll(notify=False)
        self.status = '已断开 · 本机键鼠已恢复'
        self._connected = False
        self.changed()

    def send_file(self, filename):
        if self.settings.transport == 'bluetooth':
            raise ValueError('蓝牙模式支持键鼠和文本剪贴板。发送文件请切换为局域网后连接。')
        if not self.connected:
            raise ValueError('请先连接另一台电脑。')
        self.engine.send_file(filename)

    def poll(self, notify=True):
        if self.closed:
            return
        dirty = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'status':
                if event[1] == self.engine_generation:
                    self.status = event[2]
                    dirty = True
            elif event[1] == self.generation:
                if event[0] == 'peers':
                    self.peers = {p.label: p for p in sorted(event[2], key=lambda p: p.label)}
                    if self.selected not in self.peers:
                        self.selected = next(iter(self.peers)) if len(self.peers) == 1 else ''
                    if self.settings.transport == 'bluetooth':
                        self.discovery_status = (f'发现 {len(self.peers)} 台 Mac · 选择后点击“连接 Mac”' if self.peers
                                                 else '正在查找…请先在 Mac 点击“开启蓝牙等待”')
                    else:
                        self.discovery_status = (f'发现 {len(self.peers)} 台电脑 · 选择设备即可连接' if self.peers
                                                 else '正在查找附近的电脑…请先在对方点击“开始等待”')
                else:
                    self.discovery_status = event[2]
                dirty = True
        dirty = self._poll_connection() or dirty
        if self._connected != self.connected:
            self._connected = self.connected
            dirty = True
        if notify and dirty:
            self.changed()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cancel_retry()
        if self.browser:
            self.browser.stop()
            self.browser = None
        if self.engine:
            self.engine.stop()
            self.engine = None
        if self.persist:
            try:
                self.settings.save()
            except OSError:
                pass
