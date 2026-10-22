"""Keep input-source access and system-event conversion on the main thread.

pynput 1.8.1 normally opens keycode_context inside Listener._run. macOS 27
terminates the process in TISGetInputSourceProperty when run on that thread.
NSEvent.eventWithCGEvent can also enter HIToolbox's input-source code when
translating system/media events. Copy and decode those on the main queue,
then deliver them on the original listener loop. Ordinary keys stay on their
existing Quartz path. Keep this adapter paired with pinned pynput 1.8.1.
"""
from collections import deque
from contextlib import nullcontext
import sys
import threading

import Foundation as F
import Quartz
import objc
from pynput import keyboard
from pynput.keyboard import _darwin as Darwin
from pynput._util.darwin import ListenerMixin, keycode_context
from .mac_event_tap import create_listener_tap, close_listener_tap


class KeyboardListener(keyboard.Listener):
    def __init__(self, *args, event_context=lambda: None, event_lock=None, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Mac 键盘监听必须在主线程初始化')
        super().__init__(*args, **kwargs)
        self._event_context = event_context
        self._event_lock = event_lock
        self._media_lock = threading.Lock()
        self._media_pending = deque()
        self._media_scheduled = False
        with keycode_context() as context:
            # No native input-source references escape the main thread.
            self._layout_context = context

    def _run(self):
        self._context = self._layout_context
        try:
            ListenerMixin._run(self)
        finally:
            self._context = None
            close_listener_tap(self)

    def _create_event_tap(self):
        options = (Quartz.kCGEventTapOptionListenOnly
                   if not self.suppress and self._intercept is None
                   else Quartz.kCGEventTapOptionDefault)
        return create_listener_tap(self, Quartz.kCGSessionEventTap, options)

    def _handle_message(self, proxy, event_type, event, refcon, injected):
        if event_type != Darwin.NSSystemDefined:
            # keyDown/keyUp/flagsChanged, including ordinary Caps Lock changes,
            # are already decoded directly with Quartz by the pinned adapter.
            return super()._handle_message(proxy, event_type, event, refcon, injected)
        self._flags = Quartz.CGEventGetFlags(event)
        loop = getattr(self, '_loop', None)
        if not self._live_loop(loop):
            return
        with self._event_lock if self._event_lock is not None else nullcontext():
            context = self._event_context()
        # The tap owns the original reference only during this callback.
        copied = Quartz.CGEventCreateCopy(event)
        if copied is None:
            raise RuntimeError('无法复制 Mac 系统按键事件')
        with self._media_lock:
            if not self._live_loop(loop):
                return
            self._media_pending.append((copied, injected, loop, context))
            if self._media_scheduled:
                return
            self._media_scheduled = True
        self._schedule_media_pump()

    def _schedule_media_pump(self):
        try:
            with objc.autorelease_pool():
                F.NSOperationQueue.mainQueue().addOperationWithBlock_(self._drain_media_on_main)
        except Exception:
            self._deferred_failed()

    def _drain_media_on_main(self):
        # One queued pump owns the FIFO; operation-queue scheduling order is
        # not used to order key press/release pairs. Yield between batches so
        # a burst of system events cannot monopolize the UI loop.
        for _ in range(32):
            with self._media_lock:
                if not self._media_pending:
                    self._media_scheduled = False
                    return
                item = self._media_pending.popleft()
            self._decode_media_on_main(*item)
        with self._media_lock:
            if not self._media_pending:
                self._media_scheduled = False
                return
        self._schedule_media_pump()

    def stop(self):
        super().stop()
        with self._media_lock:
            self._media_pending.clear()
            self._media_scheduled = False

    def _live_loop(self, loop):
        return bool(self.running and loop is not None and getattr(self, '_loop', None) is loop)

    def _deferred_failed(self):
        # A deferred block is outside pynput's tap exception wrapper. Preserve
        # its error for join(), stop the listener, and never escape into Cocoa.
        self._queue.put(sys.exc_info())
        self.stop()

    def _decode_media_on_main(self, event, injected, loop, context):
        try:
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError('Mac 系统按键转换必须在主线程执行')
            if not self._live_loop(loop) or self._event_context() != context:
                return
            # Never acquire the engine lock or wait for the HID loop here.
            native = Darwin.NSEvent.eventWithCGEvent_(event)
            if native is None or native.subtype() != Darwin.kSystemDefinedEventMediaKeysSubtype:
                return
            data = int(native.data1())
            key = self._SPECIAL_KEYS.get(((data >> 16) & 0xffff, True))
            transition = (data >> 8) & 0xff
            if key is None or transition not in (0x0a, 0x0b):
                return
            if not self._live_loop(loop):
                return
            Quartz.CFRunLoopPerformBlock(loop, Quartz.kCFRunLoopDefaultMode,
                lambda: self._deliver_media(key, transition == 0x0a, injected, loop, context))
            Quartz.CFRunLoopWakeUp(loop)
        except Exception:
            self._deferred_failed()

    def _deliver_media(self, key, is_press, injected, loop, context):
        try:
            with self._event_lock if self._event_lock is not None else nullcontext():
                if not self._live_loop(loop) or self._event_context() != context:
                    return
                # Preserve the injected flag; the upstream media branch omits it.
                (self.on_press if is_press else self.on_release)(key, injected)
        except self.StopException:
            self.stop()
        except Exception:
            self._deferred_failed()
