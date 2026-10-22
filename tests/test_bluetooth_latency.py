"""Virtual GATT scheduling and handoff presentation; no radios or OS hooks."""
import unittest

from sharemouse.core import Cursor, Screen
from sharemouse.engine import Engine
from tools.benchmark_bluetooth import SimulatedInput
from tools.benchmark_bluetooth_delivery import BufferedDeliveryModel as BluetoothSchedulingModel


class BluetoothLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        for mtu, packets in ((20, 1), (20, 4), (185, 1)):
            for trace in ('straight', 'foldback', 'return', 'curve'):
                for drain in (False, True):
                    key = (mtu, packets, trace, drain)
                    cls.results[key] = BluetoothSchedulingModel(
                        mtu=mtu, packets_per_event=packets, trace=trace, drain_before_return=drain).run()

    def test_all_profiles_restore_only_after_receiver_stops_and_ack_arrives(self):
        for key, result in self.results.items():
            with self.subTest(profile=key):
                self.assertEqual(result['local_remote_overlap_ms'], 0)
                self.assertTrue(result['pending_return_kept_captured'])
                self.assertTrue(result['receiver_positioned_before_visible'])
                self.assertFalse(result['receiver_active'])
                self.assertFalse(result['receiver_cursor_visible'])
                self.assertFalse(result['source_cursor_captured'])
                self.assertEqual((result['held_keys'], result['held_buttons']), (0, 0))
                if result['return_requested_at'] is not None:
                    self.assertIsNotNone(result['left_ack_received_at'])
                    self.assertGreaterEqual(result['left_ack_received_at'], result['receiver_inactive_at'])
                    self.assertGreaterEqual(result['local_restored_at'], result['left_ack_received_at'])

    def test_full_drain_preserves_every_key_and_button_barrier_in_all_profiles(self):
        for key, result in self.results.items():
            if key[-1] and key[2] != 'return':
                with self.subTest(profile=key):
                    self.assertTrue(result['input_order_preserved'])
                    self.assertEqual(result['input_events_cancelled_by_return'], 0)
                    self.assertEqual(result['input_events_delivered'], 4)
                    self.assertLessEqual(result['control_position_max_error_px'], .001)

    def test_immediate_f12_cancels_only_an_ordered_suffix_and_avoids_backlog_wait(self):
        for key, result in self.results.items():
            if not key[-1]:
                with self.subTest(profile=key):
                    self.assertTrue(result['input_prefix_preserved'])
                    if result['return_requested_at'] is not None:
                        self.assertLessEqual(result['return_control_ms'], 90)
                        self.assertLessEqual(result['local_restore_delay_ms'], 180)

    def test_straight_foldback_and_curve_snapshots_bound_lag_at_1000hz(self):
        for key, result in self.results.items():
            if key[2] in ('straight', 'foldback', 'curve'):
                with self.subTest(profile=key):
                    self.assertLessEqual(result['motion_p95_ms'], 120)
                    self.assertLessEqual(result['control_max_ms'], 180)
                    self.assertLess(result['peak_queue'], 150)

    def engine(self):
        engine = Engine(True, Screen(1920, 1080), 'right', 'ble-latency-password',
                        '', 0, lambda message: None, clipboard=False, transport='bluetooth')
        engine.connected, engine.remote, engine.epoch = True, True, 7
        engine.input = SimulatedInput(lambda: 0.)
        engine.input.captured = True
        return engine

    def test_f12_discards_queued_epoch_input_but_keeps_capture_until_matching_ack(self):
        engine = self.engine()
        for _ in range(1000):
            engine.send(dict(type='move', dx=1, dy=0))
        engine.send(dict(type='key', key={'char': 'x'}, down=True))
        engine.send(dict(type='button', button='left', down=True))
        engine.return_local(.75)
        self.assertFalse(engine.remote)
        self.assertTrue(engine.input.captured)
        self.assertFalse(engine.input.restores)
        self.assertEqual(engine.outgoing.qsize(), 1)
        self.assertEqual(engine.outgoing.get_nowait(), dict(type='leave', epoch=7))
        self.assertFalse(engine._current_input(dict(type='key', epoch=7)))
        self.assertFalse(engine._current_input(dict(type='move', epoch=6)))
        engine.receive(dict(type='left', epoch=6))
        self.assertTrue(engine.input.captured)
        engine.receive(dict(type='left', epoch=7))
        self.assertFalse(engine.input.captured)
        self.assertIsNone(engine.pending_return)
        self.assertEqual(len(engine.input.restores), 1)
        self.assertEqual(engine.input.restores[0][1][1], .75 * 1079)

    def test_repeated_enter_rebound_and_deliberate_return_preserve_epoch_and_visibility(self):
        engine = Engine(False, Screen(1920, 1080), 'right', 'ble-latency-password',
                        '', 0, lambda message: None, clipboard=False, transport='bluetooth')
        engine.connected = True
        engine.input = SimulatedInput(lambda: 0.)
        for epoch in range(1, 31):
            engine.receive(dict(type='enter', epoch=epoch, side='right', fraction=.5))
            self.assertTrue(engine.active)
            engine.receive(dict(type='move', epoch=epoch, dx=-4, dy=0))
            self.assertTrue(engine.active, 'small seam rebound returned immediately')
            engine.receive(dict(type='move', epoch=epoch, dx=20, dy=0))
            self.assertTrue(engine.active)
            engine.receive(dict(type='move', epoch=epoch, dx=-40, dy=0))
            self.assertFalse(engine.active)
            self.assertFalse(engine.input.remote_active)
            self.assertEqual(engine.outgoing.get_nowait()['epoch'], epoch)
            old_position = engine.input.positions[-1]
            engine.receive(dict(type='move', epoch=epoch - 1, dx=200, dy=0))
            self.assertEqual(engine.input.positions[-1], old_position)

    def test_snapshots_coalesce_without_crossing_key_or_button_position_barriers(self):
        engine = self.engine()
        engine.peer_screen = Screen(1920, 1080)
        engine.send(dict(type='enter', side='right', fraction=.5))
        for _ in range(1000):
            engine.send(dict(type='move', dx=1, dy=0))
        engine.send(dict(type='key', key={'char': 'x'}, down=True))
        for _ in range(50):
            engine.send(dict(type='move', dx=1, dy=0))
        engine.send(dict(type='button', button='left', down=True))
        for _ in range(20):
            engine.send(dict(type='move', dx=-1, dy=0))
        events = []
        while not engine.outgoing.empty():
            events.append(engine.outgoing.get_nowait())
        self.assertEqual([event['type'] for event in events],
                         ['enter', 'pointer', 'key', 'pointer', 'button', 'pointer'])
        self.assertEqual([event['x'] for event in events if event['type'] == 'pointer'], [1002, 1052, 1032])

    def test_snapshot_geometry_clips_overshoot_then_detects_return_before_transmission(self):
        engine = self.engine()
        engine.peer_screen = Screen(1920, 1080)
        engine.send(dict(type='enter', side='right', fraction=.5))
        engine.send(dict(type='move', dx=4000, dy=0))
        self.assertEqual(engine.peer_cursor.x, 1919)
        engine.send(dict(type='move', dx=-1900, dy=0))
        self.assertEqual(engine.peer_cursor.x, 19)
        self.assertTrue(engine.remote)
        engine.send(dict(type='move', dx=-25, dy=0))
        self.assertTrue(engine.remote)
        engine.send(dict(type='move', dx=-3, dy=0))
        self.assertFalse(engine.remote)
        self.assertIsNotNone(engine.pending_return)
        self.assertTrue(engine.input.captured)
        self.assertEqual(engine.outgoing.qsize(), 1)
        self.assertEqual(engine.outgoing.get_nowait(), dict(type='leave', epoch=7))

    def test_cursor_rebound_tolerance_scales_with_display_dpi(self):
        for scale in (1., 1.5, 2.):
            with self.subTest(scale=scale):
                cursor = Cursor(Screen(1920, 1080, scale), 'right', return_margin=8 * scale)
                cursor.enter(.5)
                self.assertFalse(cursor.move(-4 * scale, 0)[0])
                self.assertTrue(cursor.move(-20 * scale, 0)[0])


if __name__ == '__main__':
    unittest.main()
