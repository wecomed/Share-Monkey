"""Real Session/Engine/BLE encryption; only radio and OS input are replaced."""
from contextlib import ExitStack
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sharemouse.bluetooth_transport import BluetoothPeer
from sharemouse.client import MAC, WINDOWS
from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.session import Session, Settings
from test_bluetooth_integration import FragmentedWire, RecordingInput, ThreadPlatform


class Radio:
    def __init__(self):
        self.condition = threading.Condition()
        self.wires = []

    def server(self, *args, **kwargs):
        with self.condition:
            wire = FragmentedWire(20)
            wire.unavailable = False
            wire.claimed = False
            self.wires.append(wire)
            self.condition.notify_all()
        radio = self
        class Server:
            cleanup_pending = False
            def start(self):
                pass
            def accept(self, stop_event):
                return wire.streams[0]
            def close(self):
                radio.fail(wire)
        return Server()

    def connector(self, *args, **kwargs):
        radio = self
        class Connector:
            thread = None
            cleanup_pending = False
            wire = None
            def connect(self, stop_event, timeout):
                deadline = time.monotonic() + timeout
                with radio.condition:
                    while not stop_event.is_set():
                        for wire in reversed(radio.wires):
                            if not wire.unavailable and not wire.claimed:
                                wire.claimed = True
                                self.wire = wire
                                return wire.streams[1]
                        if time.monotonic() > deadline:
                            raise TimeoutError('test radio not ready')
                        radio.condition.wait(.01)
                raise ConnectionAbortedError('test cancelled')
            def close(self):
                if self.wire is not None:
                    radio.fail(self.wire)
        return Connector()

    def fail(self, wire):
        with self.condition:
            wire.unavailable = True
            wire.fail()
            self.condition.notify_all()


class ReconnectIntegrationTests(unittest.TestCase):
    def test_repeated_radio_loss_recovers_both_directions_with_fresh_encryption(self):
        for mac_controller in (True, False):
            with self.subTest(mac_controller=mac_controller), ExitStack() as stack:
                radio, platform = Radio(), ThreadPlatform()
                clock = [0.]
                engines = []
                def engine_factory(*args, **kwargs):
                    engine = Engine(*args, **kwargs)
                    native_platform = 'darwin' if args[4] == '' else 'win32'
                    original_run = engine.run
                    def run():
                        platform.local.platform = native_platform
                        original_run()
                    engine.run = run
                    engines.append(engine)
                    return engine
                def input_factory(engine):
                    adapter = RecordingInput()
                    adapter.start = lambda: None
                    return adapter
                telemetry = Mock()
                telemetry.start.return_value = telemetry
                stack.enter_context(patch('sharemouse.session.Engine', side_effect=engine_factory))
                stack.enter_context(patch('sharemouse.session.primary_screen', return_value=Screen(1920, 1080)))
                stack.enter_context(patch('sharemouse.engine.sys', platform))
                stack.enter_context(patch('sharemouse.input.Input', side_effect=input_factory))
                stack.enter_context(patch('sharemouse.engine.TelemetrySession', return_value=telemetry))
                stack.enter_context(patch('sharemouse.bluetooth_mac.MacBleServer', side_effect=radio.server))
                stack.enter_context(patch('sharemouse.bluetooth_windows.WindowsBleConnector', side_effect=radio.connector))
                sessions = [Session(client, discovery=False, persist=False,
                                    settings=Settings(transport='bluetooth', clipboard=False,
                                                      role='controller' if controller else 'receiver'),
                                    clock=lambda: clock[0])
                            for client, controller in ((MAC, mac_controller), (WINDOWS, not mac_controller))]
                peer = BluetoothPeer('AA:BB:CC:DD:EE:FF', 'Test Mac')
                sessions[1].peers = {peer.label: peer}
                sessions[1].selected = peer.label
                def wait_for(predicate):
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        for session in sessions:
                            session.poll()
                        if predicate():
                            return
                        time.sleep(.002)
                    self.fail('Recovery timed out: ' + repr([s.status for s in sessions]))
                try:
                    for session in sessions:
                        session.connect('0042')
                    nonces = set()
                    for cycle in range(10):
                        wait_for(lambda: all(s.connected for s in sessions))
                        controller = sessions[0 if mac_controller else 1].engine
                        receiver = sessions[1 if mac_controller else 0].engine
                        self.assertFalse(controller.remote)
                        self.assertFalse(receiver.active)
                        nonce = radio.wires[-1].sent[0][0]
                        self.assertNotIn(nonce, nonces)
                        nonces.add(nonce)
                        with controller.state_lock:
                            controller.remote = True
                            controller.epoch = 1
                            controller.send(dict(type='enter', side='right', fraction=.5))
                            controller.send(dict(type='key', key={'char': 'a'}, down=True))
                        wait_for(lambda: bool(receiver.input.keys))
                        radio.fail(radio.wires[-1])
                        wait_for(lambda: all(not s.connected and s.reconnecting for s in sessions))
                        self.assertFalse(receiver.input.keys, 'A held remote key survived disconnect')
                        self.assertFalse(controller.remote, 'Source input remained captured')
                        if cycle < 9:
                            clock[0] += 20
                    for session in sessions:
                        session.disconnect()
                    count = len(engines)
                    clock[0] += 100
                    for session in sessions:
                        session.poll()
                    self.assertEqual(len(engines), count)
                    wait_for(lambda: all(engine.quiescent for engine in engines))
                finally:
                    for session in sessions:
                        session.close()
                    for wire in radio.wires:
                        radio.fail(wire)
                    end = time.monotonic() + 3
                    while time.monotonic() < end and any(not e.quiescent for e in engines):
                        time.sleep(.005)
                    self.assertTrue(all(e.quiescent for e in engines), 'A connection worker leaked')


if __name__ == '__main__':
    unittest.main()
