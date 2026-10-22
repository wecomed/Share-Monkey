import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sharemouse.client import MAC, WINDOWS
from sharemouse.core import Screen
from sharemouse.discovery import Peer
from sharemouse.session import Session, Settings


class SettingsTests(unittest.TestCase):
    def test_platform_defaults_and_corrupt_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            self.assertEqual(Settings.load(MAC, path).speed, 1.5)
            self.assertEqual(Settings.load(WINDOWS, path).role, 'receiver')
            path.write_text('{broken')
            self.assertEqual(Settings.load(WINDOWS, path).speed, 1.)
            path.write_text('[]')
            self.assertEqual(Settings.load(WINDOWS, path).role, 'receiver')

    def test_saved_settings_do_not_include_password_and_validate_untrusted_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            Settings(role='receiver', speed=2.25, clipboard=False).save(path)
            data = json.loads(path.read_text())
            self.assertNotIn('password', data)
            self.assertEqual(Settings.load(MAC, path).speed, 2.25)
            path.write_text(json.dumps(dict(data, speed=float('nan'), role='invalid', port=2, files='yes')))
            loaded = Settings.load(MAC, path)
            self.assertEqual((loaded.role, loaded.speed, loaded.port, loaded.files), ('controller', 1.5, 24809, False))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.changed = Mock()
        self.session = Session(MAC, self.changed, discovery=False, settings=Settings(), persist=False)

    def tearDown(self):
        self.session.close()

    def fake_engine(self):
        engine = Mock()
        engine.stop_event = threading.Event()
        engine.connected = True
        return engine

    def test_pairing_validation_does_not_install_hooks(self):
        with patch('sharemouse.session.Engine') as engine:
            for password in ('short', 'long-enough-password'):
                with self.assertRaises(ValueError):
                    self.session.connect(password)
            engine.assert_not_called()

    def test_connect_uses_discovered_peer_and_existing_input_protocol(self):
        engine = self.fake_engine()
        peer = Peer('10.0.0.8', 24809, '工作电脑', 'Windows')
        self.session.peers = {peer.label: peer}
        self.session.selected = peer.label
        with patch('sharemouse.session.primary_screen', return_value=Screen(1920, 1080)), \
             patch('sharemouse.session.Engine', return_value=engine) as factory:
            self.session.connect('test-password-12')
            self.assertEqual(factory.call_args.args[4:6], ('10.0.0.8', 24809))
            self.assertEqual(factory.call_args.kwargs['mouse_speed'], 1.5)
            engine.start.assert_called_once()
            self.session.connect('test-password-12')
            engine.start.assert_called_once()

    def test_old_discovery_and_engine_events_cannot_overwrite_new_state(self):
        self.session.events.put(('discovery', self.session.generation - 1, 'old error'))
        self.session.events.put(('status', self.session.engine_generation - 1, 'old disconnect'))
        self.session.poll()
        self.assertNotEqual(self.session.status, 'old disconnect')
        self.assertNotEqual(self.session.discovery_status, 'old error')

    def test_disconnect_ignores_late_messages_and_reconnect_can_report_status(self):
        self.session.engine = self.fake_engine()
        previous = self.session.engine_generation
        self.session.disconnect()
        self.session.events.put(('status', previous, 'old disconnected'))
        self.session.poll()
        self.assertIn('已断开', self.session.status)
        self.session.events.put(('status', self.session.engine_generation, 'new paired'))
        self.session.poll()
        self.assertEqual(self.session.status, 'new paired')

    def test_only_speed_changes_live_and_is_validated(self):
        engine = self.fake_engine()
        self.session.engine = engine
        with self.assertRaises(ValueError):
            self.session.configure(side='left')
        self.session.set_speed(2.5)
        engine.set_mouse_speed.assert_called_once_with(2.5)
        for invalid in (float('nan'), .1, 5):
            with self.assertRaises(ValueError):
                self.session.set_speed(invalid)

    def test_receiving_permission_does_not_prevent_sending_to_a_willing_peer(self):
        self.session.engine = self.fake_engine()
        self.assertFalse(self.session.settings.files)
        self.session.send_file('example.txt')
        self.session.engine.send_file.assert_called_once_with('example.txt')

    def test_explicit_quit_stops_services_once(self):
        engine, browser = self.fake_engine(), Mock()
        self.session.engine, self.session.browser = engine, browser
        with patch.object(self.session.settings, 'save') as save:
            self.session.close()
            self.session.close()
            engine.stop.assert_called_once()
            browser.stop.assert_called_once()
            save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
