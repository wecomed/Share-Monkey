"""Public capture mechanism ordering, with no AppKit activation or cursor writes."""
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sharemouse.mac_capture_ui import AppKitShield, CaptureCoordinator, CaptureToken, _classes
import sharemouse.mac_capture_ui as capture_ui


class Backend:
    def __init__(self):
        self.queued, self.timers, self.timer_delays = [], [], []
        self.front = True
        self.activate = Mock()
        self.release = Mock()
        self.blank_cursor = Mock()
        self.verify_park = Mock()
        self.align_to_cursor = Mock(return_value=(500, 300))

    def park_for_point(self, point):
        return 500, 300

    def dispatch(self, callback):
        self.queued.append(callback)

    def later(self, seconds, callback):
        self.timers.append(callback)
        self.timer_delays.append(seconds)

    def owns_foreground(self):
        return self.front

    def drain(self):
        while self.queued:
            self.queued.pop(0)()


class CaptureUITests(unittest.TestCase):
    def coordinator(self):
        backend = Backend()
        coordinator = CaptureCoordinator(backend)
        token = coordinator.token((1919, 300))
        native, failure = Mock(), Mock()
        coordinator.acquire(token, native, failure)
        return coordinator, backend, token, native, failure

    def test_wait_requires_foreground_native_prepare_and_second_ui_verification(self):
        coordinator, backend, token, native, failure = self.coordinator()
        self.assertFalse(token.ready.is_set())
        backend.drain()
        backend.activate.assert_called_once_with((500, 300))
        self.assertFalse(token.ready.is_set())
        native.assert_called_once()
        native.call_args.args[1](None)
        self.assertFalse(token.ready.is_set())
        backend.drain()
        self.assertTrue(token.wait(threading.Event(), .1))
        backend.verify_park.assert_called_once_with((500, 300))
        failure.assert_not_called()

    def test_cancel_before_main_queue_never_activates(self):
        coordinator, backend, token, native, _ = self.coordinator()
        token.cancel()
        coordinator.release(token)
        backend.drain()
        backend.activate.assert_not_called()
        native.assert_not_called()
        backend.release.assert_called_once()

    def test_old_release_and_prepare_cannot_close_or_ready_new_capture(self):
        coordinator, backend, old, old_native, _ = self.coordinator()
        backend.drain()
        new = coordinator.token((1919, 300))
        native, failure = Mock(), Mock()
        coordinator.acquire(new, native, failure)
        coordinator.release(old)
        old_native.call_args.args[1](None)
        backend.drain()
        backend.release.assert_not_called()
        self.assertFalse(new.ready.is_set())
        native.call_args.args[1](None)
        backend.drain()
        self.assertTrue(new.prepared)

    def test_lost_foreground_before_native_completion_never_allows_enter(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        backend.front = False
        native.call_args.args[1](None)
        backend.drain()
        with self.assertRaisesRegex(RuntimeError, '前台'):
            token.wait(threading.Event(), .1)
        failure.assert_called_once()

    def test_focus_failure_callback_publishes_local_state_before_waking_writer(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        state = SimpleNamespace(remote=True)

        def return_before_wake(reason):
            self.assertIsInstance(token.error, capture_ui.CaptureFocusLost)
            self.assertFalse(token.ready.is_set())
            state.remote = False

        failure.side_effect = return_before_wake
        backend.front = False
        native.call_args.args[1](None)
        backend.drain()

        self.assertFalse(state.remote)
        self.assertTrue(token.ready.is_set())
        failure.assert_called_once()
        self.assertFalse(token.prepared)

    def test_failed_failure_callback_still_wakes_waiting_writer(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        failure.side_effect = RuntimeError('failed to recover capture')
        backend.front = False
        native.call_args.args[1](None)
        backend.drain()

        self.assertTrue(token.ready.is_set())
        self.assertTrue(token.cancelled)
        backend.release.assert_called_once()

    def test_focus_loss_after_capture_fails_closed_without_activation_loop(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        native.call_args.args[1](None)
        backend.drain()
        backend.front = False
        asserted_before_focus_loss = backend.blank_cursor.call_count
        backend.timers.pop()()
        failure.assert_called_once()
        backend.activate.assert_called_once()
        self.assertEqual(backend.blank_cursor.call_count, asserted_before_focus_loss)

    def test_monitor_reasserts_cursor_without_reactivating_application(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        native.call_args.args[1](None)
        backend.drain()
        assertions = backend.blank_cursor.call_count
        backend.timers.pop()()
        self.assertEqual(backend.blank_cursor.call_count, assertions + 1)
        backend.activate.assert_called_once()
        failure.assert_not_called()

    def test_monitor_cursor_failure_stops_capture(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        native.call_args.args[1](None)
        backend.drain()
        backend.blank_cursor.side_effect = RuntimeError('opaque current cursor')
        backend.timers.pop()()
        failure.assert_called_once()
        self.assertIn('opaque', str(token.error))

    def test_activation_timeout_and_native_errors_propagate_to_writer(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.front = False
        token.deadline = 0
        backend.drain()
        native.assert_not_called()
        with self.assertRaises(RuntimeError):
            token.wait(threading.Event(), .1)
        failure.assert_called_once()
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        native.call_args.args[1](RuntimeError('native failure'))
        backend.drain()
        with self.assertRaisesRegex(RuntimeError, 'native failure'):
            token.wait(threading.Event(), .1)

    def test_stopped_wait_never_requires_appkit_progress(self):
        stop = threading.Event(); stop.set()
        self.assertFalse(CaptureToken(1, (10, 20)).wait(stop, .05))

    def test_f12_cancels_writer_wait_without_releasing_source_capture(self):
        from sharemouse.input import Input
        adapter = Input.__new__(Input)
        adapter.engine = SimpleNamespace(remote=True, pending_return=None)
        token = adapter.capture_token = CaptureToken(1, (500, 300))
        results = []
        waiter = threading.Thread(target=lambda: results.append(adapter.wait_capture_ready(threading.Event(), 1.5)))
        waiter.start()
        adapter.engine.remote = False
        adapter.engine.pending_return = (1, .5, 99)
        waiter.join(.2)
        self.assertEqual(results, [False])
        self.assertFalse(token.cancelled)
        self.assertFalse(token.ready.is_set())

    def test_old_cancelled_wait_cannot_fail_a_new_active_capture(self):
        from sharemouse.input import Input
        adapter = Input.__new__(Input)
        adapter.engine = SimpleNamespace(remote=True, pending_return=None)
        old = adapter.capture_token = CaptureToken(1, (1919, 300))
        new = CaptureToken(2, (1919, 400))
        old.cancel()

        def delayed_wakeup(timeout):
            # The old writer was descheduled after cancellation. By the time
            # Event.wait returns, the next crossing owns a different token.
            adapter.capture_token = new
            self.assertTrue(adapter.engine.remote)
            self.assertIsNone(adapter.engine.pending_return)
            return True

        with patch.object(old.ready, 'wait', side_effect=delayed_wakeup):
            self.assertFalse(adapter.wait_capture_ready(threading.Event(), .1))
        self.assertIs(adapter.capture_token, new)
        self.assertFalse(new.cancelled)
        self.assertFalse(new.ready.is_set())

    def test_current_capture_native_failure_still_propagates_to_writer(self):
        from sharemouse.input import Input
        adapter = Input.__new__(Input)
        adapter.engine = SimpleNamespace(remote=True, pending_return=None)
        token = adapter.capture_token = CaptureToken(1, (1919, 300))
        token.error = RuntimeError('native disassociation failed')
        token.ready.set()

        with self.assertRaisesRegex(RuntimeError, 'native disassociation failed'):
            adapter.wait_capture_ready(threading.Event(), .1)
        self.assertIs(adapter.capture_token, token)

    def test_native_completion_aligns_panel_to_actual_cursor_before_ready(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        self.assertEqual(token.initial_park, (500, 300))
        self.assertEqual(token.anchor_retries, 0)
        order = Mock()
        order.attach_mock(backend.align_to_cursor, 'align')
        order.attach_mock(backend.blank_cursor, 'blank')
        order.attach_mock(backend.verify_park, 'verify')
        # A previously forwarded HID event lands during AppKit activation.
        backend.align_to_cursor.return_value = (500, 347)

        native.call_args.args[1](None)
        self.assertFalse(token.ready.is_set())
        backend.drain()

        self.assertEqual([event[0] for event in order.mock_calls], ['align', 'blank', 'verify'])
        backend.verify_park.assert_called_once_with((500, 347))
        self.assertEqual(token.park, (500, 347))
        self.assertEqual(token.initial_park, (500, 300))
        self.assertTrue(token.wait(threading.Event(), .1))
        native.assert_called_once()
        backend.activate.assert_called_once()
        failure.assert_not_called()

    def test_cursor_drift_between_alignment_and_verification_retries_without_recapture(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        backend.align_to_cursor.side_effect = [(500, 347), (500, 361)]
        backend.verify_park.side_effect = [capture_ui.CaptureAnchorChanged('late HID event'), None]

        native.call_args.args[1](None)
        backend.drain()
        self.assertFalse(token.ready.is_set())
        self.assertFalse(token.prepared)
        self.assertIsNone(token.error)
        self.assertEqual(token.anchor_retries, 1)
        self.assertEqual(len(backend.timers), 1)
        self.assertEqual(backend.timer_delays, [.01])
        failure.assert_not_called()

        backend.timers.pop(0)()
        backend.drain()

        self.assertTrue(token.wait(threading.Event(), .1))
        self.assertEqual(token.park, (500, 361))
        self.assertEqual(backend.align_to_cursor.call_count, 2)
        self.assertEqual(backend.verify_park.call_count, 2)
        native.assert_called_once()
        backend.activate.assert_called_once()
        failure.assert_not_called()

    def test_continuous_cursor_drift_has_three_retries_then_fails(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        backend.verify_park.side_effect = capture_ui.CaptureAnchorChanged('cursor never settles')
        native.call_args.args[1](None)
        backend.drain()
        callbacks = 0
        while backend.timers and callbacks < 8:
            backend.timers.pop(0)()
            backend.drain()
            callbacks += 1

        self.assertEqual(callbacks, 3)
        self.assertEqual(backend.timer_delays, [.01, .01, .01])
        self.assertEqual(token.anchor_retries, 3)
        self.assertEqual(backend.verify_park.call_count, 4)
        self.assertFalse(backend.timers)
        self.assertFalse(token.prepared)
        self.assertTrue(token.ready.is_set())
        with self.assertRaisesRegex(RuntimeError, 'cursor never settles'):
            token.wait(threading.Event(), .1)
        failure.assert_called_once()
        native.assert_called_once()
        backend.activate.assert_called_once()

    def test_cancelled_alignment_retry_never_prepares_or_reactivates(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        backend.verify_park.side_effect = capture_ui.CaptureAnchorChanged('late HID event')
        native.call_args.args[1](None)
        backend.drain()
        retry = backend.timers.pop(0)
        coordinator.release(token)
        backend.drain()

        retry()
        backend.drain()

        self.assertFalse(token.prepared)
        self.assertFalse(token.wait(threading.Event(), .1, cancelled=lambda: True))
        self.assertEqual(backend.align_to_cursor.call_count, 1)
        backend.release.assert_called_once()
        backend.activate.assert_called_once()
        failure.assert_not_called()

    def test_old_alignment_retry_cannot_ready_new_generation(self):
        coordinator, backend, old, old_native, failure = self.coordinator()
        backend.drain()
        backend.verify_park.side_effect = capture_ui.CaptureAnchorChanged('late HID event')
        old_native.call_args.args[1](None)
        backend.drain()
        retry = backend.timers.pop(0)
        new = coordinator.token((0, 400))

        retry()
        backend.drain()

        self.assertIs(coordinator.current, new)
        self.assertFalse(old.prepared)
        self.assertFalse(new.prepared)
        self.assertFalse(new.ready.is_set())
        self.assertEqual(backend.align_to_cursor.call_count, 1)
        backend.activate.assert_called_once()
        failure.assert_not_called()

    def test_monitor_tracks_late_cursor_position_by_moving_only_panel(self):
        coordinator, backend, token, native, failure = self.coordinator()
        backend.drain()
        native.call_args.args[1](None)
        backend.drain()
        alignments = backend.align_to_cursor.call_count
        backend.align_to_cursor.return_value = (500, 370)

        backend.timers.pop(0)()

        self.assertEqual(backend.align_to_cursor.call_count, alignments + 1)
        self.assertEqual(token.park, (500, 370))
        self.assertEqual(token.initial_park, (500, 300))
        self.assertTrue(token.prepared)
        backend.activate.assert_called_once()
        native.assert_called_once()
        failure.assert_not_called()


class InputCaptureFailureTests(unittest.TestCase):
    def capture(self):
        from sharemouse.core import Screen
        from sharemouse.engine import Engine
        from sharemouse.input import Input
        from sharemouse.switching import EdgeSwitch

        # Real Engine/Input methods, but no controllers, event taps, socket,
        # telemetry worker, AppKit initialization or native cursor operation.
        input_sys = patch('sharemouse.input.sys', SimpleNamespace(platform='darwin'))
        input_sys.start()
        self.addCleanup(input_sys.stop)
        engine = Engine(True, Screen(1920, 1080), 'right', 'capture-test-password',
                        '', 0, Mock(), clipboard=False, transport='bluetooth')
        adapter = Input.__new__(Input)
        adapter.engine = engine
        adapter.mouse = SimpleNamespace(position=(1919, 300))
        adapter.ml = Mock()
        adapter.ml.begin_capture.return_value = 7

        def release_mouse(position=None, after=None):
            if after is not None:
                after()

        adapter.ml.release_mouse.side_effect = release_mouse
        adapter.listeners = []
        adapter.physical, adapter.physical_buttons = set(), set()
        adapter.held_keys, adapter.key_payloads, adapter.held_buttons = {}, {}, set()
        adapter.visibility = Mock()
        adapter.edge_switch = EdgeSwitch()
        backend = Backend()
        adapter.capture_ui = CaptureCoordinator(backend)
        engine.input, engine.connected, engine.epoch = adapter, True, 7
        adapter.capture()
        engine.remote = True
        return engine, adapter, backend, adapter.capture_token

    def test_lost_focus_during_prepare_or_monitor_returns_without_disconnecting(self):
        for stage in ('preparing', 'active'):
            with self.subTest(stage=stage):
                engine, adapter, backend, token = self.capture()
                backend.drain()
                completed = adapter.ml.finish_capture.call_args.args[2]
                if stage == 'preparing':
                    backend.front = False
                    completed(None)
                    backend.drain()
                else:
                    completed(None)
                    backend.drain()
                    self.assertTrue(token.prepared)
                    backend.front = False
                    backend.timers.pop(0)()

                self.assertIsInstance(token.error, capture_ui.CaptureFocusLost)
                self.assertTrue(engine.connected)
                self.assertFalse(engine.stop_event.is_set())
                self.assertFalse(engine.remote)
                self.assertIsInstance(engine.pending_return, tuple)
                leave = engine.outgoing.get_nowait()
                self.assertEqual((leave['type'], leave['epoch']), ('leave', 7))
                self.assertIn('连接保持', engine.status.call_args.args[0])
                self.assertFalse(adapter.wait_capture_ready(threading.Event(), .1))

                # Preserve the ordinary leave/left cursor-release barrier.
                engine.receive({'type': 'left', 'epoch': 7})
                backend.drain()
                self.assertTrue(engine.connected)
                self.assertFalse(engine.stop_event.is_set())
                self.assertIsNone(engine.pending_return)
                self.assertIsNone(adapter.capture_token)
                self.assertIsNone(adapter.capture_ui.current)
                backend.release.assert_called_once()

    def test_native_cursor_and_activation_failures_still_stop_connection(self):
        for fault in ('native', 'cursor', 'activation_timeout'):
            with self.subTest(fault=fault):
                engine, adapter, backend, token = self.capture()
                if fault == 'activation_timeout':
                    backend.front = False
                    token.deadline = 0
                    backend.drain()
                else:
                    backend.drain()
                    if fault == 'native':
                        error = RuntimeError('native hide failed')
                    else:
                        error = None
                        backend.blank_cursor.side_effect = RuntimeError('opaque current cursor')
                    adapter.ml.finish_capture.call_args.args[2](error)
                    backend.drain()

                self.assertFalse(engine.connected)
                self.assertTrue(engine.stop_event.is_set())
                self.assertFalse(engine.remote)
                self.assertNotIn('连接保持', engine.status.call_args.args[0])

    def test_stale_failure_callback_cannot_return_or_stop_new_capture(self):
        engine, adapter, backend, old = self.capture()
        backend.drain()
        new = adapter.capture_ui.token((1919, 400))
        adapter.capture_token = new
        engine.return_local = Mock()
        engine.stop = Mock()
        engine.status.reset_mock()

        for error in (capture_ui.CaptureFocusLost('old focus loss'), RuntimeError('old native error')):
            with self.subTest(error=type(error).__name__):
                old.error = error
                old.on_failure(str(error))

                engine.return_local.assert_not_called()
                engine.stop.assert_not_called()
                engine.status.assert_not_called()
                self.assertTrue(engine.remote)
                self.assertTrue(engine.connected)
                self.assertIs(adapter.capture_token, new)


class CursorViewTests(unittest.TestCase):
    def view(self):
        class BaseView:
            def updateTrackingAreas(self):
                self.super_updated = True

            def bounds(self):
                return (0, 0, 96, 96)

            def addTrackingArea_(self, area):
                self.areas.append(area)

            def removeTrackingArea_(self, area):
                self.areas.remove(area)

            def window(self):
                return self.panel

        class Tracking:
            @classmethod
            def alloc(cls):
                return cls()

            def initWithRect_options_owner_userInfo_(self, rect, options, owner, info):
                self.options, self.owner = options, owner
                return self

        api = SimpleNamespace(NSPanel=object, NSView=BaseView, NSTrackingArea=Tracking,
                              NSTrackingCursorUpdate=1, NSTrackingMouseEnteredAndExited=2,
                              NSTrackingActiveAlways=4, NSTrackingInVisibleRect=8,
                              NSTrackingEnabledDuringMouseDrag=16)
        with patch('sharemouse.mac_capture_ui._CLASSES', None):
            _, view_class = _classes(api, SimpleNamespace(super=super))
        view = view_class()
        view.areas = []
        view.blank = SimpleNamespace(set=Mock())
        view.panel = SimpleNamespace(isVisible=lambda: True, isKeyWindow=lambda: True)
        view.capture_active = True
        return view

    def test_panel_never_constrains_its_frame_away_from_departure_edge(self):
        api = SimpleNamespace(NSPanel=object, NSView=object)
        with patch('sharemouse.mac_capture_ui._CLASSES', None):
            panel_class, _ = _classes(api, SimpleNamespace(super=super))
        frame = (-48, 500, 96, 96)
        self.assertIs(panel_class().constrainFrameRect_toScreen_(frame, object()), frame)

    def test_tracking_area_requests_cursor_updates_and_replaces_old_area(self):
        view = self.view()
        view.updateTrackingAreas()
        self.assertTrue(view.super_updated)
        self.assertEqual(view.areas[0].options, 31)
        self.assertIs(view.areas[0].owner, view)
        previous = view.areas[0]
        view.updateTrackingAreas()
        self.assertEqual(len(view.areas), 1)
        self.assertIsNot(view.areas[0], previous)

    def test_cursor_update_and_entry_reapply_blank_but_dismissed_panel_cannot(self):
        view = self.view()
        view.cursorUpdate_(None)
        view.mouseEntered_(None)
        self.assertEqual(view.blank.set.call_count, 2)
        view.capture_active = False
        view.cursorUpdate_(None)
        self.assertEqual(view.blank.set.call_count, 2)
        view.capture_active = True
        view.panel.isKeyWindow = lambda: False
        view.mouseEntered_(None)
        self.assertEqual(view.blank.set.call_count, 2)


class CursorAssertionTests(unittest.TestCase):
    def test_capture_preserves_exact_departure_position_including_edges(self):
        shield = AppKitShield.__new__(AppKitShield)
        for point in ((0, 0), (1919.75, 1079), (-1200, -40), (960, 524)):
            with self.subTest(point=point):
                self.assertEqual(shield.park_for_point(point), point)

    def shield(self, replacement):
        shield = AppKitShield.__new__(AppKitShield)
        shield.cursor_assertions = shield.cursor_replacements_detected = 0
        state = SimpleNamespace(current='opaque arrow')
        shield.A = SimpleNamespace(NSCursor=SimpleNamespace(currentCursor=lambda: state.current))
        shield.cursor_pixels = lambda cursor: {'verified': True, 'transparent': cursor == 'transparent copy'}
        shield.blank = SimpleNamespace(set=lambda: setattr(state, 'current', replacement))
        return shield

    def test_reassertion_verifies_actual_transparency_even_if_cursor_was_copied(self):
        shield = self.shield('transparent copy')
        shield.blank_cursor()
        self.assertEqual(shield.cursor_assertions, 1)
        self.assertEqual(shield.cursor_replacements_detected, 1)

    def test_opaque_current_cursor_after_set_raises_instead_of_claiming_ready(self):
        shield = self.shield('opaque arrow')
        with self.assertRaisesRegex(RuntimeError, '透明光标'):
            shield.blank_cursor()


class ShieldAlignmentTests(unittest.TestCase):
    def shield(self, point=(1919.75, 310.5)):
        shield = AppKitShield.__new__(AppKitShield)
        state = SimpleNamespace(point=SimpleNamespace(x=point[0], y=point[1]),
                                origin=(0, 0))

        def frame():
            return SimpleNamespace(origin=SimpleNamespace(x=state.origin[0], y=state.origin[1]),
                                   size=SimpleNamespace(width=96, height=96))

        def contains(position, rect):
            return (rect.origin.x <= position[0] < rect.origin.x + rect.size.width
                    and rect.origin.y <= position[1] < rect.origin.y + rect.size.height)

        shield.A = SimpleNamespace(NSMakePoint=lambda x, y: (x, y), NSPointInRect=contains)
        shield.Q = SimpleNamespace(
            CGEventCreate=Mock(return_value=object()),
            CGEventGetLocation=Mock(side_effect=lambda event: state.point),
            CGMainDisplayID=Mock(return_value=73),
            CGDisplayBounds=Mock(return_value=SimpleNamespace(size=SimpleNamespace(height=1080))),
            CGWarpMouseCursorPosition=Mock(), CGEventPost=Mock(),
            CGAssociateMouseAndMouseCursorPosition=Mock(),
            CGDisplayHideCursor=Mock(), CGDisplayShowCursor=Mock())
        shield.app = Mock()
        shield.view = Mock()
        shield.panel = Mock()
        shield.panel.frame.side_effect = frame
        shield.panel.setFrameOrigin_.side_effect = lambda value: setattr(state, 'origin', tuple(value))
        return shield, state

    def test_alignment_moves_panel_to_frozen_cursor_without_native_pointer_writes(self):
        for point in ((1919.75, 310.5), (-1200, -40)):
            with self.subTest(point=point):
                shield, state = self.shield(point)

                park = shield.align_to_cursor()

                self.assertEqual(park, point)
                self.assertEqual(state.origin, (point[0] - 48, 1080 - point[1] - 48))
                shield.verify_park(park)
                shield.panel.makeKeyAndOrderFront_.assert_not_called()
                shield.app.activateIgnoringOtherApps_.assert_not_called()
                for name in ('CGWarpMouseCursorPosition', 'CGEventPost',
                             'CGAssociateMouseAndMouseCursorPosition',
                             'CGDisplayHideCursor', 'CGDisplayShowCursor'):
                    getattr(shield.Q, name).assert_not_called()

    def test_verification_identifies_changed_anchor_instead_of_generic_capture_failure(self):
        shield, state = self.shield()
        park = shield.align_to_cursor()
        state.point = SimpleNamespace(x=park[0], y=park[1] + 2)

        with self.assertRaises(capture_ui.CaptureAnchorChanged):
            shield.verify_park(park)


class ShieldReleaseTests(unittest.TestCase):
    def shield(self, visible=True, miniaturized=False, foreground=True):
        shield = AppKitShield.__new__(AppKitShield)
        shield.active = True
        shield.panel, shield.view = Mock(), SimpleNamespace(capture_active=True)
        shield.previous_key = Mock()
        shield.previous_key.isVisible.return_value = visible
        shield.previous_key.isMiniaturized.return_value = miniaturized
        shield.previous_app = None
        shield.previous_cursor = Mock()
        front = SimpleNamespace(processIdentifier=lambda: os.getpid() if foreground else os.getpid() + 1000)
        shield.A = SimpleNamespace(
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: front)),
            NSApplicationActivateIgnoringOtherApps=1)
        return shield

    def test_release_never_reopens_hidden_or_minimized_previous_window(self):
        for visible, miniaturized in ((False, False), (True, True), (False, True)):
            with self.subTest(visible=visible, miniaturized=miniaturized):
                shield = self.shield(visible, miniaturized)
                previous = shield.previous_key

                shield.release()

                previous.makeKeyAndOrderFront_.assert_not_called()
                shield.panel.orderOut_.assert_called_once_with(None)
                self.assertFalse(shield.active)
                self.assertFalse(shield.view.capture_active)
                self.assertIsNone(shield.previous_key)

    def test_release_restores_still_visible_previous_window(self):
        shield = self.shield()
        previous = shield.previous_key

        shield.release()

        previous.makeKeyAndOrderFront_.assert_called_once_with(None)

    def test_release_does_not_steal_focus_from_another_application(self):
        shield = self.shield(foreground=False)
        previous = shield.previous_key

        shield.release()

        previous.makeKeyAndOrderFront_.assert_not_called()


class CursorPixelTests(unittest.TestCase):
    def pixels(self, data, width=2, height=1, stride=12, alpha_first=False):
        class Bitmap:
            pixelsWide = lambda self: width
            pixelsHigh = lambda self: height
            samplesPerPixel = lambda self: 4
            bytesPerRow = lambda self: stride
            bitsPerSample = lambda self: 8
            isPlanar = lambda self: False
            hasAlpha = lambda self: True
            bitmapFormat = lambda self: int(alpha_first)
            bitmapData = lambda self: data

        bitmap = Bitmap()
        image = SimpleNamespace(size=lambda: SimpleNamespace(width=width, height=height),
                                representations=lambda: [bitmap])
        cursor = SimpleNamespace(image=lambda: image)
        shield = AppKitShield.__new__(AppKitShield)
        shield.A = SimpleNamespace(NSBitmapImageRep=Bitmap, NSAlphaFirstBitmapFormat=1)
        return shield.cursor_pixels(cursor)

    def test_all_pixels_are_checked_while_row_padding_is_ignored(self):
        transparent = self.pixels(bytes([0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255]))
        self.assertTrue(transparent['verified'])
        self.assertTrue(transparent['transparent'])
        opaque_second_pixel = self.pixels(bytes([0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]))
        self.assertFalse(opaque_second_pixel['transparent'])
        self.assertEqual(opaque_second_pixel['max_alpha'], 1)

    def test_alpha_first_and_every_row_are_checked(self):
        values = bytearray(24)
        values[16] = 128
        result = self.pixels(values, height=2, alpha_first=True)
        self.assertFalse(result['transparent'])
        self.assertEqual(result['max_alpha'], 128)


if __name__ == '__main__':
    unittest.main()
