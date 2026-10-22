import queue
import unittest
from unittest.mock import Mock

from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.motion import collect_motion, MAX_SAMPLES


class MotionTests(unittest.TestCase):
    def engine(self, controller=False, screen=None):
        engine = Engine(controller, screen or Screen(1920, 1080), 'right',
                        'motion-test-password', '', 24809, Mock(), clipboard=False)
        engine.connected = True
        engine.input = Mock()
        engine.input.listeners = []
        return engine

    def test_batch_preserves_direction_changes_and_button_key_epoch_barriers(self):
        first = {'type': 'move', 'epoch': 1, 'dx': 1, 'dy': 0}
        for barrier in ({'type': 'button', 'down': True}, {'type': 'key'},
                        {'type': 'leave'}, {'type': 'enter', 'epoch': 2},
                        {'type': 'move', 'epoch': 2, 'dx': 100, 'dy': 0}):
            with self.subTest(barrier=barrier):
                outgoing = queue.Queue()
                outgoing.put(dict(first, dx=-2, dy=3))
                outgoing.put(barrier)
                outgoing.put(dict(first, dx=7))
                frame, pending = collect_motion(first, outgoing, 0)
                self.assertEqual(frame['path'], [[1, 0], [-2, 3]])
                self.assertIs(pending, barrier)
                self.assertEqual(outgoing.get_nowait()['dx'], 7)

    def test_batch_is_bounded_and_keeps_all_remaining_samples(self):
        first = {'type': 'move', 'epoch': 1, 'dx': 1, 'dy': 0}
        outgoing = queue.Queue()
        for _ in range(100):
            outgoing.put(first)
        frame, pending = collect_motion(first, outgoing, 0)
        self.assertEqual(len(frame['path']), MAX_SAMPLES)
        self.assertIsNone(pending)
        self.assertEqual(outgoing.qsize(), 101 - MAX_SAMPLES)

    def test_batched_path_preserves_clipping_and_first_return(self):
        engine = self.engine(screen=Screen(100, 100))
        engine.receive({'type': 'enter', 'epoch': 1, 'side': 'right', 'fraction': .5})
        engine.receive({'type': 'move', 'epoch': 1, 'path': [[0, -100], [0, 50]]})
        self.assertEqual((engine.cursor.x, engine.cursor.y), (2, 50))
        engine.input.move_to.assert_called_with((2, 50))
        engine.receive({'type': 'move', 'epoch': 1, 'path': [[-10, 0], [100, 10]]})
        self.assertFalse(engine.active)
        engine.input.move_to.assert_called_with((0, 50))
        self.assertEqual(engine.outgoing.get_nowait()['type'], 'return')

    def test_speed_and_dpi_are_snapshotted_without_losing_fractional_moves(self):
        engine = self.engine(True)
        engine.peer_screen = Screen(3840, 2160, 2)
        engine.set_mouse_speed(.25)
        engine.send({'type': 'move', 'dx': 1, 'dy': -1})
        engine.set_mouse_speed(1.5)
        engine.send({'type': 'move', 'dx': 1, 'dy': -1})
        self.assertEqual(engine.outgoing.get_nowait()['dx'], .5)
        self.assertEqual(engine.outgoing.get_nowait()['dx'], 3)
        engine.local, engine.peer_screen = Screen(3840, 2160, 2), Screen(1920, 1080)
        engine.set_mouse_speed(1)
        engine.send({'type': 'move', 'dx': 2, 'dy': -2})
        self.assertEqual(engine.outgoing.get_nowait()['dx'], 1)

    def test_writer_reduces_updates_and_click_uses_preceding_motion(self):
        sender, receiver = self.engine(True), self.engine()
        sender.send({'type': 'enter', 'side': 'right', 'fraction': .5})
        for _ in range(200):
            sender.send({'type': 'move', 'dx': .125, 'dy': 0})
        sender.send({'type': 'button', 'button': 'left', 'down': True})
        for _ in range(50):
            sender.send({'type': 'move', 'dx': .25, 'dy': 0})
        sender.send({'type': 'done'})
        frames, clicked = [], []
        def deliver(message):
            frames.append(message)
            if message['type'] == 'done':
                sender.stop_event.set()
                return
            receiver.receive(message)
            if message['type'] == 'button':
                clicked.append((receiver.cursor.x, receiver.cursor.y))
        channel = Mock()
        channel.send.side_effect = deliver
        sender.writer(channel)
        self.assertEqual(clicked, [(27, 540)])
        self.assertEqual(receiver.cursor.x, 39.5)
        self.assertLessEqual(receiver.input.move_to.call_count, 6)
        self.assertEqual(sum(len(m.get('path', [0])) for m in frames if m['type'] == 'move'), 250)

    def test_malformed_or_nonfinite_paths_are_rejected_before_moving(self):
        for path in ([], [[0, 0]] * 65, [[1]], [[float('nan'), 0]], [[float('inf'), 0]]):
            engine = self.engine()
            engine.receive({'type': 'enter', 'side': 'right', 'fraction': .5})
            engine.input.reset_mock()
            with self.assertRaises((ValueError, TypeError)):
                engine.receive({'type': 'move', 'path': path})
            engine.input.move_to.assert_not_called()

    def test_invalid_speed_and_screen_scale_are_rejected(self):
        engine = self.engine()
        for speed in (0, 5, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                engine.set_mouse_speed(speed)
        for scale in (0, 20, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Screen(1920, 1080, scale)
