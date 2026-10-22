import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sharemouse import bluetooth_windows as backend
from sharemouse.bluetooth_transport import (INFO_UUID, RX_UUID, SERVICE_UUID,
                                            TRANSPORT_PROTOCOL, TX_UUID)


DEVICE = SimpleNamespace(address='AA:BB:CC:DD:EE:FF', name='Test Mac')
ADVERTISEMENT = SimpleNamespace(local_name='Share Monkey Mac', service_uuids=[SERVICE_UUID])


class FakeScanner:
    instances = []
    found = DEVICE

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stopped = False
        self.instances.append(self)

    async def __aenter__(self):
        self.kwargs['detection_callback'](DEVICE, ADVERTISEMENT)
        return self

    async def __aexit__(self, *args):
        self.stopped = True

    @classmethod
    async def find_device_by_address(cls, address, **kwargs):
        return cls.found


class FakeClient:
    instances = []
    info = {'transport_protocol': TRANSPORT_PROTOCOL, 'name': 'Test Mac'}
    connect_delay = 0
    write_delay = 0
    missing_rx = False
    mtu_after_first_write = None

    def __init__(self, device, **kwargs):
        self.device, self.kwargs = device, kwargs
        self.is_connected = False
        self.mtu_size = 23
        self.writes = []
        self.notify = None
        self.services = self
        self.disconnect_count = 0
        self.instances.append(self)

    async def connect(self):
        await asyncio.sleep(self.connect_delay)
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_count += 1
        self.is_connected = False
        self.kwargs['disconnected_callback'](self)

    async def read_gatt_char(self, uuid):
        assert uuid == INFO_UUID
        return json.dumps(self.info).encode()

    def get_characteristic(self, uuid):
        if uuid == RX_UUID:
            return None if self.missing_rx else SimpleNamespace(uuid=uuid, properties=['write'])
        if uuid == TX_UUID:
            return SimpleNamespace(uuid=uuid, properties=['notify'])
        return None

    async def start_notify(self, characteristic, callback):
        assert characteristic.uuid == TX_UUID
        self.notify = callback

    async def write_gatt_char(self, characteristic, data, response):
        assert characteristic.uuid == RX_UUID
        assert response is True
        await asyncio.sleep(self.write_delay)
        self.writes.append(bytes(data))
        if len(self.writes) == 1 and self.mtu_after_first_write is not None:
            self.mtu_size = self.mtu_after_first_write
        # Simulate a peripheral echoing the ordered byte stream via notifications.
        self.notify(SimpleNamespace(uuid=TX_UUID), bytearray(data))


class WindowsBluetoothTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.info = {'transport_protocol': TRANSPORT_PROTOCOL, 'name': 'Test Mac'}
        FakeClient.connect_delay = FakeClient.write_delay = 0
        FakeClient.missing_rx = False
        FakeClient.mtu_after_first_write = None
        FakeScanner.instances = []
        FakeScanner.found = DEVICE
        with backend._devices_lock:
            backend._devices.clear()
        self.patch = patch.object(backend, '_bleak', return_value=(FakeClient, FakeScanner))
        self.loader = self.patch.start()
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.stop()
            if worker.thread:
                worker.thread.join(2)
                self.assertFalse(worker.thread.is_alive(), 'Bluetooth worker leaked')
        self.patch.stop()

    def connector(self):
        connector = backend.WindowsBleConnector(DEVICE.address)
        self.workers.append(connector)
        return connector

    def test_construction_does_not_import_or_access_bluetooth(self):
        backend.WindowsBleBrowser(lambda peers: None)
        backend.WindowsBleConnector(DEVICE.address)
        self.loader.assert_not_called()

    def test_scanner_filters_own_service_and_stop_closes_scanner(self):
        found, updates, errors = threading.Event(), [], []

        def update(peers):
            updates.append(peers)
            found.set()

        browser = backend.WindowsBleBrowser(update, errors.append).start()
        self.workers.append(browser)
        self.assertTrue(found.wait(2), errors)
        self.assertEqual(updates[0][0].host, DEVICE.address)
        self.assertEqual(updates[0][0].port, 0)
        self.assertEqual(updates[0][0].platform, 'Mac')
        self.assertEqual(FakeScanner.instances[0].kwargs['service_uuids'], [SERVICE_UUID])
        self.assertIs(backend._known_device(DEVICE.address.lower()), DEVICE)
        browser.stop()
        browser.thread.join(2)
        self.assertTrue(FakeScanner.instances[0].stopped)
        self.assertFalse(errors)

    def test_unrelated_advertisements_ignored_and_old_peers_expire(self):
        updates = []
        browser = backend.WindowsBleBrowser(updates.append)
        browser._advertised(DEVICE, SimpleNamespace(service_uuids=['unrelated']))
        self.assertFalse(updates)
        browser._advertised(DEVICE, ADVERTISEMENT)
        browser._expire(time.monotonic() + backend.PEER_TTL + 1)
        self.assertEqual(updates[-1], [])

    def test_stream_splits_at_mtu_and_preserves_bytes(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        data = bytes(range(256)) * 8
        stream.sendall(data)
        chunks = FakeClient.instances[0].writes
        self.assertEqual(b''.join(chunks), data)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        received = bytearray()
        while len(received) < len(data):
            received.extend(stream.recv(17))
        self.assertEqual(received, data)
        self.assertEqual(connector.info['transport_protocol'], TRANSPORT_PROTOCOL)
        self.assertFalse(FakeClient.instances[0].kwargs['winrt']['use_cached_services'])

    def test_concurrent_sends_do_not_interleave_packets(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        FakeClient.write_delay = .001
        first, second = b'A' * 101, b'B' * 101
        threads = [threading.Thread(target=stream.sendall, args=(data,)) for data in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        written = b''.join(FakeClient.instances[0].writes)
        self.assertIn(written, (first + second, second + first))

    def test_late_mtu_negotiation_reduces_writes_without_losing_bytes(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        client = FakeClient.instances[0]
        self.assertEqual(connector._write_size, 20)
        client.mtu_size = 247  # Negotiation completes after notifications started.
        data = bytes(range(256)) * 8
        stream.sendall(data)
        self.assertEqual(b''.join(client.writes), data)
        self.assertEqual(len(client.writes), 9)  # Frozen MTU23 required 103 writes.
        self.assertTrue(all(len(chunk) <= 244 for chunk in client.writes))
        self.assertEqual(connector._write_size, 244)

    def test_native_priority_uses_existing_device_on_mta_loop_and_closes_before_disconnect(self):
        from tests.test_bluetooth_priority import Device, Metrics
        device, metrics = Device(), Metrics()
        with patch.object(FakeClient, '_backend', SimpleNamespace(_requester=device), create=True), \
                patch.object(backend, '_preferred_parameters', return_value='native-fast-profile'):
            connector = backend.WindowsBleConnector(DEVICE.address, metrics=metrics)
            self.workers.append(connector)
            stream = connector.connect(threading.Event(), timeout=2)
            self.assertFalse(stream.closed)
            self.assertEqual(len(device.requests), 1)
            self.assertEqual(device.requests[0][0], 'native-fast-profile')
            self.assertEqual(set(device.operation_threads), {connector.thread.ident})
            connector.set_input_active(True)
            stream.sendall(b'peer-processed-ack')
            self.assertEqual(metrics.values['ble_write_value_bytes'], 20)
            connector.close()
            connector.thread.join(2)
            self.assertFalse(connector.thread.is_alive())
            self.assertEqual(device.requests[0][1].closes, 1)
            self.assertEqual(device.removed, [0])
            self.assertIsNone(connector._priority)
            self.assertEqual(FakeClient.instances[-1].disconnect_count, 1)

    def test_mtu_changes_during_one_send_apply_between_acknowledged_writes(self):
        for initial, later in ((23, 247), (247, 23), (1027, 23)):
            with self.subTest(initial=initial, later=later):
                connector = self.connector()
                stream = connector.connect(threading.Event(), timeout=2)
                client = FakeClient.instances[-1]
                client.mtu_size = initial
                client.mtu_after_first_write = later
                data = bytes(range(256)) * 4
                stream.sendall(data)
                self.assertEqual(b''.join(client.writes), data)
                self.assertEqual(len(client.writes[0]), min(initial - 3, 512))
                self.assertTrue(all(len(chunk) <= min(later - 3, 512)
                                    for chunk in client.writes[1:]))

    def test_peripheral_disconnect_wakes_blocked_reader(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        stream.settimeout(None)
        errors, ended = [], threading.Event()

        def receive():
            try:
                stream.recv(1)
            except ConnectionError as exc:
                errors.append(str(exc))
            finally:
                ended.set()

        reader = threading.Thread(target=receive)
        reader.start()
        client = FakeClient.instances[0]
        connector.loop.call_soon_threadsafe(client.kwargs['disconnected_callback'], client)
        self.assertTrue(ended.wait(2))
        reader.join(2)
        self.assertTrue(errors)
        self.assertTrue(stream.closed)

    def test_stream_close_disconnects_and_stops_worker(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        stream.close()
        connector.thread.join(2)
        self.assertFalse(connector.thread.is_alive())
        self.assertEqual(FakeClient.instances[0].disconnect_count, 1)

    def test_bad_protocol_and_missing_characteristic_fail_cleanly(self):
        for info, missing in (({'transport_protocol': 1}, False),
                              ({'transport_protocol': 999}, False),
                              ({'transport_protocol': True}, False),
                              ({'transport_protocol': TRANSPORT_PROTOCOL}, True)):
            with self.subTest(info=info, missing=missing):
                FakeClient.info, FakeClient.missing_rx = info, missing
                connector = self.connector()
                with self.assertRaises(ConnectionError):
                    connector.connect(threading.Event(), timeout=2)
                connector.thread.join(2)
                self.assertFalse(connector.thread.is_alive())
                self.assertEqual(FakeClient.instances[-1].disconnect_count, 1)

    def test_old_bluetooth_protocol_explains_both_clients_need_upgrading(self):
        FakeClient.info = {'transport_protocol': 1}
        with self.assertRaisesRegex(ConnectionError, 'Mac 和 Windows.*v0.5.2'):
            self.connector().connect(threading.Event(), timeout=2)

    def test_connect_can_be_cancelled_before_native_connection_finishes(self):
        FakeClient.connect_delay = 10
        connector = self.connector()
        stop = threading.Event()
        errors, finished = [], threading.Event()

        def connect():
            try:
                connector.connect(stop, timeout=15)
            except ConnectionAbortedError as exc:
                errors.append(exc)
            finally:
                finished.set()

        caller = threading.Thread(target=connect)
        caller.start()
        time.sleep(.03)
        stop.set()
        self.assertTrue(finished.wait(1))
        caller.join(1)
        self.assertTrue(errors)
        connector.thread.join(1)
        self.assertFalse(connector.thread.is_alive())

    def test_write_timeout_closes_stream_instead_of_dropping_bytes(self):
        connector = self.connector()
        stream = connector.connect(threading.Event(), timeout=2)
        FakeClient.write_delay = 10
        stream.settimeout(.03)
        with self.assertRaises(socket.timeout):
            stream.sendall(b'important encrypted data')
        self.assertTrue(stream.closed)
        connector.thread.join(2)
        self.assertFalse(connector.thread.is_alive())

    def test_missing_device_gives_setup_instruction(self):
        FakeScanner.found = None
        with self.assertRaisesRegex(ConnectionError, '开启蓝牙等待'):
            self.connector().connect(threading.Event(), timeout=2)


if __name__ == '__main__':
    unittest.main()
