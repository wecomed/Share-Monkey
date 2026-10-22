import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from sharemouse.bluetooth_priority import WindowsConnectionPriority
from sharemouse.core import Screen
from sharemouse.engine import Engine


class Metrics:
    def __init__(self):
        self.values = {}

    def gauge(self, name, value):
        self.values[name] = value


class Request:
    status = 1

    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1


class Device:
    def __init__(self):
        self.requests = []
        self.parameters = SimpleNamespace(connection_interval=12, connection_latency=0, link_timeout=200)
        self.handler = None
        self.removed = []
        self.operation_threads = []

    def add_connection_parameters_changed(self, handler):
        self.handler = handler
        return 0  # A zero event token is still a valid token.

    def remove_connection_parameters_changed(self, token):
        self.removed.append(token)

    def request_preferred_connection_parameters(self, parameters):
        self.operation_threads.append(threading.get_ident())
        request = Request()
        self.requests.append((parameters, request))
        return request

    def get_connection_parameters(self):
        self.operation_threads.append(threading.get_ident())
        return self.parameters


class PriorityLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device, self.metrics = Device(), Metrics()
        self.priority = WindowsConnectionPriority(self.device, 'native-throughput-profile', self.metrics)
        self.priority.IDLE_GRACE_SECONDS = .03

    async def asyncTearDown(self):
        self.priority.close()

    async def test_rapid_returns_reuse_lease_then_idle_releases_and_reentry_renews(self):
        for _ in range(20):
            self.priority.set_active(True)
            self.priority.set_active(False)
        self.assertEqual(len(self.device.requests), 1)
        self.assertEqual(self.device.requests[0][0], 'native-throughput-profile')
        first = self.device.requests[0][1]
        self.assertEqual(first.closes, 0)
        await asyncio.sleep(.06)
        self.assertEqual(first.closes, 1)
        self.assertEqual(self.metrics.values['ble_priority_held'], 0)
        self.priority.set_active(True)
        self.assertEqual(len(self.device.requests), 2)
        self.priority.close()
        self.priority.close()
        self.assertEqual(self.device.requests[1][1].closes, 1)
        self.assertEqual(self.device.removed, [0])

    async def test_active_request_does_not_expire_while_in_use(self):
        self.priority.set_active(True)
        self.priority.set_active(False)
        self.priority.set_active(True)
        await asyncio.sleep(.06)
        self.assertEqual(self.device.requests[0][1].closes, 0)
        self.assertEqual(self.metrics.values['ble_priority_held'], 1)

    async def test_negotiated_values_use_bluetooth_units_and_cross_thread_events_are_marshaled(self):
        self.priority.set_active(True)
        self.assertEqual(self.metrics.values['ble_connection_interval_ms'], 15)
        self.assertEqual(self.metrics.values['ble_link_timeout_ms'], 2000)
        self.device.parameters.connection_interval = 96
        thread = threading.Thread(target=lambda: self.device.handler(self.device, None))
        thread.start()
        thread.join(1)
        await asyncio.sleep(0)
        self.assertEqual(self.metrics.values['ble_connection_interval_ms'], 120)
        self.assertEqual(set(self.device.operation_threads), {threading.get_ident()})

    async def test_close_cancels_timers_and_late_events_cannot_reacquire(self):
        self.priority.set_active(True)
        self.priority.set_active(False)
        self.device.handler(self.device, None)
        self.priority.close()
        self.priority.set_active(True)
        self.device.handler(self.device, None)
        await asyncio.sleep(.06)
        self.assertEqual(len(self.device.requests), 1)
        self.assertEqual(self.device.requests[0][1].closes, 1)
        self.assertIsNone(self.priority.request)
        self.assertIsNone(self.priority.idle)

    async def test_unsupported_os_and_driver_failure_leave_connection_usable(self):
        with patch.object(self.device, 'request_preferred_connection_parameters', side_effect=OSError('unsupported')):
            self.priority.set_active(True)
        self.assertIsNone(self.priority.request)
        self.assertEqual(self.metrics.values['ble_priority_supported'], 0)
        self.priority.set_active(False)
        self.priority.close()

    async def test_zero_unnegotiated_interval_is_not_reported_as_zero_latency(self):
        self.device.parameters.connection_interval = 0
        self.priority.sample()
        self.assertEqual(self.metrics.values['ble_connection_parameters_available'], 0)
        self.assertNotIn('ble_connection_interval_ms', self.metrics.values)

    async def test_native_teardown_failure_still_drops_references(self):
        self.priority.set_active(True)
        request = self.priority.request
        with patch.object(request, 'close', side_effect=OSError('radio reset')), patch.object(
                self.device, 'remove_connection_parameters_changed', side_effect=OSError('device gone')):
            self.priority.close()
        self.assertIsNone(self.priority.request)
        self.assertIsNone(self.priority.token)
        self.assertTrue(self.priority.closed)


class EnginePriorityTests(unittest.TestCase):
    def engine(self, controller):
        engine = Engine(controller, Screen(1920, 1080), 'right', 'priority-test-password',
                        '', 0, Mock(), clipboard=False, transport='bluetooth')
        engine.connected = True
        engine.input = Mock()
        engine.input.listeners = []
        engine.transport_backend = Mock()
        return engine

    def test_windows_receiver_requests_before_ack_and_returns_release_priority(self):
        engine = self.engine(False)
        engine.receive(dict(type='enter', side='right', fraction=.5, epoch=1, _delivery=3))
        self.assertEqual(engine.transport_backend.set_input_active.call_args_list, [call(False), call(True)])
        self.assertEqual(engine.outgoing.get_nowait(), dict(type='consumed', packet=3))
        engine.receive(dict(type='leave', epoch=1))
        self.assertEqual(engine.transport_backend.set_input_active.call_args.args, (False,))
        self.assertEqual(engine.outgoing.get_nowait(), dict(type='left', epoch=1))

    def test_windows_source_requests_on_enter_and_releases_on_f12_return(self):
        engine = self.engine(True)
        engine.remote, engine.epoch = True, 1
        engine.send(dict(type='enter', side='right', fraction=.5))
        engine.transport_backend.set_input_active.assert_called_once_with(True)
        engine.return_local()
        self.assertEqual(engine.transport_backend.set_input_active.call_args.args, (False,))

    def test_mac_and_old_test_backends_without_optional_api_are_compatible(self):
        engine = self.engine(True)
        engine.transport_backend = SimpleNamespace()
        engine.remote = True
        self.assertTrue(engine.send(dict(type='enter', side='right', fraction=.5)))
        engine.return_local()


class NativeBindingContractTests(unittest.TestCase):
    def test_bundled_winrt_and_pinned_bleak_expose_bridge_and_native_methods(self):
        root = Path(__file__).resolve().parents[1] / 'build/windows-extracted-v053/pkgs'
        if not root.exists():
            self.skipTest('Release WinRT metadata not present')
        import ast
        tree = ast.parse((root / 'winrt/_winrt_windows_devices_bluetooth.pyi').read_text())
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        properties = {node.name for node in classes['BluetoothLEPreferredConnectionParameters_Static'].body
                      if isinstance(node, ast.FunctionDef)}
        methods = {node.name for node in classes['BluetoothLEDevice'].body if isinstance(node, ast.FunctionDef)}
        self.assertIn('throughput_optimized', properties)
        self.assertTrue({'request_preferred_connection_parameters', 'get_connection_parameters',
                         'add_connection_parameters_changed', 'remove_connection_parameters_changed'} <= methods)
        source = (root / 'bleak/backends/winrt/client.py').read_text()
        self.assertIn('self._requester', source)
        self.assertIn('BluetoothLEDevice', source)


if __name__ == '__main__':
    unittest.main()
