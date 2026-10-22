import base64
import unittest

from sharemouse.bluetooth_clipboard import ClipboardChunks


class BluetoothClipboardTests(unittest.TestCase):
    def test_empty_text_is_a_complete_update(self):
        chunks = ClipboardChunks()
        messages = chunks.encode('')
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['data'], '')
        self.assertEqual(chunks.receive(messages[0]), '')

    def test_utf8_split_inside_a_character_only_updates_at_finish(self):
        sender, receiver = ClipboardChunks(chunk_size=2), ClipboardChunks(chunk_size=2)
        text = '你好🐒Mac与Windows'
        messages = sender.encode(text)
        with self.assertRaises(UnicodeDecodeError):
            base64.b64decode(messages[0]['data']).decode('utf-8')
        self.assertTrue(all(receiver.receive(message) is None for message in messages[:-1]))
        self.assertEqual(receiver.receive(messages[-1]), text)

    def test_eight_kib_utf8_and_bounds(self):
        text = '🐒' * 2048
        chunks = ClipboardChunks()
        messages = chunks.encode(text)
        self.assertEqual(len(messages), 64)
        self.assertTrue(all(len(base64.b64decode(message['data'])) <= 128 for message in messages))
        results = [chunks.receive(message) for message in messages]
        self.assertEqual(results[:-1], [None] * 63)
        self.assertEqual(results[-1], text)
        with self.assertRaises(ValueError):
            chunks.encode(text + 'a')

    def test_transfers_have_distinct_ids_and_encoding_does_not_reset_receiver(self):
        chunks = ClipboardChunks(chunk_size=4)
        incoming = chunks.encode('incoming')
        self.assertIsNone(chunks.receive(incoming[0]))
        outgoing = chunks.encode('outgoing')
        self.assertNotEqual(incoming[0]['transfer'], outgoing[0]['transfer'])
        self.assertEqual(chunks.receive(incoming[1]), 'incoming')

    def test_new_transfer_replaces_old_partial_text(self):
        chunks = ClipboardChunks(chunk_size=4)
        old, new = chunks.encode('old clipboard'), chunks.encode('new text')
        self.assertIsNone(chunks.receive(old[0]))
        self.assertIsNone(chunks.receive(new[0]))
        self.assertEqual(chunks.receive(new[1]), 'new text')
        with self.assertRaises(ValueError):
            chunks.receive(old[1])

    def test_out_of_order_duplicate_and_changed_total_rejected(self):
        for alteration in ('skip', 'duplicate', 'different_id', 'changed_total'):
            with self.subTest(alteration=alteration):
                chunks = ClipboardChunks(chunk_size=4)
                messages = chunks.encode('abcdefghijkl')
                chunks.receive(messages[0])
                wrong = dict(messages[2] if alteration == 'skip' else messages[1])
                if alteration == 'duplicate':
                    chunks.receive(messages[1])
                elif alteration == 'different_id':
                    wrong['transfer'] = 'f' * 32
                elif alteration == 'changed_total':
                    wrong['total'] += 1
                with self.assertRaises(ValueError):
                    chunks.receive(wrong)
                # Invalid messages never leave old partial data resumable.
                with self.assertRaises(ValueError):
                    chunks.receive(messages[-1])
                replacement = chunks.encode('ok')[0]
                self.assertEqual(chunks.receive(replacement), 'ok')

    def test_duplicate_first_chunk_is_not_a_new_transfer(self):
        chunks = ClipboardChunks(chunk_size=4)
        message = chunks.encode('12345')[0]
        self.assertIsNone(chunks.receive(message))
        with self.assertRaises(ValueError):
            chunks.receive(message)

    def test_malformed_fields_and_noncanonical_base64_rejected(self):
        chunks = ClipboardChunks()
        baseline = chunks.encode('test')[0]
        invalid = [('type', 'clipboard'), ('transfer', ''), ('transfer', 'A' * 32),
                   ('transfer', 1), ('index', True), ('index', -1), ('index', 1),
                   ('total', True), ('total', 0), ('total', 65), ('total', '1'),
                   ('data', 1), ('data', '===='), ('data', 'A===') ,
                   ('data', 'dGVzdA==\n'), ('data', 'YR=='), ('data', '🐒'),
                   ('data', base64.b64encode(b'x' * 129).decode())]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                chunks.receive(dict(baseline, **{field: value}))
        for message in (None, [], {}, {'type': 'clipboard_part'}):
            with self.subTest(message=message), self.assertRaises(ValueError):
                chunks.receive(message)

    def test_total_limit_enforced_when_limit_is_not_a_multiple_of_chunk_size(self):
        chunks = ClipboardChunks(limit=5, chunk_size=4)
        messages = chunks.encode('12345')
        chunks.receive(messages[0])
        wrong_last = dict(messages[1], data=base64.b64encode(b'67').decode())
        with self.assertRaises(ValueError):
            chunks.receive(wrong_last)

    def test_incomplete_nonfinal_chunk_and_empty_final_rejected(self):
        chunks = ClipboardChunks(chunk_size=4)
        messages = chunks.encode('12345')
        with self.assertRaises(ValueError):
            chunks.receive(dict(messages[0], data=base64.b64encode(b'12').decode()))
        chunks.receive(messages[0])
        with self.assertRaises(ValueError):
            chunks.receive(dict(messages[1], data=''))

    def test_expired_transfer_cannot_resume_and_new_transfer_can_start(self):
        now = [0.0]
        chunks = ClipboardChunks(chunk_size=4, clock=lambda: now[0])
        messages = chunks.encode('12345')
        self.assertIsNone(chunks.receive(messages[0]))
        now[0] = 30.0
        with self.assertRaises(ValueError):
            chunks.receive(messages[1])
        self.assertIsNone(chunks.receive(messages[0]))
        self.assertEqual(chunks.receive(messages[1]), '12345')

    def test_timeout_extends_when_a_chunk_is_accepted(self):
        now = [0.0]
        chunks = ClipboardChunks(chunk_size=4, clock=lambda: now[0])
        messages = chunks.encode('123456789')
        chunks.receive(messages[0])
        now[0] = 29.0
        chunks.receive(messages[1])
        now[0] = 58.0
        self.assertEqual(chunks.receive(messages[2]), '123456789')

    def test_invalid_utf8_never_becomes_clipboard_text(self):
        chunks = ClipboardChunks()
        message = chunks.encode('ok')[0]
        with self.assertRaises(ValueError):
            chunks.receive(dict(message, data=base64.b64encode(b'\xff').decode()))
        with self.assertRaises(ValueError):
            chunks.encode('\ud800')
        with self.assertRaises(ValueError):
            chunks.encode(None)

    def test_short_limit_and_constructor_validation(self):
        chunks = ClipboardChunks(limit=2)
        self.assertEqual(chunks.receive(chunks.encode('ok')[0]), 'ok')
        for options in ({'limit': 0}, {'limit': True}, {'chunk_size': 0},
                        {'chunk_size': True}, {'timeout': 0}, {'timeout': float('inf')},
                        {'timeout': None}, {'clock': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ClipboardChunks(**options)


if __name__ == '__main__':
    unittest.main()
