"""Connection recovery without hardware hooks or UI actions."""
import threading
import unittest
from unittest.mock import Mock, patch

from sharemouse.bluetooth_transport import BluetoothPeer
from sharemouse.client import MAC, WINDOWS
from sharemouse.core import Screen
from sharemouse.session import Session, Settings


class FakeEngine:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.stop_event = threading.Event()
        self.connected = False
        self.retryable = True
        self.last_error = None
        self.cleanup_complete = True

    @property
    def quiescent(self):
        return self.stop_event.is_set() and self.cleanup_complete

    def start(self):
        pass

    def stop(self):
        self.connected = False
        self.stop_event.set()

    def set_mouse_speed(self, speed):
        self.kwargs['mouse_speed'] = speed


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.engines, self.sessions = [], []
        self.power = Mock()
        self.patches = [
            patch('sharemouse.session.primary_screen', return_value=Screen(1920, 1080)),
            patch('sharemouse.session.Engine', side_effect=self.create_engine),
            patch('sharemouse.session.create_connection_power', return_value=self.power),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for patcher in reversed(self.patches):
            patcher.stop()

    def create_engine(self, *args, **kwargs):
        engine = FakeEngine(*args, **kwargs)
        self.engines.append(engine)
        return engine

    def session(self, client=MAC, transport='bluetooth', **options):
        session = Session(client, discovery=False, persist=False,
                          settings=Settings(transport=transport, manual=True, host='127.0.0.1', **options),
                          clock=lambda: self.now)
        if transport == 'bluetooth' and client is WINDOWS:
            peer = BluetoothPeer('AA:BB:CC:DD:EE:FF', 'Own Mac')
            session.peers = {peer.label: peer}
            session.selected = peer.label
        session.connect('0042')
        self.sessions.append(session)
        return session

    def advance(self, session, seconds):
        self.now += seconds
        session.poll()

    def test_both_ble_roles_recover_without_discovery_or_password_prompt(self):
        for client in (MAC, WINDOWS):
            with self.subTest(client=client.platform):
                session = self.session(client)
                first = session.engine
                first.connected = True
                session.poll()
                first.stop()
                session.poll()
                self.assertTrue(session.running)
                self.assertTrue(session.reconnecting)
                self.assertFalse(session.connected)
                session.peers.clear()
                self.advance(session, 1)
                second = session.engine
                self.assertIsNot(second, first)
                self.assertEqual(second.args[3], '0042')
                self.assertEqual(second.args[4:6], first.args[4:6])
                self.assertFalse(second.connected)

    def test_disconnect_cancels_pending_retry_and_credentials(self):
        session = self.session()
        session.engine.stop()
        session.poll()
        count = len(self.engines)
        session.disconnect()
        self.advance(session, 100)
        self.assertEqual(len(self.engines), count)
        self.assertIsNone(session._request)
        self.assertFalse(session.running)
        self.power.close.assert_called()

    def test_quit_cannot_be_undone_by_late_callbacks(self):
        session = self.session()
        old = session.engine
        session.close()
        old.args[6]('old connected callback')
        self.advance(session, 100)
        self.assertIsNone(session.engine)
        self.assertIsNone(session._request)
        self.assertEqual(len(self.engines), 1)

    def test_old_hooks_must_finish_before_any_new_attempt(self):
        session = self.session()
        old = session.engine
        old.cleanup_complete = False
        old.stop()
        session.poll()
        self.advance(session, 100)
        self.assertEqual(len(self.engines), 1)
        old.cleanup_complete = True
        session.poll()
        self.assertEqual(len(self.engines), 2)

    def test_rapid_manual_reconnect_also_waits_for_cleanup(self):
        session = self.session()
        old = session.engine
        old.cleanup_complete = False
        session.disconnect()
        session.connect('0042')
        self.assertEqual(len(self.engines), 1)
        old.cleanup_complete = True
        session.poll()
        self.assertEqual(len(self.engines), 2)

    def test_backoff_is_bounded_and_resets_after_stable_connection(self):
        session = self.session()
        delays = []
        for _ in range(8):
            session.engine.stop()
            session.poll()
            delay = session._retry_at - self.now
            delays.append(delay)
            self.advance(session, delay)
        self.assertEqual(delays, [1, 2, 4, 8, 15, 15, 15, 15])
        session.engine.connected = True
        session.poll()
        self.advance(session, 5)
        session.engine.stop()
        session.poll()
        self.assertEqual(session._retry_at - self.now, 1)

    def test_authentication_failure_does_not_loop_forever(self):
        session = self.session()
        session.engine.retryable = False
        session.engine.last_error = '配对密码不一致'
        session.engine.stop()
        session.poll()
        self.advance(session, 100)
        self.assertEqual(session.status, '配对密码不一致')
        self.assertFalse(session.running)
        self.assertIsNone(session._request)
        self.assertEqual(len(self.engines), 1)

    def test_user_can_disable_automatic_recovery(self):
        session = self.session(auto_reconnect=False)
        session.engine.stop()
        session.poll()
        self.advance(session, 100)
        self.assertFalse(session.running)
        self.assertEqual(len(self.engines), 1)

    def test_retry_start_failure_releases_failed_engine(self):
        session = self.session()
        session.engine.stop()
        session.poll()
        with patch.object(FakeEngine, 'start', side_effect=OSError('radio is still waking')):
            self.advance(session, 1)
        self.assertIsNone(session.engine)
        self.assertTrue(self.engines[-1].stop_event.is_set())
        self.assertTrue(session.reconnecting)
        self.advance(session, 2)
        self.assertIsNotNone(session.engine)

    def test_password_is_not_serialized_or_printed_by_request(self):
        session = self.session()
        self.assertNotIn('0042', repr(session._request))
        self.assertNotIn('password', session.settings.__dataclass_fields__)

    def test_old_worker_status_is_ignored_after_recovery(self):
        session = self.session()
        old = session.engine
        old.stop()
        session.poll()
        self.advance(session, 1)
        old.args[6]('old disconnect must not replace current state')
        session.poll()
        self.assertNotIn('must not', session.status)

    def test_lan_client_and_server_recover_same_endpoint(self):
        for role in ('controller', 'receiver'):
            session = self.session(transport='lan', role=role)
            old = session.engine
            old.stop()
            session.poll()
            self.advance(session, 1)
            self.assertEqual(session.engine.args[:6], old.args[:6])

    def test_speed_adjustment_survives_recovery(self):
        session = self.session()
        session.set_speed(2.5)
        session.engine.stop()
        session.poll()
        self.advance(session, 1)
        self.assertEqual(session.engine.kwargs['mouse_speed'], 2.5)

    def test_resume_refreshes_screen_geometry(self):
        session = self.session()
        session.engine.stop()
        session.poll()
        with patch('sharemouse.session.primary_screen', return_value=Screen(1280, 720)):
            self.advance(session, 1)
        self.assertEqual(session.engine.args[1], Screen(1280, 720))

    def test_changed_ble_address_requires_same_authenticated_device(self):
        session = self.session(WINDOWS)
        session.engine.peer_id = 'abcdef123456'
        session.engine.connected = True
        session.poll()
        session.engine.stop()
        session.poll()
        peer = BluetoothPeer('00:11:22:33:44:55', 'SM Monkey abcdef123456')
        session.peers = {peer.label: peer}
        self.advance(session, 1)
        self.assertEqual(session.engine.args[4], peer.host)
        self.assertEqual(session.engine.kwargs['expected_peer_id'], 'abcdef123456')


if __name__ == '__main__':
    unittest.main()
