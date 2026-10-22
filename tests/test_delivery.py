"""Credits are earned only by remote processing, not OS enqueue success."""
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sharemouse.core import Screen
from sharemouse.delivery import DeliveryWindow
from sharemouse.engine import Engine
from sharemouse.motion import InputQueue


class DeliveryTests(unittest.TestCase):
    def test_native_acceptance_cannot_expand_window_and_unknown_ack_cannot_free_credit(self):
        window = DeliveryWindow()
        first = window.prepare(dict(type='pointer', epoch=1))
        second = window.prepare(dict(type='key', epoch=1))
        self.assertFalse(window.can_send())
        self.assertEqual(window.pending_count, 2)
        self.assertIsNone(window.acknowledge(999))
        self.assertFalse(window.can_send())
        with self.assertRaises(RuntimeError):
            window.prepare(dict(type='pointer', epoch=1))
        self.assertEqual(window.acknowledge(second['_delivery'])['kind'], 'key')
        self.assertTrue(window.can_send())
        self.assertEqual(window.acknowledge(first['_delivery'])['kind'], 'pointer')
        self.assertEqual(window.pending_count, 0)
        self.assertEqual(window.peak, 2)

    def test_background_is_limited_separately_from_foreground_and_timeouts_use_processing(self):
        clock = [10.]
        window = DeliveryWindow(clock=lambda: clock[0])
        message = window.prepare(dict(type='clipboard_part'), background=True)
        self.assertTrue(window.can_send())
        self.assertFalse(window.can_send(background=True))
        window.prepare(dict(type='pointer', epoch=1))
        clock[0] = 12.6
        self.assertLess(window.remaining(), 0)
        window.acknowledge(message['_delivery'])
        self.assertTrue(window.can_send(background=True))

    def test_epoch_cancel_does_not_drop_unrelated_background_and_late_ack_is_ignored(self):
        window = DeliveryWindow()
        message = window.prepare(dict(type='pointer', epoch=2))
        background = window.prepare(dict(type='clipboard_part'), background=True)
        window.cancel_epoch(2)
        self.assertEqual(window.pending_count, 1)
        self.assertIsNone(window.acknowledge(message['_delivery']))
        self.assertIsNotNone(window.acknowledge(background['_delivery']))
        self.assertIsNone(window.remaining())

    def test_ids_never_reused_and_invalid_acks_fail(self):
        window = DeliveryWindow()
        first = window.prepare(dict(type='key'))['_delivery']
        window.clear()
        self.assertGreater(window.prepare(dict(type='key'))['_delivery'], first)
        for value in (True, -1, 2 ** 32, '1', None):
            with self.assertRaises(ValueError):
                window.acknowledge(value)

    def test_bypass_acks_and_returns_remain_sendable_while_data_waits(self):
        outgoing = InputQueue(20, urgent=True)
        outgoing.put(dict(type='enter', epoch=2))
        outgoing.put(dict(type='key', epoch=2))
        outgoing.put(dict(type='consumed', packet=1))
        outgoing.put(dict(type='leave', epoch=2))
        self.assertEqual(outgoing.get_bypass_nowait()['type'], 'leave')
        self.assertEqual(outgoing.get_bypass_nowait()['type'], 'consumed')
        with self.assertRaises(queue.Empty):
            outgoing.get_bypass_nowait()
        self.assertEqual(outgoing.get_nowait()['type'], 'enter')

    def engine(self, controller=False):
        engine = Engine(controller, Screen(1920, 1080), 'right', 'delivery-test-password',
                        '', 0, Mock(), clipboard=False, transport='bluetooth')
        engine.connected = True
        engine.input = Mock()
        engine.input.listeners = []
        self.addCleanup(engine.stop)
        return engine

    def test_native_failure_never_acknowledges_processing(self):
        receiver = self.engine()
        receiver.receive(dict(type='enter', epoch=1, side='right', fraction=.5))
        receiver.input.move_to.side_effect = OSError('SetCursorPos failed')
        with self.assertRaises(OSError):
            receiver.receive(dict(type='pointer', epoch=1, x=20, y=20, _delivery=12))
        self.assertTrue(receiver.outgoing.empty())

    def test_successful_input_is_processed_before_credit_is_returned(self):
        receiver = self.engine()
        receiver.receive(dict(type='enter', epoch=1, side='right', fraction=.5))
        seen = []
        receiver.input.move_to.side_effect = lambda position: seen.append(position)
        receiver.receive(dict(type='pointer', epoch=1, x=100, y=200, _delivery=13))
        self.assertEqual(seen, [(100, 200)])
        self.assertEqual(receiver.outgoing.get_nowait(), dict(type='consumed', packet=13))

    def test_native_cursor_preparation_failure_never_announces_entry(self):
        sender = self.engine(True)
        sender.remote, sender.epoch = True, 1
        sender.input.wait_capture_ready.side_effect = RuntimeError('capture not ready')
        sender.send(dict(type='enter', side='right', fraction=.5))
        channel = Mock()
        sender.writer(channel)
        channel.send.assert_not_called()
        self.assertFalse(sender.connected)

    def test_f12_while_capture_is_preparing_cancels_enter_after_wait(self):
        sender = self.engine(True)
        sender.remote, sender.epoch = True, 1
        sender.send(dict(type='enter', side='right', fraction=.5))
        def prepared(stop):
            sender.return_local()
            return True
        sender.input.wait_capture_ready.side_effect = prepared
        channel = Mock()
        def sent(message):
            self.assertEqual(message['type'], 'leave')
            sender.stop_event.set()
        channel.send.side_effect = sent
        sender.writer(channel)
        channel.send.assert_called_once()

    def test_old_bluetooth_peer_is_rejected_before_any_remote_capture(self):
        for capability in (None, 2):
            with self.subTest(protocol=capability):
                sender = self.engine(True)
                sender.connected = False
                backend = Mock()
                channel = Mock()
                channel.recv.return_value = dict(type='screen', width=1920, height=1080,
                    scale=1, input_protocol=3, controller=False, transport_protocol=capability)
                module = SimpleNamespace(MacBleServer=Mock(return_value=backend))
                with patch.dict('sys.modules', {'sharemouse.bluetooth_mac': module}), \
                     patch('sharemouse.engine.sys', SimpleNamespace(platform='darwin')), \
                     patch('sharemouse.engine.Channel.handshake', return_value=channel):
                    sender.run()
                self.assertFalse(sender.connected)
                self.assertFalse(sender.remote)
                sender.input.capture.assert_not_called()
                self.assertIn('0.5.2', sender.status.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
