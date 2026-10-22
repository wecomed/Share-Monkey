import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from sharemouse.instance import InstanceLock


class InstanceTests(unittest.TestCase):
    def test_reopening_requests_existing_window_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client.lock'
            owner = InstanceLock(path).acquire()
            try:
                second = InstanceLock(path)
                self.assertFalse(owner.consume_show_request())
                self.assertTrue(second.request_show())
                self.assertTrue(owner.consume_show_request())
                self.assertFalse(owner.consume_show_request())
                second.request_show()
                self.assertTrue(owner.consume_show_request())
            finally:
                owner.close()

    def test_other_process_cannot_lock_until_owner_exits(self):
        code = "from sharemouse.instance import InstanceLock, AlreadyRunning\nimport sys\ntry:\n g = InstanceLock(sys.argv[1]).acquire()\nexcept AlreadyRunning:\n sys.exit(7)\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client.lock'
            guard = InstanceLock(path).acquire()
            try:
                self.assertEqual(subprocess.run([sys.executable, '-c', code, str(path)]).returncode, 7)
            finally:
                guard.close()
            self.assertEqual(subprocess.run([sys.executable, '-c', code, str(path)]).returncode, 0)
