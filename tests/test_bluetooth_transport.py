from enum import Enum, auto
import socket
import threading
import time
import unittest

from sharemouse.bluetooth_transport import BleStream, BluetoothPeer, normalize_error


class BluetoothTransportTests(unittest.TestCase):
    def test_fragmented_delivery_and_partial_reads_preserve_order(self):
        stream = BleStream()
        source = bytearray(b'abc')
        stream.feed(source)
        source[:] = b'xxx'
        for fragment in (b'd', b'efghi', b'j'):
            stream.feed(fragment)
        received = bytearray()
        while len(received) < 10:
            received.extend(stream.recv(2))
        self.assertEqual(received, b'abcdefghij')
        self.assertEqual(stream._size, 0)

    def test_empty_reads_do_not_consume_and_negative_size_rejected(self):
        stream = BleStream()
        stream.feed(b'abc')
        self.assertEqual(stream.recv(0), b'')
        with self.assertRaises(ValueError):
            stream.recv(-1)
        self.assertEqual(stream.recv(3), b'abc')

    def test_receive_timeout_does_not_close_an_otherwise_healthy_stream(self):
        stream = BleStream(timeout=.02)
        began = time.monotonic()
        with self.assertRaises(socket.timeout):
            stream.recv(1)
        self.assertGreaterEqual(time.monotonic() - began, .015)
        self.assertFalse(stream.closed)
        stream.feed(b'x')
        self.assertEqual(stream.recv(1), b'x')

    def test_settimeout_rejects_invalid_values_and_accepts_none(self):
        stream = BleStream()
        for timeout in (0, -1, True, False, float('inf'), float('-inf'), float('nan'), '15', object()):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                stream.settimeout(timeout)
        stream.settimeout(None)
        self.assertIsNone(stream.gettimeout())
        stream.settimeout(.25)
        self.assertEqual(stream.gettimeout(), .25)

    def test_constructor_rejects_invalid_timeout_and_buffer_limit(self):
        for timeout in (0, -1, True, float('inf'), float('nan'), '15'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                BleStream(timeout=timeout)
        for limit in (0, -1, True, False, 1.5, '100', None):
            with self.subTest(buffer_limit=limit), self.assertRaises(ValueError):
                BleStream(buffer_limit=limit)

    def test_finish_discards_old_pending_input_instead_of_replaying_it(self):
        for error in (None, ConnectionResetError('radio disconnected')):
            with self.subTest(error=error):
                stream = BleStream()
                stream.feed(b'old-movement-keydown')
                stream.finish(error)
                self.assertEqual(stream._size, 0)
                self.assertFalse(stream._chunks)
                if error is None:
                    self.assertEqual(stream.recv(100), b'')
                else:
                    with self.assertRaises(ConnectionError) as caught:
                        stream.recv(100)
                    self.assertIs(caught.exception.__cause__, error)
                stream.feed(b'late-native-callback')
                self.assertEqual(stream._size, 0)

    def test_overflow_closes_stream_and_discards_all_buffered_bytes(self):
        callbacks = []
        stream = BleStream(buffer_limit=4, on_close=lambda: callbacks.append('closed'))
        stream.feed(b'abcd')
        self.assertFalse(stream.closed)
        stream.feed(b'e')
        self.assertTrue(stream.closed)
        self.assertEqual(stream._size, 0)
        self.assertEqual(callbacks, ['closed'])
        with self.assertRaisesRegex(ConnectionError, '拥堵'):
            stream.recv(10)
        stream.close()
        self.assertEqual(callbacks, ['closed'])

    def _reader(self, stream):
        started, completed = threading.Event(), threading.Event()
        values, errors = [], []

        def receive():
            started.set()
            try:
                values.append(stream.recv(1))
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        return thread, completed, values, errors

    def test_close_wakes_a_reader_with_no_receive_timeout(self):
        stream = BleStream(timeout=None)
        reader, completed, values, errors = self._reader(stream)
        stream.close()
        self.assertTrue(completed.wait(1))
        reader.join(1)
        self.assertEqual(values, [b''])
        self.assertEqual(errors, [])

    def test_disconnect_wakes_reader_with_reason_and_preserves_first_failure(self):
        callbacks = []
        stream = BleStream(timeout=None, on_close=lambda: callbacks.append(1))
        reader, completed, values, errors = self._reader(stream)
        stream.finish(ConnectionResetError('device disconnected'))
        stream.finish(ConnectionError('later error'))
        stream.close()
        self.assertTrue(completed.wait(1))
        reader.join(1)
        self.assertFalse(values)
        self.assertEqual(len(errors), 1)
        self.assertIn('device disconnected', str(errors[0]))
        self.assertEqual(callbacks, [1])

    def test_reentrant_close_callback_runs_outside_condition_lock_once(self):
        calls, callback_finished = [], threading.Event()
        stream = BleStream()

        def close_callback():
            calls.append(stream.closed)
            # Another thread must be able to acquire the stream condition while
            # the close callback runs; Condition uses an RLock on the caller.
            read_finished = threading.Event()

            def inspect():
                calls.append(stream.closed)
                read_finished.set()

            inspector = threading.Thread(target=inspect, daemon=True)
            inspector.start()
            calls.append(read_finished.wait(1))
            inspector.join(1)
            stream.close()
            stream.finish(ConnectionError('reentrant'))
            stream.feed(b'late')
            callback_finished.set()

        stream.on_close = close_callback
        closer = threading.Thread(target=stream.close, daemon=True)
        closer.start()
        self.assertTrue(callback_finished.wait(2))
        closer.join(1)
        self.assertEqual(calls, [True, True, True])

    def test_close_observer_exception_is_contained(self):
        def broken_callback():
            raise RuntimeError('observer failed')

        stream = BleStream(on_close=broken_callback)
        stream.finish('native string error')
        self.assertTrue(stream.closed)
        with self.assertRaisesRegex(ConnectionError, 'native string error'):
            stream.recv(1)

    def test_writer_receives_owned_bytes_and_remaining_timeout(self):
        writes = []
        stream = BleStream(writer=lambda data, timeout: writes.append((data, timeout)), timeout=1)
        stream.sendall(bytearray(b'data'))
        self.assertEqual(writes[0][0], b'data')
        self.assertIsInstance(writes[0][0], bytes)
        self.assertGreater(writes[0][1], 0)
        self.assertLessEqual(writes[0][1], 1)
        stream.sendall(b'')
        self.assertEqual(len(writes), 1)

    def test_writer_failure_finishes_stream_and_wakes_reader(self):
        reason = socket.timeout('adapter stalled')

        def write(data, timeout):
            raise reason

        stream = BleStream(writer=write, timeout=None)
        reader, completed, values, errors = self._reader(stream)
        with self.assertRaises(socket.timeout) as caught:
            stream.sendall(b'key-up')
        self.assertIs(caught.exception, reason)
        self.assertTrue(completed.wait(1))
        reader.join(1)
        self.assertFalse(values)
        self.assertEqual(len(errors), 1)
        self.assertIn('adapter stalled', str(errors[0]))
        self.assertTrue(stream.closed)

    def test_missing_writer_fails_closed_and_late_writer_can_be_installed(self):
        stream = BleStream()
        with self.assertRaises(ConnectionError):
            stream.sendall(b'key')
        self.assertTrue(stream.closed)
        writes = []
        ready = BleStream()
        ready.set_writer(lambda data, timeout: writes.append(data))
        ready.sendall(b'key')
        self.assertEqual(writes, [b'key'])
        ready.shutdown(socket.SHUT_RDWR)
        with self.assertRaises(ConnectionError):
            ready.sendall(b'key')

    def test_concurrent_writers_are_serialized_and_queue_wait_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        writes, errors = [], []

        def write(data, timeout):
            writes.append(data)
            entered.set()
            if not release.wait(1):
                raise socket.timeout('test writer wait timed out')

        stream = BleStream(writer=write, timeout=.03)

        def send_first():
            try:
                stream.sendall(b'first')
            except Exception as exc:
                errors.append(exc)

        sender = threading.Thread(target=send_first, daemon=True)
        sender.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(socket.timeout):
                stream.sendall(b'second')
            self.assertEqual(writes, [b'first'])
        finally:
            release.set()
            sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertFalse(errors)

    def test_winrt_reason_enum_maps_to_actionable_messages(self):
        class Reason(Enum):
            POWERED_OFF = auto()
            NO_BLUETOOTH = auto()
            NO_BLE_CENTRAL_ROLE = auto()
            DENIED_BY_USER = auto()
            DENIED_BY_SYSTEM = auto()

        for reason in Reason:
            error = RuntimeError('backend failed')
            error.reason = reason
            actual = normalize_error(error)
            if reason is Reason.POWERED_OFF:
                self.assertIn('蓝牙已关闭', actual)
            elif reason in (Reason.NO_BLUETOOTH, Reason.NO_BLE_CENTRAL_ROLE):
                self.assertIn('适配器', actual)
            else:
                self.assertIn('权限被拒绝', actual)

    def test_normalize_text_errors_and_unknown_error(self):
        self.assertIn('权限被拒绝', normalize_error(PermissionError('access denied')))
        self.assertIn('蓝牙已关闭', normalize_error(RuntimeError('Bluetooth is off')))
        self.assertIn('适配器', normalize_error(RuntimeError('not supported')))
        self.assertIn('unfamiliar failure', normalize_error(RuntimeError('unfamiliar failure')))

    def test_same_named_devices_are_distinct_even_with_matching_address_suffixes(self):
        first = BluetoothPeer('AA:01:02:03:04:05', 'SM Monkey')
        second = BluetoothPeer('BB:01:02:03:04:05', 'SM Monkey')
        self.assertNotEqual(first.label, second.label)
        self.assertEqual(first.port, 0)
        self.assertEqual(first.platform, 'Mac')


if __name__ == '__main__':
    unittest.main()
