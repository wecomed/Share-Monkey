"""Own the native tap callback without PyObjC 12.2.2's leaking trampoline.

The incoming CGEvent is borrowed. A returned replacement needs one native
reference for CoreGraphics; the original event must never be released here.
Keep the callback alive until the listener's run loop has exited.
"""
import ctypes
import sys

import objc
import Quartz


_CG = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
_CF = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_void_p)
_CG.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_uint64, _CALLBACK, ctypes.c_void_p]
_CG.CGEventTapCreate.restype = ctypes.c_void_p
_CG.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
_CG.CGEventTapEnable.restype = None
_CF.CFRetain.argtypes = [ctypes.c_void_p]
_CF.CFRetain.restype = ctypes.c_void_p
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFRelease.restype = None
_CF.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
_CF.CFMachPortInvalidate.restype = None


class EventTap:
    def __init__(self, handler, on_error):
        self._handler = handler
        self._on_error = on_error
        self.port = None
        self.callback = _CALLBACK(self._invoke)

    def _invoke(self, proxy, event_type, pointer, refcon):
        # No Python exception may cross a C callback, including cleanup errors.
        try:
            with objc.autorelease_pool():
                event = objc.objc_object(c_void_p=pointer) if pointer else None
                result = self._handler(proxy, event_type, event, None)
                if result is None:
                    return None
                if not isinstance(result, Quartz.CGEventRef):
                    raise TypeError('Event tap must return a CGEvent or None')
                returned = objc.pyobjc_id(result)
                if returned != pointer:
                    # CoreGraphics consumes this reference after dispatch.
                    _CF.CFRetain(returned)
                return returned
        except BaseException:
            try:
                self._on_error()
            except BaseException:
                pass
            return pointer  # Preserve local input if a callback fails.

    def create(self, location, placement, options, mask):
        raw = _CG.CGEventTapCreate(location, placement, options, mask,
                                   self.callback, None)
        if raw:
            try:
                self.port = objc.objc_object(c_void_p=raw)
            finally:
                _CF.CFRelease(raw)  # Balance Create; the proxy owns its reference.
        return self.port

    def close(self):
        """Call on the listener thread after its run loop has returned."""
        try:
            if self.port is not None:
                # PyInstaller's lazy Quartz export resolution can already be
                # unavailable while a listener exits during GUI shutdown.
                pointer = objc.pyobjc_id(self.port)
                _CG.CGEventTapEnable(pointer, False)
                _CF.CFMachPortInvalidate(pointer)
        finally:
            self.port = None
            self._handler = None
            self._on_error = None
            self.callback = None


def create_listener_tap(listener, location, options):
    def failed():
        listener._queue.put(sys.exc_info())
        listener.stop()

    bridge = EventTap(listener._handler, failed)
    listener._event_tap_bridge = bridge
    return bridge.create(location, Quartz.kCGHeadInsertEventTap, options,
                         listener._EVENTS)


def close_listener_tap(listener):
    bridge = getattr(listener, '_event_tap_bridge', None)
    if bridge is not None:
        try:
            bridge.close()
        finally:
            listener._event_tap_bridge = None
