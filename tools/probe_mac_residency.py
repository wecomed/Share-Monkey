"""Probe the actual AppKit window/capture lifecycle with synthetic input.

Nothing native runs without --capture. That flag explicitly permits foreground
activation, a real smoke Desktop window, local cursor warps and production HID
capture. The receiver is fake: encrypted Channel frames travel through paired
in-memory BleStreams, never Bluetooth or a network. This does not measure
physical mouse motion, real Windows injection or radio latency.

All native input callbacks except the production F12 emergency return are
isolated by probe_mac_handoff.InputIsolation. Run only on an unlocked desktop.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_mac_handoff import HandoffHarness, InputIsolation, start_with_isolated_input


class FocusTrace:
    """Bounded metadata only; recording never changes an ownership decision."""
    def __init__(self, own_pid, previous_pid, *, location=lambda: {}, limit=256,
                 clock=time.monotonic):
        self.own_pid, self.previous_pid = own_pid, previous_pid
        self.location, self.clock, self.limit = location, clock, limit
        self.lock = threading.RLock()
        self.observers = []
        self.last_stage = 'probe-startup'
        self.evidence = dict(limit=limit, records=[], dropped_records=0,
                             observed_pids=[own_pid, previous_pid],
                             observer_count=0, observers_removed=0,
                             diagnostic_faults=0,
                             scope='Own/previous application activation and read-only capture ownership metadata; no key values, window titles or other application names.')

    def record(self, event, **details):
        at = self.clock()
        try:
            location = self.location()
        except Exception:
            location = {}
        with self.lock:
            if len(self.evidence['records']) >= self.limit:
                self.evidence['dropped_records'] += 1
                return
            self.evidence['records'].append(dict(event=event, at=at,
                last_sample_stage=self.last_stage, **location, **details))

    def fault(self, operation, exc):
        with self.lock:
            self.evidence['diagnostic_faults'] += 1
        self.record('diagnostic_fault', operation=operation, exception_type=type(exc).__name__)

    def activate(self, application, options, purpose):
        self.record('original_app_activate_intent', target_pid=self.previous_pid, purpose=purpose)
        try:
            result = application.activateWithOptions_(options)
        except Exception as exc:
            self.record('original_app_activate_raised', purpose=purpose, exception_type=type(exc).__name__)
            raise
        self.record('original_app_activate_return', purpose=purpose, accepted=bool(result))
        return result

    def wrap_owns_foreground(self, original, snapshot):
        def observed():
            result = original()
            if not result:
                decision_at = self.clock()
                try:
                    self.record('owns_foreground_false', decision_at=decision_at, **snapshot())
                except Exception as exc:
                    self.fault('owns_foreground_snapshot', exc)
            return result
        return observed

    def observe_workspace(self, center, name, application_key):
        def activated(notification):
            try:
                application = (notification.userInfo() or {}).get(application_key)
                pid = int(application.processIdentifier()) if application is not None else None
                if pid in (self.own_pid, self.previous_pid):
                    self.record('workspace_did_activate', activated_pid=pid)
            except Exception as exc:
                self.fault('workspace_notification', exc)
        self._observe(center, name, None, activated)

    def observe_application(self, center, name, application, event):
        def changed(notification):
            self.record(event, application_pid=self.own_pid)
        self._observe(center, name, application, changed)

    def _observe(self, center, name, obj, callback):
        try:
            observer = center.addObserverForName_object_queue_usingBlock_(name, obj, None, callback)
            self.observers.append((center, observer))
            self.evidence['observer_count'] += 1
        except Exception as exc:
            self.fault('observer_install', exc)

    def close(self):
        while self.observers:
            center, observer = self.observers.pop()
            try:
                center.removeObserver_(observer)
                self.evidence['observers_removed'] += 1
            except Exception as exc:
                self.fault('observer_remove', exc)


def native_probe(args):
    import AppKit as A
    import Foundation as F
    import Quartz as Q
    import HIServices
    from sharemouse.mac_capture_ui import get_capture_coordinator

    app = A.NSApplication.sharedApplication()
    ui = get_capture_coordinator()
    before = ui.backend.diagnostics()
    session = Q.CGSessionCopyCurrentDictionary() or {}
    if (before['foreground_bundle'] == 'com.apple.loginwindow'
            or session.get('CGSSessionScreenIsLocked', False)
            or not session.get('kCGSessionOnConsoleKey', True)):
        raise RuntimeError('Unlock the local Mac desktop before --capture')
    if before['foreground_pid'] in (None, before['pid']):
        raise RuntimeError('Start while another unlocked application owns the foreground')
    if not Q.CGPreflightListenEventAccess() or not HIServices.AXIsProcessTrusted():
        raise RuntimeError('Existing Input Monitoring and Accessibility permissions are required; no permission is requested')

    from sharemouse.display import primary_screen
    from sharemouse.input import Input
    from sharemouse.native_mac import Desktop
    from pynput.keyboard import Key

    evidence = dict(mode='native-synthetic-residency', scenario=args.scenario,
                    close_method=args.close_method, cycles_requested=args.cycles,
                    cycles_completed=0, cycles=[], stages=[], errors=[], before=before,
                    stimuli='Synthetic production Input calls on the real HID loop',
                    desktop='Actual Desktop.setup(smoke=True); discovery=False; settings persistence disabled',
                    transport='Compact encrypted Channel over paired in-memory BleStreams',
                    receiver='FakeReceiverInput; no Windows or other native receiver injection',
                    bluetooth_used=False, external_network_used=False,
                    physical_input_verified=False, native_prepare_checks=[], anchor_drift_injections=[],
                    background_idle=dict(requested_seconds=args.background_idle, elapsed_seconds=0,
                                         samples=[], heartbeat_scope='Both production Engine writers/readers remain live over encrypted in-memory Channels; no Bluetooth or physical peer is exercised.'),
                    activity_verification_scope='Checks that the production Desktop retains its NSProcessInfo activity token while Session.running, including main-window close and capture. This is not an OS termination or sleep endurance test.')
    desktop = Desktop.alloc().init().setup(smoke=True)
    if desktop.session.discovery_enabled or desktop.session.browser is not None:
        desktop.cleanup()
        raise RuntimeError('Smoke Desktop unexpectedly enabled device discovery')
    harness = HandoffHarness(primary_screen(), args.side)
    adapter = Input(harness.sender)
    harness.sender.input = adapter
    desktop.session.engine = harness.sender
    original_status = harness.sender.status
    def status(message):
        original_status(message)
        desktop.session.events.put(('status', desktop.session.engine_generation, str(message)))
    harness.sender.status = status
    isolation = InputIsolation(Key.f12)
    saved = tuple(before['cursor_position'])
    original_app = A.NSRunningApplication.runningApplicationWithProcessIdentifier_(before['foreground_pid'])
    errors = evidence['errors']
    verify_park_original = ui.backend.verify_park
    activate_original = ui.backend.activate
    owns_foreground_original = ui.backend.owns_foreground
    warp_original = Q.CGWarpMouseCursorPosition
    drift = dict(enabled=False, injected=False, expected=None, warps=[], cycle=None)
    focus_trace = FocusTrace(before['pid'], before['foreground_pid'],
        location=lambda: dict(phase=getattr(harness, 'phase', None),
                              cycle=getattr(harness, 'current_cycle', None)))
    evidence['focus_trace'] = focus_trace.evidence

    def focus_snapshot():
        # Deliberately avoid diagnostics()/owns_foreground(): this executes at
        # the exact false decision, before the coordinator begins recovery.
        front = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        panel = ui.backend.panel
        key_window = app.keyWindow()
        token = ui.current
        listener = getattr(adapter, 'ml', None)
        cursor = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return dict(foreground_pid=int(front.processIdentifier()) if front else None,
                    cursor_position=[cursor.x, cursor.y],
                    capture_panel_key=bool(panel and panel.isKeyWindow()),
                    capture_panel_visible=bool(panel and panel.isVisible()),
                    key_window_is_capture=bool(panel is not None and key_window is panel),
                    key_window_number=int(key_window.windowNumber()) if key_window else None,
                    application_active=bool(app.isActive()),
                    source_remote=harness.sender.remote, connected=harness.sender.connected,
                    pending_return=harness.sender.pending_return is not None,
                    native_captured=bool(listener and listener._captured),
                    native_hidden_owned=bool(listener and listener._cursor_hidden),
                    native_disassociated_owned=bool(listener and listener._mouse_disassociated),
                    current_token=None if token is None else dict(
                        generation=token.generation, prepared=token.prepared,
                        cancelled=token.cancelled, ready=token.ready.is_set(),
                        park=list(token.park), error_type=type(token.error).__name__ if token.error else None))

    def activate_previous(purpose):
        return focus_trace.activate(original_app, A.NSApplicationActivateIgnoringOtherApps, purpose)

    def tracked_warp(point):
        if drift['enabled']:
            drift['warps'].append(dict(at=time.monotonic(), position=[float(point[0]), float(point[1])]))
        return warp_original(point)

    def activate_with_optional_drift(park):
        activate_original(park)
        if not drift['enabled'] or drift['injected']:
            return
        drift['injected'] = True
        current = Q.CGEventGetLocation(Q.CGEventCreate(None))
        bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
        midpoint = bounds.origin.y + bounds.size.height / 2
        target = (current.x, current.y + (120 if current.y <= midpoint else -120))
        target = (target[0], max(bounds.origin.y, min(bounds.origin.y + bounds.size.height - 1, target[1])))
        record = dict(cycle=drift['cycle'], at=time.monotonic(), original_token_park=list(park),
                      actual_before=[current.x, current.y], probe_requested_position=list(target),
                      production_extra_warps=drift['warps'])
        evidence['anchor_drift_injections'].append(record)
        # This single explicit probe setup warp models a previously forwarded
        # event landing during async activation. Production warps remain tracked
        # through the shared Quartz API; they may not undo the displacement.
        if warp_original(target) != Q.kCGErrorSuccess:
            raise RuntimeError('Probe could not inject the synthetic activation-time cursor displacement')
        actual = Q.CGEventGetLocation(Q.CGEventCreate(None))
        drift['expected'] = (actual.x, actual.y)
        record['actual_after'] = list(drift['expected'])
        if abs(actual.y - current.y) < 80:
            raise RuntimeError('Synthetic anchor drift did not move outside the old capture panel')

    Q.CGWarpMouseCursorPosition = tracked_warp
    ui.backend.activate = activate_with_optional_drift

    def recorded_verify_park(park):
        # Retain the anchor and actual point before a failed verify_park calls
        # Input.stop, which clears capture_token and hides the panel.
        check = dict(at=time.monotonic(), token_park=list(park),
                     activity_held=desktop.activity is not None,
                     session_running=desktop.session.running)
        try:
            check.update(ui.backend.diagnostics())
            frame = ui.backend.panel.frame()
            check['capture_panel_frame_appkit'] = [frame.origin.x, frame.origin.y,
                                                    frame.size.width, frame.size.height]
            verify_park_original(park)
        except Exception as exc:
            check['error'] = str(exc)
            raise
        finally:
            evidence['native_prepare_checks'].append(check)
    ui.backend.verify_park = recorded_verify_park

    def main_call(callback):
        completed, result = threading.Event(), []
        def invoke():
            try:
                result.append(callback())
            except Exception as exc:
                result.append(exc)
            finally:
                completed.set()
        F.NSOperationQueue.mainQueue().addOperationWithBlock_(invoke)
        if not completed.wait(3):
            raise TimeoutError('AppKit main loop did not service the residency probe')
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def hid(callback):
        completed, failures = threading.Event(), []
        def invoke():
            try:
                with isolation.stimulus():
                    callback()
            except Exception as exc:
                failures.append(exc)
            finally:
                completed.set()
        if not adapter.ml.is_alive():
            raise RuntimeError('Native HID listener is no longer running')
        Q.CFRunLoopPerformBlock(adapter.ml._loop, Q.kCFRunLoopDefaultMode, invoke)
        Q.CFRunLoopWakeUp(adapter.ml._loop)
        if not completed.wait(3):
            raise TimeoutError('Native HID loop did not service the synthetic stimulus')
        if failures:
            raise failures[0]

    def state(stage, cycle):
        focus_trace.last_stage = stage
        def take():
            result = ui.backend.diagnostics()
            token = getattr(adapter, 'capture_token', None)
            panel = ui.backend.panel
            frame = panel.frame() if panel is not None else None
            result.update(stage=stage, cycle=cycle, at=time.monotonic(),
                          activation_policy=int(app.activationPolicy()),
                          main_visible=bool(desktop.window.isVisible()),
                          main_key=bool(desktop.window.isKeyWindow()),
                          main_miniaturized=bool(desktop.window.isMiniaturized()),
                          desktop_closed=desktop.closed,
                          session_running=desktop.session.running,
                          activity_held=desktop.activity is not None,
                          session_engine_attached=desktop.session.engine is harness.sender,
                          connected=harness.sender.connected,
                          source_remote=harness.sender.remote,
                          pending_return=harness.sender.pending_return,
                          source_stopped=harness.sender.stop_event.is_set(),
                          receiver_active=harness.receiver.active,
                          receiver_connected=harness.receiver.connected,
                          receiver_stopped=harness.receiver.stop_event.is_set(),
                          native_captured=adapter.ml._captured,
                          native_hidden_owned=adapter.ml._cursor_hidden,
                          native_disassociated_owned=adapter.ml._mouse_disassociated,
                          token_park=None if token is None else list(token.park),
                          capture_panel_frame_appkit=None if frame is None else
                              [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height],
                          main_display_height=Q.CGDisplayBounds(Q.CGMainDisplayID()).size.height,
                          source_metrics=harness.sender.metrics.snapshot(),
                          receiver_metrics=harness.receiver.metrics.snapshot(),
                          receiver_moves=harness.receiver.input.moves)
            return result
        result = main_call(take)
        evidence['stages'].append(result)
        return result

    def settle():
        # The driver sleeps, never the AppKit thread: focus changes, coordinator
        # dispatches, the 50 ms capture monitor and Desktop.tick all execute.
        time.sleep(.2)

    def assert_connected(sample):
        if (not sample['connected'] or sample['source_stopped']
                or sample['desktop_closed'] or not sample['session_engine_attached']):
            raise RuntimeError('Window action stopped or detached the still-connected engine')
        if not sample['session_running'] or not sample['activity_held']:
            raise RuntimeError('The running connection lost its production NSProcessInfo activity token')

    def assert_captured(sample):
        assert_connected(sample)
        if not (sample['source_remote'] and sample['receiver_active']
                and sample['foreground_pid'] == sample['pid']
                and sample['capture_panel_owner_pid'] == sample['pid']
                and sample['capture_panel_key'] and sample['capture_panel_visible']
                and sample['application_cursor_is_transparent']
                and sample['native_captured'] and sample['native_hidden_owned']
                and sample['native_disassociated_owned']):
            raise RuntimeError('Window lifecycle lost native capture, foreground ownership or the transparent cursor')

    def released(sample):
        return not (sample['source_remote'] or sample['receiver_active']
                    or sample['pending_return'] is not None
                    or sample['capture_panel_visible'] or sample['native_captured']
                    or sample['native_hidden_owned'] or sample['native_disassociated_owned'])

    def wait_released(stage, cycle, visible):
        deadline = time.monotonic() + 3
        while True:
            sample = state(stage, cycle)
            assert_connected(sample)
            if released(sample) and sample['main_visible'] == visible:
                return sample
            if time.monotonic() >= deadline:
                raise TimeoutError('Local restoration did not release native capture with the expected main-window visibility')
            time.sleep(.02)

    def close_main(cycle):
        method = args.close_method
        if method == 'alternate':
            method = 'red-button' if cycle % 2 else 'command-w'
        if method == 'red-button':
            main_call(lambda: desktop.window.performClose_(None))
        else:
            # This is the production Command-W menu action, not an injected key.
            main_call(desktop.hide_window)
        settle()
        sample = state('closed-main-' + method, cycle)
        if sample['main_visible']:
            raise RuntimeError('The close action left the main window visible')
        assert_connected(sample)
        return sample

    def prepare(cycle, hidden):
        main_call(desktop.show_window)
        settle()
        sample = state('regular-main-shown', cycle)
        if (sample['activation_policy'] != int(A.NSApplicationActivationPolicyRegular)
                or not sample['main_visible'] or not sample['main_key']):
            raise RuntimeError('Desktop.show_window did not show a Regular main window')
        assert_connected(sample)
        if hidden:
            close_main(cycle)
            # Model the normal background case: a different local application
            # owns the desktop before crossing into the receiver.
            main_call(lambda: activate_previous('hidden-background-setup'))
            settle()
            state('hidden-background-before-entry', cycle)

    def background_idle(cycle):
        """Leave AppKit and both encrypted Engine loops running while hidden."""
        report = evidence['background_idle']
        started = time.monotonic()
        deadline = started + args.background_idle
        previous = None
        try:
            while True:
                sample = state('hidden-background-idle', cycle)
                assert_connected(sample)
                if (sample['main_visible'] or not released(sample)
                        or not sample['receiver_connected'] or sample['receiver_stopped']):
                    raise RuntimeError('Hidden background idle lost the connection or unexpectedly showed/captured the desktop')
                with harness.lock:
                    heartbeats = {name: sum(event['endpoint'] == name and event['type'] == 'ping'
                                            for event in harness.events)
                                  for name in ('source', 'receiver')}
                now = time.monotonic()
                entry = dict(elapsed_seconds=now - started, state=sample,
                             heartbeat_frames_sent=heartbeats,
                             source_frames_received=sample['source_metrics']['counters'].get('received_frames', 0),
                             receiver_frames_received=sample['receiver_metrics']['counters'].get('received_frames', 0))
                report['samples'].append(entry)
                report['elapsed_seconds'] = entry['elapsed_seconds']
                if previous and entry['elapsed_seconds'] - previous['elapsed_seconds'] >= 2.5:
                    if (any(entry['heartbeat_frames_sent'][name] <= previous['heartbeat_frames_sent'][name]
                            for name in ('source', 'receiver'))
                            or entry['source_frames_received'] <= previous['source_frames_received']
                            or entry['receiver_frames_received'] <= previous['receiver_frames_received']):
                        raise RuntimeError('Encrypted bidirectional heartbeat traffic stalled during background idle')
                previous = entry
                if now >= deadline:
                    break
                next_sample = min(deadline, started + (int((now - started) / 5) + 1) * 5)
                # Only the driver waits. Desktop.tick, focus events, the HID
                # loop and both real Engine writer/reader pairs keep running.
                while time.monotonic() < next_sample:
                    if (harness.closed.is_set() or harness.errors
                            or harness.sender.stop_event.is_set() or harness.receiver.stop_event.is_set()):
                        raise RuntimeError('Connection stopped during hidden background idle: ' + '; '.join(harness.errors))
                    harness.closed.wait(min(.25, max(0, next_sample - time.monotonic())))
        finally:
            report['elapsed_seconds'] = time.monotonic() - started

    close_performed = set()
    def sample_capture(stage, cycle, departure=None):
        if stage == 'restored':
            visible = args.scenario == 'visible-control'
            sample = wait_released(stage, cycle, visible)
            expected_x = harness.sender.local.width - 12 if args.side == 'right' else 12
            if abs(sample['cursor_position'][0] - expected_x) > 1.5:
                raise RuntimeError('Native release did not restore the local-side cursor position')
            return sample
        sample = state(stage, cycle)
        assert_captured(sample)
        if stage == 'ready' and args.scenario == 'close-active' and cycle not in close_performed:
            close_performed.add(cycle)
            close_main(cycle)
            sample = state('active-after-main-close', cycle)
            assert_captured(sample)
        if departure and any(abs(a - b) > 1.5 for a, b in
                             zip(sample['cursor_position'], departure['cursor_position'])):
            raise RuntimeError('Window action moved the captured local cursor')
        return sample

    def enter_hidden_prelude(cycle, stage):
        sign = 1 if args.side == 'right' else -1
        width, height = harness.sender.local.width, harness.sender.local.height
        inner, edge = (width - 41, width - 1) if sign == 1 else (40, 0)
        y = (height - 1) // 2
        def enter():
            adapter.warp(inner, y)
            adapter.move(inner, y, dx=-sign * 40)
            adapter.warp(edge, y)
            adapter.move(edge, y, dx=sign * 40)
        hid(enter)
        harness.wait(lambda: harness.receiver.active and harness.sender.remote,
                     'Hidden main window could not enter before the lifecycle action')
        sample = state(stage, cycle)
        assert_captured(sample)
        return sample

    def reopen_prelude(cycle):
        enter_hidden_prelude(cycle, 'reopen-prelude-ready')
        main_call(desktop.show_window)
        # Showing the main window while captured must deliberately complete the
        # normal leave/left + native-release path, not trip the focus monitor.
        sample = wait_released('reopen-active-local-restored', cycle, True)
        settle()
        assert_connected(state('reopen-active-settled', cycle))
        return sample

    def focus_loss_prelude(cycle):
        ready = enter_hidden_prelude(cycle, 'focus-loss-prelude-ready')
        epoch = harness.sender.epoch
        with harness.lock:
            wire_start = len(harness.events)
        harness.phase = 'intentional-external-foreground-activation'
        main_call(lambda: activate_previous('intentional-focus-loss'))
        restored = wait_released('focus-loss-active-local-restored', cycle, False)

        def return_barrier_recorded():
            with harness.lock:
                records = harness.events[wire_start:]
                return (any(e['endpoint'] == 'source' and e['type'] == 'leave' and e['epoch'] == epoch
                            for e in records)
                        and any(e['endpoint'] == 'receiver' and e['type'] == 'left' and e['epoch'] == epoch
                                for e in records))
        harness.wait(return_barrier_recorded,
                     'Focus loss did not complete the production leave/left barrier on the same connection')
        settle()
        settled = state('focus-loss-active-settled', cycle)
        assert_connected(settled)
        if settled['main_visible'] or not released(settled):
            raise RuntimeError('Focus loss reopened the hidden main window or retained native capture')
        with harness.lock:
            barrier = [dict(event) for event in harness.events[wire_start:]
                       if event['type'] in ('leave', 'left') and event['epoch'] == epoch]
        return dict(epoch=epoch, ready=ready, restored=restored, settled=settled,
                    return_barrier=barrier, same_engine_preserved=True)

    def anchor_drift_prelude(cycle):
        """Inject one activation race, then test its input/ACK/return lifecycle."""
        sign = 1 if args.side == 'right' else -1
        screen = harness.sender.local
        inner, edge = (screen.width - 41, screen.width - 1) if sign == 1 else (40, 0)
        y = (screen.height - 1) // 2
        before_acks = harness.sender.metrics.snapshot()['observations'].get('processed_ack_ms', 0)
        before_buttons = len(harness.receiver.input.buttons)
        drift.update(enabled=False, injected=False, expected=None, warps=[], cycle=cycle)
        def enter():
            # Test-setup positions bypass tracking; only the subsequent real
            # production capture is inside the no-additional-warp window.
            for x, dx in ((inner, -sign * 40), (edge, sign * 40)):
                if warp_original((x, y)) != Q.kCGErrorSuccess:
                    raise RuntimeError('Probe could not position the source cursor before the drift race')
                if x == edge:
                    drift['enabled'] = True
                adapter.move(x, y, dx=dx)
        harness.phase = 'anchor-drift-enter'
        hid(enter)
        harness.wait(lambda: harness.sender.remote and harness.receiver.active,
                     'Activation-time cursor drift prevented remote entry')
        ready = state('anchor-drift-native-ready', cycle)
        assert_captured(ready)
        if not drift['injected'] or drift['expected'] is None:
            raise RuntimeError('The activation-time anchor drift hook did not execute')
        if any(abs(a - b) > 1.5 for a, b in zip(ready['cursor_position'], drift['expected'])):
            raise RuntimeError('Production capture moved the cursor away from its injected current position')
        if drift['warps']:
            raise RuntimeError('Production capture issued an extra CGWarp instead of following the current cursor with its panel')
        panel = ready['capture_panel_frame_appkit']
        px, py = ready['cursor_position'][0], ready['main_display_height'] - ready['cursor_position'][1]
        if panel is None or not (panel[0] <= px <= panel[0] + panel[2] and panel[1] <= py <= panel[1] + panel[3]):
            raise RuntimeError('Production capture panel did not follow the injected current cursor position')
        # Keep the no-warp assertion active through input and active holding.
        for frame in range(args.frames):
            old_moves = harness.receiver.input.moves
            hid(lambda f=frame: adapter.mac_move(edge, y, sign * 8, 1 if f % 2 else -1))
            harness.wait(lambda: harness.receiver.input.moves > old_moves
                         and harness.sender.delivery.pending_count == 0,
                         'Anchor-drift pointer input did not receive its processed ACK')
            assert_captured(state('anchor-drift-input-ack', cycle))
        from pynput.mouse import Button
        def click():
            adapter.click(edge, y, Button.left, True)
            adapter.click(edge, y, Button.left, False)
        hid(click)
        harness.wait(lambda: len(harness.receiver.input.buttons) == before_buttons + 2
                     and harness.sender.delivery.pending_count == 0,
                     'Anchor-drift button pair did not receive its processed ACK')
        if [item['down'] for item in harness.receiver.input.buttons[before_buttons:]] != [True, False]:
            raise RuntimeError('Anchor-drift button order was lost')
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            held = state('anchor-drift-active-holding', cycle)
            assert_captured(held)
            if drift['warps'] or any(abs(a - b) > 1.5 for a, b in zip(held['cursor_position'], drift['expected'])):
                raise RuntimeError('Production warped or moved the captured cursor during anchor-drift input')
            time.sleep(.05)
        drift['enabled'] = False  # The normal return position warp is required.
        with harness.sender.state_lock:
            cursor = harness.sender.peer_cursor
            distance = cursor.x if sign == 1 else screen.width - 1 - cursor.x
            gain = harness.sender.mouse_speed * screen.scale / harness.sender.local.scale
            reverse = -sign * (distance + cursor.return_margin + 16) / gain
        hid(lambda: adapter.mac_move(edge, y, reverse, 0))
        restored = wait_released('anchor-drift-restored', cycle, False)
        after_acks = harness.sender.metrics.snapshot()['observations'].get('processed_ack_ms', 0)
        if after_acks <= before_acks:
            raise RuntimeError('Anchor-drift cycle completed without processed ACKs')
        return dict(ready=ready, restored=restored, processed_acks=after_acks - before_acks,
                    production_extra_warp_count=len(drift['warps']),
                    production_extra_warps=list(drift['warps']), injected_current_position=list(drift['expected']))

    def stop_run_loop():
        app.stop_(None)
        event = A.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            A.NSEventTypeApplicationDefined, A.NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
        app.postEvent_atStart_(event, True)

    def driver():
        try:
            for listener in adapter.listeners:
                listener.wait()
            harness.start()
            main_call(desktop.refresh)
            for cycle in range(1, args.cycles + 1):
                harness.current_cycle, harness.phase = cycle, 'window-lifecycle-setup'
                hidden = args.scenario in ('hidden-entry', 'reopen-active', 'anchor-drift', 'focus-loss-active')
                prepare(cycle, hidden)
                if cycle == 1 and args.background_idle:
                    harness.phase = 'hidden-background-idle'
                    background_idle(cycle)
                prelude = reopen_prelude(cycle) if args.scenario == 'reopen-active' else None
                anchor_prelude = anchor_drift_prelude(cycle) if args.scenario == 'anchor-drift' else None
                focus_prelude = focus_loss_prelude(cycle) if args.scenario == 'focus-loss-active' else None
                if prelude is not None:
                    close_main(cycle)
                    main_call(lambda: activate_previous('reopen-background-setup'))
                    settle()
                result = harness.exercise_cycle(cycle, hid, sample_capture, args.seconds, args.frames)
                if prelude is not None:
                    result['reopen_prelude'] = prelude
                if anchor_prelude is not None:
                    result['anchor_drift_prelude'] = anchor_prelude
                if focus_prelude is not None:
                    result['focus_loss_prelude'] = focus_prelude
                evidence['cycles'].append(result)
                evidence['cycles_completed'] = cycle
        except Exception as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc))
            if not harness.failures:
                harness.failures.append(harness.failure_snapshot(exc))
            try:
                state('failure-before-cleanup', harness.current_cycle)
            except Exception as sample_error:
                errors.append('Failure snapshot: ' + str(sample_error))
        finally:
            drift['enabled'] = False
            try:
                if adapter.ml.is_alive():
                    hid(lambda: harness.sender.return_local(confirmed=True))
            except Exception as exc:
                errors.append('Emergency local return: ' + str(exc))
            harness.close()
            for listener in adapter.listeners:
                if listener.is_alive():
                    listener.join(2)
            def finish():
                try:
                    if adapter.ml._captured or adapter.ml._cursor_hidden or adapter.ml._mouse_disassociated:
                        adapter.ml._release_native()
                    ui.close()
                    desktop.cleanup()
                    desktop.window.orderOut_(None)
                    if original_app is not None and not original_app.isTerminated():
                        activate_previous('final-foreground-restore')
                    if Q.CGWarpMouseCursorPosition(saved) != Q.kCGErrorSuccess:
                        errors.append('Final saved cursor restoration failed')
                    evidence.update(after=ui.backend.diagnostics(), status=harness.status,
                                    wire_events=harness.events, failures=harness.failures,
                                    native_input_isolation=isolation.snapshot(),
                                    source_metrics=harness.sender.metrics.snapshot(),
                                    receiver_metrics=harness.receiver.metrics.snapshot(),
                                    workers_alive=[t.name for t in harness.threads if t.is_alive()],
                                    listeners_alive=[type(l).__name__ for l in adapter.listeners if l.is_alive()])
                    errors.extend(harness.errors)
                    if evidence['workers_alive'] or evidence['listeners_alive']:
                        errors.append('A probe worker or native listener did not stop')
                except Exception as exc:
                    errors.append('Final recovery: ' + str(exc))
                finally:
                    stop_run_loop()
            F.NSOperationQueue.mainQueue().addOperationWithBlock_(finish)

    try:
        focus_trace.observe_workspace(A.NSWorkspace.sharedWorkspace().notificationCenter(),
                                      A.NSWorkspaceDidActivateApplicationNotification,
                                      A.NSWorkspaceApplicationKey)
        notification_center = F.NSNotificationCenter.defaultCenter()
        focus_trace.observe_application(notification_center, A.NSApplicationDidBecomeActiveNotification,
                                        app, 'application_did_become_active')
        focus_trace.observe_application(notification_center, A.NSApplicationDidResignActiveNotification,
                                        app, 'application_did_resign_active')
        ui.backend.owns_foreground = focus_trace.wrap_owns_foreground(owns_foreground_original, focus_snapshot)
        start_with_isolated_input(adapter, isolation)
        F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            .2, False, lambda timer: threading.Thread(target=driver, name='residency-probe-driver', daemon=True).start())
        app.run()
    finally:
        # Restore monkeypatched APIs even if native/UI cleanup itself raises.
        drift['enabled'] = False
        ui.backend.verify_park = verify_park_original
        ui.backend.activate = activate_original
        ui.backend.owns_foreground = owns_foreground_original
        Q.CGWarpMouseCursorPosition = warp_original
        try:
            harness.close()
            desktop.cleanup()
            ui.close()
        finally:
            try:
                warp_original(saved)
            finally:
                focus_trace.close()
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', action='store_true', help='Explicitly allow native desktop activation, cursor warps and capture')
    parser.add_argument('--scenario', choices=('hidden-entry', 'close-active', 'reopen-active', 'visible-control', 'anchor-drift', 'focus-loss-active'), default='hidden-entry')
    parser.add_argument('--close-method', choices=('red-button', 'command-w', 'alternate'), default='alternate')
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--frames', type=int, default=8)
    parser.add_argument('--seconds', type=float, default=.3)
    parser.add_argument('--background-idle', type=float, default=0,
                        help='Keep the first hidden cycle idle before entry for 0..120 seconds; the main AppKit loop remains live')
    parser.add_argument('--side', choices=('left', 'right'), default='right')
    parser.add_argument('--output', type=Path, default=Path('/tmp/share-monkey-native-residency.json'))
    args = parser.parse_args()
    if not 1 <= args.cycles <= 30 or not 3 <= args.frames <= 30 or not .01 <= args.seconds <= 5:
        parser.error('cycles 1..30, frames 3..30 and seconds .01..5 required')
    if args.scenario == 'focus-loss-active' and args.cycles < 2:
        parser.error('focus-loss-active requires at least 2 cycles to verify repeated interruption and re-entry')
    if not 0 <= args.background_idle <= 120:
        parser.error('background-idle must be 0..120 seconds')
    if args.background_idle and args.scenario not in ('hidden-entry', 'reopen-active', 'anchor-drift'):
        parser.error('background-idle requires hidden-entry, reopen-active or anchor-drift')
    if args.capture and sys.platform != 'darwin':
        parser.error('--capture requires macOS')
    evidence = (native_probe(args) if args.capture else
                dict(mode='no-native-actions', scenario=args.scenario, cycles_completed=0,
                     errors=[], native_started=False, capture_requires_explicit_flag=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(mode=evidence['mode'], scenario=args.scenario,
                         cycles_completed=evidence['cycles_completed'], errors=evidence['errors'],
                         output=str(args.output)), ensure_ascii=False))
    if evidence['errors'] or args.capture and evidence['cycles_completed'] != args.cycles:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
