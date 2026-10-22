"""Cursor recovery tests use fake native APIs; never hide or move a real cursor."""
import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sharemouse.cursor_guard import GuardSession, run_guard
from sharemouse.cursor_visibility import MacCursorVisibility, WindowsCursorVisibility
from sharemouse.input import Input
from sharemouse.switching import EdgeSwitch


class FakeApi:
    def __init__(self):
        self.calls = []
        self.point = (50, 60)

    def show(self, visible):
        self.calls.append(bool(visible))

    def position(self):
        return self.point


class GuardTests(unittest.TestCase):
    def session(self):
        api = FakeApi()
        now = [10.]
        alive = [True]
        replies = []
        session = GuardSession(api, lambda: alive[0], replies.append, lambda: now[0])
        return session, api, now, alive, replies

    def test_hidden_lease_expires_and_heartbeat_does_not_rehide(self):
        s, api, now, _, replies = self.session()
        self.assertTrue(s.command('H 1 0'))
        now[0] += 1.1
        self.assertTrue(s.tick())
        self.assertEqual(api.calls, [False, True])
        self.assertEqual(replies[-1], 'EXPIRED')
        s.command('P 2')
        self.assertFalse(s.hidden)
        self.assertEqual(api.calls, [False, True])

    def test_heartbeat_renews_lease_but_explicit_show_ends_it(self):
        s, api, now, _, _ = self.session()
        s.command('H 1 0')
        for index in range(2, 20):
            now[0] += .3
            s.command(f'P {index}')
            self.assertTrue(s.tick())
        self.assertTrue(s.hidden)
        s.command('S 20')
        self.assertFalse(s.hidden)
        self.assertEqual(api.calls, [False, True])

    def test_physical_receiver_mouse_movement_restores_until_explicit_hide(self):
        s, api, _, _, replies = self.session()
        s.command('H 1 1')
        api.point = (51, 60)
        s.tick()
        self.assertFalse(s.hidden)
        self.assertEqual(replies[-1], 'TAKEOVER')
        s.command('P 2')
        s.tick()
        self.assertEqual(api.calls, [False, True])
        s.command('H 3 1')
        self.assertTrue(s.hidden)

    def test_source_capture_does_not_treat_suppressed_motion_as_takeover(self):
        s, api, _, _, replies = self.session()
        s.command('H 1 0')
        api.point = (900, 900)
        s.tick()
        self.assertTrue(s.hidden)
        self.assertNotIn('TAKEOVER', replies)

    def test_parent_exit_eof_and_exception_always_restore(self):
        s, api, _, alive, _ = self.session()
        s.command('H 1 0')
        alive[0] = False
        self.assertFalse(s.tick())
        s.close()
        self.assertTrue(api.calls[-1])
        stream = io.BytesIO(b'H 1 0\n')
        replies = []
        run_guard(api, lambda: True, lambda: stream.read(256), replies.append)
        self.assertEqual(replies[0], 'READY')
        self.assertEqual(api.calls[-2:], [False, True])
        def fail_reply(text):
            raise BrokenPipeError('parent closed')
        with self.assertRaises(BrokenPipeError):
            run_guard(api, lambda: True, lambda: b'', fail_reply)
        self.assertTrue(api.calls[-1])

    def test_invalid_command_and_parent_exit_never_hide(self):
        s, api, _, alive, _ = self.session()
        self.assertFalse(s.command('invalid'))
        alive[0] = False
        self.assertFalse(s.command('H 1 0'))
        self.assertEqual(api.calls, [])


CHILD = '''
import sys
from sharemouse.cursor_guard import run_guard
class FakeApi:
    def show(self, visible): pass
    def position(self): return (0, 0)
run_guard(FakeApi(), lambda: True, lambda: sys.stdin.buffer.read1(256),
          lambda text: print(text, flush=True), lease=.25)
'''


class WindowsVisibilityTests(unittest.TestCase):
    def visibility(self, child=CHILD):
        def spawn():
            return subprocess.Popen([sys.executable, '-u', '-c', child],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        restored, failure = Mock(), Mock()
        visibility = WindowsCursorVisibility(failure, spawn, restored)
        visibility.HEARTBEAT = .04
        self.addCleanup(visibility.close)
        return visibility, restored, failure

    def wait_for(self, predicate, timeout=2):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.005)
        self.fail('timed out waiting for fake cursor guard')

    def test_real_pipe_handshake_repeated_hide_show_and_normal_exit(self):
        v, restored, failure = self.visibility()
        v.start()
        for _ in range(30):
            v.hide(watch_position=True)
            v.show()
        v.close()
        self.assertIsNotNone(v._process.poll())
        self.assertTrue(v._closed)
        restored.assert_called()
        failure.assert_not_called()

    def test_helper_crash_restores_parent_fallback_and_notifies_engine(self):
        v, restored, failure = self.visibility()
        v.hide()
        v._process.terminate()
        self.wait_for(lambda: failure.called)
        restored.assert_called()
        self.assertFalse(v._hidden)
        with self.assertRaises(RuntimeError):
            v.hide()

    def test_missed_heartbeat_lease_restores_and_stops_session(self):
        v, restored, failure = self.visibility()
        v.hide()
        v._stopped.set()
        self.wait_for(lambda: failure.called)
        restored.assert_called()
        self.assertIn('心跳', failure.call_args.args[0])

    def test_stop_before_start_is_inert(self):
        factory = Mock()
        restored = Mock()
        v = WindowsCursorVisibility(process_factory=factory, recovery=restored)
        v.close()
        factory.assert_not_called()
        restored.assert_not_called()

    def test_first_hide_after_long_idle_has_a_new_reply_deadline(self):
        v, _, failure = self.visibility()
        v.start()
        v._last_reply = time.monotonic() - 100
        v.hide()
        time.sleep(.08)
        failure.assert_not_called()
        self.assertTrue(v._hidden)

    def test_new_hide_ack_supersedes_older_physical_takeover_in_pipe(self):
        child = CHILD.replace(
            "run_guard(FakeApi(), lambda: True, lambda: sys.stdin.buffer.read1(256),\n          lambda text: print(text, flush=True), lease=.25)",
            "def reply(text):\n    if text.startswith('OK ') and text.endswith(' H'):\n        print('TAKEOVER', flush=True)\n    print(text, flush=True)\nrun_guard(FakeApi(), lambda: True, lambda: sys.stdin.buffer.read1(256), reply, lease=.25)")
        v, _, failure = self.visibility(child)
        v.hide(watch_position=True)
        self.assertTrue(v._hidden)
        time.sleep(.3)  # Continued heartbeats keep the new lease alive.
        failure.assert_not_called()

    def test_frozen_and_embedded_python_select_correct_helper_entrypoint(self):
        with patch('sharemouse.cursor_visibility.sys.platform', 'win32'), patch('sharemouse.cursor_visibility.subprocess.Popen') as spawn:
            with patch('sharemouse.cursor_visibility.sys.frozen', True, create=True):
                WindowsCursorVisibility._spawn()
                self.assertEqual(spawn.call_args.args[0][1], '--cursor-guard')
            with patch('sharemouse.cursor_visibility.sys.frozen', False, create=True):
                WindowsCursorVisibility._spawn()
                self.assertEqual(spawn.call_args.args[0][1:3], ['-m', 'sharemouse.cursor_guard'])

    def test_close_during_process_creation_leaves_no_helper_or_pipe(self):
        created, release = threading.Event(), threading.Event()
        processes, errors = [], []
        def spawn():
            process = subprocess.Popen([sys.executable, '-u', '-c', CHILD],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0)
            processes.append(process)
            created.set()
            release.wait(2)
            return process
        v = WindowsCursorVisibility(process_factory=spawn, recovery=Mock())
        self.addCleanup(v.close)
        def start():
            try:
                v.start()
            except RuntimeError as exc:
                errors.append(exc)
        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(created.wait(2))
        v.close()
        release.set()
        starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed and processes[0].stdout.closed)


class MacVisibilityTests(unittest.TestCase):
    def test_repeated_hide_show_calls_are_balanced_on_original_display(self):
        api = Mock()
        api.kCGErrorSuccess = 0
        api.CGMainDisplayID.return_value = 73
        api.CGDisplayHideCursor.return_value = 0
        api.CGDisplayShowCursor.return_value = 0
        v = MacCursorVisibility(api)
        for _ in range(30):
            v.hide(); v.hide(); v.show(); v.show()
        self.assertEqual(api.CGDisplayHideCursor.call_count, 30)
        self.assertEqual(api.CGDisplayShowCursor.call_count, 30)
        api.CGDisplayShowCursor.assert_called_with(73)

    def test_failed_hide_never_adds_show_and_failed_show_can_retry(self):
        api = Mock(kCGErrorSuccess=0)
        api.CGMainDisplayID.return_value = 1
        api.CGDisplayHideCursor.return_value = 1
        v = MacCursorVisibility(api)
        with self.assertRaises(RuntimeError):
            v.hide()
        v.show()
        api.CGDisplayShowCursor.assert_not_called()
        api.CGDisplayHideCursor.return_value = 0
        v.hide()
        api.CGDisplayShowCursor.return_value = 1
        with self.assertRaises(RuntimeError):
            v.show()
        api.CGDisplayShowCursor.return_value = 0
        v.close()
        self.assertFalse(v._hidden)

    def test_inactive_mac_local_mouse_restores_once_until_explicit_reentry(self):
        api = Mock(kCGErrorSuccess=0)
        api.CGMainDisplayID.return_value = 73
        api.CGDisplayHideCursor.return_value = 0
        api.CGDisplayShowCursor.return_value = 0
        point = SimpleNamespace(x=500, y=300)
        api.CGEventGetLocation.return_value = point
        v = MacCursorVisibility(api)
        v.POLL_INTERVAL = 10  # Drive deterministic polling below, not wall time.
        self.addCleanup(v.close)
        v.hide(watch_position=True)
        v._watch_once()
        api.CGDisplayShowCursor.assert_not_called()
        point.x += 1
        v._watch_once()
        self.assertFalse(v._hidden)
        for _ in range(5):
            point.x += 1
            v._watch_once()
        api.CGDisplayShowCursor.assert_called_once_with(73)
        self.assertEqual(api.CGDisplayHideCursor.call_count, 1)
        v.hide(watch_position=True)
        self.assertTrue(v._hidden)
        self.assertEqual(api.CGDisplayHideCursor.call_count, 2)
        v.close()
        self.assertFalse(v._monitor.is_alive())
        self.assertEqual(api.CGDisplayShowCursor.call_count, 2)

    def test_mac_monitor_error_restores_and_can_close_from_its_own_thread(self):
        api = Mock(kCGErrorSuccess=0)
        api.CGMainDisplayID.return_value = 1
        api.CGDisplayHideCursor.return_value = 0
        api.CGDisplayShowCursor.return_value = 0
        api.CGEventGetLocation.return_value = SimpleNamespace(x=2, y=3)
        failure = threading.Event()
        v = MacCursorVisibility(api)
        self.addCleanup(v.close)
        v.on_failure = lambda reason: (v.close(), failure.set())
        v.hide(watch_position=True)
        api.CGEventGetLocation.side_effect = RuntimeError('read failed')
        self.assertTrue(failure.wait(1))
        self.assertFalse(v._hidden)
        v.close()
        self.assertFalse(v._monitor.is_alive())

    def test_mac_nonidle_capture_never_polls_position(self):
        api = Mock(kCGErrorSuccess=0)
        api.CGMainDisplayID.return_value = 1
        api.CGDisplayHideCursor.return_value = 0
        api.CGDisplayShowCursor.return_value = 0
        v = MacCursorVisibility(api)
        v.hide(watch_position=False)
        v._watch_once()
        api.CGEventGetLocation.assert_not_called()
        self.assertIsNone(v._monitor)
        v.close()


class InputVisibilityTests(unittest.TestCase):
    def adapter(self, controller=False):
        adapter = Input.__new__(Input)
        adapter.engine = SimpleNamespace(controller=controller, status=Mock(), local=SimpleNamespace(width=1920, height=1080))
        adapter.visibility = Mock()
        adapter.target_position = (100, 200)
        adapter.mouse = Mock()
        return adapter

    def test_receiver_hide_invalidates_cached_position_then_show(self):
        adapter = self.adapter()
        adapter.set_remote_active(False)
        self.assertIsNone(adapter.target_position)
        adapter.visibility.hide.assert_called_once_with(watch_position=True)
        adapter.move_to((100, 200))
        self.assertEqual(adapter.mouse.position, (100, 200))
        adapter.set_remote_active(True)
        adapter.visibility.show.assert_called_once()

    def test_source_warp_failure_restores_cursor(self):
        adapter = self.adapter(True)
        adapter.warp = Mock(side_effect=RuntimeError('warp failed'))
        with patch('sharemouse.input.sys.platform', 'win32'):
            with self.assertRaises(RuntimeError):
                adapter.capture()
        adapter.visibility.hide.assert_called_once_with(watch_position=False)
        adapter.visibility.show.assert_called_once()

    def test_stop_restores_even_when_native_listener_or_key_release_fails(self):
        adapter = self.adapter()
        adapter.ml = Mock()
        adapter.ml.release_mouse.side_effect = RuntimeError('native listener ended')
        adapter.listeners = [Mock()]
        adapter.release = Mock(side_effect=RuntimeError('key release failed'))
        adapter.physical_buttons = {'left'}
        with patch('sharemouse.input.sys.platform', 'darwin'):
            adapter.stop()
        adapter.visibility.close.assert_called_once()
        adapter.listeners[0].stop.assert_called_once()
        self.assertEqual(adapter.physical_buttons, set())

    def test_pending_return_blocks_windows_reentry_and_still_suppresses_mouse(self):
        adapter = self.adapter(True)
        adapter.engine.state_lock = threading.RLock()
        adapter.engine.connected = True
        adapter.engine.remote = False
        adapter.engine.pending_return = (3, .5, 99.)
        adapter.engine.send = Mock()
        adapter.engine.epoch = 3
        adapter.edge_switch = EdgeSwitch()
        adapter.ml = Mock()
        point = SimpleNamespace(flags=0, pt=SimpleNamespace(x=1919, y=300))
        with patch('sharemouse.input.sys.platform', 'win32'):
            adapter.win_mouse(0x200, point)
        adapter.ml.suppress_event.assert_called_once()
        adapter.engine.send.assert_not_called()
        self.assertFalse(adapter.engine.remote)
        self.assertEqual(adapter.engine.epoch, 3)

    def test_pending_return_blocks_mac_deltas_without_touching_edge_gate(self):
        adapter = self.adapter(True)
        adapter.engine.state_lock = threading.RLock()
        adapter.engine.connected = True
        adapter.engine.remote = False
        adapter.engine.pending_return = (3, .5, 99.)
        adapter.engine.send = Mock()
        adapter.edge_switch = EdgeSwitch()
        adapter.mac_move(1919, 300, 12, 0)
        adapter.engine.send.assert_not_called()
        self.assertIsNone(adapter.edge_switch.last_x)

    def test_pending_return_keyboard_is_suppressed_but_f12_still_processed(self):
        from pynput import keyboard
        adapter = self.adapter(True)
        adapter.engine.state_lock = threading.RLock()
        adapter.engine.remote = False
        adapter.engine.pending_return = (3, .5, 99.)
        adapter.engine.return_local = Mock()
        adapter.kl = Mock()
        adapter.kl._event_to_key.return_value = keyboard.Key.f12
        adapter.physical = set()
        adapter.key_payloads = {}
        with patch('sharemouse.input.sys.platform', 'win32'):
            adapter.win_key(0x100, SimpleNamespace(flags=0, vkCode=0x7b))
        adapter.engine.return_local.assert_called_once()
        adapter.kl.suppress_event.assert_called_once()


if __name__ == '__main__':
    unittest.main()
