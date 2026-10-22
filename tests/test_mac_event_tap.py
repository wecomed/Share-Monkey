"""Native ownership regressions without registering a tap or posting input."""
import ctypes
import gc
import sys
import unittest
import weakref
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == 'darwin', 'CoreGraphics ownership')
class EventTapTests(unittest.TestCase):
    def setUp(self):
        import objc
        import Quartz
        from sharemouse import mac_event_tap
        self.objc, self.Q, self.M = objc, Quartz, mac_event_tap
        self.event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved,
                                                  (100, 100), 0)
        self.pointer = objc.pyobjc_id(self.event)
        self.errors = []
        self.taps = []

    def tearDown(self):
        for tap in self.taps:
            tap.close()

    def tap(self, handler):
        tap = self.M.EventTap(handler, lambda: self.errors.append(sys.exc_info()))
        self.taps.append(tap)
        return tap

    def invoke(self, tap):
        return tap.callback(None, self.Q.kCGEventMouseMoved, self.pointer, None)

    def test_pass_through_does_not_retain_each_event(self):
        tap = self.tap(lambda proxy, kind, event, refcon: event)
        before = sys.getrefcount(self.event)
        for _ in range(10000):
            self.assertEqual(self.invoke(tap), self.pointer)
        self.assertEqual(sys.getrefcount(self.event), before)
        self.assertFalse(self.errors)

    def test_suppression_does_not_release_the_borrowed_event(self):
        tap = self.tap(lambda *args: None)
        before = sys.getrefcount(self.event)
        self.assertIsNone(self.invoke(tap))
        self.assertEqual(sys.getrefcount(self.event), before)
        self.assertEqual(self.Q.CGEventGetType(self.event), self.Q.kCGEventMouseMoved)

    def test_modification_survives_callback(self):
        def modify(proxy, kind, event, refcon):
            self.Q.CGEventSetIntegerValueField(event, self.Q.kCGMouseEventDeltaX, 17)
            return event
        self.assertEqual(self.invoke(self.tap(modify)), self.pointer)
        self.assertEqual(self.Q.CGEventGetIntegerValueField(
            self.event, self.Q.kCGMouseEventDeltaX), 17)

    def test_temporary_replacement_survives_until_system_consumes_it(self):
        tap = self.tap(lambda proxy, kind, event, refcon: self.Q.CGEventCreateCopy(event))
        pointer = self.invoke(tap)
        self.assertNotEqual(pointer, self.pointer)
        try:
            replacement = self.objc.objc_object(c_void_p=pointer)
            self.assertEqual(self.Q.CGEventGetType(replacement), self.Q.kCGEventMouseMoved)
        finally:
            self.M._CF.CFRelease(pointer)  # Simulate CoreGraphics consuming it.
        self.assertFalse(self.errors)

    def test_errors_and_invalid_results_fail_open(self):
        for result in (object(), 42):
            tap = self.tap(lambda *args: result)
            self.assertEqual(self.invoke(tap), self.pointer)
        def failed(*args):
            raise KeyboardInterrupt('synthetic callback failure')
        self.assertEqual(self.invoke(self.tap(failed)), self.pointer)
        self.assertEqual(len(self.errors), 3)

    def test_close_breaks_callback_ownership_cycle(self):
        tap = self.tap(lambda *args: None)
        reference = weakref.ref(tap)
        self.taps.remove(tap)
        tap.close()
        del tap
        gc.collect()
        self.assertIsNone(reference())

    def test_failed_native_creation_still_cleans_up(self):
        from types import SimpleNamespace
        listener = SimpleNamespace(_handler=lambda *args: None, _EVENTS=1,
                                   _queue=Mock(), stop=Mock())
        with patch.object(self.M._CG, 'CGEventTapCreate', return_value=None):
            self.assertIsNone(self.M.create_listener_tap(listener, 0, 0))
        bridge = weakref.ref(listener._event_tap_bridge)
        self.M.close_listener_tap(listener)
        gc.collect()
        self.assertIsNone(bridge())

    def test_mouse_and_keyboard_use_the_owned_callback(self):
        from sharemouse.mac_keyboard import KeyboardListener
        from sharemouse.mac_mouse import MouseListener
        listeners = (MouseListener(on_delta=lambda *args: None), KeyboardListener())
        for listener in listeners:
            with self.subTest(listener=type(listener).__name__):
                with patch.object(self.Q, 'CGEventTapCreate',
                                  side_effect=AssertionError('Leaking bridge used')), \
                     patch('pynput._util.darwin.CGEventTapCreate',
                           side_effect=AssertionError('Leaking bridge used')), \
                     patch.object(self.M._CG, 'CGEventTapCreate', return_value=None) as create:
                    try:
                        self.assertIsNone(listener._create_event_tap())
                        self.assertIsInstance(listener._event_tap_bridge, self.M.EventTap)
                        self.assertIs(create.call_args.args[-2], listener._event_tap_bridge.callback)
                    finally:
                        self.M.close_listener_tap(listener)


if __name__ == '__main__':
    unittest.main()
