import json
import threading
import time
import unittest
from unittest.mock import patch

from sharemouse.discovery import Advertiser, Browser, decode, packet, parse_peer

NONCE = 'a' * 32


class DiscoveryTests(unittest.TestCase):
    def offer(self, **fields):
        return packet('offer', NONCE, **dict(name='Office PC', platform='Windows', port=24809, **fields))

    def test_source_address_used_instead_of_advertised_host(self):
        raw = self.offer(host='attacker.example')
        peer = parse_peer(raw, ('192.168.1.5', 12345), NONCE)
        self.assertEqual(peer.host, '192.168.1.5')
        self.assertEqual(peer.port, 24809)
        self.assertIn('Office PC', peer.label)

    def test_unsolicited_reply_rejected(self):
        with self.assertRaises(ValueError):
            parse_peer(self.offer(), ('192.168.1.5', 12345), 'b' * 32)

    def test_malformed_packets_rejected(self):
        for raw in (b'[]', b'null', b'invalid', b'x' * 1025, b'{"protocol":"other"}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                decode(raw)

    def test_invalid_peer_fields_rejected(self):
        baseline = json.loads(self.offer())
        for field, bad in [('port', 0), ('port', True), ('port', 99999),
                           ('name', ''), ('name', 'a\nb'), ('name', 'x' * 81),
                           ('platform', 'unknown'), ('type', 'query')]:
            raw = json.dumps(dict(baseline, **{field: bad})).encode()
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                parse_peer(raw, ('192.168.1.5', 12345), NONCE)

    def test_udp_query_response_and_expiry(self):
        errors, updates = [], []
        found, expired = threading.Event(), threading.Event()
        def update(peers):
            updates.append(peers)
            if peers:
                found.set()
            elif found.is_set():
                expired.set()
        advertiser = Advertiser(26000, errors.append, bind='127.0.0.1', udp_port=0).start()
        browser = None
        try:
            deadline = time.monotonic() + 3
            port = 0
            while time.monotonic() < deadline and not errors:
                if advertiser.sock:
                    port = advertiser.sock.getsockname()[1]
                    if port:
                        break
                time.sleep(.01)
            self.assertFalse(errors, errors)
            self.assertNotEqual(port, 0)
            with patch('sharemouse.discovery.PEER_TTL', .2):
                browser = Browser(update, errors.append, target='127.0.0.1', udp_port=port).start()
                self.assertTrue(found.wait(3), errors)
                self.assertEqual(updates[0][0].host, '127.0.0.1')
                self.assertEqual(updates[0][0].port, 26000)
                advertiser.stop()
                self.assertTrue(expired.wait(3))
                self.assertEqual(updates[-1], [])
            self.assertFalse(errors, errors)
        finally:
            advertiser.stop()
            if browser:
                browser.stop()
