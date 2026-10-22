"""Filter a short return backlog without assuming the OS event clock's units."""
import time


class ReturnEventFilter:
    MAX_AGE_NS = 250_000_000

    def __init__(self, raw_clock, ns_clock=None):
        self.raw_clock = raw_clock
        self.ns_clock = ns_clock or (lambda: time.monotonic_ns())
        self.cutoffs = ()
        self.mark_ns = 0

    @property
    def active(self):
        return bool(self.cutoffs)

    def mark_return(self):
        self.mark_ns = self.ns_clock()
        self.cutoffs = (self.raw_clock(), self.mark_ns)

    def stale(self, timestamp):
        if not self.active:
            return False
        if self.ns_clock() - self.mark_ns > self.MAX_AGE_NS:
            self.cutoffs = ()
            return False
        if timestamp <= 0:
            # Some drivers omit timestamps; that must never disable input.
            return False
        # ARM hardware at the HID tap can carry raw Mach ticks, whereas posted
        # events carry nanoseconds. Choose the matching return-clock domain.
        cutoff = min(self.cutoffs, key=lambda value: abs(timestamp - value))
        if timestamp <= cutoff:
            return True
        # The first fresh event is a FIFO boundary: the return backlog drained.
        self.cutoffs = ()
        return False
