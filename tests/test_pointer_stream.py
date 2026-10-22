"""Pointer freshness, clipping, and handoff at a slow BLE stream boundary."""
import queue
import unittest
from unittest.mock import Mock

from sharemouse.core import Cursor, Screen
from sharemouse.engine import Engine
from sharemouse.motion import InputQueue


class PointerStreamTests(unittest.TestCase):
    def pair(self, scale=1):
        engines = [Engine(controller, Screen(1920, 1080, scale), 'right',
                          'pointer-stream-test', '', 0, Mock(), clipboard=False,
                          transport='bluetooth', mouse_speed=1.5) for controller in (True, False)]
        for engine in engines:
            engine.input = Mock()
            engine.connected = True
            engine.peer_screen = engines[1].local
        sender, receiver = engines
        sender.remote, sender.epoch = True, 1
        sender.send(dict(type='enter', side='right', fraction=.5))
        self.addCleanup(lambda: [e.stop() for e in engines])
        return sender, receiver

    def drain(self, sender, receiver):
        messages = []
        while not sender.outgoing.empty():
            message = sender.outgoing.get_nowait()
            messages.append(message)
            receiver.receive(message)
        return messages

    def test_high_rate_curve_has_only_one_unsent_snapshot_with_exact_final_clipping(self):
        sender, receiver = self.pair()
        reference = Cursor(receiver.local, 'right', return_margin=8)
        reference.enter(.5)
        for index in range(10000):
            dx, dy = ((1, 0), (1, 1), (0, 1), (-1, 1),
                      (-1, 0), (-1, -1), (0, -1), (1, -1))[index % 8]
            reference.move(dx * 1.5, dy * 1.5)
            sender.send(dict(type='move', dx=dx, dy=dy))
        self.assertEqual(sender.outgoing.qsize(), 2)
        self.drain(sender, receiver)
        receiver.input.move_to.assert_called_with((reference.x, reference.y))

    def test_click_and_key_barriers_preserve_their_preceding_pointer(self):
        sender, _ = self.pair()
        sender.send(dict(type='move', dx=10, dy=0))
        sender.send(dict(type='button', button='left', down=True))
        sender.send(dict(type='move', dx=20, dy=0))
        sender.send(dict(type='key', key={'char': 'x'}, down=True))
        sender.send(dict(type='move', dx=30, dy=0))
        sender.send(dict(type='move', dx=40, dy=0))
        frames = [sender.outgoing.get_nowait() for _ in range(sender.outgoing.qsize())]
        self.assertEqual([m['type'] for m in frames], ['enter', 'pointer', 'button', 'pointer', 'key', 'pointer'])
        self.assertEqual([m['x'] for m in frames if m['type'] == 'pointer'], [17, 47, 152])

    def test_small_entry_rebound_does_not_return_but_deliberate_outward_motion_does(self):
        sender, receiver = self.pair()
        sender.send(dict(type='move', dx=-1, dy=0))
        sender.send(dict(type='move', dx=-2, dy=0))
        self.assertTrue(sender.remote)
        self.assertIsNone(sender.pending_return)
        sender.send(dict(type='move', dx=-20, dy=0))
        self.assertFalse(sender.remote)
        self.assertIsNotNone(sender.pending_return)
        sender.input.restore_local.assert_not_called()
        messages = self.drain(sender, receiver)
        self.assertEqual([m['type'] for m in messages], ['leave'])
        sender.receive(receiver.outgoing.get_nowait())
        sender.input.restore_local.assert_called_once()
        self.assertIsNone(sender.pending_return)

    def test_fast_return_without_delivered_enter_is_acknowledged_and_cannot_reactivate(self):
        sender, receiver = self.pair()
        enter = sender.outgoing.get_nowait()
        sender.return_local()
        self.drain(sender, receiver)
        receiver.receive(enter)
        self.assertFalse(receiver.active)
        sender.receive(receiver.outgoing.get_nowait())
        sender.input.restore_local.assert_called_once()
        self.assertTrue(sender.connected)

    def test_return_ack_hides_target_before_showing_source_and_next_entry_remains_usable(self):
        sender, receiver = self.pair()
        events = []
        receiver.input.set_remote_active.side_effect = lambda active: events.append(('target', active))
        sender.input.restore_local.side_effect = lambda x, y: events.append(('source', True))
        self.drain(sender, receiver)
        for cycle in range(128):
            events.clear()
            sender.return_local(.4)
            sender.input.restore_local.reset_mock()
            self.drain(sender, receiver)
            self.assertIn(('target', False), events)
            self.assertNotIn(('source', True), events)
            while not receiver.outgoing.empty():
                sender.receive(receiver.outgoing.get_nowait())
            self.assertEqual(events[-1], ('source', True))
            self.assertFalse(receiver.active)
            sender.epoch += 1
            sender.remote = True
            sender.send(dict(type='enter', side='right', fraction=.4))
            self.drain(sender, receiver)
            self.assertTrue(receiver.active)
        self.assertTrue(sender.connected and receiver.connected)

    def test_return_cancels_stale_pending_input_and_held_remote_keys_are_released(self):
        sender, receiver = self.pair()
        self.drain(sender, receiver)
        receiver.receive(dict(type='key', epoch=1, key={'char': 'x'}, down=True))
        for _ in range(500):
            sender.send(dict(type='move', dx=.1, dy=.1))
        sender.send(dict(type='key', key={'char': 'x'}, down=False))
        sender.return_local()
        messages = self.drain(sender, receiver)
        self.assertEqual([m['type'] for m in messages], ['leave'])
        receiver.input.release.assert_called()
        self.assertFalse(receiver.active)
        self.assertFalse(sender._current_input(dict(type='pointer', epoch=1)))

    def test_pointer_validation_and_stale_epoch(self):
        sender, receiver = self.pair()
        self.drain(sender, receiver)
        for x, y in ((-1, 2), (1920, 0), (0, 1080), (float('nan'), 0)):
            with self.assertRaises(ValueError):
                receiver.receive(dict(type='pointer', epoch=1, x=x, y=y))
        receiver.input.move_to.reset_mock()
        receiver.receive(dict(type='pointer', epoch=0, x=20, y=20))
        receiver.input.move_to.assert_not_called()

    def test_urgent_queue_discards_only_superseded_epoch_and_replaces_only_adjacent_pointer(self):
        outgoing = InputQueue(8, urgent=True)
        outgoing.put(dict(type='pointer', epoch=1, x=1))
        outgoing.put(dict(type='pointer', epoch=1, x=2))
        outgoing.put(dict(type='key', epoch=1))
        outgoing.put(dict(type='pointer', epoch=1, x=3))
        outgoing.put(dict(type='clipboard_part'))
        outgoing.discard_epoch(1)
        outgoing.put(dict(type='leave', epoch=1))
        self.assertEqual(outgoing.get_nowait()['type'], 'leave')
        self.assertEqual(outgoing.get_nowait()['type'], 'clipboard_part')
        with self.assertRaises(queue.Empty):
            outgoing.get_nowait()
