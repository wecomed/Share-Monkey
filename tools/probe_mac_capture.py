"""Explicit foreground-capture probe. Default is read-only; --capture changes UI/cursor.

Run only when no other client is capturing input. This probe performs no network
connection or input injection. It records actual foreground/panel/park ownership;
CGCursorIsVisible is recorded as advisory evidence, never as the pass criterion.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--seconds', type=float, default=1.)
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--output', type=Path, default=Path('/tmp/share-monkey-native-capture.json'))
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('macOS only')
    if not .2 <= args.seconds <= 5:
        parser.error('--seconds must be between .2 and 5')
    if not 1 <= args.cycles <= 30:
        parser.error('--cycles must be between 1 and 30')
    import AppKit as A
    import Foundation as F
    import Quartz as Q
    from sharemouse.mac_capture_ui import get_capture_coordinator
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    ui = get_capture_coordinator()
    evidence = {'mode': 'native-foreground-capture' if args.capture else 'read-only',
                'before': ui.backend.diagnostics(), 'stages': [],
                'cycles_requested': args.cycles, 'cycles_completed': 0}
    if not args.capture:
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False))
        return
    if evidence['before']['foreground_bundle'] == 'com.apple.loginwindow':
        raise RuntimeError('Unlock the Mac before --capture; secure desktop is never captured')
    if evidence['before']['foreground_pid'] is None:
        raise RuntimeError('No foreground application is observable; capture probe was not started')
    if evidence['before']['foreground_pid'] == evidence['before']['pid']:
        raise RuntimeError('Probe must begin while another application owns the foreground')
    from sharemouse.display import primary_screen
    from sharemouse.engine import Engine
    from sharemouse.input import Input
    engine = Engine(True, primary_screen(), 'right', 'local-probe-no-network', '', 24809,
                    lambda text: evidence.setdefault('status', []).append(text), clipboard=False)
    engine.input = adapter = Input(engine)
    engine.send = lambda message: True
    saved = tuple(evidence['before']['cursor_position'])
    errors = []

    def snapshot(stage, cycle=None, record=True):
        done = threading.Event()
        sampled = []
        def sample():
            try:
                state = ui.backend.diagnostics()
                state.update(stage=stage, cycle=cycle)
                state['native_captured'] = adapter.ml._captured
                state['native_hidden_owned'] = adapter.ml._cursor_hidden
                state['native_disassociated_owned'] = adapter.ml._mouse_disassociated
                if ui.backend.blank is not None:
                    bitmap = ui.backend.blank.image().representations()[0]
                    state['blank_cursor_pixel_alpha'] = float(bitmap.colorAtX_y_(0, 0).alphaComponent())
                sampled.append(state)
                if record:
                    evidence['stages'].append(state)
            except Exception as exc:
                sampled.append(exc)
            finally:
                done.set()
        F.NSOperationQueue.mainQueue().addOperationWithBlock_(sample)
        if not done.wait(1):
            raise TimeoutError('AppKit did not service the diagnostic sample')
        if isinstance(sampled[0], Exception):
            raise sampled[0]
        return sampled[0]

    def restore_cycle(cycle):
        restored = threading.Event()
        def restore():
            try:
                with engine.state_lock:
                    engine.remote = engine.connected = False
                    adapter.restore_local(*saved)
            finally:
                restored.set()
        if adapter.ml.is_alive():
            Q.CFRunLoopPerformBlock(adapter.ml._loop, Q.kCFRunLoopDefaultMode, restore)
            Q.CFRunLoopWakeUp(adapter.ml._loop)
            if not restored.wait(2):
                raise TimeoutError('HID native release callback timed out')
        else:
            adapter.restore_local(*saved)
        deadline = time.monotonic() + 2
        while True:
            state = snapshot('restored', cycle, record=False)
            if (not state['capture_panel_visible'] and not state['native_captured']
                    and not state['native_hidden_owned'] and not state['native_disassociated_owned']
                    and state['foreground_pid'] == evidence['before']['foreground_pid']
                    and all(abs(actual - expected) <= 1.5 for actual, expected in
                            zip(state['cursor_position'], saved))):
                evidence['stages'].append(state)
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Previous foreground/panel/position did not restore between cycles')
            threading.Event().wait(.01)

    def driver():
        try:
            for listener in adapter.listeners:
                listener.wait()
            for cycle in range(1, args.cycles + 1):
                if engine.stop_event.is_set():
                    raise RuntimeError('Capture was interrupted; check recorded status')
                requested = threading.Event()
                def capture():
                    try:
                        with engine.state_lock:
                            engine.connected = engine.remote = True
                            adapter.capture()
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        requested.set()
                Q.CFRunLoopPerformBlock(adapter.ml._loop, Q.kCFRunLoopDefaultMode, capture)
                Q.CFRunLoopWakeUp(adapter.ml._loop)
                if not requested.wait(2):
                    raise TimeoutError('HID begin-capture callback timed out')
                if errors:
                    raise errors[0]
                if not adapter.wait_capture_ready(engine.stop_event, 2):
                    raise RuntimeError('Capture failed before native readiness')
                snapshot('ready', cycle)
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    threading.Event().wait(min(.1, max(0, deadline - time.monotonic())))
                    snapshot('holding', cycle)
                    if engine.stop_event.is_set():
                        raise RuntimeError('Capture was interrupted; check recorded status')
                restore_cycle(cycle)
                evidence['cycles_completed'] = cycle
        except Exception as exc:
            errors.append(exc)
        finally:
            try:
                restore_cycle(None)
            except Exception as exc:
                errors.append(exc)
            try:
                engine.stop()
                for listener in adapter.listeners:
                    if listener.is_alive():
                        listener.join(2)
                snapshot('after_native_release')
            except Exception as exc:
                errors.append(exc)
            def finish():
                try:
                    # Explicit native recovery is confined to --capture mode;
                    # even an earlier listener failure must restore the saved
                    # position rather than the Engine's emergency edge point.
                    if adapter.ml._captured or adapter.ml._cursor_hidden or adapter.ml._mouse_disassociated:
                        adapter.ml._release_native()
                    result = Q.CGWarpMouseCursorPosition(saved)
                    if result != Q.kCGErrorSuccess:
                        errors.append(RuntimeError('Final saved-position restoration failed: ' + str(result)))
                    ui.close()
                    evidence['after'] = ui.backend.diagnostics()
                    evidence['native_state_after'] = {
                        'captured': adapter.ml._captured,
                        'cursor_hidden_owned': adapter.ml._cursor_hidden,
                        'mouse_disassociated_owned': adapter.ml._mouse_disassociated,
                        'tap_thread_alive': adapter.ml.is_alive(),
                    }
                    evidence['errors'] = list(map(str, errors))
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                finally:
                    app.stop_(None)
                    event = A.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                        A.NSEventTypeApplicationDefined, A.NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
                    app.postEvent_atStart_(event, True)
            F.NSOperationQueue.mainQueue().addOperationWithBlock_(finish)

    try:
        adapter.start()
        F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            .2, False, lambda timer: threading.Thread(target=driver, daemon=True).start())
        app.run()
    finally:
        engine.remote = engine.connected = False
        engine.stop()
        ui.close()
    if errors:
        raise RuntimeError('; '.join(map(str, errors)))
    stages = evidence['stages']
    if not stages or evidence['cycles_completed'] != args.cycles or not all(
            s['foreground_pid'] == s['pid'] and s['capture_panel_owner_pid'] == s['pid'] and s['capture_panel_key'] and
            s['capture_panel_visible'] and s['application_cursor_is_blank'] and s['blank_cursor_pixel_alpha'] == 0.
            for s in stages if s['stage'] in ('ready', 'holding')):
        raise RuntimeError('Actual foreground/panel ownership did not meet the capture conditions')
    print('Native foreground/panel/park probe completed; restoration recorded at', args.output)


if __name__ == '__main__':
    main()
