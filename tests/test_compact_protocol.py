"""BLE AEAD security/framing checks without Bluetooth or native input hooks."""
import os
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from sharemouse import protocol
from sharemouse import compact_codec
from sharemouse.protocol import BLE_MAGIC, Channel, MAX_BLE_FRAME


PASSWORD = 'compact-test-password'
SALT = b'ShareMonkeyBLE2' + b'S' * 32 + b'C' * 32
MOVE = dict(type='move', epoch=4, dx=1.25, dy=-2.5)


class BufferSocket:
    def __init__(self, data=b'', fragment=7):
        self.data = bytearray(data)
        self.sent = bytearray()
        self.fragment = fragment
        self.read_count = 0

    def recv(self, size):
        self.read_count += 1
        size = min(size, self.fragment)
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    def sendall(self, data):
        self.sent.extend(data)


class CipherSpy:
    def __init__(self, cipher):
        self.cipher = cipher
        self.encrypted = []

    def encrypt(self, nonce, data, aad):
        self.encrypted.append((nonce, data, aad))
        return self.cipher.encrypt(nonce, data, aad)


class CompactProtocolTests(unittest.TestCase):
    def channel(self, server=True, salt=SALT, sock=None):
        return Channel(sock or BufferSocket(), PASSWORD, salt, server, compact=True)

    def wire(self, message=MOVE):
        sender = self.channel()
        sender.send(message)
        return bytes(sender.sock.sent)

    def test_compact_move_is_34_bytes_and_fragmented_stream_roundtrips(self):
        sender, receiver = self.channel(), self.channel(False)
        sender.send(MOVE)
        self.assertEqual(len(sender.sock.sent), 34)
        receiver.sock.data.extend(sender.sock.sent)
        self.assertEqual(receiver.recv(), MOVE)
        self.assertEqual((sender.sent, receiver.received), (1, 1))
        self.assertGreater(receiver.sock.read_count, 1)

    def test_absolute_pointer_is_33_bytes_and_preserves_subpixel_accuracy(self):
        sender, receiver = self.channel(), self.channel(False)
        message = dict(type='pointer', epoch=2 ** 32 - 1, x=32767.123456, y=1589.654321, _sample_time=12345.)
        sender.send(message)
        self.assertEqual(len(sender.sock.sent), 33)
        receiver.sock.data.extend(sender.sock.sent)
        decoded = receiver.recv()
        self.assertEqual(decoded['type'], 'pointer')
        self.assertEqual(decoded['epoch'], message['epoch'])
        self.assertLess(abs(decoded['x'] - message['x']), .001)
        self.assertLess(abs(decoded['y'] - message['y']), .001)
        self.assertNotIn('_sample_time', decoded)

    def test_absolute_pointer_bounds_and_malformed_binary_rejected(self):
        for axis in ('x', 'y'):
            for value in (float('nan'), float('inf'), -float('inf'), 32769, -32769):
                with self.subTest(axis=axis, value=value), self.assertRaises(ValueError):
                    compact_codec.encode(dict(dict(type='pointer', epoch=1, x=1, y=1), **{axis: value}))
        for epoch in (-1, 2 ** 32, True, '1'):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                compact_codec.encode(dict(type='pointer', epoch=epoch, x=0, y=0))
        good = compact_codec.encode(dict(type='pointer', epoch=1, x=0, y=32767))
        for raw in (good[:-1], good + b'x', struct.pack('!BIff', 9, 1, float('nan'), 0)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                compact_codec.decode(raw)

    def test_directional_keys_and_aad_separate_simultaneous_sequence_zero(self):
        server, client = self.channel(), self.channel(False)
        server.tx, client.tx = CipherSpy(server.tx), CipherSpy(client.tx)
        server.send(MOVE)
        client.send(MOVE)
        self.assertNotEqual(server.sock.sent, client.sock.sent)
        self.assertEqual(server.tx.encrypted[0][0], client.tx.encrypted[0][0])
        self.assertNotEqual(server.tx.encrypted[0][2], client.tx.encrypted[0][2])
        server.sock.data.extend(client.sock.sent)
        client.sock.data.extend(server.sock.sent)
        self.assertEqual(server.recv(), MOVE)
        self.assertEqual(client.recv(), MOVE)

    def test_nonce_counter_is_unique_and_authenticated_with_header(self):
        sender = self.channel()
        sender.tx = CipherSpy(sender.tx)
        for _ in range(4):
            sender.send(MOVE)
        for sequence, (nonce, plain, aad) in enumerate(sender.tx.encrypted):
            encoded_sequence = struct.pack('!Q', sequence)
            self.assertEqual(nonce, b'SMB2' + encoded_sequence)
            self.assertEqual(aad, BLE_MAGIC + b'S' + struct.pack('!I', len(plain) + 16) + encoded_sequence)
        self.assertEqual(len({call[0] for call in sender.tx.encrypted}), 4)

    def test_replayed_frame_and_missing_frame_fail_authentication_and_poison(self):
        sender = self.channel()
        sender.send(MOVE)
        first = bytes(sender.sock.sent)
        sender.sock.sent.clear()
        sender.send(dict(MOVE, dx=5.0))
        second = bytes(sender.sock.sent)
        receiver = self.channel(False)
        receiver.sock.data.extend(first + first + second)
        self.assertEqual(receiver.recv(), MOVE)
        with self.assertRaises(InvalidTag):
            receiver.recv()
        reads = receiver.sock.read_count
        with self.assertRaises(ConnectionError):
            receiver.recv()
        self.assertEqual(receiver.sock.read_count, reads)
        with self.assertRaises(ConnectionError):
            receiver.send(MOVE)
        missing = self.channel(False)
        missing.sock.data.extend(second)
        with self.assertRaises(InvalidTag):
            missing.recv()
        self.assertTrue(missing.failed)

    def test_reflected_ciphertext_rejected(self):
        server = self.channel()
        server.send(MOVE)
        server.sock.data.extend(server.sock.sent)
        with self.assertRaises(InvalidTag):
            server.recv()
        self.assertTrue(server.failed)

    def test_ciphertext_header_and_tag_tampering_rejected(self):
        wire = self.wire()
        body_size = struct.unpack('!I', wire[:4])[0]
        bad_body = bytearray(wire)
        bad_body[4] ^= 1
        bad_tag = bytearray(wire)
        bad_tag[-1] ^= 1
        for name, changed in (
            ('ciphertext', bad_body), ('tag', bad_tag),
            ('larger header', struct.pack('!I', body_size + 1) + wire[4:] + b'x'),
            ('smaller header', struct.pack('!I', body_size - 1) + wire[4:]),
        ):
            with self.subTest(name=name):
                receiver = self.channel(False)
                receiver.sock.data.extend(changed)
                with self.assertRaises(InvalidTag):
                    receiver.recv()
                self.assertTrue(receiver.failed)

    def test_invalid_frame_lengths_rejected_before_reading_body(self):
        for size in (0, 1, 16, MAX_BLE_FRAME + 1, 2 ** 32 - 1):
            with self.subTest(size=size):
                receiver = self.channel(False, sock=BufferSocket(struct.pack('!I', size), fragment=4))
                with self.assertRaises(ValueError):
                    receiver.recv()
                self.assertEqual(receiver.sock.read_count, 1)
                self.assertTrue(receiver.failed)

    def test_truncated_frame_poisoned_instead_of_resynchronizing(self):
        receiver = self.channel(False, sock=BufferSocket(self.wire()[:-1]))
        with self.assertRaises(ConnectionError):
            receiver.recv()
        self.assertTrue(receiver.failed)
        receiver.sock.data.extend(self.wire())
        with self.assertRaises(ConnectionError):
            receiver.recv()

    def test_different_session_salt_rejects_old_authenticated_frame(self):
        receiver = self.channel(False, salt=b'ShareMonkeyBLE2' + b'S' * 32 + b'D' * 32)
        receiver.sock.data.extend(self.wire())
        with self.assertRaises(InvalidTag):
            receiver.recv()

    def test_partial_send_failure_never_reuses_nonce_even_if_caller_retries(self):
        class BrokenSocket(BufferSocket):
            def sendall(self, data):
                self.sent.extend(data[:12])
                raise socket.timeout('partial write')

        sender = self.channel(sock=BrokenSocket())
        sender.tx = CipherSpy(sender.tx)
        with self.assertRaises(socket.timeout):
            sender.send(MOVE)
        self.assertTrue(sender.failed)
        self.assertEqual(len(sender.tx.encrypted), 1)
        with self.assertRaises(ConnectionError):
            sender.send(MOVE)
        self.assertEqual(len(sender.tx.encrypted), 1)
        self.assertEqual(len(sender.sock.sent), 12)

    def test_counter_boundary_last_nonce_then_no_wrap(self):
        sender, receiver = self.channel(), self.channel(False)
        sender.sent = receiver.received = 2 ** 64 - 1
        sender.tx = CipherSpy(sender.tx)
        sender.send(MOVE)
        self.assertEqual(sender.tx.encrypted[0][0], b'SMB2' + b'\xff' * 8)
        receiver.sock.data.extend(sender.sock.sent)
        self.assertEqual(receiver.recv(), MOVE)
        with self.assertRaisesRegex(ValueError, '计数'):
            sender.send(MOVE)
        with self.assertRaisesRegex(ValueError, '计数'):
            receiver.recv()
        self.assertTrue(sender.failed)
        self.assertTrue(receiver.failed)
        self.assertEqual(len(sender.tx.encrypted), 1)

    def test_concurrent_senders_never_interleave_frames_or_repeat_nonces(self):
        sender, receiver = self.channel(), self.channel(False)
        sender.tx = CipherSpy(sender.tx)
        errors = []

        def send(number):
            try:
                for _ in range(20):
                    sender.send(dict(MOVE, dx=float(number)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=send, args=(number,)) for number in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len({item[0] for item in sender.tx.encrypted}), 60)
        receiver.sock.data.extend(sender.sock.sent)
        decoded = [receiver.recv() for _ in range(60)]
        for number in range(3):
            self.assertEqual(sum(message['dx'] == float(number) for message in decoded), 20)

    def test_authenticated_malformed_plaintext_poisoned(self):
        sender, receiver = self.channel(), self.channel(False)
        # Simulate an authenticated peer using an unsupported binary body tag.
        with patch('sharemouse.protocol.compact_codec.encode', return_value=b'\xff'):
            sender.send(MOVE)
        receiver.sock.data.extend(sender.sock.sent)
        with self.assertRaises(ValueError):
            receiver.recv()
        self.assertTrue(receiver.failed)

    def test_handshake_uses_both_fresh_random_nonces_in_same_derived_salt(self):
        left, right = socket.socketpair()
        left.settimeout(2)
        right.settimeout(2)
        errors, channels, salts, random_calls = [], {}, [], []
        original = protocol.PBKDF2HMAC

        def random(size):
            random_calls.append((threading.current_thread().name, size))
            return (b'S' if threading.current_thread().name == 'compact-server' else b'C') * size

        def kdf(**kwargs):
            salts.append(kwargs['salt'])
            return original(**kwargs)

        def server():
            try:
                channels['server'] = Channel.handshake(left, PASSWORD, True, compact=True)
            except Exception as exc:
                errors.append(exc)

        try:
            with patch('sharemouse.protocol.os.urandom', side_effect=random), \
                    patch('sharemouse.protocol.PBKDF2HMAC', side_effect=kdf):
                thread = threading.Thread(target=server, name='compact-server')
                thread.start()
                channels['client'] = Channel.handshake(right, PASSWORD, False, compact=True)
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(salts, [SALT, SALT])
            self.assertEqual(len(random_calls), 2)
            self.assertTrue(all(size == 32 for _, size in random_calls))
            channels['server'].send(MOVE)
            self.assertEqual(channels['client'].recv(), MOVE)
        finally:
            left.close()
            right.close()

    def test_handshake_wrong_password_never_completes(self):
        left, right = socket.socketpair()
        left.settimeout(1)
        right.settimeout(1)
        errors = []

        def server():
            try:
                Channel.handshake(left, PASSWORD, True, compact=True)
            except Exception as exc:
                errors.append(exc)
            finally:
                left.close()

        thread = threading.Thread(target=server)
        thread.start()
        try:
            with self.assertRaises(InvalidTag):
                Channel.handshake(right, PASSWORD + '-wrong', False, compact=True)
        finally:
            right.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_old_ble_magic_rejected_before_key_derivation(self):
        for server in (False, True):
            with self.subTest(server=server):
                sock = BufferSocket(protocol.MAGIC + os.urandom(32))
                with patch('sharemouse.protocol.PBKDF2HMAC') as kdf:
                    with self.assertRaisesRegex(ValueError, 'v0.5.2'):
                        Channel.handshake(sock, PASSWORD, server, compact=True)
                    kdf.assert_not_called()


if __name__ == '__main__':
    unittest.main()
