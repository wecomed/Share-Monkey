"""Native delegate semantics without starting a radio or permission prompt."""
import json
import queue
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sharemouse.bluetooth_mac import MacBleServer
from sharemouse.bluetooth_transport import RX_UUID, TX_UUID, TRANSPORT_PROTOCOL


class UUID:
    def __init__(self, value):
        self.value = value

    def UUIDString(self):
        return self.value


class Characteristic:
    def __init__(self, value):
        self.uuid = UUID(value)

    def UUID(self):
        return self.uuid


class Central:
    def __init__(self, name='Windows A', mtu=3):
        self.name, self.mtu = name, mtu

    def identifier(self):
        return UUID(self.name)

    def maximumUpdateValueLength(self):
        return self.mtu


class Request:
    def __init__(self, central, data, uuid=RX_UUID, offset=0):
        self._central, self._data, self._uuid, self._offset = central, data, uuid, offset

    def central(self):
        return self._central

    def value(self):
        return self._data

    def characteristic(self):
        return Characteristic(self._uuid)

    def offset(self):
        return self._offset


class Manager:
    def __init__(self):
        self.quota = 1000
        self.sent = []
        self.attempts = []
        self.responses = []
        self.targets = []
        self.stopped = False
        self.removed = False
        self.delegate = object()
        self._state = 5

    def updateValue_forCharacteristic_onSubscribedCentrals_(self, value, characteristic, targets):
        self.attempts.append(bytes(value))
        if self.quota == 0:
            return False
        self.quota -= 1
        self.sent.append(bytes(value))
        self.targets.append(targets)
        return True

    def stopAdvertising(self):
        self.stopped = True

    def removeAllServices(self):
        self.removed = True

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def respondToRequest_withResult_(self, request, result):
        self.responses.append((request, result))

    def state(self):
        return self._state


CB = SimpleNamespace(
    CBManagerStatePoweredOn=5, CBManagerStateUnauthorized=3,
    CBManagerStateUnsupported=2, CBManagerStatePoweredOff=4,
    CBATTErrorSuccess=0, CBATTErrorInsufficientAuthorization=8,
    CBATTErrorWriteNotPermitted=3, CBATTErrorInvalidOffset=7,
    CBATTErrorInsufficientResources=17, CBATTErrorReadNotPermitted=2,
)
F = SimpleNamespace(NSData=SimpleNamespace(dataWithBytes_length_=lambda data, length: bytes(data[:length])))


class MacBluetoothTests(unittest.TestCase):
    def server(self, central=None):
        server = MacBleServer()
        server._native = (CB, F, None)
        server._started = True
        server._manager = Manager()
        server._tx = Characteristic(TX_UUID)
        # Real adapters schedule on NSOperationQueue; a queue lets tests decide
        # precisely when Apple's main-queue callback runs, without native UI.
        pending = queue.Queue()
        server._dispatch = pending.put
        if central is not False:
            server._subscribed(server._manager, central or Central(), server._tx)
        return server, pending

    def test_import_does_not_load_framework_or_start_bluetooth(self):
        subprocess.run([sys.executable, '-c',
                        'import sys; import sharemouse.bluetooth_mac; '
                        'assert "CoreBluetooth" not in sys.modules'], check=True)

    def test_explicit_start_reports_missing_component(self):
        server = MacBleServer()
        with patch('sharemouse.bluetooth_mac._load_native', side_effect=OSError('missing')):
            with self.assertRaisesRegex(OSError, 'missing'):
                server.start()
        self.assertFalse(server._started)

    def test_missing_bundle_privacy_declaration_never_creates_manager(self):
        server = MacBleServer()
        created = []
        bundle = SimpleNamespace(objectForInfoDictionaryKey_=lambda key: None)
        foundation = SimpleNamespace(NSBundle=SimpleNamespace(mainBundle=lambda: bundle))
        framework = SimpleNamespace(CBPeripheralManager=SimpleNamespace(alloc=lambda: created.append(True)))
        server._native = (framework, foundation, None)
        server._dispatch = lambda callback: callback()
        server._start_native()
        self.assertEqual(created, [])
        self.assertTrue(server._closed)
        with self.assertRaisesRegex(OSError, 'NSBluetoothAlwaysUsageDescription'):
            server.accept(threading.Event())

    def test_info_contains_role_and_protocol(self):
        info = json.loads(MacBleServer(role='receiver')._info_bytes)
        self.assertEqual(info['role'], 'receiver')
        self.assertEqual(info['platform'], 'Mac')
        self.assertEqual(info['transport_protocol'], TRANSPORT_PROTOCOL)
        self.assertTrue(info['name'])
        with self.assertRaises(ValueError):
            MacBleServer(role='invalid')

    def test_accept_wait_is_cancelled_and_teardown_is_scheduled(self):
        server, pending = self.server(False)
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(OSError, '取消'):
            server.accept(stop)
        self.assertTrue(server._closed)
        pending.get(timeout=1)()
        self.assertIsNone(server._manager)

    def test_accept_timeout_and_second_accept_are_explicit(self):
        server, _ = self.server(False)
        with self.assertRaises(TimeoutError):
            server.accept(threading.Event(), .005)
        server, _ = self.server()
        stream = server.accept(threading.Event(), 1)
        self.assertIs(stream, server._stream)
        with self.assertRaisesRegex(OSError, '一个连接'):
            server.accept(threading.Event(), 1)

    def test_backpressure_retries_same_chunk_and_preserves_all_bytes(self):
        central = Central(mtu=3)
        server, pending = self.server(central)
        manager = server._manager
        manager.quota = 1
        errors = []

        def send():
            try:
                server._stream.sendall(b'abcdefghij')
            except Exception as exc:
                errors.append(exc)

        sender = threading.Thread(target=send)
        sender.start()
        pending.get(timeout=1)()
        self.assertEqual(manager.sent, [b'abc'])
        self.assertTrue(sender.is_alive())
        self.assertEqual(server._queued_bytes, 7)
        manager.quota = 10
        server._drain()  # peripheralManagerIsReadyToUpdateSubscribers callback.
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(manager.attempts, [b'abc', b'def', b'def', b'ghi', b'j'])
        self.assertEqual(b''.join(manager.sent), b'abcdefghij')
        self.assertTrue(all(target == [central] for target in manager.targets))
        self.assertEqual(server._queued_bytes, 0)

    def test_late_mtu_negotiation_reduces_notifications_with_one_dispatch(self):
        central = Central(mtu=20)
        server, pending = self.server(central)
        central.mtu = 244  # Negotiated after the original subscription callback.
        data = bytes(range(256)) * 8
        errors = []

        def send():
            try:
                server._stream.sendall(data)
            except Exception as exc:
                errors.append(exc)

        sender = threading.Thread(target=send)
        sender.start()
        pending.get(timeout=1)()
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertFalse(errors)
        self.assertEqual(b''.join(server._manager.sent), data)
        self.assertEqual(len(server._manager.sent), 9)  # Was 103 at frozen 20 bytes.
        self.assertEqual(server._mtu, 244)
        self.assertEqual(pending.qsize(), 0)  # One main-queue dispatch, not per fragment.

    def test_mtu_changes_during_backpressure_preserve_exact_stream(self):
        for initial, later in ((20, 244), (244, 20), (1024, 20)):
            with self.subTest(initial=initial, later=later):
                central = Central(mtu=initial)
                server, pending = self.server(central)
                manager = server._manager
                manager.quota = 1
                data = bytes(range(256)) * 4
                errors = []

                def send():
                    try:
                        server._stream.sendall(data)
                    except Exception as exc:
                        errors.append(exc)

                sender = threading.Thread(target=send)
                sender.start()
                pending.get(timeout=1)()
                self.assertTrue(sender.is_alive())
                self.assertEqual(len(manager.sent[0]), min(initial, 512))
                central.mtu = later
                manager.quota = 1000
                server._drain()
                sender.join(1)
                self.assertFalse(sender.is_alive())
                self.assertFalse(errors)
                self.assertEqual(b''.join(manager.sent), data)
                self.assertTrue(all(len(chunk) <= min(later, 512) for chunk in manager.sent[1:]))
                self.assertEqual(server._queued_bytes, 0)

    def test_second_central_cannot_take_over_or_write_into_session(self):
        first, second = Central('first'), Central('second')
        server, _ = self.server(first)
        stream = server._stream
        server._subscribed(server._manager, second, server._tx)
        self.assertIs(server._stream, stream)
        self.assertIs(server._central, first)
        request = Request(second, b'injected')
        server._write_requests(server._manager, [request])
        self.assertEqual(server._manager.responses, [(request, 8)])
        stream.settimeout(.005)
        with self.assertRaises(socket.timeout):
            stream.recv(99)

    def test_att_batch_is_atomic_and_responds_once(self):
        server, _ = self.server()
        central = server._central
        good, bad = Request(central, b'first'), Request(central, b'bad', offset=1)
        server._write_requests(server._manager, [good, bad])
        self.assertEqual(server._manager.responses, [(good, 7)])
        server._stream.settimeout(.005)
        with self.assertRaises(socket.timeout):
            server._stream.recv(99)
        second = Request(central, b'second')
        server._write_requests(server._manager, [good, second])
        self.assertEqual(server._manager.responses[-1], (good, 0))
        self.assertEqual(len(server._manager.responses), 2)
        self.assertEqual(server._stream.recv(99), b'firstsecond')

    def test_unsubscribe_unblocks_pending_writer_and_reader(self):
        server, pending = self.server()
        manager, stream = server._manager, server._stream
        manager.quota = 0
        errors = []

        def send():
            try:
                stream.sendall(b'blocked')
            except Exception as exc:
                errors.append(exc)

        sender = threading.Thread(target=send)
        sender.start()
        pending.get(timeout=1)()
        server._unsubscribed(manager, server._central, server._tx)
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(stream.closed)
        self.assertTrue(server._closed)
        with self.assertRaises(ConnectionError):
            stream.recv(1)

    def test_send_timeout_cancels_queued_chunks(self):
        server, pending = self.server()
        stream = server._stream
        stream.settimeout(.02)
        server._manager.quota = 0
        errors = []

        def send():
            try:
                stream.sendall(b'queued')
            except Exception as exc:
                errors.append(exc)

        sender = threading.Thread(target=send)
        sender.start()
        pending.get(timeout=1)()
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertIsInstance(errors[0], TimeoutError)
        self.assertTrue(server._closed)
        self.assertEqual(server._queued_bytes, 0)

    def test_radio_and_permissions_fail_with_actionable_errors(self):
        for state, expected in [(2, '不支持'), (3, '隐私与安全性'), (4, '蓝牙已关闭')]:
            with self.subTest(state=state):
                server, _ = self.server(False)
                server._manager._state = state
                server._state_changed(server._manager)
                with self.assertRaisesRegex(OSError, expected):
                    server.accept(threading.Event())

    def test_radio_reset_after_publishing_does_not_leave_stale_service(self):
        server, _ = self.server(False)
        server._adding_service = True
        server._manager._state = 1  # CBManagerStateResetting invalidates services.
        server._state_changed(server._manager)
        self.assertTrue(server._closed)
        with self.assertRaisesRegex(ConnectionError, '重新连接'):
            server.accept(threading.Event())

    def test_async_native_block_exceptions_end_stream_without_escaping(self):
        server, _ = self.server()
        # Invoke the real dispatcher on the test/main thread: no OS queue is used.
        def broken_native_operation():
            raise RuntimeError('native failure')
        MacBleServer._dispatch(server, broken_native_operation)
        self.assertTrue(server._closed)
        with self.assertRaisesRegex(OSError, 'native failure'):
            server._stream.recv(1)

    def test_close_is_idempotent_and_detaches_delegate(self):
        server, pending = self.server()
        manager = server._manager
        stream = server._stream
        server.stop()
        server.stop()
        self.assertTrue(stream.closed)
        self.assertEqual(pending.qsize(), 1)
        pending.get(timeout=1)()
        self.assertTrue(manager.removed)
        self.assertIsNone(manager.delegate)
        with self.assertRaises(OSError):
            server.start()

    def test_native_cleanup_still_detaches_after_radio_reset_error(self):
        server, pending = self.server()
        manager = server._manager
        def invalidated():
            raise RuntimeError('radio reset')
        manager.removeAllServices = invalidated
        server.stop()
        pending.get(timeout=1)()
        self.assertIsNone(manager.delegate)
        self.assertIsNone(server._manager)


if __name__ == '__main__':
    unittest.main()
