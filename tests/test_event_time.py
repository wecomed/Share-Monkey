import unittest
from sharemouse.event_time import ReturnEventFilter


class EventTimeTests(unittest.TestCase):
    def filter(self):
        self.now = 171450286841833
        result = ReturnEventFilter(lambda: 4114806884204, lambda: self.now)
        result.mark_return()
        return result

    def test_arm_mach_ticks_reject_old_and_accept_fresh_hardware(self):
        f = self.filter()
        self.assertTrue(f.stale(4114806884203))
        self.assertFalse(f.stale(4152947091044))
        self.assertFalse(f.active)

    def test_nanosecond_events_remain_supported(self):
        f = self.filter()
        self.assertTrue(f.stale(self.now - 1))
        self.assertFalse(f.stale(self.now + 1))
        self.assertFalse(f.active)

    def test_zero_or_unexpected_clock_cannot_permanently_disable_input(self):
        f = self.filter()
        self.assertFalse(f.stale(0))
        self.assertTrue(f.stale(123))
        self.now += f.MAX_AGE_NS + 1
        self.assertFalse(f.stale(124))
        self.assertFalse(f.active)

    def test_each_return_takes_a_new_clock_boundary(self):
        f = self.filter()
        self.assertFalse(f.stale(self.now + 1))
        self.now += 1000
        f.mark_return()
        self.assertTrue(f.stale(self.now - 1))
        self.assertFalse(f.stale(self.now + 1))
