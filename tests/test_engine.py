import unittest
from unittest.mock import Mock

from sharemouse.core import Screen
from sharemouse.engine import Engine


class EngineTests(unittest.TestCase):
    def engine(self, controller=False):
        e = Engine(controller, Screen(1920, 1080), 'right', 'long-test-password',
                   '127.0.0.1', 24809, Mock(), clipboard=False)
        e.input = Mock()
        e.input.listeners = []
        e.input.mouse = Mock()
        e.connected = True
        return e

    def test_remote_exit_releases_keys(self):
        e = self.engine()
        e.receive({'type': 'enter', 'side': 'right', 'fraction': .25})
        self.assertTrue(e.active)
        e.receive({'type': 'move', 'dx': -10, 'dy': 0})
        self.assertFalse(e.active)
        self.assertEqual(e.outgoing.get_nowait()['type'], 'return')
        self.assertEqual(e.input.release.call_count, 2)

    def test_disconnect_recovers_controller(self):
        e = self.engine(True)
        e.remote = True
        e.stop()
        self.assertFalse(e.remote)
        self.assertFalse(e.connected)
        e.input.stop.assert_called_once()
        e.input.restore_local.assert_called_once()

    def test_connection_error_is_not_overwritten_by_return_status(self):
        e = self.engine(True)
        e.remote = True
        e.stop('发送中断：测试故障')
        self.assertEqual(e.status.call_args.args, ('发送中断：测试故障',))
        self.assertFalse(e.connected)

    def test_inactive_receiver_ignores_keys(self):
        e = self.engine()
        e.receive({'type': 'key', 'key': {'char': 'a'}, 'down': True})
        e.input.inject_key.assert_not_called()

    def test_file_receive_defaults_off(self):
        e = self.engine()
        e.receive({'type': 'file_offer', 'name': 'a', 'size': 0})
        self.assertEqual(e.outgoing.get_nowait(), {'type': 'file_reply', 'accepted': False})

    def test_queue_overflow_restores_local_control(self):
        e = self.engine(True)
        e.remote = True
        for _ in range(2048):
            e.send({'type': 'ping'})
        self.assertFalse(e.send({'type': 'ping'}))
        self.assertFalse(e.remote)
        self.assertTrue(e.stop_event.is_set())
