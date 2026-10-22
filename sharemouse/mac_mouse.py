"""Use Quartz coordinates consistently, including queued move/click sequences."""
import Quartz
import ctypes
import time
import threading
from AppKit import NSEvent
from pynput import mouse
from .event_time import ReturnEventFilter
from .mac_event_tap import create_listener_tap, close_listener_tap


mach_absolute_time = ctypes.CDLL('/usr/lib/libSystem.B.dylib').mach_absolute_time
mach_absolute_time.argtypes = []
mach_absolute_time.restype = ctypes.c_uint64

MOTION_EVENTS = (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged,
                 Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged)


class MouseListener(mouse.Listener):
    """Keep native deltas; absolute locations can be stale or clipped.

    pynput's on_move callback only exposes the absolute event location. Read
    deltas before that information is discarded. ListenerMixin still handles
    injected-event detection, callback errors and event suppression.
    """
    def __init__(self, *args, on_delta, on_failure=None, **kwargs):
        self.on_delta = on_delta
        self.on_failure = on_failure or (lambda reason: None)
        self._capture_lock = threading.RLock()
        self._captured = False
        self._cursor_hidden = False
        self._mouse_disassociated = False
        self._hidden_display = None
        self._capture_generation = 0
        self._return_position = None
        self._tap = None
        self._return_filter = ReturnEventFilter(lambda: mach_absolute_time())
        self._release_callbacks = []
        super().__init__(*args, **kwargs)

    def _create_event_tap(self):
        # Consume hardware events before the session cursor/apps process them.
        self._tap = create_listener_tap(
            self, Quartz.kCGHIDEventTap, Quartz.kCGEventTapOptionDefault)
        return self._tap

    def _run(self):
        try:
            super()._run()
        finally:
            # Also runs when a tap fails, permissions vanish or stop interrupts
            # the run loop before a queued return operation can execute.
            try:
                self._release_native()
            finally:
                try:
                    self._released_callbacks()
                finally:
                    close_listener_tap(self)
                    self._tap = None

    def _handler(self, proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            self._release_native()
            self.on_failure('Mac 鼠标监听被系统停用，已恢复本机控制')
            self.stop()
            return event
        if (event_type in MOTION_EVENTS and
                not Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) and
                self._return_filter.stale(Quartz.CGEventGetTimestamp(event))):
            # Consume old native events as well as skipping their callbacks;
            # otherwise they can undo the return warp in the system cursor.
            return None
        was_captured = self._captured
        result = super()._handler(proxy, event_type, event, refcon)
        if was_captured or self._captured:
            return None
        return result

    def capture_mouse(self):
        # Called by the input callback on this native run-loop thread. Freeze
        # at the existing position: no entry warp, hence no warp-induced delta.
        with self._capture_lock:
            self._capture_generation += 1
            if self._captured:
                return
            if self._cursor_hidden or self._mouse_disassociated:
                self._release_native()
            point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            self._return_position = (point.x, point.y)
            self._capture_native()

    def _capture_native(self):
        display = Quartz.CGMainDisplayID()
        result = Quartz.CGDisplayHideCursor(display)
        if result != Quartz.kCGErrorSuccess:
            raise RuntimeError('无法隐藏 Mac 指针：' + str(result))
        self._cursor_hidden, self._hidden_display = True, display
        try:
            self._mouse_disassociated = True
            result = Quartz.CGAssociateMouseAndMouseCursorPosition(False)
            if result != Quartz.kCGErrorSuccess:
                raise RuntimeError('无法捕获 Mac 鼠标：' + str(result))
        except Exception:
            self._release_native()
            raise
        self._captured = True

    def begin_capture(self, park):
        """Freeze dispatch immediately; AppKit activation must not block this tap."""
        with self._capture_lock:
            self._capture_generation += 1
            generation = self._capture_generation
            point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            self._return_position = (point.x, point.y)
            self._captured = True
            return generation

    def finish_capture(self, generation, park, completed):
        """Only called after AppKit confirms our panel owns foreground focus."""
        def finish():
            error = None
            with self._capture_lock:
                if generation != self._capture_generation or not self._captured:
                    error = RuntimeError('Mac 捕获请求已过期')
                else:
                    try:
                        # The foreground blank panel covers the departure
                        # point. Keep that position: a warp can be folded into
                        # the next hardware delta and immediately return across
                        # the remote entry seam.
                        self._capture_native()
                    except Exception as exc:
                        error = exc
                        try:
                            self._release_native()
                        except Exception as cleanup:
                            error = RuntimeError(str(exc) + '; ' + str(cleanup))
            try:
                completed(error)
            except Exception as exc:
                try:
                    self.on_failure('Mac 捕获完成回调失败：' + str(exc))
                except Exception:
                    pass
        loop = getattr(self, '_loop', None)
        if loop is None:
            completed(RuntimeError('Mac 鼠标事件循环尚未就绪'))
        elif threading.current_thread() is self:
            finish()
        else:
            Quartz.CFRunLoopPerformBlock(loop, Quartz.kCFRunLoopDefaultMode, finish)
            Quartz.CFRunLoopWakeUp(loop)

    def release_mouse(self, position=None, after=None):
        with self._capture_lock:
            self._capture_generation += 1
            generation = self._capture_generation
            if position is not None:
                self._return_position = tuple(position)
            if after is not None:
                if not hasattr(self, '_release_callbacks'):
                    self._release_callbacks = []
                self._release_callbacks.append(after)

        def release():
            error = None
            performed = False
            with self._capture_lock:
                if generation == self._capture_generation:
                    performed = True
                    try:
                        self._release_native()
                    except Exception as exc:
                        error = exc
            if error is not None:
                # Never let a Python exception escape a CFRunLoop block.
                self.on_failure('Mac 光标恢复失败：' + str(error))
            if performed:
                self._released_callbacks()

        loop = getattr(self, '_loop', None)
        if loop is not None and threading.current_thread() is not self:
            # Network/F12/UI callbacks must not warp concurrently with this tap.
            Quartz.CFRunLoopPerformBlock(loop, Quartz.kCFRunLoopDefaultMode, release)
            Quartz.CFRunLoopWakeUp(loop)
        elif threading.current_thread() is self or not self.is_alive():
            release()

    def _released_callbacks(self):
        with self._capture_lock:
            callbacks = getattr(self, '_release_callbacks', [])
            self._release_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def _release_native(self):
        with self._capture_lock:
            if not (self._captured or self._cursor_hidden or self._mouse_disassociated):
                return
            errors = []
            was_captured = self._captured or self._mouse_disassociated
            try:
                if self._mouse_disassociated:
                    result = Quartz.CGAssociateMouseAndMouseCursorPosition(True)
                    if result != Quartz.kCGErrorSuccess:
                        raise RuntimeError('无法重新连接 Mac 鼠标与指针：' + str(result))
                    self._mouse_disassociated = False
            except Exception as exc:
                errors.append(exc)
            try:
                if was_captured and self._return_position is not None:
                    result = Quartz.CGWarpMouseCursorPosition(self._return_position)
                    if result != Quartz.kCGErrorSuccess:
                        raise RuntimeError('无法恢复 Mac 指针位置：' + str(result))
            except Exception as exc:
                errors.append(exc)
            finally:
                if self._cursor_hidden:
                    try:
                        result = Quartz.CGDisplayShowCursor(self._hidden_display)
                        if result != Quartz.kCGErrorSuccess:
                            raise RuntimeError('无法显示 Mac 指针：' + str(result))
                        self._cursor_hidden = False
                    except Exception as exc:
                        errors.append(exc)
                self._captured = False
                # Hardware timestamps observed at the HID tap on ARM are raw
                # Mach ticks; CGEventPost timestamps are nanoseconds. Keep both
                # domains and end filtering at the first fresh hardware event.
                self._return_filter.mark_return()
            if errors:
                raise RuntimeError('; '.join(map(str, errors)))

    def _handle_message(self, proxy, event_type, event, refcon, injected):
        if event_type in MOTION_EVENTS:
            if not injected and self._return_filter.stale(Quartz.CGEventGetTimestamp(event)):
                return
            point = Quartz.CGEventGetLocation(event)
            self.on_delta(point.x, point.y,
                          Quartz.CGEventGetDoubleValueField(event, Quartz.kCGMouseEventDeltaX),
                          Quartz.CGEventGetDoubleValueField(event, Quartz.kCGMouseEventDeltaY),
                          injected)
        else:
            super()._handle_message(proxy, event_type, event, refcon, injected)


class MouseController(mouse.Controller):
    def __init__(self):
        super().__init__()
        self._event_position = None
        self._click_history = {}
        self._active_counts = {}
        self._double_click_interval = float(NSEvent.doubleClickInterval()) or .5

    def _position_set(self, position):
        if self._drag_button in self._click_history:
            _, _, start = self._click_history[self._drag_button]
            if abs(position[0] - start[0]) > 4 or abs(position[1] - start[1]) > 4:
                del self._click_history[self._drag_button]
        super()._position_set(position)

    def _press(self, button):
        position = tuple(self.position)
        now = time.monotonic()
        previous = self._click_history.get(button)
        count = 1
        if previous:
            old_count, when, old_position = previous
            if (now - when <= self._double_click_interval and
                    abs(position[0] - old_position[0]) <= 4 and
                    abs(position[1] - old_position[1]) <= 4):
                count = old_count + 1
        self._click_history[button] = (count, now, position)
        self._active_counts[button] = count
        (press, _, _), number = button.value
        event = Quartz.CGEventCreateMouseEvent(None, press, position, number)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, count)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        self._drag_button = button

    def _release(self, button):
        (_, release, _), number = button.value
        event = Quartz.CGEventCreateMouseEvent(None, release, self.position, number)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState,
                                         self._active_counts.pop(button, 1))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        if button == self._drag_button:
            self._drag_button = None

    def _position_get(self):
        if self._event_position is not None:
            return self._event_position
        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return point.x, point.y

    def warp(self, position):
        # Capturing/returning the local pointer must not queue another motion
        # event. Remote receiver moves still use _position_set for app delivery.
        result = Quartz.CGWarpMouseCursorPosition(position)
        if result != Quartz.kCGErrorSuccess:
            raise RuntimeError('Mac 鼠标定位失败：' + str(result))

    def button_at(self, button, down, position):
        # CGEventPost is queued. Do not read the OS cursor back immediately
        # after a move: it may still report the previous location.
        self._event_position = tuple(position)
        try:
            (self.press if down else self.release)(button)
        finally:
            self._event_position = None
