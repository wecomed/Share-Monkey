"""Foreground-owned Mac capture shield using public AppKit APIs.

Quartz cursor hiding/capture is foreground-scoped. A successful CGError alone
does not prove the hardware cursor disappeared. During remote control a small
transparent key panel owns the parked cursor; the previous application is
restored after native cursor release. No work here runs on the HID callback.
"""
import os
import threading
import time

_COORDINATOR = None
_CLASSES = None


class CaptureAnchorChanged(RuntimeError):
    """An already-forwarded event moved the cursor while capture settled."""


class CaptureFocusLost(RuntimeError):
    """Foreground ownership changed; end this capture without closing transport."""


class CaptureToken:
    def __init__(self, generation, park):
        self.generation, self.park = generation, tuple(park)
        self.initial_park = self.park
        self.anchor_retries = 0
        self.ready = threading.Event()
        self.cancelled = False
        self.error = None
        self.prepared = False
        self.deadline = time.monotonic() + 1.2

    def cancel(self):
        self.cancelled = True
        self.error = RuntimeError('Mac 远程捕获已取消')
        self.ready.set()

    def wait(self, stop_event, timeout=1.5, cancelled=lambda: False):
        deadline = time.monotonic() + timeout
        while not self.ready.wait(.01):
            if stop_event.is_set() or cancelled():
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError('Mac 前台捕获尚未完成，已停止向对方发送输入')
        if stop_event.is_set() or cancelled():
            return False
        if self.error is not None:
            raise RuntimeError(str(self.error)) from self.error
        return self.prepared and not self.cancelled


class CaptureCoordinator:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()
        self.generation = 0
        self.current = None

    def token(self, point):
        # park_for_point reads cached geometry, never AppKit, on the HID thread.
        with self.lock:
            self.generation += 1
            if self.current is not None:
                self.current.cancel()
            token = CaptureToken(self.generation, self.backend.park_for_point(point))
            self.current = token
            return token

    def acquire(self, token, native_prepare, on_failure):
        token.native_prepare, token.on_failure = native_prepare, on_failure
        self.backend.dispatch(lambda: self._activate(token))

    def _current(self, token):
        with self.lock:
            return self.current is token and not token.cancelled

    def _activate(self, token):
        if not self._current(token):
            return
        try:
            self.backend.activate(token.park)
            self._check_activation(token)
        except Exception as exc:
            self._fail(token, exc)

    def _check_activation(self, token):
        if not self._current(token):
            return
        try:
            if not self.backend.owns_foreground():
                if time.monotonic() >= token.deadline:
                    raise RuntimeError('macOS 未允许 Share Monkey 获得临时捕获焦点')
                self.backend.later(.01, lambda: self._check_activation(token))
                return
            self.backend.blank_cursor()
            token.native_prepare(token.park, lambda error=None: self._native_prepared(token, error))
        except Exception as exc:
            self._fail(token, exc)

    def _native_prepared(self, token, error):
        # Native capture completes on the HID loop, then verify foreground on
        # AppKit again before permitting the network writer to send enter.
        self.backend.dispatch(lambda: self._finish_prepare(token, error))

    def _finish_prepare(self, token, error):
        if not self._current(token):
            return
        try:
            if error is not None:
                raise error
            if not self.backend.owns_foreground():
                raise CaptureFocusLost('Mac 捕获期间前台焦点被其他程序接管')
            # App activation is asynchronous. Events forwarded before the HID
            # capture became active can land after the initial edge sample.
            # Cover the actual frozen cursor, never warp it to the old sample.
            token.park = tuple(self.backend.align_to_cursor())
            if not self._current(token):
                return
            self.backend.blank_cursor()
            self.backend.verify_park(token.park)
            if not self._current(token):
                return
            token.prepared = True
            token.ready.set()
            self.backend.later(.05, lambda: self._monitor(token))
        except CaptureAnchorChanged as exc:
            if self._current(token) and token.anchor_retries < 3:
                token.anchor_retries += 1
                self.backend.later(.01, lambda: self._finish_prepare(token, None))
            else:
                self._fail(token, exc)
        except Exception as exc:
            self._fail(token, exc)

    def _monitor(self, token):
        if not self._current(token) or not token.prepared:
            return
        try:
            if not self.backend.owns_foreground():
                raise CaptureFocusLost('Mac 捕获焦点已离开，已结束本次捕获')
            token.park = tuple(self.backend.align_to_cursor())
            if not self._current(token):
                return
            # AppKit may reset the application cursor after a queued cursor
            # update. The view handles those events; also renew the blank
            # cursor while (and only while) we still own the foreground.
            self.backend.blank_cursor()
            self.backend.later(.05, lambda: self._monitor(token))
        except Exception as exc:
            self._fail(token, exc)

    def _fail(self, token, error):
        if not self._current(token):
            return
        token.error = error
        # A recoverable focus loss first publishes remote=False through the
        # input callback. Waking a waiting writer before that state transition
        # would turn this intentional return into a fatal capture exception.
        try:
            token.on_failure('Mac 捕获失败：' + str(error))
        except Exception:
            self.release(token)
        finally:
            token.ready.set()

    def release(self, token):
        if token is None:
            return
        token.cancelled = True
        if not token.ready.is_set():
            token.cancel()
        self.backend.dispatch(lambda: self._release(token))

    def _release(self, token):
        with self.lock:
            if self.current is not token:
                return
            self.current = None
        self.backend.release()

    def close(self):
        with self.lock:
            token = self.current
        if token is not None:
            self.release(token)


def _classes(A, objc):
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES

    class ShareMonkeyCapturePanel(A.NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

        def constrainFrameRect_toScreen_(self, frame, screen):
            # The cursor may be on an outer edge or above a Dock/menu bar.
            # Keep the capture view centred on that exact point, including
            # the off-screen part; moving the pointer to fit a window would
            # contaminate the next relative mouse delta.
            return frame

    class ShareMonkeyCaptureView(A.NSView):
        def acceptsFirstResponder(self):
            return True

        def updateTrackingAreas(self):
            previous = getattr(self, '_cursor_tracking', None)
            if previous is not None:
                self.removeTrackingArea_(previous)
            objc.super(ShareMonkeyCaptureView, self).updateTrackingAreas()
            options = (A.NSTrackingCursorUpdate | A.NSTrackingMouseEnteredAndExited |
                       A.NSTrackingActiveAlways | A.NSTrackingInVisibleRect |
                       A.NSTrackingEnabledDuringMouseDrag)
            self._cursor_tracking = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                self.bounds(), options, self, None)
            self.addTrackingArea_(self._cursor_tracking)

        def resetCursorRects(self):
            self.addCursorRect_cursor_(self.bounds(), self.blank)

        def cursorUpdate_(self, event):
            window = self.window()
            # A delayed event from a dismissed panel must never leave the
            # restored local application with our transparent cursor.
            if (getattr(self, 'capture_active', False) and window is not None
                    and window.isVisible() and window.isKeyWindow()):
                self.blank.set()
                self.cursor_update_count = getattr(self, 'cursor_update_count', 0) + 1

        def mouseEntered_(self, event):
            self.cursorUpdate_(event)

    _CLASSES = ShareMonkeyCapturePanel, ShareMonkeyCaptureView
    return _CLASSES


class AppKitShield:
    def __init__(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Mac 捕获界面必须在主线程初始化')
        import AppKit as A
        import Foundation as F
        import Quartz as Q
        import objc
        self.A, self.F, self.Q = A, F, Q
        self.Panel, self.View = _classes(A, objc)
        self.app = A.NSApplication.sharedApplication()
        self.panel = self.view = self.blank = None
        self.previous_app = self.previous_cursor = self.previous_key = None
        self.active = False
        self.cursor_assertions = self.cursor_replacements_detected = 0
        self.geometry = []
        main_height = Q.CGDisplayBounds(Q.CGMainDisplayID()).size.height
        for screen in A.NSScreen.screens():
            display = int(screen.deviceDescription()['NSScreenNumber'])
            bounds, visible = Q.CGDisplayBounds(display), screen.visibleFrame()
            park = (visible.origin.x + visible.size.width / 2,
                    main_height - visible.origin.y - visible.size.height / 2)
            self.geometry.append((bounds, park))
        if not self.geometry:
            raise RuntimeError('无法读取 Mac 显示器的安全捕获区域')

    def dispatch(self, callback):
        def invoke():
            try:
                callback()
            except Exception:
                # Coordinator callbacks report their own failures; never let a
                # Python exception unwind through an Objective-C block.
                pass
        if threading.current_thread() is threading.main_thread():
            invoke()
        else:
            self.F.NSOperationQueue.mainQueue().addOperationWithBlock_(invoke)

    def later(self, seconds, callback):
        self.F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            seconds, False, lambda timer: callback())

    def park_for_point(self, point):
        # Park in place. CGWarp posts no event, but its displacement can still
        # be included in the next physical movement event (SDL/GLFW both
        # compensate for that). Avoid introducing the displacement entirely.
        return tuple(point)

    def _position_panel(self, point):
        main_height = self.Q.CGDisplayBounds(self.Q.CGMainDisplayID()).size.height
        origin = (point[0] - 48, main_height - point[1] - 48)
        current = self.panel.frame().origin
        if abs(current.x - origin[0]) > .01 or abs(current.y - origin[1]) > .01:
            self.panel.setFrameOrigin_(self.A.NSMakePoint(*origin))

    def align_to_cursor(self):
        point = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        park = (point.x, point.y)
        self._position_panel(park)
        return park

    def activate(self, park):
        A, Q = self.A, self.Q
        if not self.active:
            front = A.NSWorkspace.sharedWorkspace().frontmostApplication()
            self.previous_app = front if front and front.processIdentifier() != os.getpid() else None
            self.previous_key = self.app.keyWindow()
            self.previous_cursor = A.NSCursor.currentCursor()
            self.active = True
        if self.panel is None:
            self.panel = self.Panel.alloc().initWithContentRect_styleMask_backing_defer_(
                A.NSMakeRect(0, 0, 96, 96), A.NSWindowStyleMaskBorderless,
                A.NSBackingStoreBuffered, False)
            self.panel.setTitle_('Share Monkey · 远程光标捕获')
            self.panel.setReleasedWhenClosed_(False)
            self.panel.setOpaque_(False)
            self.panel.setHasShadow_(False)
            self.panel.setBackgroundColor_(A.NSColor.whiteColor().colorWithAlphaComponent_(.01))
            self.panel.setLevel_(A.NSStatusWindowLevel)
            self.panel.setHidesOnDeactivate_(False)
            self.panel.setAcceptsMouseMovedEvents_(True)
            self.panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces |
                                               A.NSWindowCollectionBehaviorFullScreenAuxiliary |
                                               A.NSWindowCollectionBehaviorStationary |
                                               A.NSWindowCollectionBehaviorIgnoresCycle)
            image = A.NSImage.alloc().initWithSize_(A.NSMakeSize(1, 1))
            bitmap = A.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, 1, 1, 8, 4, True, False, A.NSDeviceRGBColorSpace, 4, 32)
            # Write RGBA components directly: clearColor's dynamic/gray color
            # space is not necessarily compatible with an RGB bitmap.
            bitmap.bitmapData()[:4] = b'\x00\x00\x00\x00'
            image.addRepresentation_(bitmap)
            self.blank = A.NSCursor.alloc().initWithImage_hotSpot_(image, A.NSMakePoint(0, 0))
            self.view = self.View.alloc().initWithFrame_(A.NSMakeRect(0, 0, 96, 96))
            self.view.blank = self.blank
            self.view.capture_active = False
            self.panel.setContentView_(self.view)
        self._position_panel(park)
        self.view.capture_active = True
        self.panel.makeKeyAndOrderFront_(None)
        if not self.panel.makeFirstResponder_(self.view):
            raise RuntimeError('Mac 捕获面板无法接收光标更新')
        self.view.updateTrackingAreas()
        self.panel.resetCursorRects()
        self.app.activateIgnoringOtherApps_(True)

    def owns_foreground(self):
        front = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(front and front.processIdentifier() == os.getpid() and
                    self.panel is not None and self.panel.isKeyWindow() and self.panel.isVisible())

    def blank_cursor(self):
        current = self.cursor_pixels(self.A.NSCursor.currentCursor())
        if not current.get('transparent', False):
            self.cursor_replacements_detected += 1
        self.blank.set()
        self.cursor_assertions += 1
        applied = self.cursor_pixels(self.A.NSCursor.currentCursor())
        if not applied.get('verified') or not applied.get('transparent'):
            raise RuntimeError('Mac 捕获面板未能设置透明光标：' + str(applied))

    def verify_park(self, park):
        point = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        if abs(point.x - park[0]) > 1 or abs(point.y - park[1]) > 1:
            raise CaptureAnchorChanged('Mac 捕获位置仍在变化，已取消远控')
        main_height = self.Q.CGDisplayBounds(self.Q.CGMainDisplayID()).size.height
        if not self.A.NSPointInRect(self.A.NSMakePoint(point.x, main_height - point.y), self.panel.frame()):
            raise RuntimeError('Mac 捕获面板未覆盖指针，已取消远控')

    def release(self):
        if not self.active:
            return
        self.active = False
        front = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        restore_focus = bool(front and front.processIdentifier() == os.getpid())
        if self.panel is not None:
            self.view.capture_active = False
            self.panel.orderOut_(None)
        if self.previous_cursor is not None:
            self.previous_cursor.set()
        previous, self.previous_app = self.previous_app, None
        if restore_focus and previous is not None and not previous.isTerminated():
            previous.activateWithOptions_(self.A.NSApplicationActivateIgnoringOtherApps)
        elif (restore_focus and self.previous_key is not None and self.previous_key is not self.panel
              and self.previous_key.isVisible() and not self.previous_key.isMiniaturized()):
            self.previous_key.makeKeyAndOrderFront_(None)
        self.previous_key = self.previous_cursor = None

    def diagnostics(self):
        front = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        point = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        cursor = self.A.NSCursor.currentCursor()
        pixels = self.cursor_pixels(cursor)
        window_number = int(self.panel.windowNumber()) if self.panel else None
        owner = None
        if window_number is not None:
            info = self.Q.CGWindowListCopyWindowInfo(self.Q.kCGWindowListOptionIncludingWindow, window_number)
            for record in info or []:
                if int(record.get(self.Q.kCGWindowNumber, -1)) == window_number:
                    owner = int(record[self.Q.kCGWindowOwnerPID])
                    break
        return {
            'pid': os.getpid(), 'foreground_pid': int(front.processIdentifier()) if front else None,
            'foreground_bundle': str(front.bundleIdentifier()) if front else None,
            'capture_panel_key': bool(self.panel and self.panel.isKeyWindow()),
            'capture_panel_visible': bool(self.panel and self.panel.isVisible()),
            'capture_panel_number': window_number, 'capture_panel_owner_pid': owner,
            'application_cursor_is_blank': bool(self.blank and cursor == self.blank),
            'application_cursor_image': pixels,
            'application_cursor_is_transparent': bool(pixels.get('verified') and pixels.get('transparent')),
            'cursor_assertions': self.cursor_assertions,
            'cursor_replacements_detected': self.cursor_replacements_detected,
            'cursor_tracking_areas': len(self.view.trackingAreas()) if self.view else 0,
            'cursor_update_events': getattr(self.view, 'cursor_update_count', 0) if self.view else 0,
            'cursor_position': [point.x, point.y],
            'cg_cursor_visible_advisory_only': bool(self.Q.CGCursorIsVisible()),
        }

    def cursor_pixels(self, cursor):
        if cursor is None:
            return {'verified': False, 'reason': 'no cursor'}
        image = cursor.image()
        result = {'size': [image.size().width, image.size().height], 'verified': False}
        try:
            bitmap = next((rep for rep in image.representations()
                           if isinstance(rep, self.A.NSBitmapImageRep)), None)
            if bitmap is None:
                bitmap = self.A.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            if bitmap is None:
                return result
            width, height = int(bitmap.pixelsWide()), int(bitmap.pixelsHigh())
            samples, stride = int(bitmap.samplesPerPixel()), int(bitmap.bytesPerRow())
            result.update(pixels=[width, height], samples=samples,
                          bits_per_sample=int(bitmap.bitsPerSample()), planar=bool(bitmap.isPlanar()),
                          has_alpha=bool(bitmap.hasAlpha()))
            if not bitmap.hasAlpha():
                result.update(verified=True, max_alpha=255, transparent=False)
            elif not bitmap.isPlanar() and bitmap.bitsPerSample() == 8 and samples in (2, 4):
                data = bitmap.bitmapData()
                alpha = 0 if int(bitmap.bitmapFormat()) & int(self.A.NSAlphaFirstBitmapFormat) else samples - 1
                maximum = max((int(data[y * stride + x * samples + alpha])
                               for y in range(height) for x in range(width)), default=255)
                result.update(verified=True, max_alpha=maximum, transparent=maximum == 0)
            return result
        except Exception as exc:
            result['reason'] = str(exc)
            return result


def get_capture_coordinator():
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = CaptureCoordinator(AppKitShield())
    return _COORDINATOR


def close_capture_ui():
    if _COORDINATOR is not None:
        _COORDINATOR.close()
