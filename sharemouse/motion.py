"""Short motion frames preserve the path and every button/key ordering barrier."""
import queue
import time
from collections import deque
import math

FRAME_INTERVAL = 1 / 240
MAX_SAMPLES = 64
INPUT_TYPES = frozenset(('enter', 'leave', 'move', 'pointer', 'key', 'button', 'scroll'))
URGENT_TYPES = frozenset(('enter', 'leave', 'return', 'left', 'consumed'))


class InputQueue(queue.Queue):
    """Preserve input order, with explicit epoch cancellation at handoff."""
    def __init__(self, maxsize=2048, urgent=False):
        super().__init__(maxsize)
        self.urgent = urgent

    def _put(self, item):
        if self.urgent and item.get('type') in URGENT_TYPES:
            self.queue.appendleft(item)
        else:
            self.queue.append(item)

    def put(self, item, block=True, timeout=None):
        # BLE pointers are absolute snapshots computed from every hardware
        # delta. Replace only an adjacent unsent snapshot, never cross a key,
        # button or handoff barrier. No distance/path is lost by coalescing.
        if self.urgent and item.get('type') == 'pointer':
            with self.mutex:
                if (self.queue and self.queue[-1].get('type') == 'pointer'
                        and self.queue[-1].get('epoch') == item.get('epoch')):
                    self.queue[-1] = item
                    self.not_empty.notify()
                    return
        return super().put(item, block, timeout)

    def get_bypass_nowait(self):
        # A processing ACK must not wait behind payload awaiting its own ACK.
        from .delivery import BYPASS_TYPES
        with self.mutex:
            for index, message in enumerate(self.queue):
                if message.get('type') in BYPASS_TYPES:
                    del self.queue[index]
                    self.not_full.notify()
                    return message
            raise queue.Empty

    def discard_epoch(self, epoch):
        with self.mutex:
            kept = deque(m for m in self.queue
                         if m.get('type') not in INPUT_TYPES or m.get('epoch') != epoch)
            removed = len(self.queue) - len(kept)
            self.queue.clear()
            self.queue.extend(kept)
            self.unfinished_tasks = max(0, self.unfinished_tasks - removed)
            self.not_full.notify_all()
            if not self.unfinished_tasks:
                self.all_tasks_done.notify_all()


def _mergeable(first, second):
    ax, ay = first
    bx, by = second
    return (all(math.isfinite(v) for v in (*first, *second))
            and ax * bx + ay * by >= 0
            and abs(ax * by - ay * bx) <= 1e-12 * max(1, math.hypot(ax, ay) * math.hypot(bx, by))
            and abs(ax + bx) <= 32768 and abs(ay + by) <= 32768)


def collect_motion(first, outgoing, deadline, compact=False):
    samples = [[first['dx'], first['dy']]]
    pending = None
    consumed = 1
    while (consumed < 2048 if compact else len(samples) < MAX_SAMPLES):
        remaining = deadline - time.monotonic()
        try:
            message = (outgoing.get(timeout=remaining) if remaining > 0 and not compact
                       else outgoing.get_nowait())
        except queue.Empty:
            break
        if message.get('type') != 'move' or message.get('epoch') != first.get('epoch'):
            pending = message
            break
        point = [message['dx'], message['dy']]
        consumed += 1
        if compact and _mergeable(samples[-1], point):
            samples[-1] = [samples[-1][0] + point[0], samples[-1][1] + point[1]]
        elif len(samples) < MAX_SAMPLES:
            samples.append(point)
        else:
            pending = message
            break
    if len(samples) == 1:
        return dict(first, dx=samples[0][0], dy=samples[0][1]), pending
    return {'type': 'move', 'epoch': first.get('epoch', 0), 'path': samples}, pending
