"""Real Engine/Input lifecycle with fake controllers; no taps or input injection."""
from contextlib import contextmanager
from enum import Enum
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pynput import keyboard

from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.input import Input
from sharemouse.switching import EdgeSwitch


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin media-key marker and listener')
class MacMediaLifecycleTests(unittest.TestCase):
    def setUp(self):
        # Replace only this module's platform reference, not process-wide sys.
        platform = patch('sharemouse.input.sys', SimpleNamespace(platform='darwin'))
        platform.start()
        self.addCleanup(platform.stop)
        self.sender = Engine(True, Screen(1920, 1080), 'right', 'media-lifecycle-test',
                             '', 0, Mock(), clipboard=False, transport='bluetooth')
        self.receiver = Engine(False, Screen(2560, 1440), 'right', 'media-lifecycle-test',
                               '', 0, Mock(), clipboard=False, transport='bluetooth')
        self.source = self.adapter(self.sender)
        self.target = self.adapter(self.receiver)
        self.sender.peer_screen = self.receiver.local
        self.receiver.peer_screen = self.sender.local
        self.sender.connected = self.receiver.connected = True
        self.messages = []

    def adapter(self, engine):
        result = Input.__new__(Input)
        result.engine = engine
        result.mouse = SimpleNamespace(position=(1800, 300))
        result.keyboard = Mock()
        result.visibility = Mock()
        result.listeners = []
        result.target_position = None
        result.held_keys, result.key_payloads = {}, {}
        result.held_buttons, result.physical = set(), set()
        result.physical_buttons = set()
        result.edge_switch = EdgeSwitch()
        result.capture_ui = result.capture_token = None
        result.kl, result.ml = Mock(), Mock()
        result.kl.canonical.side_effect = lambda key: key
        result.ml._captured = False
        result.ml.capture_mouse.side_effect = lambda: setattr(result.ml, '_captured', True)
        result.ml.release_mouse.side_effect = lambda *args: setattr(result.ml, '_captured', False)
        engine.input = result
        return result

    def pump(self):
        # Engine messages are passed directly, not over a radio or socket.
        for _ in range(8):
            processed = False
            for source, target in ((self.sender, self.receiver), (self.receiver, self.sender)):
                while not source.outgoing.empty():
                    message = source.outgoing.get_nowait()
                    self.messages.append((source, message))
                    target.receive(message)
                    processed = True
            if not processed:
                return
        self.fail('engine messages did not settle')

    def enter(self):
        self.source.mac_move(1800, 300, 1, 0)
        self.source.mac_move(1919, 300, 1, 0)
        self.assertTrue(self.sender.remote)
        self.pump()
        self.assertTrue(self.receiver.active)

    def deferred_listener(self):
        from sharemouse.mac_keyboard import KeyboardListener

        @contextmanager
        def layout_context():
            yield (40, b'fake-layout-no-native-api')

        with patch('sharemouse.mac_keyboard.keycode_context', layout_context):
            listener = KeyboardListener(
                on_press=lambda key, injected=False: self.source.key(key, True, injected),
                on_release=lambda key, injected=False: self.source.key(key, False, injected),
                event_context=lambda: (self.sender.epoch, self.sender.remote,
                                       self.source.return_pending()),
                event_lock=self.sender.state_lock)
        # Exercise the real delayed dispatcher without starting its OS loop.
        listener._running = True
        listener._loop = object()
        listener._stop_platform = Mock()
        self.source.kl = listener
        self.addCleanup(listener.stop)
        return listener

    def test_queued_media_release_after_return_cannot_block_next_crossing(self):
        self.enter()
        listener = self.deferred_listener()
        key = keyboard.Key.media_play_pause
        context = listener._event_context()
        loop = listener._loop
        listener._deliver_media(key, True, False, loop, context)
        self.pump()
        self.assertTrue(self.source.key_payloads)
        self.assertTrue(self.target.held_keys)
        self.target.keyboard.press.assert_called_once_with(key)

        # Its already-decoded release waits behind a local return. This is the
        # real stale-epoch dispatcher path, without an NSEvent or native block.
        queued_release = lambda: listener._deliver_media(key, False, False, loop, context)
        self.sender.return_local()
        self.assertIsInstance(self.sender.pending_return, tuple)
        self.pump()
        self.assertIsNone(self.sender.pending_return)
        self.assertFalse(self.sender.remote)
        self.assertFalse(self.receiver.active)
        self.assertEqual(self.source.key_payloads, {})
        self.assertEqual(self.target.held_keys, {})
        self.target.keyboard.release.assert_called_once_with(key)
        messages_before_release = len(self.messages)
        queued_release()
        self.pump()
        self.assertEqual(len(self.messages), messages_before_release,
                         'stale release must not send to a newer session')

        self.source.mac_move(1919, 300, 1, 0)
        self.pump()
        self.assertTrue(self.sender.remote)
        self.assertEqual(self.source.physical, set())
        self.assertTrue(self.receiver.active)
        self.assertEqual(self.sender.epoch, context[0] + 1)
        self.assertTrue(self.sender.connected and self.receiver.connected)

    def test_each_darwin_consumer_key_does_not_gate_pointer_entry(self):
        media = [key for key in keyboard.Key if getattr(key.value, '_is_media', False)]
        self.assertGreaterEqual(len(media), 7)
        for key in media:
            with self.subTest(key=key):
                self.source.key(key, True)
                try:
                    self.assertEqual(self.source.physical, set())
                finally:
                    self.source.key(key, False)

    def test_normal_keys_and_modifiers_still_gate_crossing_until_release(self):
        for key in (keyboard.KeyCode.from_char('a', vk=0),
                    keyboard.Key.shift_l, keyboard.Key.ctrl_l, keyboard.Key.f1):
            with self.subTest(key=key):
                self.source.key(key, True)
                self.source.mac_move(1800, 300, 1, 0)
                self.source.mac_move(1919, 300, 1, 0)
                self.assertFalse(self.sender.remote)
                self.assertTrue(self.source.physical)
                self.source.key(key, False)
                self.enter()
                self.sender.return_local()
                self.pump()
                self.assertFalse(self.sender.remote)

    def test_windows_consumer_key_without_darwin_marker_keeps_existing_gate(self):
        class WindowsKey(Enum):
            media_play_pause = SimpleNamespace(vk=0xb3)
            f12 = SimpleNamespace(vk=0x7b)

        namespace = SimpleNamespace(Key=WindowsKey)
        key = WindowsKey.media_play_pause
        self.assertFalse(hasattr(key.value, '_is_media'))
        with patch('sharemouse.input.sys', SimpleNamespace(platform='win32')), \
                patch('sharemouse.input.keyboard', namespace):
            self.source.key(key, True)
            self.source.move(1919, 300, dx=1)
            self.assertFalse(self.sender.remote)
            self.assertEqual(self.source.physical, {('name', 'media_play_pause')})
            self.source.key(key, False)
            self.assertEqual(self.source.physical, set())
            self.source.move(1800, 300, dx=-1)
            self.source.move(1919, 300, dx=1)
            self.assertTrue(self.sender.remote)

    def test_windows_marker_does_not_apply_darwin_only_exemption(self):
        key = keyboard.Key.media_play_pause
        self.assertTrue(key.value._is_media)
        with patch('sharemouse.input.sys', SimpleNamespace(platform='win32')):
            self.source.key(key, True)
            self.assertEqual(self.source.physical, {('name', key.name)})
            self.source.key(key, False)
            self.assertEqual(self.source.physical, set())


if __name__ == '__main__':
    unittest.main()
