"""Window lifecycle regression without creating an application, window or tap.

Only allocate the delegate object; every collaborator is a Python test double.
These checks exercise the UI/native-release barrier, not real macOS focus.
"""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


@unittest.skipUnless(sys.platform == 'darwin', 'AppKit delegate is macOS-only')
class MacWindowLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sharemouse.native_mac import Desktop
        cls.Desktop = Desktop

    def setUp(self):
        # Do not call setup(): it owns the real NSApplication, status item,
        # discovery and capture coordinator. alloc/init alone creates no UI.
        self.desktop = self.Desktop.alloc().init()
        self.desktop.closed = False
        self.desktop.window_open_requested = False
        self.desktop.app = Mock(spec=['activateIgnoringOtherApps_', 'setActivationPolicy_'])
        self.desktop.window = Mock(spec=['makeKeyAndOrderFront_', 'orderOut_'])
        self.desktop.capture_ui = SimpleNamespace(current=None)
        self.engine = Mock()
        self.engine.controller = True
        self.engine.connected = True
        self.engine.remote = False
        self.engine.pending_return = None
        self.desktop.session = Mock()
        self.desktop.session.engine = self.engine
        self.desktop.guard = Mock()
        self.desktop.guard.consume_show_request.return_value = False
        self.desktop.activity = object()
        self.activity = self.desktop.activity
        # A lifecycle regression must not end a real process activity while
        # this suite is checking that closing a window leaves it alone.
        process_info = patch('sharemouse.native_mac.F.NSProcessInfo')
        self.process_info = process_info.start()
        self.addCleanup(process_info.stop)

    def assert_not_shown(self):
        self.desktop.window.makeKeyAndOrderFront_.assert_not_called()
        self.desktop.app.activateIgnoringOtherApps_.assert_not_called()

    def assert_connection_untouched(self):
        self.engine.stop.assert_not_called()
        self.desktop.session.disconnect.assert_not_called()
        self.desktop.session.close.assert_not_called()
        self.assertIs(self.desktop.activity, self.activity)
        self.desktop.app.setActivationPolicy_.assert_not_called()
        self.process_info.processInfo.assert_not_called()

    def test_normal_show_activates_window_without_changing_policy(self):
        order = Mock()
        order.attach_mock(self.desktop.window.makeKeyAndOrderFront_, 'window')
        order.attach_mock(self.desktop.app.activateIgnoringOtherApps_, 'activate')

        self.desktop.show_window()

        self.assertEqual(order.mock_calls, [call.window(None), call.activate(True)])
        self.assertFalse(self.desktop.window_open_requested)
        self.engine.return_local.assert_not_called()
        self.assert_connection_untouched()

    def test_show_requests_return_before_cancelling_capture_token(self):
        token = Mock()
        self.desktop.capture_ui.current = token
        self.engine.remote = True
        events = []

        def request_return():
            events.append('return')
            self.engine.remote = False
            self.engine.pending_return = (7, .5, 1234)

        self.engine.return_local.side_effect = request_return

        def cancel_token():
            # Waking the writer before remote becomes false can let it treat
            # intentional cancellation as capture failure and stop the engine.
            self.assertFalse(self.engine.remote)
            self.assertIsInstance(self.engine.pending_return, tuple)
            events.append('cancel')

        token.cancel.side_effect = cancel_token
        self.desktop.show_window()

        self.assertEqual(events, ['return', 'cancel'])
        self.assertTrue(self.desktop.window_open_requested)
        self.assert_not_shown()
        self.assert_connection_untouched()

    def test_cancelled_token_remains_barrier_until_native_release(self):
        token = Mock()
        self.desktop.capture_ui.current = token
        self.desktop.show_window()
        token.cancel.assert_called_once()

        # A cancelled token does not mean the HID run loop has reassociated
        # and shown the pointer or that the capture panel has been dismissed.
        self.engine.pending_return = None
        self.desktop._flush_window_request()
        self.assert_not_shown()
        self.assertTrue(self.desktop.window_open_requested)

        self.desktop.capture_ui.current = None
        self.desktop._flush_window_request()
        self.desktop.window.makeKeyAndOrderFront_.assert_called_once_with(None)
        self.desktop.app.activateIgnoringOtherApps_.assert_called_once_with(True)
        self.assertFalse(self.desktop.window_open_requested)
        self.assert_connection_untouched()

    def test_pending_return_remains_barrier_after_native_token_is_cleared(self):
        self.engine.pending_return = (3, .3, 1234)
        self.desktop.show_window()
        self.assert_not_shown()
        self.assertTrue(self.desktop.window_open_requested)

        self.engine.pending_return = None
        self.desktop.tick_(None)
        self.desktop.window.makeKeyAndOrderFront_.assert_called_once_with(None)
        self.desktop.app.activateIgnoringOtherApps_.assert_called_once_with(True)
        self.assertFalse(self.desktop.window_open_requested)
        self.assert_connection_untouched()

    def test_hide_only_orders_out_window_and_keeps_activity_and_connection(self):
        self.desktop.hide_window()

        self.desktop.window.orderOut_.assert_called_once_with(None)
        self.assertFalse(self.desktop.window_open_requested)
        self.assert_not_shown()
        self.engine.return_local.assert_not_called()
        self.assert_connection_untouched()

    def test_close_cancels_queued_show_and_release_cannot_reopen_window(self):
        token = Mock()
        self.desktop.capture_ui.current = token
        self.desktop.show_window()
        self.assertTrue(self.desktop.window_open_requested)

        self.assertFalse(self.desktop.windowShouldClose_(self.desktop.window))
        self.assertFalse(self.desktop.window_open_requested)
        self.desktop.window.orderOut_.assert_called_once_with(None)
        self.desktop.capture_ui.current = None
        self.engine.pending_return = None
        self.desktop.tick_(None)

        self.assert_not_shown()
        self.assert_connection_untouched()

    def test_flush_without_request_never_reopens_hidden_window(self):
        self.desktop._flush_window_request()
        self.assert_not_shown()
        self.assert_connection_untouched()

    def test_closed_delegate_cannot_flush_stale_show_request(self):
        self.desktop.window_open_requested = True
        self.desktop.closed = True
        self.desktop._flush_window_request()
        self.assert_not_shown()
        self.assert_connection_untouched()

    def test_closed_tick_does_not_poll_or_consume_reopen_request(self):
        self.desktop.closed = True
        self.desktop.guard.consume_show_request.return_value = True
        self.desktop.window_open_requested = True

        self.desktop.tick_(None)

        self.desktop.session.poll.assert_not_called()
        self.desktop.guard.consume_show_request.assert_not_called()
        self.assert_not_shown()
        self.assert_connection_untouched()

    def test_tick_polls_then_consumes_show_request_once(self):
        order = Mock()
        order.attach_mock(self.desktop.session.poll, 'poll')
        order.attach_mock(self.desktop.guard.consume_show_request, 'request')
        order.attach_mock(self.desktop.window.makeKeyAndOrderFront_, 'window')
        order.attach_mock(self.desktop.app.activateIgnoringOtherApps_, 'activate')
        self.desktop.guard.consume_show_request.return_value = True

        self.desktop.tick_(None)

        self.assertEqual(order.mock_calls, [call.poll(), call.request(),
                                            call.window(None), call.activate(True)])
        self.desktop.guard.consume_show_request.return_value = False
        self.desktop.tick_(None)
        self.desktop.window.makeKeyAndOrderFront_.assert_called_once()
        self.assert_connection_untouched()

    def test_tick_flushes_pending_request_even_without_instance_guard(self):
        self.desktop.guard = None
        self.desktop.window_open_requested = True

        self.desktop.tick_(None)

        self.desktop.session.poll.assert_called_once_with()
        self.desktop.window.makeKeyAndOrderFront_.assert_called_once_with(None)
        self.assertFalse(self.desktop.window_open_requested)
        self.assert_connection_untouched()

    def test_closing_last_window_does_not_terminate_application(self):
        self.assertFalse(self.desktop.applicationShouldTerminateAfterLastWindowClosed_(None))
        self.assert_connection_untouched()


if __name__ == '__main__':
    unittest.main()
