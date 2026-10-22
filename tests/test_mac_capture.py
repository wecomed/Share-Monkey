"""Mac capture balance through mocks only; no real cursor or event tap is used."""
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == 'darwin', 'Quartz is macOS-only')
class MacCaptureTests(unittest.TestCase):
    def listener(self):
        from sharemouse.mac_mouse import MouseListener
        listener = object.__new__(MouseListener)
        listener._capture_lock = threading.RLock()
        listener._capture_generation = 0
        listener._captured = listener._cursor_hidden = listener._mouse_disassociated = False
        listener._hidden_display = None
        listener._return_position = None
        listener._return_filter = Mock()
        listener.on_failure = Mock()
        listener.is_alive = Mock(return_value=False)
        self.native = Mock()
        methods = {
            'CGMainDisplayID': Mock(return_value=73),
            'CGEventCreate': Mock(return_value=object()),
            'CGEventGetLocation': Mock(return_value=SimpleNamespace(x=800, y=300)),
            'CGDisplayHideCursor': Mock(return_value=0),
            'CGDisplayShowCursor': Mock(return_value=0),
            'CGAssociateMouseAndMouseCursorPosition': Mock(return_value=0),
            'CGWarpMouseCursorPosition': Mock(return_value=0),
        }
        for name, method in methods.items():
            self.native.attach_mock(method, name)
        patcher = patch.multiple('sharemouse.mac_mouse.Quartz', **methods)
        patcher.start()
        self.addCleanup(patcher.stop)
        return listener

    def test_repeated_capture_release_balances_hide_and_show(self):
        listener = self.listener()
        for _ in range(50):
            listener.capture_mouse(); listener.capture_mouse()
            listener.release_mouse((100, 200)); listener.release_mouse()
            self.assertFalse(listener._captured or listener._cursor_hidden or listener._mouse_disassociated)
        self.assertEqual(self.native.CGDisplayHideCursor.call_count, 50)
        self.assertEqual(self.native.CGDisplayShowCursor.call_count, 50)
        self.native.CGDisplayShowCursor.assert_called_with(73)
        listener.on_failure.assert_not_called()

    def test_disassociation_failure_rolls_back_successful_hide(self):
        listener = self.listener()
        self.native.CGAssociateMouseAndMouseCursorPosition.side_effect = [1, 0]
        with self.assertRaises(RuntimeError):
            listener.capture_mouse()
        self.native.CGDisplayShowCursor.assert_called_once_with(73)
        self.assertFalse(listener._captured or listener._cursor_hidden or listener._mouse_disassociated)

    def test_bridge_exception_after_disassociation_also_rolls_back(self):
        listener = self.listener()
        self.native.CGAssociateMouseAndMouseCursorPosition.side_effect = [RuntimeError('bridge failed'), 0]
        with self.assertRaisesRegex(RuntimeError, 'bridge failed'):
            listener.capture_mouse()
        self.native.CGDisplayShowCursor.assert_called_once_with(73)
        self.assertFalse(listener._cursor_hidden or listener._mouse_disassociated)

    def test_warp_failure_still_shows_and_is_reported_outside_native_block(self):
        listener = self.listener()
        listener.capture_mouse()
        self.native.CGWarpMouseCursorPosition.side_effect = RuntimeError('warp failed')
        listener.release_mouse((100, 200))
        self.native.CGDisplayShowCursor.assert_called_once_with(73)
        listener.on_failure.assert_called_once()
        self.assertFalse(listener._cursor_hidden)

    def test_failed_hide_never_shows_an_unowned_hide_count(self):
        listener = self.listener()
        self.native.CGDisplayHideCursor.return_value = 1
        with self.assertRaises(RuntimeError):
            listener.capture_mouse()
        listener.release_mouse()
        self.native.CGDisplayShowCursor.assert_not_called()

    def test_deferred_old_return_cannot_release_new_capture(self):
        listener = self.listener()
        listener.capture_mouse()
        listener._loop = object()
        callbacks = []
        with patch('sharemouse.mac_mouse.Quartz.CFRunLoopPerformBlock', side_effect=lambda loop, mode, cb: callbacks.append(cb)), patch('sharemouse.mac_mouse.Quartz.CFRunLoopWakeUp'):
            listener.release_mouse((100, 200))
            listener.capture_mouse()  # Cancels the queued return by generation.
            callbacks.pop()()
        self.assertTrue(listener._captured)
        self.native.CGDisplayShowCursor.assert_not_called()

    def test_begin_and_foreground_finish_capture_never_warp_departure_pointer(self):
        listener = self.listener()
        generation = listener.begin_capture((500, 300))
        self.assertTrue(listener._captured)
        self.assertFalse(listener._cursor_hidden or listener._mouse_disassociated)
        self.native.CGDisplayHideCursor.assert_not_called()
        self.native.CGAssociateMouseAndMouseCursorPosition.assert_not_called()
        self.native.CGWarpMouseCursorPosition.assert_not_called()
        listener._loop = object()
        callbacks, completed = [], Mock()
        with patch('sharemouse.mac_mouse.Quartz.CFRunLoopPerformBlock', side_effect=lambda loop, mode, cb: callbacks.append(cb)), patch('sharemouse.mac_mouse.Quartz.CFRunLoopWakeUp'):
            listener.finish_capture(generation, (500, 300), completed)
            completed.assert_not_called()
            callbacks.pop()()
        completed.assert_called_once_with(None)
        self.native.CGDisplayHideCursor.assert_called_once()
        self.assertTrue(listener._cursor_hidden and listener._mouse_disassociated)
        self.native.CGWarpMouseCursorPosition.assert_not_called()

    def test_ui_release_callback_runs_only_after_native_show_and_warp(self):
        listener = self.listener()
        listener.capture_mouse()
        order = []
        self.native.CGDisplayShowCursor.side_effect = lambda display: (order.append('show'), 0)[1]
        self.native.CGWarpMouseCursorPosition.side_effect = lambda point: (order.append('warp'), 0)[1]
        listener.release_mouse((100, 200), after=lambda: order.append('ui_release'))
        self.assertEqual(order, ['warp', 'show', 'ui_release'])

    def test_production_async_capture_writer_motion_and_return_repeat_without_entry_warp(self):
        from sharemouse.core import Screen
        from sharemouse.engine import Engine
        from sharemouse.input import Input
        from sharemouse.mac_capture_ui import CaptureCoordinator
        from sharemouse.switching import EdgeSwitch

        # Input.__init__/start would construct OS controllers and real taps.
        # Instead retain the actual Input methods and MouseListener capture
        # implementation while replacing every OS write and AppKit backend.
        listener = self.listener()
        sender = Engine(True, Screen(1920, 1080), 'right', 'capture-test-password',
                        '', 0, Mock(), clipboard=False, transport='bluetooth')
        receiver = Engine(False, Screen(2560, 1440), 'right', 'capture-test-password',
                          '', 0, Mock(), clipboard=False, transport='bluetooth')
        sender.connected = receiver.connected = True
        sender.peer_screen = receiver.local
        receiver.input = Mock()
        source = Input.__new__(Input)
        source.engine, source.ml = sender, listener
        source.mouse = SimpleNamespace(position=(1919, 270))
        source.listeners = []
        source.physical, source.physical_buttons = set(), set()
        source.held_keys, source.key_payloads, source.held_buttons = {}, {}, set()
        source.edge_switch, source.visibility = EdgeSwitch(), Mock()
        sender.input = source
        listener._loop, listener.stop = object(), Mock()
        self.native.CGEventGetLocation.return_value = SimpleNamespace(x=1919, y=270)
        queued, sent = [], []
        backend = SimpleNamespace(
            park_for_point=lambda point: tuple(point), dispatch=queued.append,
            activate=Mock(), owns_foreground=lambda: True, blank_cursor=Mock(),
            align_to_cursor=lambda: tuple(source.mouse.position),
            verify_park=Mock(), release=Mock(), later=lambda delay, callback: None)
        source.capture_ui = CaptureCoordinator(backend)

        def drain_ui():
            while queued:
                queued.pop(0)()

        def send(message):
            sent.append(message)
            receiver.receive(message)
            while not receiver.outgoing.empty():
                sender.receive(receiver.outgoing.get_nowait())

        def wait_for(predicate):
            deadline = time.monotonic() + 1
            while not predicate() and time.monotonic() < deadline:
                threading.Event().wait(.001)
            self.assertTrue(predicate())

        with patch('sharemouse.mac_mouse.Quartz.CFRunLoopPerformBlock',
                   side_effect=lambda loop, mode, callback: callback()), \
                patch('sharemouse.mac_mouse.Quartz.CFRunLoopWakeUp'):
            worker = threading.Thread(target=sender.writer, args=(SimpleNamespace(send=send),), daemon=True)
            worker.start()
            try:
                for epoch in range(1, 21):
                    self.native.CGWarpMouseCursorPosition.reset_mock()
                    source.move(1919, 270, dx=1)
                    self.assertTrue(sender.remote)
                    self.assertFalse(source.capture_token.ready.is_set())
                    self.assertFalse(any(m['type'] == 'enter' and m['epoch'] == epoch for m in sent))
                    drain_ui()
                    wait_for(lambda: receiver.active and receiver.received_epoch == epoch)
                    self.assertEqual(source.capture_token.park, (1919, 270))
                    self.native.CGWarpMouseCursorPosition.assert_not_called()
                    for _ in range(30):
                        source.mac_move(1919, 270, 3, 1)
                    wait_for(lambda: any(m['type'] == 'pointer' and m['epoch'] == epoch for m in sent))
                    self.assertTrue(sender.remote)
                    self.assertIsNone(sender.pending_return)
                    self.native.CGWarpMouseCursorPosition.assert_not_called()
                    sender.return_local(.25)
                    wait_for(lambda: sender.pending_return is None and not receiver.active)
                    drain_ui()
                    self.assertFalse(listener._captured or listener._cursor_hidden or listener._mouse_disassociated)
                    self.native.CGWarpMouseCursorPosition.assert_called()
                self.assertEqual(sum(m['type'] == 'enter' for m in sent), 20)
                self.assertEqual(sum(m['type'] == 'leave' for m in sent), 20)
            finally:
                sender.stop()
                receiver.stop()
                worker.join(1)
                listener._loop = None
                drain_ui()


if __name__ == '__main__':
    unittest.main()
