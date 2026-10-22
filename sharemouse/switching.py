"""Intentional edge crossings, with distance-based rearming after a return."""
from functools import wraps


def engine_locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.state_lock:
            return method(self, *args, **kwargs)
    return call


def input_locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.engine.state_lock:
            return method(self, *args, **kwargs)
    return call


class EdgeSwitch:
    REARM_DISTANCE = 24

    def __init__(self):
        self.armed = True
        self.last_x = None

    def returned(self, x=None, width=None, side=None):
        # A confirmed return warp is already inside the source screen. Do not
        # require a second inward gesture before the user can cross again.
        self.armed = (x is not None and width is not None
                      and 0 < x < width - 1)
        self.last_x = x

    def update(self, x, width, side, dx=None, allow_entry=True):
        if dx is None:
            dx = 0 if self.last_x is None else x - self.last_x
        self.last_x = x
        inward = width - 1 - x if side == 'right' else x
        outward_motion = dx > 0 if side == 'right' else dx < 0
        if not self.armed:
            if inward >= self.REARM_DISTANCE and not outward_motion:
                self.armed = True
            return False
        if allow_entry and inward <= 0 and outward_motion:
            self.armed = False
            return True
        return False
