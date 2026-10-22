import base64
import hashlib
import math
import queue
import socket
import sys
import threading
import time
from pathlib import Path

from .core import Cursor, FileReceiver, Screen
from .protocol import Channel
from .discovery import Advertiser
from .switching import engine_locked
from .motion import collect_motion, FRAME_INTERVAL, MAX_SAMPLES, InputQueue, INPUT_TYPES
from .bluetooth_clipboard import ClipboardChunks
from .delivery import DeliveryWindow, BYPASS_TYPES
from .performance import TelemetrySession
from .bluetooth_transport import TRANSPORT_PROTOCOL
from cryptography.exceptions import InvalidTag
from cryptography.fernet import InvalidToken


class Engine:
    def __init__(self, controller, local, side, password, host, port, status,
                 clipboard=True, files=False, map_command=True, directory=None, mouse_speed=1.0, transport='lan',
                 device_id=None, expected_peer_id=None):
        if transport not in ('lan', 'bluetooth'):
            raise ValueError('未知连接方式')
        self.transport = transport
        self.transport_backend = None
        self.controller, self.local, self.side = controller, local, side
        self.password, self.host, self.port, self.status = password, host, port, status
        self.clipboard_enabled, self.files_enabled = clipboard, files and transport != 'bluetooth'
        self.clipboard_limit = 8 * 1024 if transport == 'bluetooth' else 256 * 1024
        self.map_command = map_command
        self.peer_screen = None
        self.peer_cursor = None
        self.set_mouse_speed(mouse_speed)
        self.files = FileReceiver(directory or Path.home() / 'Downloads' / 'Share Monkey')
        self.connected = self.remote = self.active = False
        self.state_lock = threading.RLock()
        self.epoch = 0
        self.received_epoch = -1
        self.cooldown = 0
        self.stop_event = threading.Event()
        self.outgoing = InputQueue(maxsize=2048, urgent=transport == 'bluetooth')
        self.pending_return = None
        self.delivery = DeliveryWindow(clock=lambda: time.monotonic())
        self.metrics = None
        self.last_input_at = float('-inf')
        self.background_outgoing = queue.Queue(maxsize=256)
        self.write_wake = threading.Event()
        self.clipboard_chunks = ClipboardChunks(chunk_size=32)
        self.clipboard_tx_lock = threading.Lock()
        self.clipboard_pending = None
        self.clipboard_sent_at = None
        self.sock = self.listener = None
        self.cursor = None
        self.last_clipboard = None
        self.clip_lock = threading.Lock()
        self.transfer_lock = threading.Lock()
        self.file_reply = threading.Event()
        self.file_accepted = False
        self.input = None
        self.advertiser = None
        self.retryable = True
        self.last_error = None
        self._threads = []
        self.device_id, self.expected_peer_id = device_id, expected_peer_id
        self.peer_id = None

    @property
    def quiescent(self):
        """No old hook or transport may outlive a replacement connection."""
        with self.state_lock:
            threads = list(self._threads)
            threads.extend(getattr(self.input, 'listeners', ()))
            backend_thread = getattr(self.transport_backend, 'thread', None)
            if backend_thread is not None:
                threads.append(backend_thread)
            return (self.stop_event.is_set() and not any(t.is_alive() for t in threads)
                    and not getattr(self.transport_backend, 'cleanup_pending', False))

    def _spawn(self, target, *args):
        with self.state_lock:
            if self.stop_event.is_set():
                return
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            thread = threading.Thread(target=target, args=args, daemon=True)
            self._threads.append(thread)
            thread.start()

    def set_mouse_speed(self, value):
        value = float(value)
        if not math.isfinite(value) or not .25 <= value <= 4:
            raise ValueError('鼠标速度范围为 25%–400%')
        self.mouse_speed = value

    def start(self):
        from .input import Input
        self.input = Input(self)
        try:
            self.input.start()
        except Exception:
            self.input.stop()
            raise
        self.metrics = TelemetrySession(self.transport, self.controller).start()
        self._spawn(self.run)

    @engine_locked
    def send(self, message):
        if not self.connected:
            return False
        try:
            if message.get('type') in INPUT_TYPES:
                self.last_input_at = time.monotonic()
            if self.controller and message.get('type') in ('enter', 'leave', 'move', 'button', 'key', 'scroll'):
                message = dict(message, epoch=self.epoch)
                if message['type'] == 'move':
                    peer_scale = self.peer_screen.scale if self.peer_screen else self.local.scale
                    gain = self.mouse_speed * peer_scale / self.local.scale
                    message['dx'] *= gain
                    message['dy'] *= gain
                    if self.transport == 'bluetooth' and self.peer_cursor is not None:
                        if not self.remote:
                            return False
                        # Compute every sample locally, including clipping and
                        # the return seam. BLE carries the newest absolute
                        # result instead of replaying an ageing delta backlog.
                        leave, position = self.peer_cursor.move(message['dx'], message['dy'])
                        if leave:
                            if self.metrics:
                                self.metrics.increment('source_edge_returns')
                            self.return_local(position[1] / (self.peer_screen.height - 1))
                            return True
                        message = dict(type='pointer', epoch=self.epoch, x=position[0], y=position[1],
                                       _sample_time=time.monotonic())
                elif message['type'] == 'enter' and self.transport == 'bluetooth' and self.peer_screen:
                    self.peer_cursor = Cursor(self.peer_screen, message['side'],
                                              return_margin=8 * self.peer_screen.scale)
                    self.peer_cursor.enter(message['fraction'])
                if message['type'] == 'enter' and self.metrics:
                    self.metrics.increment('enter_queued')
                if message['type'] == 'enter' and self.transport == 'bluetooth':
                    self._transport_input_active(True)
            self.outgoing.put_nowait(message)
            self.write_wake.set()
            return True
        except queue.Full:
            self.stop('发送拥堵，已断开以恢复本机控制')
            return False

    def queue_clipboard(self, value):
        if not self.connected:
            return False
        if self.transport != 'bluetooth':
            return self.send({'type': 'clipboard', 'text': value})
        chunks = self.clipboard_chunks.encode(value)
        with self.clipboard_tx_lock:
            self._replace_clipboard_chunks(chunks)
            self.clipboard_pending = value
            self.clipboard_sent_at = None
        self.write_wake.set()
        return True

    def _replace_clipboard_chunks(self, chunks):
        while True:
            try:
                self.background_outgoing.get_nowait()
            except queue.Empty:
                break
        for chunk in chunks:
            self.background_outgoing.put_nowait(chunk)

    def _next_clipboard_chunk(self):
        with self.clipboard_tx_lock:
            if self.background_outgoing.empty():
                return None
            # Sustained input can pause this low-priority transfer. Restart from
            # a fresh ID before the receiver's partial-transfer deadline expires.
            now = time.monotonic()
            if self.controller and self.remote and now - self.last_input_at < .15:
                return None
            if self.clipboard_sent_at is not None and now - self.clipboard_sent_at >= 20:
                self._replace_clipboard_chunks(self.clipboard_chunks.encode(self.clipboard_pending))
            message = self.background_outgoing.get_nowait()
            self.clipboard_sent_at = now
            return message

    def run(self):
        reason = None
        try:
            if self.stop_event.is_set():
                return
            bluetooth = self.transport == 'bluetooth'
            server = not self.controller
            if bluetooth:
                if sys.platform == 'darwin':
                    from .bluetooth_mac import MacBleServer
                    options = {'device_id': self.device_id} if self.device_id else {}
                    backend = MacBleServer(self.status, role='controller' if self.controller else 'receiver', **options)
                    self.transport_backend = backend
                    if self.stop_event.is_set():
                        return
                    backend.start()
                    self.sock = backend.accept(self.stop_event)
                    server = True
                elif sys.platform == 'win32':
                    from .bluetooth_windows import WindowsBleConnector
                    backend = WindowsBleConnector(self.host, self.status, metrics=self.metrics)
                    self.transport_backend = backend
                    if self.stop_event.is_set():
                        return
                    self.sock = backend.connect(self.stop_event, timeout=20)
                    server = False
                else:
                    raise RuntimeError('蓝牙模式仅支持 Mac 与 Windows')
            elif self.controller:
                self.status('正在连接 ' + self.host)
                self.sock = socket.create_connection((self.host, self.port), timeout=8)
            else:
                self.listener = socket.socket()
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind((self.host, self.port))
                self.listener.listen(1)
                self.listener.settimeout(.5)
                self.advertiser = Advertiser(self.port, self.status).start()
                self.status('等待控制端连接 · 端口 ' + str(self.port))
                while not self.stop_event.is_set():
                    try:
                        self.sock, _ = self.listener.accept()
                        self.advertiser.stop()
                        break
                    except socket.timeout:
                        continue
                if self.stop_event.is_set():
                    return
            if self.stop_event.is_set():
                return
            if not bluetooth:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(15 if bluetooth else 8)
            channel = Channel.handshake(self.sock, self.password, server=server, compact=bluetooth)
            channel.send({'type': 'screen', 'width': self.local.width, 'height': self.local.height,
                          'scale': self.local.scale, 'input_protocol': 3, 'controller': self.controller,
                          'transport_protocol': TRANSPORT_PROTOCOL if bluetooth else None,
                          'device_id': self.device_id})
            peer = channel.recv()
            if peer.get('type') != 'screen':
                raise ValueError('未收到屏幕信息')
            if peer.get('input_protocol') != 3:
                raise ValueError('键鼠协议已升级，请将两端都更新到 v0.3.1 或更新版本')
            if bluetooth and peer.get('transport_protocol') != TRANSPORT_PROTOCOL:
                raise ValueError('蓝牙处理确认协议已升级，请将两端都更新到 v0.5.2')
            if isinstance(peer.get('controller'), bool) and peer['controller'] == self.controller:
                raise ValueError('两端角色相同，请让一端使用本机键鼠、另一端接受控制')
            self.peer_id = peer.get('device_id')
            if self.expected_peer_id and self.peer_id != self.expected_peer_id:
                raise PermissionError('恢复连接的设备与之前不同，请手动重新配对')
            self.peer_screen = Screen(peer['width'], peer['height'], float(peer.get('scale', 1)))
            with self.state_lock:
                if self.stop_event.is_set():
                    return
                if not self.controller:
                    self._receiver_cursor(False)
                self.connected = True
            self.status(('蓝牙已连接' if bluetooth else '已配对') +
                        (' · 移动到屏幕边缘切换' if self.controller else ' · 等待对方操作'))
            self._spawn(self.writer, channel)
            if self.clipboard_enabled:
                self._spawn(self.clipboard_loop)
            while not self.stop_event.is_set():
                self.receive(channel.recv())
        except Exception as exc:
            if not self.stop_event.is_set():
                self.retryable = not isinstance(exc, (InvalidTag, InvalidToken, ValueError, PermissionError))
                detail = ('配对密码不一致，请检查两端密码' if isinstance(exc, (InvalidTag, InvalidToken))
                          else str(exc) or '连接暂时不可用')
                reason = self.last_error = '连接结束：' + detail
        finally:
            self.stop(reason)

    def writer(self, channel):
        try:
            pending = None
            last_motion = float('-inf')
            last_send = time.monotonic()
            while not self.stop_event.is_set():
                if self.pending_return and time.monotonic() >= self.pending_return[2]:
                    raise RuntimeError('返回确认超时，已断开以恢复本机控制')
                bluetooth = self.transport == 'bluetooth'
                credit_deadline = self.delivery.remaining() if bluetooth else None
                if credit_deadline is not None and credit_deadline <= 0:
                    raise RuntimeError('蓝牙对端处理确认超时，已恢复本机控制')
                if self.controller and any(not listener.is_alive() for listener in self.input.listeners):
                    raise RuntimeError('键鼠监听已停止，请检查系统权限后重新连接')
                self.write_wake.clear()
                message, background = None, False
                if bluetooth and not self.delivery.can_send():
                    try:
                        message = self.outgoing.get_bypass_nowait()
                    except queue.Empty:
                        self.write_wake.wait(.25 if credit_deadline is None else min(.25, max(0, credit_deadline)))
                        continue
                else:
                    message, pending = pending, None
                    if message is None:
                        try:
                            message = self.outgoing.get_nowait()
                        except queue.Empty:
                            if not bluetooth or self.delivery.can_send(background=True):
                                message = self._next_clipboard_chunk()
                                background = message is not None
                if message is None:
                    idle = time.monotonic() - last_send
                    if idle < 1:
                        wait = min(1 - idle, max(0, self.pending_return[2] - time.monotonic())) if self.pending_return else 1 - idle
                        if (bluetooth and self.controller and self.remote
                                and not self.background_outgoing.empty()
                                and self.delivery.can_send(background=True)):
                            quiet_wait = .15 - (time.monotonic() - self.last_input_at)
                            if quiet_wait > 0:
                                wait = min(wait, quiet_wait)
                        if credit_deadline is not None:
                            wait = min(wait, max(0, credit_deadline))
                        self.write_wake.wait(wait)
                        continue
                    message = {'type': 'ping'}
                if message.get('type') == 'move':
                    message, pending = collect_motion(message, self.outgoing, last_motion + FRAME_INTERVAL,
                                                      compact=self.transport == 'bluetooth')
                if bluetooth and not self._current_input(message):
                    continue
                if message.get('type') == 'enter':
                    wait_capture = getattr(self.input, 'wait_capture_ready', None)
                    if wait_capture is not None and wait_capture(self.stop_event) is False:
                        if self.metrics:
                            self.metrics.increment('capture_wait_cancelled')
                        continue
                    if bluetooth and not self._current_input(message):
                        continue
                if bluetooth and message.get('type') not in BYPASS_TYPES:
                    with self.state_lock:
                        if not self._current_input(message):
                            continue
                        message = self.delivery.prepare(message, background=background)
                sent_at = time.monotonic()
                channel.send(message)
                if self.metrics:
                    self.metrics.observe('local_send_ms', (time.monotonic() - sent_at) * 1000)
                    self.metrics.increment('sent_frames')
                    if message.get('type') == 'enter':
                        self.metrics.increment('enter_sent')
                    self.metrics.gauge('input_queue', self.outgoing.qsize())
                    self.metrics.gauge('data_inflight', self.delivery.pending_count)
                    self.metrics.gauge('peak_data_inflight', self.delivery.peak)
                last_send = time.monotonic()
                if message.get('type') == 'move':
                    last_motion = time.monotonic()
        except Exception as exc:
            reason = '发送中断：' + str(exc) if not self.stop_event.is_set() else None
            self.stop(reason)

    def _current_input(self, message):
        kind = message.get('type')
        if self.controller and kind in INPUT_TYPES:
            return message.get('epoch') == self.epoch and (kind == 'leave' or self.remote)
        if not self.controller and kind in ('return', 'left'):
            return message.get('epoch') == self.received_epoch
        return True

    def _transport_input_active(self, active):
        callback = getattr(self.transport_backend, 'set_input_active', None)
        if callback is not None:
            callback(active)

    def _receiver_cursor(self, active):
        self._transport_input_active(active)
        callback = getattr(self.input, 'set_remote_active', None)
        if callback:
            callback(active)

    def _finish_return(self, fraction):
        self.pending_return = None
        self.input.restore_local(self.local.width - 12 if self.side == 'right' else 12,
                                 max(0, min(1, fraction)) * (self.local.height - 1))
        self.status('已回到本机')
        if self.metrics:
            self.metrics.increment('returns_completed')

    @engine_locked
    def return_local(self, fraction=.5, confirmed=False):
        if not self.controller:
            return
        was_remote = self.remote
        self.remote = False
        # The restored pointer is inside the screen; native return filtering
        # removes old events. A timer would swallow a fast intentional return.
        self.cooldown = 0
        if was_remote:
            self._transport_input_active(False)
            if self.metrics:
                self.metrics.increment('return_requests')
            if self.transport == 'bluetooth':
                self.outgoing.discard_epoch(self.epoch)
                self.delivery.cancel_epoch(self.epoch)
            # Publish the release barrier before invoking native restoration:
            # a native failure can synchronously stop this engine.
            self.send({'type': 'leave'})
        if self.input:
            self.input.release()
            if was_remote or self.pending_return:
                try:
                    if self.transport == 'bluetooth' and self.connected and not confirmed:
                        self.pending_return = (self.epoch, fraction, time.monotonic() + 1.5)
                    else:
                        self._finish_return(fraction)
                except RuntimeError as exc:
                    # Cursor positioning failure must not prevent leave delivery
                    # or stop() from releasing hooks and closing the connection.
                    self.status(str(exc))
        if was_remote:
            self.status('正在返回本机…' if self.pending_return else '已回到本机')

    @engine_locked
    def receive(self, m):
        if self.metrics and '_delivery' not in m:
            self.metrics.increment('received_frames')
        if self.transport == 'bluetooth' and '_delivery' in m:
            packet = m['_delivery']
            if type(packet) is not int or not 0 <= packet < 2 ** 32:
                raise ValueError('蓝牙处理编号无效')
            message = {key: value for key, value in m.items() if key != '_delivery'}
            if message.get('type') in BYPASS_TYPES:
                raise ValueError('蓝牙确认消息不能再次请求确认')
            # Only successful native input/clipboard processing earns credit.
            processing_at = time.monotonic()
            self.receive(message)
            if self.metrics:
                self.metrics.observe('receiver_processing_ms', (time.monotonic() - processing_at) * 1000)
            self.send({'type': 'consumed', 'packet': packet})
            return
        kind = m.get('type')
        if kind == 'ping':
            return
        if kind == 'consumed' and self.transport == 'bluetooth':
            record = self.delivery.acknowledge(m.get('packet'))
            if record is not None:
                if self.metrics:
                    now = time.monotonic()
                    self.metrics.observe('processed_ack_ms', (now - record['sent_at']) * 1000)
                    if record['sample_time'] is not None:
                        self.metrics.observe('sample_to_ack_ms', (now - record['sample_time']) * 1000)
                self.write_wake.set()
                if self.metrics:
                    self.metrics.gauge('data_inflight', self.delivery.pending_count)
            elif self.metrics:
                self.metrics.increment('late_consumed_acks')
            return
        if kind == 'return' and self.controller:
            if (self.remote or self.pending_return) and m.get('epoch', 0) == self.epoch:
                if self.metrics:
                    self.metrics.increment('peer_edge_returns')
                self.return_local(m['fraction'], confirmed=True)
        elif kind == 'left' and self.controller and self.transport == 'bluetooth':
            if self.pending_return and m.get('epoch') == self.pending_return[0]:
                self._finish_return(self.pending_return[1])
        elif kind == 'clipboard' and self.clipboard_enabled:
            import pyperclip
            value = m['text']
            if not isinstance(value, str) or len(value.encode()) > self.clipboard_limit:
                raise ValueError('剪贴板内容过大')
            with self.clip_lock:
                self.last_clipboard = value
                pyperclip.copy(value)
        elif kind == 'clipboard_part' and self.clipboard_enabled and self.transport == 'bluetooth':
            value = self.clipboard_chunks.receive(m)
            if value is not None:
                import pyperclip
                with self.clip_lock:
                    self.last_clipboard = value
                    pyperclip.copy(value)
        elif kind == 'file_offer':
            if self.files_enabled:
                self.files.begin(m)
            self.send({'type': 'file_reply', 'accepted': self.files_enabled})
        elif kind == 'file_reply':
            self.file_accepted = m['accepted'] is True
            self.file_reply.set()
        elif kind == 'file_chunk':
            if not self.files_enabled:
                raise ValueError('未开启接收文件')
            self.files.chunk(m['data'])
        elif kind == 'file_end':
            path = self.files.finish(m['sha256'])
            self.status('已收到文件：' + str(path))
            self.send({'type': 'file_done', 'name': path.name})
        elif kind == 'file_done':
            self.status('对方已接收并校验文件：' + m['name'])
        elif not self.controller:
            if kind == 'enter':
                epoch = m.get('epoch', 0)
                if type(epoch) is not int or epoch <= self.received_epoch:
                    return
                if m['side'] not in ('left', 'right'):
                    raise ValueError('屏幕方向无效')
                self._receiver_cursor(False)
                self.cursor = Cursor(self.local, m['side'],
                                     return_margin=8 * self.local.scale if self.transport == 'bluetooth' else 0)
                self.received_epoch = epoch
                self.input.release()
                self.input.move_to(self.cursor.enter(m['fraction']))
                self._receiver_cursor(True)
                self.active = True
            elif kind == 'leave' and self.transport == 'bluetooth':
                epoch = m.get('epoch', 0)
                if type(epoch) is not int or not 0 <= epoch < 2 ** 32:
                    raise ValueError('输入会话编号无效')
                if epoch >= self.received_epoch:
                    # A very fast out-and-back can cancel an unsent enter.
                    # Acknowledge that leave even if this epoch never entered,
                    # and reject any stale enter that was already in flight.
                    self.received_epoch = epoch
                    self.active = False
                    self.input.release()
                    self._receiver_cursor(False)
                    self.send({'type': 'left', 'epoch': epoch})
            elif kind in ('leave', 'move', 'pointer', 'key', 'button', 'scroll') and m.get('epoch', 0) != self.received_epoch:
                return
            elif kind == 'leave':
                self.active = False
                self.input.release()
                self._receiver_cursor(False)
            elif self.active and kind == 'move':
                samples = m.get('path', [[m.get('dx'), m.get('dy')]])
                if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_SAMPLES:
                    raise ValueError('鼠标轨迹无效')
                path = []
                for sample in samples:
                    if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                        raise ValueError('鼠标轨迹无效')
                    dx, dy = map(float, sample)
                    if not all(math.isfinite(value) and abs(value) <= 32768 for value in (dx, dy)):
                        raise ValueError('鼠标位移无效')
                    path.append((dx, dy))
                for dx, dy in path:
                    leave, position = self.cursor.move(dx, dy)
                    if leave:
                        break
                self.input.move_to(position)
                if leave:
                    self.active = False
                    self.input.release()
                    self._receiver_cursor(False)
                    self.send({'type': 'return', 'fraction': position[1] / (self.local.height - 1), 'epoch': self.received_epoch})
            elif self.active and kind == 'pointer' and self.transport == 'bluetooth':
                x, y = float(m['x']), float(m['y'])
                if not (math.isfinite(x) and math.isfinite(y)
                        and 0 <= x <= self.local.width - 1 and 0 <= y <= self.local.height - 1):
                    raise ValueError('鼠标位置无效')
                self.cursor.x, self.cursor.y = x, y
                self.cursor.return_pressure = 0
                self.input.move_to((x, y))
            elif self.active and kind == 'key':
                self.input.inject_key(m['key'], m['down'])
            elif self.active and kind == 'button':
                from pynput.mouse import Button
                button = getattr(Button, m['button'])
                self.input.inject_button(button, m['down'])
            elif self.active and kind == 'scroll':
                dx, dy = float(m['dx']), float(m['dy'])
                if abs(dx) > 100 or abs(dy) > 100:
                    raise ValueError('滚轮数据无效')
                self.input.mouse.scroll(dx, dy)

    def clipboard_loop(self):
        import pyperclip
        # Initial contents stay local. Only subsequent changes are shared.
        try:
            with self.clip_lock:
                self.last_clipboard = pyperclip.paste()
            while not self.stop_event.wait(.6):
                pending = None
                oversized = False
                with self.clip_lock:
                    value = pyperclip.paste()
                    if value != self.last_clipboard:
                        self.last_clipboard = value
                        if isinstance(value, str):
                            if len(value.encode()) <= self.clipboard_limit:
                                pending = value
                            elif self.transport == 'bluetooth':
                                oversized = True
                # receive() holds state_lock before taking clip_lock. LAN
                # queue_clipboard() takes state_lock too, so never call it
                # while holding clip_lock in this background poller.
                if pending is not None:
                    self.queue_clipboard(pending)
                if oversized:
                    self.status('此段剪贴板超过蓝牙 8 KB 上限，未同步；长文本请使用局域网')
        except Exception as exc:
            self.status('剪贴板共享已停止：' + str(exc))

    def send_file(self, filename):
        if self.transport == 'bluetooth':
            self.status('蓝牙模式支持键鼠和文本剪贴板；发送文件请切换为局域网')
            return
        if not self.connected:
            self.status('请先连接另一台电脑')
            return
        self._spawn(self._send_file, filename)

    def _send_file(self, filename):
        if not self.transfer_lock.acquire(blocking=False):
            self.status('已有文件正在发送')
            return
        try:
            path = Path(filename)
            size = path.stat().st_size
            if not path.is_file() or size > FileReceiver.LIMIT:
                raise ValueError('请选择不超过 100 MB 的普通文件')
            self.file_reply.clear()
            self.file_accepted = False
            self.send({'type': 'file_offer', 'name': path.name, 'size': size})
            if not self.file_reply.wait(10) or not self.file_accepted:
                raise ValueError('对方未开启接收文件，或未响应')
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                while True:
                    raw = handle.read(49152)
                    if not raw:
                        break
                    digest.update(raw)
                    # Keep space in the queue for input and heartbeats.
                    while self.outgoing.qsize() > 8 and not self.stop_event.wait(.01):
                        pass
                    if not self.send({'type': 'file_chunk', 'data': base64.b64encode(raw).decode()}):
                        return
            self.send({'type': 'file_end', 'sha256': digest.hexdigest()})
        except Exception as exc:
            self.status('发送文件失败：' + str(exc))
        finally:
            self.transfer_lock.release()

    @engine_locked
    def stop(self, reason=None):
        self.connected = False
        self.stop_event.set()
        self.write_wake.set()
        self.delivery.clear()
        if self.advertiser:
            self.advertiser.stop()
        self.return_local()
        self.active = False
        for sock in (self.sock, self.listener):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        if self.transport_backend:
            try:
                self.transport_backend.close()
            except Exception:
                pass
        if self.input:
            self.input.stop()
        if self.metrics:
            self.metrics.close()
        self.files.abort()
        if reason:
            self.status(reason)
