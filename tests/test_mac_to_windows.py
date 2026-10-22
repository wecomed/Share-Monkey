"""Exercise the real Darwin listener callback, not Input.move in isolation.

Create native events with independent absolute positions and hardware deltas.
Capture OS writes; no input is posted and no system tap is installed.
"""
import json
import random
import sys
import time
import unittest
from unittest.mock import patch

from sharemouse.core import Screen
from sharemouse.engine import Engine
from tests.test_simulation import adapter, WindowsPointer


@unittest.skipUnless(sys.platform == 'darwin', 'requires Quartz and Darwin listener')
class MacToWindowsTests(unittest.TestCase):
    def setUp(self):
        import Quartz
        from pynput import mouse
        from sharemouse.input import Input
        self.q = Quartz
        self.posts, self.warps = [], []
        for target, kwargs in (
            # This suite validates the native delta path with OS writes mocked.
            # Foreground capture has its own coordinator tests and explicit
            # native probe; never inherit a UI singleton from another suite.
            ('sharemouse.mac_capture_ui.get_capture_coordinator', {'return_value': None}),
            ('Quartz.CGAssociateMouseAndMouseCursorPosition', {'return_value': 0}),
            ('Quartz.CGDisplayHideCursor', {'return_value': 0}),
            ('Quartz.CGDisplayShowCursor', {'return_value': 0}),
            ('Quartz.CGEventPost', {'side_effect': lambda tap, event: self.posts.append(event)}),
            ('Quartz.CGWarpMouseCursorPosition', {'side_effect': lambda pos: self.warps.append(tuple(pos)) or 0}),
            ('HIServices.AXIsProcessTrusted', {'return_value': True}),
            ('Quartz.CGPreflightListenEventAccess', {'return_value': True}),
            ('pynput._util.AbstractListener.start', {}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.sender = Engine(True, Screen(1920, 1080), 'right', 'test-password-123', '', 24809, lambda s: None, clipboard=False)
        self.receiver = Engine(False, Screen(2560, 1440), 'right', 'test-password-123', '', 24809, lambda s: None, clipboard=False)
        self.sender.connected = self.receiver.connected = True
        self.mac = Input(self.sender)
        self.sender.input = self.mac
        self.mac.start()  # Actual listener classes and production callback wiring.
        self.win = adapter(self.receiver, WindowsPointer((0, 0)))
        self.messages = []
        self.channels = None
        self.addCleanup(self.sender.stop)
        self.addCleanup(self.receiver.stop)

    def event(self, position, delta=(0, 0), kind=None, injected=False, flush=True):
        q = self.q
        kind = q.kCGEventMouseMoved if kind is None else kind
        event = q.CGEventCreateMouseEvent(None, kind, position, 0)
        q.CGEventSetTimestamp(event, time.monotonic_ns())
        q.CGEventSetIntegerValueField(event, q.kCGEventSourceUnixProcessID, 123 if injected else 0)
        q.CGEventSetDoubleValueField(event, q.kCGMouseEventDeltaX, delta[0])
        q.CGEventSetDoubleValueField(event, q.kCGMouseEventDeltaY, delta[1])
        result = self.mac.ml._handler(None, kind, event, None)
        if flush:
            self.pump()
        return result

    def pump(self):
        while not self.sender.outgoing.empty():
            message = json.loads(json.dumps(self.sender.outgoing.get_nowait()))
            self.messages.append(message)
            if self.channels:
                self.channels[0].send(message)
                message = self.channels[1].recv()
            with patch('sharemouse.input.sys.platform', 'win32'):
                self.receiver.receive(message)
        while not self.receiver.outgoing.empty():
            message = self.receiver.outgoing.get_nowait()
            if self.channels:
                self.channels[1].send(message)
                message = self.channels[0].recv()
            self.sender.receive(message)

    def enter(self):
        self.event((1800, 270), (-1, 0))
        self.assertIsNone(self.event((1919, 270), (1, 0)))
        self.assertTrue(self.sender.remote)
        self.assertEqual(self.win.target_position, (2, 360))

    def place_remote_center(self):
        self.enter()
        self.receiver.cursor.x, self.receiver.cursor.y = 1280, 720
        self.win.move_to((1280, 720))

    def test_four_directions_ignore_stale_absolute_position(self):
        self.place_remote_center()
        for delta, expected in (((5, 0), (1285, 720)), ((0, 7), (1285, 727)),
                                ((-5, 0), (1280, 727)), ((0, -7), (1280, 720))):
            with self.subTest(delta=delta):
                self.assertIsNone(self.event((100, 100), delta))
                self.assertEqual(self.win.target_position, expected)

    def test_queued_edge_events_do_not_explode_or_reverse(self):
        self.enter()
        for _ in range(1000):
            self.event((1919, 270), (1, 0), flush=False)
        self.pump()
        self.assertEqual(self.win.target_position, (1002, 360))
        self.assertTrue(self.sender.remote)

    def test_constant_center_coordinates_still_carry_motion(self):
        self.place_remote_center()
        self.event((960, 540), (3, -4))
        self.assertEqual(self.win.target_position, (1283, 716))

    def test_zero_delta_never_drifts_even_when_absolute_location_changes(self):
        self.place_remote_center()
        for position in ((0, 0), (1919, 1079), (100, 100), (960, 540)) * 10:
            self.event(position)
        self.assertEqual(self.win.target_position, (1280, 720))
        self.assertTrue(self.sender.remote)

    def test_capture_warp_does_not_post_synthetic_motion(self):
        self.enter()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.warps, [])
        for _ in range(100):
            self.event((1919, 270), (1, 0))
        self.assertEqual(self.warps, [], 'no entry or repeated recentering')

    def test_native_drag_clicks_and_injected_events(self):
        self.place_remote_center()
        q = self.q
        self.event((960, 540), kind=q.kCGEventLeftMouseDown)
        self.event((960, 540), (12, -8), kind=q.kCGEventLeftMouseDragged)
        self.event((960, 540), kind=q.kCGEventLeftMouseUp)
        self.assertEqual(self.win.target_position, (1292, 712))
        self.win.mouse.press.assert_called_once()
        self.win.mouse.release.assert_called_once()
        count = len(self.messages)
        self.event((0, 0), (-10000, 10000), injected=True)
        self.assertEqual(len(self.messages), count)

    def test_closed_paths_do_not_accumulate_drift(self):
        self.place_remote_center()
        rng = random.Random(932)
        for _ in range(500):
            dx, dy = rng.randint(1, 20), rng.randint(1, 20)
            for delta in ((dx, 0), (0, dy), (-dx, 0), (0, -dy)):
                self.event((rng.randrange(1920), rng.randrange(1080)), delta)
            self.assertEqual(self.win.target_position, (1280, 720))

    def test_left_right_layout_and_resolution_matrix(self):
        cases = 0
        for local in (Screen(1440, 900), Screen(1920, 1080), Screen(2560, 1440)):
            for remote in (Screen(1920, 1080), Screen(2560, 1440), Screen(3840, 2160)):
                for side in ('left', 'right'):
                    for fraction in (0, .25, .5, .75, 1):
                        with self.subTest(local=local, remote=remote, side=side, fraction=fraction):
                            self.sender.return_local()
                            self.pump()
                            self.sender.cooldown = 0
                            self.sender.local, self.sender.side = local, side
                            self.receiver.local = remote
                            edge = local.width - 1 if side == 'right' else 0
                            y = round(fraction * (local.height - 1))
                            self.event((local.width // 2, y), (-1 if side == 'right' else 1, 0))
                            self.assertIsNone(self.event((edge, y), (1 if side == 'right' else -1, 0)))
                            ex = 2 if side == 'right' else remote.width - 3
                            ey = round(y / (local.height - 1) * (remote.height - 1))
                            self.assertEqual(self.win.target_position, (ex, ey))
                            dx = 400 if side == 'right' else -400
                            self.event((edge, y), (dx, 0))
                            ex += dx
                            rng = random.Random(cases)
                            for _ in range(200):
                                dx, dy = rng.randint(-3, 3), rng.randint(-3, 3)
                                ex = max(0, min(remote.width - 1, ex + dx))
                                ey = max(0, min(remote.height - 1, ey + dy))
                                self.event((rng.randrange(local.width), rng.randrange(local.height)), (dx, dy))
                                self.assertEqual(self.win.target_position, (ex, ey))
                            cases += 1
        self.assertEqual(cases, 90)

    def test_edge_return_and_f12_restore_local_dispatch(self):
        from pynput import keyboard
        self.enter()
        self.event((960, 540), (-10, 0))
        self.assertFalse(self.sender.remote)
        self.assertFalse(self.receiver.active)
        self.assertEqual(self.warps[-1], (1908, 270))
        self.assertIsNotNone(self.event((1908, 270), (1, 1)))
        self.sender.cooldown = 0
        self.enter()
        self.mac.kl.on_press(keyboard.Key.f12)
        self.mac.kl.on_release(keyboard.Key.f12)
        self.pump()
        self.assertFalse(self.sender.remote)
        self.assertFalse(self.receiver.active)
        self.assertIsNotNone(self.event((1908, 540), (1, 1)))
        self.assertEqual(self.posts, [])

    def test_local_drag_does_not_enter_and_remote_drag_directions_match(self):
        q = self.q
        self.event((100, 100), kind=q.kCGEventRightMouseDown)
        self.event((1919, 100), (10, 0), kind=q.kCGEventRightMouseDragged)
        self.assertFalse(self.sender.remote)
        self.event((1919, 100), kind=q.kCGEventRightMouseUp)
        self.place_remote_center()
        self.event((960, 540), kind=q.kCGEventRightMouseDown)
        self.event((0, 0), (-5, 7), kind=q.kCGEventRightMouseDragged)
        self.assertEqual(self.win.target_position, (1275, 727))
        self.event((0, 0), kind=q.kCGEventRightMouseUp)
        self.assertFalse(self.win.held_buttons)

    def test_native_callback_through_encrypted_transport(self):
        import os
        import socket
        from sharemouse.protocol import Channel
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(2)
        b.settimeout(2)
        salt = os.urandom(32)
        self.channels = (Channel(a, 'test-password-123', salt, False),
                         Channel(b, 'test-password-123', salt, True))
        self.place_remote_center()
        for _ in range(250):
            for delta in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                self.event((1919, 0), delta)
        self.assertEqual(self.win.target_position, (1280, 720))
        self.sender.return_local()
        self.pump()
        self.assertFalse(self.receiver.active)

    def test_repeated_return_does_not_truncate_height(self):
        for _ in range(100):
            self.sender.cooldown = 0
            self.enter()
            self.event((960, 540), (-10, 0))
            self.assertEqual(self.warps[-1], (1908, 270))

    def test_return_then_directly_cross_again_without_extra_inward_gesture(self):
        for side, edge, toward, restore in (('right', 1919, 1, 1908), ('left', 0, -1, 12)):
            self.sender.side = side
            for _ in range(100):
                self.assertIsNone(self.event((edge, 270), (12 * toward, 0)))
                self.assertTrue(self.sender.remote)
                self.event((edge, 270), (-10 * toward, 0))
                self.assertFalse(self.sender.remote)
                self.assertTrue(self.sender.connected)
                self.assertEqual(self.warps[-1], (restore, 270))

    def test_native_deltas_fractional_scaling_and_live_speed_dpi(self):
        self.place_remote_center()
        self.sender.peer_screen = Screen(2560, 1440, 1.5)
        self.sender.set_mouse_speed(.5)
        for _ in range(8):
            self.event((1919, 270), (1, -1))
        self.assertEqual(self.win.target_position, (1286, 714))
        self.assertEqual((self.receiver.cursor.x, self.receiver.cursor.y), (1286, 714))
        self.sender.set_mouse_speed(.25)
        for _ in range(8):
            self.event((1919, 270), (-1, 1))
        self.assertEqual(self.win.target_position, (1283, 717))

    def test_pending_native_restore_cannot_start_another_capture(self):
        self.enter()
        blocks = []
        self.mac.ml._loop = object()
        with patch('Quartz.CFRunLoopPerformBlock', side_effect=lambda loop, mode, block: blocks.append(block)), patch('Quartz.CFRunLoopWakeUp'):
            self.event((1919, 270), (-10, 0))
            self.assertFalse(self.sender.remote)
            self.assertTrue(self.mac.ml._captured)
            self.event((1919, 270), (100, 0))
            self.assertFalse(self.sender.remote)
            blocks.pop(0)()
            self.assertFalse(self.mac.ml._captured)
            self.event((1919, 270), (12, 0))
            self.assertTrue(self.sender.remote)
        self.mac.ml._loop = None

    def test_local_subpixel_motion_with_zero_raw_delta_can_cross_again(self):
        self.enter()
        self.event((1919, 270), (-10, 0))
        self.assertFalse(self.sender.remote)
        # Recorded hardware events can have changing absolute positions and
        # zero integer delta fields. Local direction must remain observable.
        self.event((1908.5, 270), (0, 0))
        self.event((1919, 270), (0, 0))
        self.assertTrue(self.sender.remote)

    def test_old_native_motion_is_consumed_before_the_os_can_undo_return(self):
        self.enter()
        self.event((1919, 270), (-10, 0))
        self.mac.ml._return_filter.cutoffs = (1000, 1000)
        self.mac.ml._return_filter.mark_ns = time.monotonic_ns()
        q = self.q
        event = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, (1919, 270), 0)
        q.CGEventSetTimestamp(event, 999)
        q.CGEventSetIntegerValueField(event, q.kCGEventSourceUnixProcessID, 0)
        self.assertIsNone(self.mac.ml._handler(None, q.kCGEventMouseMoved, event, None))
        self.assertFalse(self.sender.remote)

    def test_capture_failure_never_enters_remote_mode(self):
        with patch('Quartz.CGAssociateMouseAndMouseCursorPosition', return_value=1001):
            with self.assertRaisesRegex(RuntimeError, '1001'):
                self.mac.mac_move(1919, 270, 1, 0)
        self.assertFalse(self.sender.remote)
        self.assertTrue(self.sender.outgoing.empty())

    def test_return_warp_failure_still_releases_remote(self):
        self.enter()
        self.event((960, 540), kind=self.q.kCGEventLeftMouseDown)
        with patch('Quartz.CGWarpMouseCursorPosition', return_value=1001):
            self.sender.return_local()
        self.pump()
        self.assertFalse(self.sender.remote)
        self.assertFalse(self.receiver.active)
        self.assertFalse(self.win.held_buttons)


if __name__ == '__main__':
    unittest.main()
