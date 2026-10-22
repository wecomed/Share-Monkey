"""Concurrent clipboard polling must not invert the receiver's lock order."""
import threading
import unittest
from unittest.mock import Mock, patch

from sharemouse.core import Screen
from sharemouse.engine import Engine


class DeadlineLock:
    """Turn a regression deadlock into a bounded test failure."""
    def __init__(self):
        self.lock = threading.Lock()

    def __enter__(self):
        if not self.lock.acquire(timeout=.5):
            raise TimeoutError('clipboard lock order deadlock')
        return self

    def __exit__(self, *_):
        self.lock.release()


class SinglePoll(threading.Event):
    def __init__(self, receiver_has_state_lock):
        super().__init__()
        self.receiver_has_state_lock = receiver_has_state_lock
        self.polled = False

    def wait(self, timeout=None):
        if self.polled:
            return True
        self.polled = True
        if not self.receiver_has_state_lock.wait(1):
            raise TimeoutError('receiver did not start')
        return False


class ClipboardLockingTests(unittest.TestCase):
    def test_simultaneous_lan_send_and_receive_complete_without_deadlock(self):
        engine = Engine(True, Screen(1920, 1080), 'right', 'clipboard-test-password',
                        '127.0.0.1', 24809, Mock(), clipboard=True)
        engine.connected = True
        engine.clip_lock = DeadlineLock()
        state_held, sending, received = threading.Event(), threading.Event(), threading.Event()
        engine.stop_event = SinglePoll(state_held)
        errors = []
        original_queue = engine.queue_clipboard

        def queue_clipboard(value):
            # Receiver already owns state_lock. The sender will now need it.
            sending.set()
            return original_queue(value)

        def receive():
            try:
                with engine.state_lock:
                    state_held.set()
                    if not sending.wait(1):
                        raise TimeoutError('poller did not reach send')
                    engine.receive({'type': 'clipboard', 'text': 'remote text'})
                    received.set()
            except Exception as exc:
                errors.append(exc)

        with patch('pyperclip.paste', side_effect=['initial text', 'local change']), \
                patch('pyperclip.copy') as copy, \
                patch.object(engine, 'queue_clipboard', side_effect=queue_clipboard):
            receiver = threading.Thread(target=receive, daemon=True)
            poller = threading.Thread(target=engine.clipboard_loop, daemon=True)
            receiver.start()
            poller.start()
            receiver.join(2)
            poller.join(2)
        self.assertFalse(receiver.is_alive())
        self.assertFalse(poller.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(received.is_set())
        copy.assert_called_once_with('remote text')
        self.assertEqual(engine.outgoing.get_nowait(), {'type': 'clipboard', 'text': 'local change'})
        self.assertTrue(engine.outgoing.empty())
        engine.status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
