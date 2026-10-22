import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sharemouse.core import Screen
from sharemouse.engine import Engine


class IntegrationTests(unittest.TestCase):
    def wait_for(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for peer state')

    def test_encrypted_engine_pair_input_and_bidirectional_files(self):
        # Real sockets + protocol + engine, with only physical OS input mocked.
        raw_a, raw_b = socket.socketpair()
        a, b = Mock(wraps=raw_a), Mock(wraps=raw_b)
        # AF_UNIX socketpair has no TCP_NODELAY option; all byte I/O stays real.
        a.setsockopt = Mock()
        b.setsockopt = Mock()
        listener = Mock()
        listener.accept.return_value = (b, ('127.0.0.1', 0))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = Engine(True, Screen(1920, 1080), 'right', 'integration-password',
                                '127.0.0.1', 24809, Mock(), clipboard=False,
                                files=True, directory=root / 'controller')
            receiver = Engine(False, Screen(1280, 720), 'right', 'integration-password',
                              '127.0.0.1', 24809, Mock(), clipboard=False,
                              files=True, directory=root / 'receiver')
            for engine in (controller, receiver):
                engine.input = Mock()
                engine.input.listeners = []
            threads = [threading.Thread(target=e.run) for e in (controller, receiver)]
            try:
                with patch('sharemouse.engine.socket.create_connection', return_value=a), \
                     patch('sharemouse.engine.socket.socket', return_value=listener), \
                     patch('sharemouse.engine.Advertiser'):
                    for thread in threads:
                        thread.start()
                    self.wait_for(lambda: controller.connected and receiver.connected)
                controller.send({'type': 'enter', 'side': 'right', 'fraction': .5})
                self.wait_for(lambda: receiver.active)
                controller.send({'type': 'key', 'key': {'char': 'a'}, 'down': True})
                self.wait_for(lambda: receiver.input.inject_key.called)
                receiver.input.inject_key.assert_called_with({'char': 'a'}, True)
                controller.send({'type': 'move', 'dx': -100, 'dy': 0})
                self.wait_for(lambda: not receiver.active)
                for epoch in range(1, 101):
                    with controller.state_lock:
                        controller.epoch = epoch
                        controller.remote = True
                        controller.send({'type': 'enter', 'side': 'right', 'fraction': .5})
                        controller.send({'type': 'move', 'dx': 100, 'dy': 0})
                        controller.send({'type': 'move', 'dx': -200, 'dy': 0})
                    self.wait_for(lambda: not controller.remote)
                    self.assertTrue(controller.connected)
                    self.assertTrue(receiver.connected)
                    self.assertEqual(receiver.received_epoch, epoch)
                self.assertEqual(controller.input.restore_local.call_count, 100)
                sample = root / '测试.txt'
                sample.write_bytes('macOS ⇄ Windows'.encode() * 5000)
                controller.send_file(sample)
                self.wait_for(lambda: bool(list((root / 'receiver').glob('*_测试.txt'))))
                self.assertEqual(next((root / 'receiver').glob('*_测试.txt')).read_bytes(), sample.read_bytes())
                receiver.send_file(sample)
                self.wait_for(lambda: bool(list((root / 'controller').glob('*_测试.txt'))))
                self.assertEqual(next((root / 'controller').glob('*_测试.txt')).read_bytes(), sample.read_bytes())
            finally:
                controller.stop()
                receiver.stop()
                for thread in threads:
                    thread.join(3)
            self.assertFalse(controller.connected)
            self.assertFalse(receiver.connected)
