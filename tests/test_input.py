import unittest
import threading
from sharemouse.switching import EdgeSwitch
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pynput import keyboard

from sharemouse.core import Screen
from sharemouse.input import Input


class InputTests(unittest.TestCase):
    def adapter(self):
        adapter = Input.__new__(Input)
        adapter.engine = Mock()
        adapter.engine.state_lock = threading.RLock()
        adapter.engine.epoch = 0
        adapter.edge_switch = EdgeSwitch()
        adapter.engine.remote = True
        adapter.engine.connected = True
        adapter.engine.cooldown = 0
        adapter.engine.local = Screen(1920, 1080)
        adapter.physical = set()
        adapter.key_payloads = {}
        adapter.held_keys = {}
        adapter.held_buttons = set()
        adapter.physical_buttons = set()
        adapter.ignore_until = 0
        adapter.target_position = None
        adapter.kl = Mock()
        adapter.kl.canonical.side_effect = lambda key: key
        adapter.mouse = Mock()
        adapter.ml = Mock()
        adapter.keyboard = Mock()
        return adapter

    def test_injected_events_are_not_forwarded(self):
        adapter = self.adapter()
        adapter.key(keyboard.KeyCode.from_char('a'), True, injected=True)
        adapter.move(400, 500, injected=True)
        adapter.engine.send.assert_not_called()

    def test_release_reuses_press_identity_when_shift_changes_character(self):
        adapter = self.adapter()
        adapter.key(keyboard.KeyCode.from_char('A', vk=42), True)
        adapter.key(keyboard.KeyCode.from_char('a', vk=42), False)
        events = [call.args[0] for call in adapter.engine.send.call_args_list]
        self.assertEqual(events[0]['key'], events[1]['key'])
        self.assertTrue(events[0]['down'])
        self.assertFalse(events[1]['down'])
        self.assertEqual(adapter.physical, set())

    def test_emergency_f12_never_transmitted(self):
        adapter = self.adapter()
        adapter.key(keyboard.Key.f12, True)
        adapter.engine.return_local.assert_called_once()
        adapter.engine.send.assert_not_called()

    def test_windows_remote_move_is_relative_to_suppressed_cursor(self):
        adapter = self.adapter()
        with patch('sharemouse.input.sys.platform', 'win32'), patch.object(adapter, 'warp') as warp:
            adapter.move(975, 535)
            warp.assert_not_called()
        adapter.engine.send.assert_called_once_with({'type': 'move', 'dx': 15, 'dy': -5})

    def test_command_maps_to_control_on_windows(self):
        adapter = self.adapter()
        adapter.engine.map_command = True
        with patch('sharemouse.input.sys.platform', 'win32'):
            adapter.inject_key({'name': 'cmd'}, True)
            adapter.inject_key({'name': 'cmd'}, False)
        adapter.keyboard.press.assert_called_once_with(keyboard.Key.ctrl)
        adapter.keyboard.release.assert_called_once_with(keyboard.Key.ctrl)
        self.assertEqual(adapter.held_keys, {})

    def test_release_all_injected_keys(self):
        adapter = self.adapter()
        adapter.inject_key({'char': 'a'}, True)
        adapter.release()
        adapter.keyboard.release.assert_called_once()
        self.assertEqual(adapter.held_keys, {})

    def test_windows_entry_event_is_suppressed_and_small_moves_preserved(self):
        adapter = self.adapter()
        adapter.engine.remote = False
        adapter.engine.side = 'right'
        data = lambda x, y: SimpleNamespace(flags=0, pt=SimpleNamespace(x=x, y=y))
        with patch('sharemouse.input.sys.platform', 'win32'):
            adapter.win_mouse(0x200, data(1800, 300))
            adapter.win_mouse(0x200, data(1919, 300))
            self.assertTrue(adapter.engine.remote)
            adapter.ml.suppress_event.assert_called_once()
            self.assertEqual(adapter.mouse.position, (960, 540))
            self.assertEqual(adapter.engine.send.call_args.args[0]['type'], 'enter')
            adapter.mouse.reset_mock()
            with patch.object(adapter, 'warp') as warp:
                for _ in range(10):
                    adapter.win_mouse(0x200, data(961, 540))
                warp.assert_not_called()
            moves = [c.args[0] for c in adapter.engine.send.call_args_list if c.args[0]['type'] == 'move']
            self.assertEqual(moves, [{'type': 'move', 'dx': 1, 'dy': 0}] * 10)

    def test_local_windows_move_runs_once_and_is_not_suppressed(self):
        adapter = self.adapter()
        adapter.engine.remote = False
        adapter.engine.side = 'right'
        data = SimpleNamespace(flags=0, pt=SimpleNamespace(x=100, y=100))
        self.assertIs(adapter.win_mouse(0x200, data), False)
        adapter.ml.suppress_event.assert_not_called()
        adapter.engine.send.assert_not_called()

    def test_mac_button_uses_latest_remote_target(self):
        from pynput import mouse
        adapter = self.adapter()
        with patch('sharemouse.input.sys.platform', 'darwin'):
            adapter.move_to((123, 456))
            adapter.inject_button(mouse.Button.left, True)
            adapter.release()
        self.assertEqual(adapter.mouse.button_at.call_args_list[0].args,
                         (mouse.Button.left, True, (123, 456)))
        self.assertEqual(adapter.mouse.button_at.call_args_list[1].args,
                         (mouse.Button.left, False, (123, 456)))

    def test_inward_motion_during_cooldown_rearms_next_crossing(self):
        adapter = self.adapter()
        adapter.engine.remote = False
        adapter.engine.side = 'right'
        adapter.engine.cooldown = 10
        adapter.edge_switch.returned()
        with patch('sharemouse.input.sys.platform', 'win32'):
            with patch('sharemouse.input.time.monotonic', return_value=9):
                adapter.move(1800, 500)
                adapter.move(1919, 500)
                self.assertFalse(adapter.engine.remote)
            with patch('sharemouse.input.time.monotonic', return_value=11):
                adapter.move(1900, 500)
                adapter.move(1919, 500)
                self.assertTrue(adapter.engine.remote)
