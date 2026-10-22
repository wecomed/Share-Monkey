import unittest
from unittest.mock import Mock
from sharemouse.switching import EdgeSwitch
from sharemouse.core import Screen
from sharemouse.engine import Engine


class SwitchingTests(unittest.TestCase):
    def test_requires_outward_motion_and_rearms_by_distance_not_time(self):
        for side, edge, inward, toward, away in (('right', 1919, 1880, 1, -1), ('left', 0, 40, -1, 1)):
            g = EdgeSwitch()
            self.assertFalse(g.update(edge, 1920, side, 0))
            self.assertFalse(g.update(edge, 1920, side, away))
            self.assertTrue(g.update(edge, 1920, side, toward))
            g.returned()
            for _ in range(1000):
                self.assertFalse(g.update(edge, 1920, side, toward))
            self.assertFalse(g.update(inward, 1920, side, away))
            self.assertTrue(g.update(edge, 1920, side, toward))

    def engine(self, controller=False):
        e = Engine(controller, Screen(1920, 1080), 'right', 'long-password', '', 24809, Mock(), clipboard=False)
        e.input = Mock()
        e.input.listeners = []
        e.connected = True
        return e

    def test_stale_return_cannot_end_new_capture(self):
        e = self.engine(True)
        e.remote, e.epoch = True, 3
        e.receive({'type': 'return', 'epoch': 2, 'fraction': .1})
        self.assertTrue(e.remote)
        e.input.restore_local.assert_not_called()
        e.receive({'type': 'return', 'epoch': 3, 'fraction': .5})
        self.assertFalse(e.remote)
        e.input.restore_local.assert_called_once()

    def test_duplicate_enter_and_old_input_do_not_reset_or_move_cursor(self):
        e = self.engine()
        enter = {'type': 'enter', 'epoch': 3, 'fraction': .5, 'side': 'right'}
        e.receive(enter)
        e.receive({'type': 'move', 'epoch': 3, 'dx': 200, 'dy': 20})
        before = (e.cursor.x, e.cursor.y)
        e.input.reset_mock()
        for message in (enter, dict(enter, epoch=2), {'type': 'leave', 'epoch': 2},
                        {'type': 'move', 'epoch': 2, 'dx': -1000, 'dy': 500},
                        {'type': 'button', 'epoch': 2, 'button': 'left', 'down': True}):
            e.receive(message)
        self.assertEqual((e.cursor.x, e.cursor.y), before)
        self.assertTrue(e.active)
        e.input.move_to.assert_not_called()
        e.input.inject_button.assert_not_called()
        e.receive({'type': 'move', 'epoch': 3, 'dx': -1000, 'dy': 0})
        self.assertEqual(e.outgoing.get_nowait()['epoch'], 3)

    def test_input_queue_preserves_epoch_at_enqueue(self):
        e = self.engine(True)
        e.epoch = 4
        e.send({'type': 'move', 'dx': 1, 'dy': 0})
        e.epoch = 5
        e.send({'type': 'enter', 'side': 'right', 'fraction': .5})
        self.assertEqual(e.outgoing.get_nowait()['epoch'], 4)
        self.assertEqual(e.outgoing.get_nowait()['epoch'], 5)
