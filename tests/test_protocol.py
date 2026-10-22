import os
import socket
import struct
import threading
import unittest

from cryptography.fernet import InvalidToken

from sharemouse.protocol import Channel, MAX_FRAME, read_exact


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.a.settimeout(2)
        self.b.settimeout(2)
        self.salt = os.urandom(32)

    def tearDown(self):
        self.a.close()
        self.b.close()

    def pair(self):
        return (Channel(self.a, 'a-long-test-password', self.salt, True),
                Channel(self.b, 'a-long-test-password', self.salt, False))

    def test_bidirectional_unicode(self):
        server, client = self.pair()
        server.send({'type': 'clipboard', 'text': '你好 Mac 和 Windows'})
        self.assertEqual(client.recv()['text'], '你好 Mac 和 Windows')
        client.send({'type': 'key', 'down': True})
        self.assertEqual(server.recv(), {'type': 'key', 'down': True})

    def test_handshake(self):
        result = []
        def serve():
            result.append(Channel.handshake(self.a, 'a-long-test-password', True))
        thread = threading.Thread(target=serve)
        thread.start()
        client = Channel.handshake(self.b, 'a-long-test-password', False)
        thread.join(3)
        self.assertEqual(len(result), 1)
        client.send({'type': 'ping'})
        self.assertEqual(result[0].recv(), {'type': 'ping'})

    def test_wrong_password(self):
        server, _ = self.pair()
        wrong = Channel(self.b, 'another-password', self.salt, False)
        server.send({'type': 'hello'})
        with self.assertRaises(InvalidToken):
            wrong.recv()

    def test_replay_rejected(self):
        server, client = self.pair()
        server.send({'type': 'ping'})
        size = read_exact(self.b, 4)
        token = read_exact(self.b, struct.unpack('!I', size)[0])
        self.a.sendall(size + token)
        client.recv()
        self.a.sendall(size + token)
        with self.assertRaises(ValueError):
            client.recv()

    def test_frame_limit_before_allocation(self):
        _, client = self.pair()
        self.a.sendall(struct.pack('!I', MAX_FRAME + 1))
        with self.assertRaises(ValueError):
            client.recv()

    def test_connection_closed_mid_frame(self):
        self.a.sendall(b'12')
        self.a.close()
        with self.assertRaises(ConnectionError):
            read_exact(self.b, 4)

    def test_short_password(self):
        with self.assertRaises(ValueError):
            Channel(self.a, '123', self.salt, True)

    def test_four_digit_pin_preserves_leading_zeroes_and_encryption(self):
        for compact in (False, True):
            server = Channel(self.a, '0042', self.salt, True, compact=compact)
            client = Channel(self.b, '0042', self.salt, False, compact=compact)
            server.send({'type': 'ping'})
            self.assertEqual(client.recv(), {'type': 'ping'})
            client.send({'type': 'key', 'key': {'char': 'a'}, 'down': True})
            self.assertEqual(server.recv()['type'], 'key')
