"""Opt-in Windows input probe confined to its own disposable Qt window.

No listener, peer connection, clipboard, system shortcut, service, settings or
installer is used. Importing this module and running it without --native never
imports pynput/Qt or calls a native API. A successful result covers synthetic
receiver input in the explicitly selected source/installed adapter, not the
whole application binary.
"""
import argparse
import ast
from collections import Counter
import ctypes
from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ProbeAborted(RuntimeError):
    pass


def function_hashes(directory):
    """Identify tested method bodies separately from the harness version."""
    result = {}
    for filename, class_name, names in (
            ('input.py', 'Input', ('inject_key', 'inject_button', 'move_to', 'release')),
            ('windows_pointer.py', 'WindowsPointer', ('move_to',))):
        source = (Path(directory) / filename).read_text(encoding='utf-8')
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        for name in names:
            method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
            body = ast.get_source_segment(source, method).encode('utf-8')
            result[f'{filename}:{class_name}.{name}'] = hashlib.sha256(body).hexdigest()
    return result


@dataclass(frozen=True)
class Step:
    name: str
    operation: str
    argument: object
    event: str | None = None


def steps_for_cycle(cycle):
    """Only fixed, innocuous stimuli; no values obtained from user input."""
    prefix = f'cycle-{cycle + 1}'
    return [Step(prefix + '-move', 'move', cycle, 'move'),
            Step(prefix + '-a-down', 'key_down', {'char': 'a'}, 'key_down'),
            Step(prefix + '-a-up', 'key_up', {'char': 'a'}, 'key_up'),
            Step(prefix + '-f8-down', 'key_down', {'name': 'f8'}, 'key_down'),
            Step(prefix + '-f8-up', 'key_up', {'name': 'f8'}, 'key_up'),
            Step(prefix + '-left-down', 'button_down', None, 'button_down'),
            Step(prefix + '-left-up', 'button_up', None, 'button_up'),
            Step(prefix + '-scroll', 'scroll', None, 'wheel')]


class OwnWindowGuard:
    """Checks are deliberately repeated immediately before every injection."""
    def __init__(self, api, hwnd, aborted=lambda: False):
        self.api, self.hwnd, self.aborted = api, int(hwnd), aborted
        self.checks = 0

    def require(self, *, pointer=False):
        if self.aborted():
            raise ProbeAborted('Probe was cancelled with Escape or its window was closed')
        if not self.api.is_window(self.hwnd) or self.api.foreground() != self.hwnd:
            raise ProbeAborted('Own test window lost foreground; input stopped')
        if self.api.modifiers_down():
            raise ProbeAborted('A physical modifier is held; input stopped')
        if pointer and not self.api.pointer_is_ours(self.hwnd):
            raise ProbeAborted('Cursor left the own test window; input stopped')
        self.checks += 1


def perform_step(step, *, guard, adapter, pointer, position, button):
    """Small injectable boundary also exercised by non-native unit tests."""
    guard.require(pointer=step.operation in ('button_down', 'button_up', 'scroll'))
    if step.operation == 'move':
        requested = tuple(position)
        if not guard.api.point_is_ours(guard.hwnd, requested):
            raise ProbeAborted('Requested cursor position is outside own client area')
        pointer.move_to(requested)
        actual = tuple(guard.api.cursor_position())
        if actual != requested:
            raise RuntimeError('Win32 cursor readback differs from physical requested coordinates')
        return {'requested': list(requested), 'readback': list(actual)}
    if step.operation in ('key_down', 'key_up'):
        adapter.inject_key(step.argument, step.operation == 'key_down')
    elif step.operation in ('button_down', 'button_up'):
        adapter.inject_button(button, step.operation == 'button_down')
    elif step.operation == 'scroll':
        # Same production receiver call as Engine's scroll branch.
        adapter.mouse.scroll(0, 1)
    else:
        raise ValueError('Unknown fixed probe operation')
    return {}


class WinApi:
    """Read-only ownership/readback plus scoped DPI context; no global hooks."""
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('Native Windows probe is unsupported on this platform')
        from ctypes import wintypes as W
        self.W = W
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        signatures = {
            'GetForegroundWindow': ([], W.HWND),
            'IsWindow': ([W.HWND], W.BOOL),
            'IsWindowVisible': ([W.HWND], W.BOOL),
            'GetCursorPos': ([ctypes.POINTER(W.POINT)], W.BOOL),
            'GetClientRect': ([W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
            'ClientToScreen': ([W.HWND, ctypes.POINTER(W.POINT)], W.BOOL),
            'WindowFromPoint': ([W.POINT], W.HWND),
            'IsChild': ([W.HWND, W.HWND], W.BOOL),
            'GetAsyncKeyState': ([ctypes.c_int], ctypes.c_short),
            'SetForegroundWindow': ([W.HWND], W.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            method = getattr(self.api, name)
            method.argtypes, method.restype = argtypes, restype
        self.dpi_context = getattr(self.api, 'SetThreadDpiAwarenessContext', None)
        if self.dpi_context is not None:
            self.dpi_context.argtypes = [ctypes.c_void_p]
            self.dpi_context.restype = ctypes.c_void_p
        self.dpi_window = getattr(self.api, 'GetDpiForWindow', None)
        if self.dpi_window is not None:
            self.dpi_window.argtypes = [W.HWND]
            self.dpi_window.restype = W.UINT

    def _physical(self, callback):
        previous = self.dpi_context(-4) if self.dpi_context else None
        if self.dpi_context and not previous:
            raise OSError('Cannot enter physical-pixel DPI context')
        try:
            return callback()
        finally:
            if previous is not None and not self.dpi_context(previous):
                raise OSError('Cannot restore thread DPI context')

    def cursor_position(self):
        def read():
            point = self.W.POINT()
            if not self.api.GetCursorPos(ctypes.byref(point)):
                raise OSError('GetCursorPos failed')
            return point.x, point.y
        return self._physical(read)

    def client_bounds(self, hwnd):
        def read():
            rect, origin = self.W.RECT(), self.W.POINT(0, 0)
            if not self.api.GetClientRect(hwnd, ctypes.byref(rect)):
                raise OSError('GetClientRect failed')
            if not self.api.ClientToScreen(hwnd, ctypes.byref(origin)):
                raise OSError('ClientToScreen failed')
            return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top
        return self._physical(read)

    def point_is_ours(self, hwnd, position):
        x, y, width, height = self.client_bounds(hwnd)
        px, py = position
        return x + 30 <= px < x + width - 30 and y + 70 <= py < y + height - 30

    def pointer_is_ours(self, hwnd):
        point = self.cursor_position()
        if not self.point_is_ours(hwnd, point):
            return False
        hit = self._physical(lambda: self.api.WindowFromPoint(self.W.POINT(*point)))
        return hit == hwnd or bool(self.api.IsChild(hwnd, hit))

    def foreground(self):
        return self.api.GetForegroundWindow()

    def is_window(self, hwnd):
        return bool(self.api.IsWindow(hwnd) and self.api.IsWindowVisible(hwnd))

    def modifiers_down(self):
        return any(self.api.GetAsyncKeyState(code) & 0x8000
                   for code in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def escape_down(self):
        return bool(self.api.GetAsyncKeyState(0x1B) & 0x8000)


def native_probe(args):
    if sys.platform != 'win32':
        raise OSError('Native Windows probe is unsupported on this platform')
    target = getattr(args, 'target', 'source')
    if target == 'installed':
        if args.installed_root is None:
            raise ValueError('--target installed requires --installed-root')
        module_directory = (args.installed_root / 'pkgs' / 'sharemouse').resolve()
        if not module_directory.is_dir():
            raise FileNotFoundError('Installed Share Monkey source directory was not found')
        sys.path.insert(0, str(module_directory.parent))
    else:
        module_directory = (ROOT / 'sharemouse').resolve()
        sys.path.insert(0, str(ROOT))
    # A preloaded source module must never silently turn an installed test into
    # a source test. The one-click bootstrap imports no product modules.
    if 'sharemouse' in sys.modules:
        loaded = Path(sys.modules['sharemouse'].__file__).resolve().parent
        if loaded != module_directory:
            raise RuntimeError('A different Share Monkey package is already loaded; restart the probe process')
    from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
    from pynput import mouse
    from sharemouse import __version__ as tested_version
    from sharemouse.input import Input
    from sharemouse.windows_pointer import WindowsPointer

    api, pointer = WinApi(), WindowsPointer()
    original_cursor = api.cursor_position()
    original_foreground = api.foreground()
    started = time.monotonic()
    app = W.QApplication.instance() or W.QApplication([])
    counts = Counter()
    evidence = dict(native_actions=True, executed_on_windows=True,
                    peer_connection=False, global_listeners=False,
                    records_user_text=False, physical_hardware_verified=False,
                    application_binary_verified=False, target=target, phases=[], errors=[],
                    counts={}, coverage={'own_window_input': False, 'physical_coordinates': False,
                                         'dpi_current_monitor': False, 'all_monitor_scales': False},
                    boundary='Synthetic receiver adapter input delivered only to this probe window; no peer or whole application binary is tested')
    runtime = {'kind': target, 'version': tested_version, 'adapter_version': tested_version,
               'version_verified': True,
               'binary_verified': False, 'path': str(module_directory),
               'source_files': {name: hashlib.sha256((module_directory / name).read_bytes()).hexdigest()
                                for name in ('input.py', 'windows_pointer.py')},
               'python': sys.version.split()[0], 'python_executable': str(Path(sys.executable)),
               'adapter_module': str(Path(sys.modules[Input.__module__].__file__).resolve()),
               'pointer_module': str(Path(sys.modules[WindowsPointer.__module__].__file__).resolve())}
    runtime['tested_function_hashes'] = function_hashes(module_directory)
    if args.installed_root:
        installed_source = args.installed_root / 'pkgs' / 'sharemouse'
        try:
            version_match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]",
                                      (installed_source / '__init__.py').read_text(encoding='utf-8'), re.M)
            installed_hashes = function_hashes(installed_source)
            runtime['installed_source_comparison'] = {
                'version': version_match.group(1) if version_match else None,
                'function_hashes': installed_hashes,
                'same_method_source_only': installed_hashes == runtime['tested_function_hashes'],
                'installed_binary_verified': False}
        except (OSError, SyntaxError, StopIteration):
            runtime['installed_source_comparison'] = {'available': False, 'installed_binary_verified': False}
    evidence['tested_runtime'] = runtime
    if Path(runtime['adapter_module']) != (module_directory / 'input.py').resolve():
        raise RuntimeError('Input adapter did not load from the requested target directory')
    if Path(runtime['pointer_module']) != (module_directory / 'windows_pointer.py').resolve():
        raise RuntimeError('Pointer adapter did not load from the requested target directory')
    evidence['adapter_provenance_verified'] = target == 'installed'

    def receive(kind, *, key=None, button=None, wheel=None):
        # Ignore startup motion; the fixed stimuli still require a matching
        # native delivery. Never inspect event.text() or retain real key values.
        pending = state['pending']
        if pending is not None:
            step = pending[0]
            if kind == step.event:
                if kind in ('key_down', 'key_up'):
                    wanted = C.Qt.Key.Key_A if 'char' in step.argument else C.Qt.Key.Key_F8
                    if key != wanted:
                        window.cancelled = True
                        return
                if kind in ('button_down', 'button_up') and button != C.Qt.MouseButton.LeftButton:
                    window.cancelled = True
                    return
                if kind == 'wheel' and wheel <= 0:
                    window.cancelled = True
                    return
        counts[kind] += 1

    class ProbeWindow(W.QWidget):
        def __init__(self):
            super().__init__()
            self.cancelled = False
            self.finishing = False
            self.setWindowTitle('Share Monkey — Windows 原生自检（Esc 停止）')
            self.setMinimumSize(640, 420)
            self.resize(720, 480)
            self.setMouseTracking(True)
            self.setFocusPolicy(C.Qt.FocusPolicy.StrongFocus)

        def paintEvent(self, event):
            painter = G.QPainter(self)
            painter.fillRect(self.rect(), G.QColor('#f3f5f8'))
            painter.setPen(G.QColor('#182338'))
            painter.drawText(self.rect().adjusted(30, 25, -30, -25),
                             C.Qt.AlignmentFlag.AlignTop | C.Qt.TextFlag.TextWordWrap,
                             'Windows 原生输入自检\n\n测试会在本窗口移动鼠标、输入固定测试按键并滚动。\n'
                             '请暂勿操作键鼠。按 Esc、关闭或切换窗口会立即停止。\n\n'
                             '不连接对端、不修改系统设置、不读取密码或剪贴板。')

        def keyPressEvent(self, event):
            if event.key() == C.Qt.Key.Key_Escape:
                self.cancelled = True
            receive('key_down', key=event.key())
            event.accept()

        def keyReleaseEvent(self, event):
            receive('key_up', key=event.key())
            event.accept()

        def mouseMoveEvent(self, event):
            receive('move')
            event.accept()

        def mousePressEvent(self, event):
            receive('button_down', button=event.button())
            event.accept()

        def mouseReleaseEvent(self, event):
            receive('button_up', button=event.button())
            event.accept()

        def wheelEvent(self, event):
            receive('wheel', wheel=event.angleDelta().y())
            event.accept()

        def closeEvent(self, event):
            self.cancelled = not self.finishing
            event.accept()
            if not self.finishing:
                finish('Own probe window was closed')

    window = ProbeWindow()
    hwnd = int(window.winId())
    guard = OwnWindowGuard(api, hwnd, lambda: window.cancelled or api.escape_down())
    adapter = Input(SimpleNamespace(map_command=False))
    # Input is intentionally never started: no pynput listener, cursor hiding,
    # recovery subprocess or networking can begin through this probe.
    queue = [step for cycle in range(args.cycles) for step in steps_for_cycle(cycle)]
    timer = C.QTimer()
    timer.setInterval(15)
    state = {'index': 0, 'pending': None, 'done': False, 'activation_at': None}

    def finish(error=None):
        if state['done']:
            return
        state['done'] = True
        timer.stop()
        if error:
            evidence['errors'].append(error)
        # Cleanup is the sole ownership-guard exception: release only our held
        # injected keys/buttons, then restore the saved cursor without clicking.
        # These releases are needed even if the user switches away mid-pair.
        try:
            adapter.release()
            evidence['held_input_released'] = not adapter.held_keys and not adapter.held_buttons
        except Exception as exc:
            evidence['errors'].append('Cleanup failed: ' + type(exc).__name__)
            evidence['held_input_released'] = False
        try:
            pointer.move_to(original_cursor)
            evidence['cursor_restored'] = api.cursor_position() == original_cursor
        except Exception as exc:
            evidence['errors'].append('Cursor restoration failed: ' + type(exc).__name__)
            evidence['cursor_restored'] = False
        evidence['foreground_guard_checks'] = guard.checks
        evidence['counts'] = dict(counts)
        evidence['elapsed_seconds'] = round(time.monotonic() - started, 3)
        evidence['completed_cycles'] = state['index'] // len(steps_for_cycle(0))
        if not evidence['held_input_released'] or not evidence['cursor_restored']:
            evidence['errors'].append('Input cleanup was incomplete')
        window.finishing = True
        window.close()
        # Do not reactivate a previous app: if the user changed foreground,
        # cleanup must not steal it back. Closing lets Windows choose focus.
        evidence['original_foreground_observed'] = bool(original_foreground)
        C.QTimer.singleShot(0, app.quit)

    def tick():
        try:
            if time.monotonic() - started > args.timeout:
                raise TimeoutError('Native probe exceeded its bounded time limit')
            guard.require()
            pending = state['pending']
            if pending is not None:
                step, before, deadline, details = pending
                if counts[step.event] <= before:
                    if time.monotonic() > deadline:
                        raise TimeoutError('Own Qt window did not receive expected ' + step.event)
                    return
                evidence['phases'].append(dict(name=step.name, event=step.event,
                                               received=True, **details))
                state['index'] += 1
                state['pending'] = None
                return
            if state['index'] == len(queue):
                evidence['coverage'].update(own_window_input=True, physical_coordinates=True,
                                            dpi_current_monitor=bool(api.dpi_window))
                finish()
                return
            step = queue[state['index']]
            x, y, width, height = api.client_bounds(hwnd)
            # Alternate two interior physical coordinates to force a real move.
            position = (x + width // 2 + (35 if step.argument % 2 else -35),
                        y + height * 2 // 3) if step.operation == 'move' else (x, y)
            before = counts[step.event]
            details = perform_step(step, guard=guard, adapter=adapter, pointer=pointer,
                                   position=position, button=mouse.Button.left)
            state['pending'] = (step, before, time.monotonic() + 1.5, details)
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))

    def start():
        try:
            evidence['dpi'] = {'window_dpi': api.dpi_window(hwnd) if api.dpi_window else None,
                               'qt_device_pixel_ratio': window.devicePixelRatioF(),
                               'client_bounds_physical': list(api.client_bounds(hwnd))}
            if api.dpi_window and not evidence['dpi']['window_dpi']:
                raise OSError('GetDpiForWindow failed')
            timer.start()
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))

    timer.timeout.connect(tick)
    window.show()
    window.raise_()
    window.activateWindow()
    window.setFocus()
    api.api.SetForegroundWindow(hwnd)
    C.QTimer.singleShot(500, start)
    try:
        app.exec()
    finally:
        if not state['done']:
            finish('Qt event loop exited before the probe completed')
    return evidence


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--native', action='store_true', help='Explicitly permit own-window native synthetic input on Windows')
    result.add_argument('--target', choices=('source', 'installed'), default='source',
                        help='Load source-tree adapters or the actual installed adapters; neither validates the whole EXE')
    result.add_argument('--cycles', type=int, default=5)
    result.add_argument('--timeout', type=float, default=60)
    result.add_argument('--output', type=Path, default=ROOT / 'validation' / 'windows-native.json')
    result.add_argument('--installed-root', type=Path, help='Installed Share Monkey folder (contains Python and pkgs)')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.cycles <= 100 or not 5 <= args.timeout <= 600:
        parser().error('cycles must be 1..100 and timeout 5..600 seconds')
    from tools.validation_common import make_report, source_context, utc_now, write_report
    started_at = utc_now()
    context = source_context(ROOT, runtime={'kind': args.target, 'target_not_loaded_yet': True,
                                          'binary_verified': False, 'path': str(ROOT)})
    if args.installed_root:
        context['installed_dependency_root'] = str(args.installed_root.resolve())
    if not args.native or sys.platform != 'win32':
        evidence = {'native_actions': False, 'executed_on_windows': False, 'phases': [],
                    'errors': [], 'coverage': {}, 'application_binary_verified': False}
        reason = ('Explicit --native is required; no native action was performed' if not args.native else
                  'Windows is required; no native action was performed')
        evidence['unsupported'] = True
        report = make_report('windows-native', 'not_run', evidence=evidence,
                             context=context, evidence_scope='native_synthetic',
                             started_at=started_at, reason=reason)
        code = 2
    else:
        try:
            evidence = native_probe(args)
            runtime = evidence.get('tested_runtime', {})
            if runtime.get('version_verified'):
                context = source_context(ROOT, tested_version=runtime['version'], runtime=runtime)
        except Exception as exc:
            evidence = {'native_actions': False, 'executed_on_windows': False,
                        'phases': [], 'errors': [type(exc).__name__ + ': ' + str(exc)], 'coverage': {}}
        complete = (bool(evidence.get('native_actions')) and bool(evidence.get('executed_on_windows'))
                    and len(evidence.get('phases', [])) == args.cycles * len(steps_for_cycle(0))
                    and not evidence.get('errors'))
        report = make_report('windows-native', 'passed' if complete else 'failed',
                             evidence=evidence, context=context, evidence_scope='native_synthetic',
                             started_at=started_at)
        code = 0 if complete else 1
    write_report(report, args.output)
    print(f'{report["status"]}: {args.output}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
