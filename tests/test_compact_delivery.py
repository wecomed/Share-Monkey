import struct
import base64
import unittest

from sharemouse import compact_codec


class CompactDeliveryTests(unittest.TestCase):
    def test_tracked_pointer_and_clipboard_preserve_delivery_id(self):
        for message in (dict(type='pointer', epoch=7, x=300, y=200),
                        dict(type='clipboard_part', transfer='a' * 32, index=0, total=1, data='aGVsbG8=')):
            for packet in (0, 1, 2 ** 32 - 1):
                with self.subTest(message=message, packet=packet):
                    tracked = dict(message, _delivery=packet)
                    raw = compact_codec.encode(tracked)
                    self.assertEqual(raw[:5], b'\xfe' + struct.pack('!I', packet))
                    self.assertEqual(compact_codec.decode(raw), tracked)

    def test_consumed_ack_is_five_byte_body_and_never_requires_ack(self):
        self.assertEqual(compact_codec.encode(dict(type='consumed', packet=42)), b'\x0a\0\0\0*')
        self.assertEqual(compact_codec.decode(b'\x0a\0\0\0*'), dict(type='consumed', packet=42))
        with self.assertRaises(ValueError):
            compact_codec.encode(dict(type='consumed', packet=42, _delivery=43))
        with self.assertRaises(ValueError):
            compact_codec.decode(b'\xfe\0\0\0+' + b'\x0a\0\0\0*')

    def test_invalid_packet_ids_and_truncated_or_nested_wrappers_rejected(self):
        for value in (None, True, False, -1, 2 ** 32, 1.5, '1'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    compact_codec.encode(dict(type='pointer', epoch=1, x=0, y=0, _delivery=value))
                with self.assertRaises(ValueError):
                    compact_codec.encode(dict(type='consumed', packet=value))
        for raw in (b'\xfe', b'\xfe\0\0\0\1', b'\xfe\0\0\0\1\xfe\0\0\0\2\6',
                    b'\x0a', b'\x0a\0\0\0\1extra'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                compact_codec.decode(raw)

    def test_binary_clipboard_payload_avoids_json_base64_wire_overhead(self):
        for size in (0, 32, 128):
            with self.subTest(size=size):
                message = dict(type='clipboard_part', transfer='ab' * 16, index=0, total=1,
                               data=base64.b64encode(bytes(range(size))).decode())
                raw = compact_codec.encode(message)
                self.assertEqual(raw[0], 11)
                self.assertEqual(len(raw), 21 + size)
                self.assertEqual(compact_codec.decode(raw), message)
                tracked = dict(message, _delivery=7)
                self.assertEqual(compact_codec.decode(compact_codec.encode(tracked)), tracked)

    def test_binary_clipboard_rejects_malformed_fields_and_packets(self):
        message = dict(type='clipboard_part', transfer='ab' * 16, index=0, total=1, data='aGVsbG8=')
        for key, value in (('transfer', 'AB' * 16), ('index', True), ('index', -1),
                           ('total', 0), ('total', 8193), ('total', True),
                           ('data', 'YR=='), ('data', base64.b64encode(b'x' * 129).decode())):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                compact_codec.encode(dict(message, **{key: value}))
        for raw in (b'\x0b', struct.pack('!B16sHH', 11, bytes(16), 1, 1),
                    struct.pack('!B16sHH', 11, bytes(16), 0, 0),
                    struct.pack('!B16sHH', 11, bytes(16), 0, 2),
                    struct.pack('!B16sHH', 11, bytes(16), 0, 1) + bytes(129)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                compact_codec.decode(raw)


if __name__ == '__main__':
    unittest.main()
