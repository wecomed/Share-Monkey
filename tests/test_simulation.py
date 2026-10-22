"""Windows hook -> serialized messages -> Mac engine -> real Quartz events.

No system input is sent. Windows applies a physical event only when the hook
does not suppress it. Mac CGEventPost is captured before it reaches the OS.
The simulation deliberately delays OS cursor readback to expose click races.
"""
import json
import queue
import random
import socket
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pynput import keyboard, mouse
from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.input import Input
from sharemouse.protocol import Channel
from sharemouse.switching import EdgeSwitch


class Suppressed(Exception):
    pass


class WindowsPointer:
    def __init__(self, position):
        self.position = position
        self.press = Mock()
        self.release = Mock()


def adapter(engine, pointer):
    result = Input.__new__(Input)
    result.engine, result.mouse = engine, pointer
    result.keyboard = Mock()
    result.listeners = []
    result.target_position = None
    result.held_keys, result.key_payloads = {}, {}
    result.held_buttons, result.physical = set(), set()
    result.physical_buttons = set()
    result.edge_switch = EdgeSwitch()
    result.kl, result.ml = Mock(), Mock()
    result.kl.canonical.side_effect = lambda key: key
    result.ml.suppress_event.side_effect = Suppressed
    engine.input = result
    return result


class Pair:
    def __init__(self, windows=Screen(1920, 1080), mac=Screen(1920, 1080), side='right', encrypted=False):
        import Quartz
        from sharemouse.mac_mouse import MouseController
        self.q = Quartz
        self.events = []
        self.status = []
        self.sender = Engine(True, windows, side, 'simulation-password', '', 24809, self.status.append, clipboard=False)
        self.receiver = Engine(False, mac, side, 'simulation-password', '', 24809, self.status.append, clipboard=False)
        self.sender.connected = self.receiver.connected = True
        self.win = adapter(self.sender, WindowsPointer((windows.width // 2, windows.height // 2)))
        self.mac = adapter(self.receiver, MouseController())
        self.post = patch('Quartz.CGEventPost', side_effect=lambda tap, event: self.events.append(event))
        self.post.start()
        self.channels = None
        if encrypted:
            import os
            a, b = socket.socketpair()
            a.settimeout(2)
            b.settimeout(2)
            salt = os.urandom(32)
            self.channels = (Channel(a, 'simulation-password', salt, False),
                             Channel(b, 'simulation-password', salt, True))

    def pump(self):
        for _ in range(8):
            processed = False
            for index, (source, target, platform) in enumerate(((self.sender, self.receiver, 'darwin'), (self.receiver, self.sender, 'win32'))):
                while not source.outgoing.empty():
                    message = json.loads(json.dumps(source.outgoing.get_nowait()))
                    if self.channels:
                        self.channels[index].send(message)
                        message = self.channels[1 - index].recv()
                    with patch('sharemouse.input.sys.platform', platform):
                        target.receive(message)
                    processed = True
            if not processed:
                return
        raise AssertionError('message loop failed to settle')

    def physical(self, message, x=None, y=None, wheel=0, flush=True, injected=False):
        point = self.win.mouse.position if x is None else (x, y)
        data = SimpleNamespace(flags=int(injected), pt=SimpleNamespace(x=point[0], y=point[1]), mouseData=wheel)
        suppressed = False
        with patch('sharemouse.input.sys.platform', 'win32'):
            try:
                self.win.win_mouse(message, data)
            except Suppressed:
                suppressed = True
        if message == 0x200 and not suppressed:
            self.win.mouse.position = point
        if flush:
            self.pump()
        return suppressed

    def enter(self, fraction=.5):
        x = self.sender.local.width - 1 if self.sender.side == 'right' else 0
        y = round(fraction * (self.sender.local.height - 1))
        self.physical(0x200, self.sender.local.width // 2, y)
        self.physical(0x200, x, y)

    def move(self, dx, dy, flush=True):
        x, y = self.win.mouse.position
        return self.physical(0x200, x + dx, y + dy, flush=flush)

    def key(self, key, down):
        with patch('sharemouse.input.sys.platform', 'win32'):
            self.win.key(key, down)
        self.pump()

    def close(self):
        with patch('sharemouse.input.sys.platform', 'win32'):
            self.sender.stop()
        with patch('sharemouse.input.sys.platform', 'darwin'):
            self.receiver.stop()
        self.post.stop()
        if self.channels:
            for channel in self.channels:
                channel.sock.close()


@unittest.skipUnless(sys.platform == 'darwin', 'requires native Quartz event generation')
class EndToEndSimulation(unittest.TestCase):
    def pair(self, *args, **kwargs):
        pair = Pair(*args, **kwargs)
        self.addCleanup(pair.close)
        return pair

    def test_resolution_direction_entry_and_random_motion_matrix(self):
        cases = 0
        for windows in (Screen(1920, 1080), Screen(2560, 1440), Screen(3840, 2160)):
            for mac in (Screen(1440, 900), Screen(1920, 1080), Screen(2560, 1440)):
                for side in ('left', 'right'):
                    for fraction in (0, .25, .5, .75, 1):
                        with self.subTest(windows=windows, mac=mac, side=side, fraction=fraction):
                            p = Pair(windows, mac, side)
                            try:
                                p.enter(fraction)
                                self.assertTrue(p.receiver.active)
                                self.assertEqual(p.win.mouse.position, (windows.width // 2, windows.height // 2))
                                expected_x = 2 if side == 'right' else mac.width - 3
                                expected_y = round(round(fraction * (windows.height - 1)) / (windows.height - 1) * (mac.height - 1))
                                self.assertEqual(p.mac.target_position, (expected_x, expected_y))
                                # Move inward, then run reproducible fine movements.
                                dx = 400 if side == 'right' else -400
                                p.move(dx, 0)
                                expected_x += dx
                                rng = random.Random(100 + cases)
                                for _ in range(200):
                                    dx, dy = rng.randint(-3, 3), rng.randint(-3, 3)
                                    expected_x = max(0, min(mac.width - 1, expected_x + dx))
                                    expected_y = max(0, min(mac.height - 1, expected_y + dy))
                                    p.move(dx, dy)
                                    self.assertEqual(p.mac.target_position, (expected_x, expected_y))
                                cases += 1
                            finally:
                                p.close()
        self.assertEqual(cases, 90)

    def test_delayed_burst_then_click_preserves_order_and_target(self):
        p = self.pair()
        p.enter()
        for _ in range(1000):
            p.move(1, 0, flush=False)
        p.physical(0x201, flush=False)
        p.physical(0x202, flush=False)
        p.pump()
        self.assertEqual(p.mac.target_position, (1002, 540))
        for event in p.events[-2:]:
            point = p.q.CGEventGetLocation(event)
            self.assertEqual((point.x, point.y), (1002, 540))

    def test_drag_emits_drag_events_at_the_target_and_releases(self):
        p = self.pair()
        p.enter()
        p.move(200, 0)
        p.physical(0x201)
        p.move(10, 15)
        self.assertEqual(p.q.CGEventGetType(p.events[-1]), p.q.kCGEventLeftMouseDragged)
        self.assertEqual(p.mac.target_position, (212, 555))
        p.physical(0x202)
        self.assertFalse(p.mac.held_buttons)
        self.assertIsNone(p.mac.mouse._drag_button)

    def test_double_click_has_native_click_count_two(self):
        p = self.pair()
        p.enter()
        p.move(200, 0)
        for _ in range(2):
            p.physical(0x201)
            p.physical(0x202)
        counts = [p.q.CGEventGetIntegerValueField(event, p.q.kCGMouseEventClickState) for event in p.events[-4:]]
        self.assertEqual(counts, [1, 1, 2, 2])

    def test_f12_and_held_button_do_not_reenter_until_physical_release(self):
        p = self.pair()
        p.enter()
        p.physical(0x201)
        p.key(keyboard.Key.f12, True)
        p.key(keyboard.Key.f12, False)
        self.assertFalse(p.receiver.active)
        self.assertFalse(p.mac.held_buttons)
        p.sender.cooldown = 0
        p.enter()
        self.assertFalse(p.sender.remote, 'still physically holding the mouse button')
        p.physical(0x202)
        p.enter()
        self.assertTrue(p.sender.remote)

    def test_edge_return_preserves_height_and_releases_keys(self):
        p = self.pair(mac=Screen(1440, 900))
        p.enter(.75)
        p.key(keyboard.Key.ctrl, True)
        p.move(-20, 0)
        self.assertFalse(p.sender.remote)
        self.assertFalse(p.receiver.active)
        self.assertFalse(p.mac.held_keys)
        self.assertAlmostEqual(p.win.mouse.position[1] / 1079, .75, places=2)

    def test_disconnect_during_drag_releases_buttons_and_modifiers(self):
        p = self.pair()
        p.enter()
        p.key(keyboard.Key.shift, True)
        p.physical(0x201)
        p.move(50, 20)
        p.close()
        self.assertFalse(p.sender.remote)
        self.assertFalse(p.mac.held_keys)
        self.assertFalse(p.mac.held_buttons)

    def test_local_drag_cannot_accidentally_switch(self):
        p = self.pair()
        p.physical(0x201)
        p.enter()
        self.assertFalse(p.sender.remote)
        p.physical(0x202)
        p.enter()
        self.assertTrue(p.sender.remote)

    def test_injected_warp_never_becomes_remote_motion(self):
        p = self.pair()
        p.enter()
        before = p.mac.target_position
        p.physical(0x200, 960, 540, injected=True)
        self.assertEqual(p.mac.target_position, before)

    def test_congestion_recovers_local_control(self):
        p = self.pair()
        p.enter()
        for _ in range(2050):
            p.move(1, 0, flush=False)
        self.assertFalse(p.sender.remote)
        self.assertFalse(p.sender.connected)

    def test_full_path_through_encrypted_socket_frames(self):
        p = self.pair(encrypted=True)
        p.enter(.25)
        p.key(keyboard.Key.ctrl, True)
        p.key(keyboard.KeyCode.from_char('c', vk=67), True)
        p.key(keyboard.KeyCode.from_char('c', vk=67), False)
        p.key(keyboard.Key.ctrl, False)
        for _ in range(1000):
            p.move(1, 0)
        p.physical(0x201)
        p.move(20, 30)
        p.physical(0x202)
        self.assertEqual(p.mac.target_position, (1022, 300))
        self.assertFalse(p.mac.held_keys)
        self.assertFalse(p.mac.held_buttons)
        self.assertEqual(p.mac.keyboard.press.call_args_list[0].args[0], keyboard.Key.cmd)
        p.key(keyboard.Key.f12, True)
        self.assertFalse(p.receiver.active)

    def test_double_click_resets_after_timeout_or_a_drag(self):
        p = self.pair()
        p.enter()
        p.move(200, 0)
        with patch('sharemouse.mac_mouse.time.monotonic', return_value=100):
            p.physical(0x201)
            p.physical(0x202)
        with patch('sharemouse.mac_mouse.time.monotonic', return_value=110):
            p.physical(0x201)
            p.move(20, 0)
            p.move(-20, 0)
            p.physical(0x202)
            p.physical(0x201)
            p.physical(0x202)
        counts = [p.q.CGEventGetIntegerValueField(e, p.q.kCGMouseEventClickState)
                  for e in p.events if p.q.CGEventGetType(e) == p.q.kCGEventLeftMouseDown]
        self.assertEqual(counts, [1, 1, 1])

    def test_right_button_drag_and_scroll_forward_once(self):
        p = self.pair()
        p.enter()
        p.move(100, 0)
        p.physical(0x204)
        p.move(10, 10)
        self.assertEqual(p.q.CGEventGetType(p.events[-1]), p.q.kCGEventRightMouseDragged)
        p.physical(0x205)
        with patch.object(p.mac.mouse, 'scroll') as scroll:
            p.physical(0x20A, wheel=120 << 16)
            scroll.assert_called_once_with(0, 1)

    def test_fifty_switches_do_not_accumulate_drift(self):
        p = self.pair()
        for _ in range(50):
            p.sender.cooldown = 0
            p.enter(.5)
            p.move(300, 0)
            p.move(-400, 0)
            self.assertFalse(p.sender.remote)
            self.assertFalse(p.receiver.active)
            self.assertEqual(p.win.mouse.position, (1908, 540))


if __name__ == '__main__':
    unittest.main()
