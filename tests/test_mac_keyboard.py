"""Keyboard thread regressions; never start a tap, post an event or touch UI."""
from contextlib import contextmanager, ExitStack
import gc
import sys
import threading
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == 'darwin', 'macOS keyboard regression')
class MacKeyboardTests(unittest.TestCase):
    def test_layout_read_on_main_thread_and_only_tap_on_worker(self):
        from sharemouse.mac_keyboard import KeyboardListener
        calls = []
        layout = (40, b'copied-layout-bytes')
        @contextmanager
        def context():
            calls.append(('layout', threading.current_thread()))
            yield layout
        def tap(listener):
            calls.append(('tap', threading.current_thread()))
            self.assertIs(listener._context, layout)
        with patch('sharemouse.mac_keyboard.keycode_context', context), \
             patch('sharemouse.mac_keyboard.ListenerMixin._run', tap):
            listener = KeyboardListener()
            thread = threading.Thread(target=listener._run)
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([name for name, _ in calls], ['layout', 'tap'])
        self.assertIs(calls[0][1], threading.main_thread())
        self.assertIs(calls[1][1], thread)
        self.assertIsNone(listener._context)

    def test_background_construction_fails_before_native_api(self):
        from sharemouse.mac_keyboard import KeyboardListener
        errors = []
        def create():
            try:
                KeyboardListener()
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch('sharemouse.mac_keyboard.keycode_context') as context:
            thread = threading.Thread(target=create)
            thread.start()
            thread.join(2)
            context.assert_not_called()
        self.assertEqual(len(errors), 1)

    def test_real_layout_and_worker_translation_without_input_hooks(self):
        from sharemouse.mac_keyboard import KeyboardListener
        from pynput._util.darwin import keycode_to_string
        listener = KeyboardListener()
        results, errors = [], []
        def tap(instance):
            try:
                results.append(keycode_to_string(instance._context, 0))
            except Exception as exc:
                errors.append(exc)
        with patch('sharemouse.mac_keyboard.ListenerMixin._run', tap):
            thread = threading.Thread(target=listener._run)
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 1)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS keyboard regression')
class MacKeyboardMediaThreadTests(unittest.TestCase):
    """Drive both queues explicitly, so accidental native decoding fails fast."""

    def setUp(self):
        import Foundation
        import Quartz
        from pynput.keyboard import _darwin
        from sharemouse import mac_keyboard

        self.Q, self.D, self.module = Quartz, _darwin, mac_keyboard
        self.main_blocks, self.tap_blocks, self.calls, self.decoded = [], [], [], []
        self.context = (3, True, False)
        self.state_lock = threading.RLock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch(
            'sharemouse.mac_keyboard.keycode_context',
            lambda: self.layout_context()))

        # No actual NSOperationQueue is run, and no CFRunLoop is awakened.
        operation_queue = Mock()
        operation_queue.addOperationWithBlock_.side_effect = self.main_blocks.append
        operation_queue.waitUntilAllOperationsAreFinished.side_effect = AssertionError(
            'the HID listener must never wait for the main queue')
        self.stack.enter_context(patch.object(
            Foundation, 'NSOperationQueue',
            SimpleNamespace(mainQueue=lambda: operation_queue)))
        self.copy = self.stack.enter_context(patch.object(
            Quartz, 'CGEventCreateCopy', side_effect=lambda event: dict(event)))
        self.perform = self.stack.enter_context(patch.object(
            Quartz, 'CFRunLoopPerformBlock',
            side_effect=lambda loop, mode, callback: self.tap_blocks.append((loop, callback))))
        self.wake = self.stack.enter_context(patch.object(Quartz, 'CFRunLoopWakeUp'))

        native_event = SimpleNamespace(eventWithCGEvent_=self.decode_on_main)
        self.stack.enter_context(patch.object(_darwin, 'NSEvent', native_event))
        self.stack.enter_context(patch.object(Quartz, 'NSEvent', native_event))
        # Also cover adapters which import the class directly into their module.
        if hasattr(mac_keyboard, 'NSEvent'):
            self.stack.enter_context(patch.object(mac_keyboard, 'NSEvent', native_event))
        readers = dict(
            CGEventGetIntegerValueField=lambda event, field: event.get('vk', 0),
            CGEventGetType=lambda event: event['type'],
            CGEventGetFlags=lambda event: event.get('flags', 0),
            CGEventKeyboardGetUnicodeString=lambda event, *args: (
                len(event.get('text', '')), event.get('text', '')))
        for namespace in (Quartz, _darwin):
            for name, callback in readers.items():
                self.stack.enter_context(patch.object(namespace, name, side_effect=callback))

    @contextmanager
    def layout_context(self):
        yield (40, b'copied-layout-bytes')

    def listener(self, on_press=None, on_release=None):
        def record(kind):
            return lambda key, injected=False: self.calls.append((
                kind, key, injected, threading.current_thread(),
                self.state_lock._is_owned()))
        listener = self.module.KeyboardListener(
            on_press=on_press or record('press'),
            on_release=on_release or record('release'),
            event_context=lambda: self.context,
            event_lock=self.state_lock)
        listener._running = True
        listener._loop = object()
        listener._stop_platform = Mock()
        listener._log = Mock()
        return listener

    def decode_on_main(self, event):
        self.assertIs(threading.current_thread(), threading.main_thread(),
                      'NSEvent conversion on HID thread reproduces the SIGTRAP')
        self.decoded.append(event)
        return SimpleNamespace(subtype=lambda: event.get('subtype', 8),
                               data1=lambda: event['data1'])

    def on_worker(self, callback):
        errors = []
        def run():
            try:
                callback()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive(), 'listener blocked waiting for AppKit')
        self.assertEqual(errors, [], 'exception escaped a native callback boundary')
        return worker

    def media(self, listener, *, code=16, state=0x0a, injected=False, subtype=8):
        event = dict(type=self.Q.NSSystemDefined,
                     data1=(code << 16) | (state << 8), subtype=subtype)
        worker = self.on_worker(lambda: listener._handle_message(
            None, event['type'], event, None, injected))
        return event, worker

    def decode_next(self):
        self.assertTrue(self.main_blocks)
        self.main_blocks.pop(0)()

    def deliver_next(self):
        self.assertTrue(self.tap_blocks)
        loop, block = self.tap_blocks.pop(0)
        worker = self.on_worker(block)
        return loop, worker

    def test_system_event_is_copied_without_blocking_or_decoding_on_hid(self):
        listener = self.listener()
        event, worker = self.media(listener)
        self.copy.assert_called_once_with(event)
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.main_blocks), 1)
        self.assertEqual(self.tap_blocks, [])
        # The copied event remains valid after the event-tap callback returns.
        event['data1'] = 0
        self.decode_next()
        self.assertEqual(self.decoded[0]['data1'], (16 << 16) | 0x0a00)
        self.assertEqual(self.calls, [])
        loop, delivery_worker = self.deliver_next()
        self.assertIs(loop, listener._loop)
        self.assertEqual(self.calls[0][:3], ('press', self.D.Key.media_play_pause, False))
        self.assertIs(self.calls[0][3], delivery_worker)
        self.assertTrue(self.calls[0][4], 'stale check and delivery must share the engine lock')
        self.assertIsNot(worker, threading.main_thread())
        self.wake.assert_called_once_with(loop)

    def test_media_release_preserves_injected_flag(self):
        listener = self.listener()
        self.media(listener, code=0, state=0x0b, injected=True)
        self.decode_next()
        self.deliver_next()
        self.assertEqual(self.calls[0][:3], ('release', self.D.Key.media_volume_up, True))

    def test_media_fifo_uses_one_main_pump_across_batch_boundaries(self):
        listener = self.listener()
        events, expected = [], []
        for index in range(65):
            code = 16 if (index // 2) % 2 == 0 else 0
            pressed, injected = index % 2 == 0, index % 3 == 1
            events.append(dict(type=self.Q.NSSystemDefined, marker=index,
                               data1=(code << 16) | ((0x0a if pressed else 0x0b) << 8)))
            key = self.D.Key.media_play_pause if code == 16 else self.D.Key.media_volume_up
            expected.append(('press' if pressed else 'release', key, injected))
        def enqueue_all():
            for index, event in enumerate(events):
                listener._handle_message(None, event['type'], event, None, index % 3 == 1)
        self.on_worker(enqueue_all)
        self.assertEqual(self.copy.call_count, 65)
        self.assertEqual(len(self.main_blocks), 1,
                         'one NSOperationQueue operation per key may reorder press/release')
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.tap_blocks, [])

        for decoded_count in (32, 64, 65):
            # A scheduler may select any ready operation. There must be only
            # one media pump ready, independent of its ready-operation order.
            self.assertEqual(len(self.main_blocks), 1)
            self.main_blocks.pop()()
            self.assertEqual([event['marker'] for event in self.decoded],
                             list(range(decoded_count)))
            self.assertEqual(len(self.tap_blocks), decoded_count)
            self.assertEqual(self.calls, [])
        self.assertEqual(self.main_blocks, [])
        while self.tap_blocks:
            loop, _ = self.deliver_next()
            self.assertIs(loop, listener._loop)
        self.assertEqual([call[:3] for call in self.calls], expected)
        self.assertTrue(all(call[4] for call in self.calls))

        # Draining must release the scheduled flag so the next event can wake
        # the pump; otherwise keys silently stop after the first burst.
        self.media(listener, code=16, state=0x0b)
        self.assertEqual(len(self.main_blocks), 1)
        self.decode_next()
        self.deliver_next()
        self.assertEqual(self.calls[-1][:3], ('release', self.D.Key.media_play_pause, False))

    def test_media_stop_releases_copied_events_before_main_queue_runs(self):
        class CopiedEvent(dict):
            pass
        copied_references = []
        def copy_event(event):
            copied = CopiedEvent(event)
            copied_references.append(weakref.ref(copied))
            return copied
        self.copy.side_effect = copy_event
        listener = self.listener()
        for _ in range(4):
            self.media(listener)
        self.assertTrue(all(reference() is not None for reference in copied_references))
        self.assertEqual(len(self.main_blocks), 1)
        listener.stop()
        gc.collect()
        self.assertTrue(all(reference() is None for reference in copied_references),
                        'stopped listeners must not retain native CGEvent copies')
        self.decode_next()
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.tap_blocks, [])
        self.assertEqual(self.calls, [])

    def test_unknown_subtype_and_media_code_are_ignored(self):
        listener = self.listener()
        for kwargs in ({'subtype': 9}, {'code': 0xfffe}):
            with self.subTest(kwargs=kwargs):
                self.media(listener, **kwargs)
                self.decode_next()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.tap_blocks, [])

    def test_unrecognized_media_state_is_not_a_release(self):
        listener = self.listener()
        self.media(listener, state=0x08)
        self.decode_next()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tap_blocks, [])

    def test_normal_unicode_and_key_up_stay_on_listener_without_appkit(self):
        listener = self.listener()
        event = dict(type=self.Q.kCGEventKeyDown, vk=0, text='é')
        worker = self.on_worker(lambda: listener._handle_message(
            None, event['type'], event, None, True))
        self.assertEqual(self.calls[0][0], 'press')
        self.assertEqual(self.calls[0][1].char, 'é')
        self.assertTrue(self.calls[0][2])
        self.assertIs(self.calls[0][3], worker)
        event['type'] = self.Q.kCGEventKeyUp
        self.on_worker(lambda: listener._handle_message(None, event['type'], event, None, False))
        self.assertEqual(self.calls[1][0], 'release')
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.main_blocks, [])
        self.copy.assert_not_called()

    def test_caps_lock_remains_paired_without_nsevent_conversion(self):
        listener = self.listener()
        event = dict(type=self.Q.kCGEventFlagsChanged, vk=0x39,
                     flags=self.Q.kCGEventFlagMaskAlphaShift)
        worker = self.on_worker(lambda: listener._handle_message(
            None, event['type'], event, None, False))
        self.assertEqual([call[:3] for call in self.calls], [
            ('press', self.D.Key.caps_lock, False),
            ('release', self.D.Key.caps_lock, False)])
        self.assertTrue(all(call[3] is worker for call in self.calls))
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.main_blocks, [])

    def test_left_and_right_modifiers_keep_native_key_identity(self):
        listener = self.listener()
        for code, key in ((0x38, self.D.Key.shift_l), (0x3c, self.D.Key.shift_r)):
            for flags, kind in ((self.Q.kCGEventFlagMaskShift, 'press'), (0, 'release')):
                event = dict(type=self.Q.kCGEventFlagsChanged, vk=code, flags=flags)
                self.on_worker(lambda: listener._handle_message(
                    None, event['type'], event, None, False))
                self.assertEqual(self.calls[-1][:3], (kind, key, False))
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.main_blocks, [])

    def test_stop_before_main_decode_never_delivers(self):
        listener = self.listener()
        self.media(listener)
        listener.stop()
        self.decode_next()
        while self.tap_blocks:
            self.deliver_next()
        self.assertEqual(self.calls, [])
        self.assertFalse(listener.running)

    def test_stop_after_decode_never_delivers(self):
        listener = self.listener()
        self.media(listener)
        self.decode_next()
        listener.stop()
        self.deliver_next()
        self.assertEqual(self.calls, [])

    def test_changed_context_before_main_decode_never_calls_appkit(self):
        listener = self.listener()
        self.media(listener)
        self.context = (4, True, False)
        self.decode_next()
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.tap_blocks, [])

    def test_main_decode_never_acquires_engine_lock(self):
        listener = self.listener()
        lock_threads = []
        @contextmanager
        def listener_only_lock():
            current = threading.current_thread()
            self.assertIsNot(current, threading.main_thread(),
                             'main queue must not wait on the listener engine lock')
            lock_threads.append(current)
            with self.state_lock:
                yield
        # A reusable context manager is required for the two separate accesses.
        class Lock:
            def __enter__(self):
                self.context = listener_only_lock()
                return self.context.__enter__()
            def __exit__(self, *args):
                return self.context.__exit__(*args)
        listener._event_lock = Lock()
        self.media(listener)
        self.decode_next()
        self.deliver_next()
        self.assertEqual(len(lock_threads), 2)
        self.assertEqual(len(self.calls), 1)

    def test_main_decode_exception_is_saved_and_stops_without_escaping(self):
        listener = self.listener()
        self.media(listener)
        with patch.object(self.D.NSEvent, 'eventWithCGEvent_',
                          side_effect=RuntimeError('native bridge test failure')):
            self.decode_next()
        self.assertFalse(listener.running)
        self.assertEqual(self.tap_blocks, [])
        exc_type, exc, traceback = listener._queue.get_nowait()
        self.assertIs(exc_type, RuntimeError)
        self.assertEqual(str(exc), 'native bridge test failure')
        self.assertIsNotNone(traceback)
        listener._stop_platform.assert_called_once()

    def test_main_block_scheduled_on_wrong_thread_fails_before_native_conversion(self):
        listener = self.listener()
        self.media(listener)
        self.on_worker(self.main_blocks.pop(0))
        self.assertEqual(self.decoded, [])
        self.assertEqual(self.tap_blocks, [])
        self.assertFalse(listener.running)

    def test_run_loop_schedule_failure_stops_without_escaping_main_queue(self):
        listener = self.listener()
        self.media(listener)
        self.perform.side_effect = RuntimeError('test loop scheduling failure')
        self.decode_next()
        self.assertFalse(listener.running)
        self.wake.assert_not_called()
        listener._stop_platform.assert_called_once()

    def test_changed_epoch_remote_or_pending_state_discards_delayed_key(self):
        listener = self.listener()
        for new_context in ((4, True, False), (3, False, False), (3, True, True)):
            with self.subTest(new_context=new_context):
                self.context = (3, True, False)
                self.media(listener)
                self.decode_next()
                self.context = new_context
                self.deliver_next()
                self.assertEqual(self.calls, [])

    def test_replaced_run_loop_discards_old_media_callback(self):
        listener = self.listener()
        self.media(listener)
        self.decode_next()
        listener._loop = object()
        self.deliver_next()
        self.assertEqual(self.calls, [])

    def test_callback_exception_stops_listener_without_escaping_block(self):
        def fail(key, injected=False):
            raise RuntimeError('test callback failure')
        listener = self.listener(on_press=fail)
        self.media(listener)
        self.decode_next()
        self.deliver_next()
        self.assertFalse(listener.running)
        listener._stop_platform.assert_called_once()

    def test_false_callback_stops_listener_without_escaping_block(self):
        listener = self.listener(on_press=lambda key, injected=False: False)
        self.media(listener)
        self.decode_next()
        self.deliver_next()
        self.assertFalse(listener.running)
        listener._stop_platform.assert_called_once()
