"""Bounded ordered BLE byte stream shared by native peripheral and central.

GATT operations are packets; the encrypted Channel expects a TCP-like byte stream.
The native adapters split at their negotiated MTU and apply backpressure, while
this layer preserves every byte and promptly wakes readers on disconnect.
"""
from collections import deque
from dataclasses import dataclass
import math
import socket
import threading
import time

SERVICE_UUID = '4ec6a000-5f7d-4b2a-9b87-1d1c4e7a9a10'
RX_UUID = '4ec6a001-5f7d-4b2a-9b87-1d1c4e7a9a10'
TX_UUID = '4ec6a002-5f7d-4b2a-9b87-1d1c4e7a9a10'
INFO_UUID = '4ec6a003-5f7d-4b2a-9b87-1d1c4e7a9a10'
TRANSPORT_PROTOCOL = 3
CLIPBOARD_LIMIT = 8 * 1024


@dataclass(frozen=True)
class BluetoothPeer:
    host: str
    name: str
    platform: str = 'Mac'
    port: int = 0

    @property
    def label(self):
        return f'{self.name} · {self.platform} · 蓝牙 ({self.host})'


class BleStream:
    def __init__(self, writer=None, on_close=None, timeout=15., buffer_limit=262144):
        self._validate_timeout(timeout)
        if type(buffer_limit) is not int or buffer_limit < 1:
            raise ValueError('蓝牙接收队列长度无效')
        self._writer = writer
        self.on_close = on_close
        self._timeout = timeout
        self._limit = buffer_limit
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._chunks = deque()
        self._size = 0
        self._closed = False
        self._error = None

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def set_writer(self, writer):
        with self._condition:
            self._writer = writer
            self._condition.notify_all()

    def settimeout(self, timeout):
        self._validate_timeout(timeout)
        with self._condition:
            self._timeout = timeout
            self._condition.notify_all()

    @staticmethod
    def _validate_timeout(timeout):
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('蓝牙超时必须是大于零的有限数值')

    def gettimeout(self):
        return self._timeout

    def feed(self, data):
        data = bytes(data)
        overflow = False
        with self._condition:
            if self._closed or not data:
                return
            if self._size + len(data) > self._limit:
                overflow = True
            else:
                self._chunks.append(data)
                self._size += len(data)
                self._condition.notify_all()
        if overflow:
            self.finish(ConnectionError('蓝牙接收队列拥堵，已断开以恢复本机键鼠'))

    def recv(self, size):
        if size < 0:
            raise ValueError('读取长度无效')
        if size == 0:
            return b''
        with self._condition:
            deadline = None if self._timeout is None else time.monotonic() + self._timeout
            while not self._chunks:
                if self._closed:
                    if self._error:
                        raise ConnectionError(str(self._error)) from self._error
                    return b''
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise socket.timeout('蓝牙接收超时，请检查两台电脑的蓝牙和距离')
                self._condition.wait(remaining)
            result = self._chunks.popleft()
            if len(result) > size:
                self._chunks.appendleft(result[size:])
                result = result[:size]
            self._size -= len(result)
            return result

    def sendall(self, data):
        data = bytes(data)
        if not data:
            return
        timeout = self._timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        acquired = self._write_lock.acquire(timeout=timeout) if timeout is not None else self._write_lock.acquire()
        if not acquired:
            raise socket.timeout('蓝牙发送队列等待超时')
        try:
            with self._condition:
                if self._closed:
                    raise ConnectionError(str(self._error or '蓝牙连接已断开'))
                writer = self._writer
            if writer is None:
                raise ConnectionError('蓝牙发送通道尚未准备好')
            remaining = None if deadline is None else max(.001, deadline - time.monotonic())
            writer(data, remaining)
        except Exception as exc:
            self.finish(exc)
            raise
        finally:
            self._write_lock.release()

    def finish(self, error=None):
        if error is not None and not isinstance(error, BaseException):
            error = ConnectionError(str(error))
        callback = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._error = error
            # End input delivery immediately; buffered movements/keys from a
            # disconnected session must not be replayed before local recovery.
            self._chunks.clear()
            self._size = 0
            callback, self.on_close = self.on_close, None
            self._condition.notify_all()
        if callback:
            try:
                callback()
            except Exception:
                pass

    def shutdown(self, how=socket.SHUT_RDWR):
        self.finish()

    def close(self):
        self.finish()


def normalize_error(error):
    """Translate common OS adapter failures into actionable setup instructions."""
    text = str(error)
    lower = text.casefold()
    reason = getattr(error, 'reason', None)
    reason = getattr(reason, 'name', str(reason)).upper()
    if reason in ('DENIED_BY_USER', 'DENIED_BY_SYSTEM'):
        lower += ' permission'
    elif reason == 'POWERED_OFF':
        lower += ' powered off'
    elif reason in ('NO_BLUETOOTH', 'NO_BLE_CENTRAL_ROLE'):
        lower += ' no adapter'
    if any(word in lower for word in ('unauthorized', 'not authorized', 'access denied', 'permission', 'deniedbypolicy')):
        return '蓝牙权限被拒绝。Mac 请在“系统设置 → 隐私与安全性 → 蓝牙”允许 Share Monkey；Windows 请检查蓝牙设置和组织策略。'
    if any(word in lower for word in ('powered off', 'poweredoff', 'radio off', 'bluetooth is off')):
        return '蓝牙已关闭，请在 Mac 和 Windows 的系统设置中开启蓝牙后重试。'
    if any(word in lower for word in ('unsupported', 'not supported', 'no bluetooth', 'no adapter')):
        return '未发现可用的低功耗蓝牙（BLE）适配器。请检查蓝牙驱动或使用支持 BLE 的适配器。'
    return '蓝牙连接失败：' + (text or type(error).__name__)
