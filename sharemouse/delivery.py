"""Bound native BLE queues by peer processing, never by local write acceptance."""
import threading
import time

from .motion import INPUT_TYPES

BYPASS_TYPES = frozenset(('leave', 'left', 'return', 'consumed', 'ping'))


class DeliveryWindow:
    def __init__(self, limit=2, timeout=2.5, clock=time.monotonic):
        if type(limit) is not int or not 1 <= limit <= 4:
            raise ValueError('蓝牙确认窗口无效')
        self.limit, self.timeout, self.clock = limit, timeout, clock
        self._lock = threading.Lock()
        self._next = 1
        self._pending = {}
        self.peak = 0

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def can_send(self, background=False):
        with self._lock:
            return (len(self._pending) < self.limit
                    and (not background or not any(p['background'] for p in self._pending.values())))

    def prepare(self, message, background=False):
        with self._lock:
            if (len(self._pending) >= self.limit
                    or background and any(p['background'] for p in self._pending.values())):
                raise RuntimeError('蓝牙确认窗口已满')
            if self._next >= 2 ** 32:
                raise ValueError('蓝牙处理计数已耗尽，请重新连接')
            packet = self._next
            self._next += 1
            self._pending[packet] = dict(packet=packet, kind=message.get('type'),
                                         epoch=message.get('epoch'), sent_at=self.clock(),
                                         sample_time=message.get('_sample_time'), background=background)
            self.peak = max(self.peak, len(self._pending))
            return dict(message, _delivery=packet)

    def acknowledge(self, packet):
        if type(packet) is not int or not 0 <= packet < 2 ** 32:
            raise ValueError('蓝牙处理确认编号无效')
        with self._lock:
            return self._pending.pop(packet, None)

    def cancel_epoch(self, epoch):
        with self._lock:
            self._pending = {k: p for k, p in self._pending.items()
                             if p['epoch'] != epoch or p['kind'] not in INPUT_TYPES}

    def remaining(self):
        with self._lock:
            return (min(p['sent_at'] + self.timeout for p in self._pending.values()) - self.clock()
                    if self._pending else None)

    def clear(self):
        with self._lock:
            self._pending.clear()
