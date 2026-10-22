import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from sharemouse.core import Cursor, FileReceiver, Screen


class CursorTests(unittest.TestCase):
    def test_right_entry_and_return(self):
        cursor = Cursor(Screen(1920, 1080), 'right')
        self.assertEqual(cursor.enter(.5), (2, 540))
        self.assertFalse(cursor.move(100, -100)[0])
        leave, position = cursor.move(-200, -1000)
        self.assertTrue(leave)
        self.assertEqual(position, (0, 0))

    def test_left_entry_and_return(self):
        cursor = Cursor(Screen(1280, 720), 'left')
        self.assertEqual(cursor.enter(1), (1277, 719))
        self.assertTrue(cursor.move(10, 0)[0])
        self.assertEqual(cursor.x, 1279)

    def test_resolution_validation(self):
        for width in (0, 99, 40000):
            with self.assertRaises(ValueError):
                Screen(width, 1080)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.receiver = FileReceiver(self.temp.name)

    def tearDown(self):
        self.receiver.abort()
        self.temp.cleanup()

    def test_success_with_unicode_and_no_overwrite(self):
        raw = '测试文件'.encode()
        existing = Path(self.temp.name) / '文件.txt'
        existing.write_text('keep')
        self.receiver.begin({'name': '文件.txt', 'size': len(raw)})
        self.receiver.chunk(base64.b64encode(raw).decode())
        path = self.receiver.finish(hashlib.sha256(raw).hexdigest())
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(existing.read_text(), 'keep')
        self.assertFalse(list(Path(self.temp.name).glob('*.part')))

    def test_traversal_and_size_rejected(self):
        for name in ('../a', '..\\a', '.', '..', '/tmp/a', 'a:b', ''):
            with self.assertRaises(ValueError):
                self.receiver.begin({'name': name, 'size': 1})
        for size in (-1, FileReceiver.LIMIT + 1, True):
            with self.assertRaises(ValueError):
                self.receiver.begin({'name': 'a', 'size': size})

    def test_overflow(self):
        self.receiver.begin({'name': 'a', 'size': 1})
        with self.assertRaises(ValueError):
            self.receiver.chunk(base64.b64encode(b'ab').decode())

    def test_bad_checksum_removes_partial_file(self):
        self.receiver.begin({'name': 'a', 'size': 1})
        self.receiver.chunk('YQ==')
        with self.assertRaises(ValueError):
            self.receiver.finish('bad')
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_truncated_transfer_removes_partial(self):
        self.receiver.begin({'name': 'a', 'size': 10})
        self.receiver.abort()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
