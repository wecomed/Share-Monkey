"""A local success must not be confused with peer native-input processing."""
import unittest
from unittest.mock import patch

from tools.benchmark_bluetooth_delivery import BufferedDeliveryModel


class BufferedBluetoothDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        for mtu, packets in ((20, 1), (20, 4), (185, 1)):
            for background in (False, True):
                for limit in (2, 1000000):
                    key = (mtu, packets, background, limit)
                    cls.results[key] = BufferedDeliveryModel(
                        mtu=mtu, packets_per_event=packets, samples=1000, trace='curve',
                        clipboard=background, credit_limit=limit, drain_before_return=True).run()

    def test_local_acceptance_is_not_delivery_and_forward_delivery_is_not_consumed_ack(self):
        model = BufferedDeliveryModel(mtu=20, samples=1, rate=1)
        message = model.sender.delivery.prepare(dict(type='enter', epoch=1, side='right', fraction=.5))
        model._send(0, message)
        self.assertEqual(model.now, 0)
        self.assertEqual(model.arrived_frames, 0)
        self.assertFalse(model.receiver.active)
        self.assertEqual(model.sender.delivery.pending_count, 1)
        self.assertGreater(model._pending_packets[0], 0)
        model.advance(.03)
        self.assertTrue(model.receiver.active)
        self.assertEqual(model.arrived_frames, 1)
        self.assertEqual(model.sender.delivery.pending_count, 1, 'peer ACK still crosses reverse FIFO')
        model.advance(.03)
        self.assertEqual(model.sender.delivery.pending_count, 0)
        self.assertEqual(model.arrived_frames, 2)

    def test_processed_credit_bounds_os_queue_after_immediate_local_acceptance(self):
        for key, after in self.results.items():
            if key[-1] != 2:
                continue
            with self.subTest(profile=key):
                before = self.results[key[:-1] + (1000000,)]
                self.assertEqual(after['peak_inflight_data'], 2)
                self.assertLessEqual(after['peak_os_packets'][0], 20)
                self.assertEqual(before['peak_os_packets'][0], 128)
                self.assertGreater(before['motion_p95_ms'], after['motion_p95_ms'])
                self.assertGreater(before['peak_inflight_data'], 2)

    def test_every_delivered_key_and_button_keeps_its_position_and_order(self):
        for key, result in self.results.items():
            with self.subTest(profile=key):
                self.assertTrue(result['input_order_preserved'])
                self.assertEqual(result['input_events_delivered'], 4)
                self.assertEqual(result['input_events_cancelled_by_return'], 0)
                self.assertLessEqual(result['control_position_max_error_px'], .001)
                self.assertEqual((result['held_keys'], result['held_buttons']), (0, 0))
                self.assertEqual(result['local_remote_overlap_ms'], 0)

    def test_background_clipboard_is_bounded_and_completes_without_deadlock(self):
        for key, result in self.results.items():
            if key[2]:
                with self.subTest(profile=key):
                    self.assertTrue(result['clipboard_complete'])
                    self.assertEqual(result['peak_inflight_background'], 1)
                    if key[-1] == 2:
                        self.assertLessEqual(result['motion_p95_ms'], 250)
                        self.assertLessEqual(result['control_max_ms'], 180)

    def test_native_injection_failure_never_produces_consumed_ack(self):
        model = BufferedDeliveryModel(mtu=20, samples=1, rate=1)
        model.receiver.receive(dict(type='enter', epoch=1, side='right', fraction=.5))
        self.assertTrue(model.receiver.outgoing.empty())
        with patch.object(model.receiver.input, 'move_to', side_effect=OSError('SetCursorPos failed')):
            with self.assertRaisesRegex(OSError, 'SetCursorPos'):
                model.receiver.receive(dict(type='pointer', epoch=1, x=10, y=10, _delivery=123))
        self.assertTrue(model.receiver.outgoing.empty(), 'failed native operation must not earn credit')


if __name__ == '__main__':
    unittest.main()
