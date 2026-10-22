from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sharemouse.bluetooth_transport import BluetoothPeer
from sharemouse.client import MAC, WINDOWS
from sharemouse.core import Screen
from sharemouse.session import Session, Settings


PASSWORD = 'test-password-12'
PEER = BluetoothPeer('AA:BB:CC:DD:EE:FF', 'SM Monkey')


class FakeBrowser:
    def __init__(self, update, error):
        self.update, self.error = update, error
        self.started = False
        self.stop_count = 0

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stop_count += 1


class BluetoothSessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions, self.ble_browsers, self.lan_browsers = [], [], []

        def browser_factory(collection):
            def create(update, error):
                browser = FakeBrowser(update, error)
                collection.append(browser)
                return browser
            return create

        def engine_factory(*args, **kwargs):
            engine = Mock()
            engine.connected = False
            engine.stop_event = threading.Event()
            engine.stop.side_effect = engine.stop_event.set
            return engine

        self.patches = [
            patch('sharemouse.bluetooth_windows.WindowsBleBrowser',
                  side_effect=browser_factory(self.ble_browsers)),
            patch('sharemouse.session.Browser', side_effect=browser_factory(self.lan_browsers)),
            patch('sharemouse.session.primary_screen', return_value=Screen(1920, 1080)),
            patch('sharemouse.session.Engine', side_effect=engine_factory),
        ]
        self.ble_factory, self.lan_factory, self.screen, self.engine_factory = [
            patcher.start() for patcher in self.patches]

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for patcher in reversed(self.patches):
            patcher.stop()

    def session(self, client=MAC, settings=None, discovery=False):
        session = Session(client, discovery=discovery,
                          settings=settings or Settings(transport='bluetooth'), persist=False)
        self.sessions.append(session)
        return session

    def choose(self, session):
        session.peers = {PEER.label: PEER}
        session.selected = PEER.label

    def test_settings_load_transport_whitelist_and_preserve_files_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            for value in ('lan', 'bluetooth', 'invalid', '', None, True, 123, ['bluetooth']):
                with self.subTest(value=value):
                    path.write_text(json.dumps({'transport': value, 'files': True}), encoding='utf-8')
                    settings = Settings.load(WINDOWS, path)
                    self.assertEqual(settings.transport, value if value in ('lan', 'bluetooth') else 'lan')
                    self.assertTrue(settings.files)
            settings = Settings(transport='bluetooth', files=True)
            settings.save(path)
            loaded = Settings.load(MAC, path)
            self.assertEqual(loaded.transport, 'bluetooth')
            self.assertTrue(loaded.files)

    def test_invalid_transport_configuration_is_atomic(self):
        session = self.session(settings=Settings(transport='lan', role='controller', side='right'))
        before, generation = asdict(session.settings), session.generation
        with self.assertRaisesRegex(ValueError, '连接方式'):
            session.configure(side='left', role='receiver', transport='not-a-transport')
        self.assertEqual(asdict(session.settings), before)
        self.assertEqual(session.generation, generation)
        with self.assertRaises(ValueError):
            session.configure(side='left', unknown_setting=True)
        self.assertEqual(asdict(session.settings), before)

    def test_mac_bluetooth_both_roles_ignore_lan_host_and_invalid_port(self):
        for role in ('controller', 'receiver'):
            with self.subTest(role=role):
                settings = Settings(transport='bluetooth', role=role, files=True,
                                    host='unused.example', port='not-a-number')
                session = self.session(settings=settings)
                session.connect(PASSWORD, window=123)
                call = self.engine_factory.call_args
                self.assertEqual(call.args[0], role == 'controller')
                self.assertEqual(call.args[4:6], ('', 0))
                self.assertFalse(call.args[8])
                self.assertEqual(call.kwargs['transport'], 'bluetooth')
                self.assertTrue(settings.files)
                session.engine.start.assert_called_once()
                self.assertIn('蓝牙等待', session.status)
        self.ble_factory.assert_not_called()
        self.lan_factory.assert_not_called()

    def test_windows_bluetooth_requires_selection_in_either_role(self):
        for role in ('controller', 'receiver'):
            with self.subTest(role=role):
                settings = Settings(transport='bluetooth', role=role, manual=True,
                                    host='10.0.0.8', files=True)
                session = self.session(WINDOWS, settings=settings)
                with self.assertRaisesRegex(ValueError, '选择发现的 Mac'):
                    session.connect(PASSWORD)
                self.assertIsNone(session.engine)
        self.engine_factory.assert_not_called()
        self.screen.assert_not_called()

    def test_windows_receiver_also_scans_and_connects_to_selected_mac(self):
        for role in ('controller', 'receiver'):
            with self.subTest(role=role):
                session = self.session(WINDOWS,
                                       Settings(transport='bluetooth', role=role, files=True, manual=True),
                                       discovery=True)
                browser = self.ble_browsers[-1]
                self.assertTrue(browser.started)
                browser.update([PEER])
                session.poll()
                self.assertEqual(session.selected, PEER.label)
                session.connect(PASSWORD)
                call = self.engine_factory.call_args
                self.assertEqual(call.args[0], role == 'controller')
                self.assertEqual(call.args[4:6], (PEER.host, 0))
                self.assertFalse(call.args[8])
                self.assertEqual(call.kwargs['transport'], 'bluetooth')
                self.assertTrue(session.settings.files)
                self.assertIn('正在连接 Mac 蓝牙', session.status)
        self.assertEqual(self.ble_factory.call_count, 2)
        self.lan_factory.assert_not_called()

    def test_connecting_blocks_mode_changes_and_duplicate_connect(self):
        session = self.session()
        session.connect(PASSWORD)
        self.assertFalse(session.connected)
        self.assertTrue(session.running)
        generation = session.generation
        with self.assertRaisesRegex(ValueError, '先断开'):
            session.configure(transport='lan', role='receiver')
        self.assertEqual(session.settings.transport, 'bluetooth')
        self.assertEqual(session.settings.role, 'controller')
        self.assertEqual(session.generation, generation)
        session.connect(PASSWORD)
        self.assertEqual(self.engine_factory.call_count, 1)
        session.disconnect()
        session.configure(transport='lan')
        self.assertEqual(session.settings.transport, 'lan')

    def test_mode_change_stops_scan_and_late_scan_callbacks_are_ignored(self):
        session = self.session(WINDOWS, Settings(transport='bluetooth', role='receiver'), discovery=True)
        browser = self.ble_browsers[-1]
        session.configure(transport='lan')
        self.assertEqual(browser.stop_count, 1)
        discovery_status = session.discovery_status
        browser.update([PEER])
        browser.error('old Bluetooth error')
        session.poll()
        self.assertFalse(session.peers)
        self.assertEqual(session.discovery_status, discovery_status)
        session.configure(transport='bluetooth')
        replacement = self.ble_browsers[-1]
        self.assertIsNot(browser, replacement)
        browser.update([BluetoothPeer('00:00:00:00:00:00', 'old peer')])
        replacement.update([PEER])
        session.poll()
        self.assertEqual(session.peers, {PEER.label: PEER})

    def test_close_stops_scan_and_connecting_engine_once_and_ignores_late_events(self):
        session = self.session(WINDOWS, Settings(transport='bluetooth', role='receiver'), discovery=True)
        browser = self.ble_browsers[-1]
        self.choose(session)
        session.connect(PASSWORD)
        engine = session.engine
        status_callback = self.engine_factory.call_args.args[6]
        session.close()
        session.close()
        self.assertEqual(browser.stop_count, 1)
        engine.stop.assert_called_once()
        self.assertTrue(engine.stop_event.is_set())
        old_status = session.status
        browser.update([PEER])
        status_callback('late paired event')
        session.poll()
        self.assertEqual(session.status, old_status)
        self.assertIsNone(session.engine)
        self.assertIsNone(session.browser)

    def test_disconnect_drops_late_engine_status_while_discovery_remains_available(self):
        session = self.session(WINDOWS, Settings(transport='bluetooth', role='receiver'), discovery=True)
        self.choose(session)
        session.connect(PASSWORD)
        status_callback = self.engine_factory.call_args.args[6]
        engine, browser = session.engine, session.browser
        session.disconnect()
        engine.stop.assert_called_once()
        status_callback('late old disconnect')
        browser.update([PEER])
        session.poll()
        self.assertIn('已断开', session.status)
        self.assertEqual(session.selected, PEER.label)
        self.assertEqual(browser.stop_count, 0)

    def test_bluetooth_files_rejected_then_lan_restores_saved_files_preference(self):
        settings = Settings(transport='bluetooth', role='receiver', files=True)
        session = self.session(settings=settings)
        session.connect(PASSWORD)
        self.assertFalse(self.engine_factory.call_args.args[8])
        session.engine.connected = True
        with self.assertRaisesRegex(ValueError, '切换为局域网'):
            session.send_file('example.txt')
        session.engine.send_file.assert_not_called()
        session.disconnect()
        self.assertTrue(settings.files)
        session.configure(transport='lan')
        session.connect(PASSWORD)
        self.assertEqual(self.engine_factory.call_args.args[4:6], ('0.0.0.0', 24809))
        self.assertTrue(self.engine_factory.call_args.args[8])
        self.assertNotIn('transport', self.engine_factory.call_args.kwargs)
        session.engine.connected = True
        session.send_file('example.txt')
        session.engine.send_file.assert_called_once_with('example.txt')

    def test_synchronous_engine_start_failure_stops_partial_engine(self):
        engine = Mock()
        engine.stop_event = threading.Event()
        engine.start.side_effect = RuntimeError('input permission missing')
        self.engine_factory.side_effect = None
        self.engine_factory.return_value = engine
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, 'permission missing'):
            session.connect(PASSWORD)
        engine.stop.assert_called_once()
        self.assertIsNone(session.engine)

    def test_invalid_password_never_constructs_engine_or_queries_screen(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.connect('123')
        self.engine_factory.assert_not_called()
        self.screen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
