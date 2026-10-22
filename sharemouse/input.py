"""OS input adapter. Listeners exist only on the controlling computer."""
import sys
import threading
import time

from pynput import keyboard, mouse
from .switching import EdgeSwitch, input_locked
from .cursor_visibility import create_cursor_visibility


class Input:
    def __init__(self, engine):
        if sys.platform == 'darwin' and threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Mac 键鼠控制必须在主线程初始化')
        self.engine = engine
        if sys.platform == 'darwin':
            from .mac_mouse import MouseController
            self.mouse = MouseController()
            from .mac_capture_ui import get_capture_coordinator
            self.capture_ui = get_capture_coordinator()
            self.capture_token = None
        else:
            self.mouse = mouse.Controller()
            if sys.platform == 'win32':
                from .windows_pointer import WindowsPointer
                self._native_move = WindowsPointer().move_to
        self.target_position = None
        self.keyboard = keyboard.Controller()
        self.listeners = []
        self.held_keys = {}
        self.held_buttons = set()
        self.physical_buttons = set()
        self.physical = set()
        self.key_payloads = {}
        self.edge_switch = EdgeSwitch()
        self.visibility = create_cursor_visibility(self.listener_failed)

    def start(self):
        if sys.platform == 'darwin':
            import HIServices
            if not HIServices.AXIsProcessTrusted():
                raise PermissionError('请在系统设置 → 隐私与安全性 → 辅助功能，授权启动本程序的终端或应用，然后重新启动')
            if self.engine.controller:
                import Quartz
                preflight = getattr(Quartz, 'CGPreflightListenEventAccess', None)
                if preflight and not preflight():
                    raise PermissionError('请在系统设置 → 隐私与安全性 → 输入监控，授权启动本程序的终端或应用，然后重新启动')
        self.visibility.start()
        if not self.engine.controller:
            return
        options = {}
        if sys.platform == 'darwin':
            options['darwin_intercept'] = self.intercept
            from .mac_keyboard import KeyboardListener
        else:
            KeyboardListener = keyboard.Listener
        km = dict(options)
        mm = dict(options)
        MouseListener = mouse.Listener
        if sys.platform == 'darwin':
            from .mac_mouse import MouseListener
            km['event_context'] = lambda: (self.engine.epoch, self.engine.remote, self.return_pending())
            km['event_lock'] = self.engine.state_lock
            mm['on_delta'] = self.mac_move
            mm['on_failure'] = self.listener_failed
        if sys.platform == 'win32':
            km['win32_event_filter'] = self.win_key
            mm['win32_event_filter'] = self.win_mouse
        self.kl = KeyboardListener(on_press=lambda key, injected=False: self.key(key, True, injected),
                                    on_release=lambda key, injected=False: self.key(key, False, injected), **km)
        self.ml = MouseListener(on_move=lambda x, y, injected=False: self.move(x, y, injected),
                                on_click=self.click, on_scroll=self.scroll, **mm)
        self.listeners = [self.kl, self.ml]
        for listener in self.listeners:
            listener.start()

    def intercept(self, event_type, event):
        import Quartz
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID):
            return event
        return None if self.engine.remote or self.return_pending() else event

    def return_pending(self):
        # Engine uses (epoch, fraction, deadline) while its source cursor stays
        # captured until the peer confirms it has hidden the other cursor.
        return isinstance(getattr(self.engine, 'pending_return', None), tuple)

    def listener_failed(self, reason):
        self.engine.stop()
        self.engine.status(reason)

    @input_locked
    def win_key(self, msg, data):
        if data.flags & 0x10:
            return False
        if self.engine.remote or self.return_pending():
            # Pinned pynput adapter translates OEM punctuation and modifiers.
            key = self.kl._event_to_key(msg, data.vkCode)
            self.key(key, msg in (0x100, 0x104))
            self.kl.suppress_event()

    @input_locked
    def win_mouse(self, msg, data):
        if data.flags & 1:
            return False
        was_remote = self.engine.remote or self.return_pending()
        x, y = data.pt.x, data.pt.y
        if msg == 0x200:
            self.move(x, y)
        elif msg in (0x201, 0x202, 0x204, 0x205, 0x207, 0x208):
            button = {0x201: mouse.Button.left, 0x202: mouse.Button.left,
                      0x204: mouse.Button.right, 0x205: mouse.Button.right,
                      0x207: mouse.Button.middle, 0x208: mouse.Button.middle}[msg]
            self.click(x, y, button, msg in (0x201, 0x204, 0x207))
        elif msg in (0x20A, 0x20E):
            import ctypes
            delta = ctypes.c_short(data.mouseData >> 16).value / 120
            self.scroll(x, y, delta if msg == 0x20E else 0, delta if msg == 0x20A else 0)
        # The entering edge event must also be suppressed: otherwise Windows
        # applies it after our center warp, undoing the warp and causing a jump.
        if was_remote or self.engine.remote or self.return_pending():
            self.ml.suppress_event()
        # We dispatched above; do not run pynput's callbacks a second time.
        return False

    def warp(self, x, y):
        position = (round(x), round(y))
        if sys.platform == 'darwin':
            self.mouse.warp(position)
        else:
            self.mouse.position = position

    def capture(self):
        if sys.platform == 'darwin':
            coordinator = getattr(self, 'capture_ui', None)
            if coordinator is None:
                self.ml.capture_mouse()
                return
            token = coordinator.token(tuple(self.mouse.position))
            try:
                generation = self.ml.begin_capture(token.park)
            except Exception:
                coordinator.release(token)
                raise
            self.capture_token = token

            def capture_failed(reason):
                from .mac_capture_ui import CaptureFocusLost
                with self.engine.state_lock:
                    # A delayed main-loop failure must not return or stop a
                    # newer crossing which already owns a different token.
                    if self.capture_token is not token:
                        return
                    if isinstance(token.error, CaptureFocusLost):
                        self.engine.return_local()
                        if self.engine.connected:
                            action = '正在返回本机' if self.return_pending() else '已回到本机'
                            self.engine.status('Mac 焦点已切换 · ' + action + '，连接保持')
                    else:
                        self.listener_failed(reason)

            coordinator.acquire(token, lambda park, done: self.ml.finish_capture(generation, park, done),
                                capture_failed)
        else:
            visibility = getattr(self, 'visibility', None)
            if visibility is not None:
                visibility.hide(watch_position=False)
            try:
                self.warp(self.engine.local.width // 2, self.engine.local.height // 2)
            except Exception:
                if visibility is not None:
                    visibility.show()
                raise

    def wait_capture_ready(self, stop_event, timeout=1.5):
        token = getattr(self, 'capture_token', None)
        if token is None:
            return not stop_event.is_set()
        return token.wait(stop_event, timeout,
                          cancelled=lambda: self.capture_token is not token or not self.engine.remote or self.return_pending())

    def _release_mac_capture(self, position=None):
        token = getattr(self, 'capture_token', None)
        coordinator = getattr(self, 'capture_ui', None)
        if token is None or coordinator is None:
            self.ml.release_mouse(position)
            return
        token.cancel()
        self.capture_token = None
        self.ml.release_mouse(position, after=lambda: coordinator.release(token))

    def restore_local(self, x, y):
        self.edge_switch.returned(x, self.engine.local.width, self.engine.side)
        if sys.platform == 'darwin':
            self._release_mac_capture((round(x), round(y)))
        else:
            try:
                self.warp(x, y)
            finally:
                visibility = getattr(self, 'visibility', None)
                if visibility is not None:
                    visibility.show()

    def set_remote_active(self, active):
        """Show only the receiver currently being controlled; local input may override idle hiding."""
        if self.engine.controller:
            return
        visibility = getattr(self, 'visibility', None)
        if not active:
            # A local physical mouse may have moved while the remote pointer
            # was inactive. A repeated entry must not skip its initial warp.
            self.target_position = None
        if visibility is not None:
            if active:
                visibility.show()
            else:
                visibility.hide(watch_position=True)

    @input_locked
    def mac_move(self, x, y, dx, dy, injected=False):
        if injected:
            return
        e = self.engine
        if not e.connected or self.return_pending():
            return
        if e.remote:
            # Never infer motion from distance to a commanded cursor position.
            # Quartz deltas survive queued/stationary/clipped absolute locations.
            if dx or dy:
                e.send({'type': 'move', 'dx': dx, 'dy': dy})
        else:
            if self.ml._captured:
                # The network thread has requested a return, but the native
                # run-loop block has not restored the cursor yet. These are
                # still remote events, not a new local edge crossing.
                return
            previous = self.edge_switch.last_x
            # Local absolute positions retain subpixel motion even when the
            # hardware delta field is rounded to zero. Remote motion still
            # exclusively uses hardware deltas, never a frozen cursor offset.
            local_dx = x - previous if previous is not None and x != previous else dx
            self.move(x, y, dx=local_dx)

    @input_locked
    def move(self, x, y, injected=False, dx=None):
        if injected:
            return
        e = self.engine
        if not e.connected or self.return_pending():
            return
        if time.monotonic() < e.cooldown:
            if not e.remote:
                self.edge_switch.update(x, e.local.width, e.side, dx, allow_entry=False)
            return
        if e.remote:
            if sys.platform == 'darwin':
                # mac_move is the only remote motion path on macOS.
                return
            cx, cy = e.local.width // 2, e.local.height // 2
            if (x, y) == (cx, cy):
                return
            e.send({'type': 'move', 'dx': x - cx, 'dy': y - cy})
        elif not self.physical and not self.physical_buttons:
            if self.edge_switch.update(x, e.local.width, e.side, dx):
                # If native capture fails, stay local and do not announce entry.
                self.capture()
                e.epoch += 1
                e.remote = True
                if e.send({'type': 'enter', 'fraction': y / (e.local.height - 1), 'side': e.side}):
                    e.status('正在控制对方 · F12 回到本机')

    @input_locked
    def key(self, key, pressed, injected=False):
        if injected or key is None:
            return
        identity = ('name', key.name) if isinstance(key, keyboard.Key) else ('vk', key.vk) if key.vk is not None else ('char', key.char)
        # Consumer controls are decoded asynchronously on Mac. Their delayed
        # release may belong to an old crossing and be discarded; they must
        # not leave a physical-key entry that prevents another edge crossing.
        media = (sys.platform == 'darwin' and isinstance(key, keyboard.Key)
                 and getattr(key.value, '_is_media', False))
        if pressed and not media:
            self.physical.add(identity)
        elif not pressed:
            self.physical.discard(identity)
        if key == keyboard.Key.f12:
            if pressed:
                self.engine.return_local()
            return
        if not self.engine.remote:
            if not pressed:
                self.key_payloads.pop(identity, None)
            return
        if not pressed and identity in self.key_payloads:
            self.engine.send({'type': 'key', 'key': self.key_payloads.pop(identity), 'down': False})
            return
        if isinstance(key, keyboard.Key):
            payload = {'name': key.name}
        else:
            char = self.kl.canonical(key).char
            if char and len(char) == 1 and 1 <= ord(char) <= 26:
                char = chr(ord(char) + 96)
            if not char:
                if sys.platform == 'win32' and key.vk and 48 <= key.vk <= 90:
                    char = chr(key.vk).lower()
                else:
                    return
            payload = {'char': char}
        if pressed:
            self.key_payloads[identity] = payload
        self.engine.send({'type': 'key', 'key': payload, 'down': pressed})

    @input_locked
    def click(self, x, y, button, pressed, injected=False):
        if injected:
            return
        if self.engine.remote:
            self.engine.send({'type': 'button', 'button': button.name, 'down': pressed})
        # Physical button state also prevents switching during local drags.
        if pressed:
            self.physical_buttons.add(button)
        else:
            self.physical_buttons.discard(button)

    @input_locked
    def scroll(self, x, y, dx, dy, injected=False):
        if not injected and self.engine.remote:
            self.engine.send({'type': 'scroll', 'dx': dx, 'dy': dy})

    def inject_key(self, data, down):
        identity = tuple(sorted(data.items()))
        if 'name' in data:
            name = data['name']
            if self.engine.map_command:
                if sys.platform == 'win32' and name in ('cmd', 'cmd_l', 'cmd_r'):
                    name = 'ctrl'
                elif sys.platform == 'darwin' and name in ('ctrl', 'ctrl_l', 'ctrl_r'):
                    name = 'cmd'
            key = getattr(keyboard.Key, name)
        else:
            char = data['char']
            if not isinstance(char, str) or len(char) != 1:
                raise ValueError('按键数据无效')
            key = keyboard.KeyCode.from_char(char)
        if down:
            self.keyboard.press(key)
            self.held_keys[identity] = key
        else:
            self.keyboard.release(self.held_keys.pop(identity, key))

    def move_to(self, position):
        if sys.platform == 'win32':
            position = tuple(round(value) for value in position)
        if tuple(position) == self.target_position:
            return
        requested = tuple(position)
        native_move = getattr(self, '_native_move', None)
        if native_move is not None:
            native_move(requested)
        else:
            self.mouse.position = requested
        self.target_position = requested

    def inject_button(self, button, down):
        if sys.platform == 'darwin' and self.target_position is not None:
            self.mouse.button_at(button, down, self.target_position)
        else:
            (self.mouse.press if down else self.mouse.release)(button)
        if down:
            self.held_buttons.add(button)
        else:
            self.held_buttons.discard(button)

    def release(self):
        for key in list(self.held_keys.values()):
            self.keyboard.release(key)
        self.held_keys.clear()
        self.key_payloads.clear()
        for button in list(self.held_buttons):
            self.inject_button(button, False)
        self.held_buttons.clear()

    def stop(self):
        errors = []
        if sys.platform == 'darwin' and hasattr(self, 'ml'):
            try:
                self._release_mac_capture()
            except Exception as exc:
                errors.append(exc)
        for listener in self.listeners:
            try:
                listener.stop()
            except Exception as exc:
                errors.append(exc)
        try:
            self.release()
        except Exception as exc:
            errors.append(exc)
        finally:
            self.physical_buttons.clear()
            visibility = getattr(self, 'visibility', None)
            if visibility is not None:
                try:
                    visibility.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            self.engine.status('键鼠恢复提示：' + '; '.join(map(str, errors)))
