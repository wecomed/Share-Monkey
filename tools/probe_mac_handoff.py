"""Exercise the full local handoff state machine without a remote computer.

Default: read-only AppKit diagnostics. ``--capture`` explicitly permits local
cursor warps, real HID listeners and the production foreground capture panel.
The stimuli are synthetic Input.move/mac_move calls on that real HID loop;
they are not physical mouse events. Native motion, buttons, scroll and non-F12
keyboard callbacks are counted and never forwarded into the engine. Only
explicit synthetic stimulus blocks call the original production Input methods;
real F12 callbacks always retain their production emergency return behavior. A fake
receiver never injects OS input.
Both production Engine writers/readers exchange real compact encrypted frames
through paired in-memory BleStreams. No Bluetooth, TCP or remote I/O is used.
``--self-test`` checks the same handoff/ACK path with only native APIs mocked.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProbeMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.observations, self.counters, self.gauges = {}, {}, {}

    def observe(self, name, value):
        with self.lock:
            self.observations[name] = self.observations.get(name, 0) + 1

    def increment(self, name, n=1):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def gauge(self, name, value):
        with self.lock:
            self.gauges[name] = value

    def close(self):
        pass

    def snapshot(self):
        with self.lock:
            return dict(observations=dict(self.observations), counters=dict(self.counters),
                        gauges=dict(self.gauges))


class InputIsolation:
    """Keep native callback bindings isolated, including dynamic keyboard lambdas."""
    METHODS = ('move', 'mac_move', 'click', 'scroll', 'key')

    def __init__(self, emergency_key):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.emergency_key = emergency_key
        self.events = self.injected = 0
        self.absolute_dx = self.absolute_dy = self.max_delta = 0.
        self.ignored = {name: 0 for name in self.METHODS}
        self.synthetic = {name: 0 for name in self.METHODS}
        self.emergency_callbacks = 0
        self.originals, self.wrappers = {}, {}

    @contextmanager
    def stimulus(self):
        previous = getattr(self.local, 'in_stimulus', False)
        self.local.in_stimulus = True
        try:
            yield
        finally:
            self.local.in_stimulus = previous

    def bind(self, adapter):
        for name in self.METHODS:
            original = getattr(adapter, name)
            self.originals[name] = original
            def callback(*args, _name=name, _original=original, **kwargs):
                synthetic = getattr(self.local, 'in_stimulus', False)
                emergency = _name == 'key' and bool(args) and args[0] == self.emergency_key
                if synthetic or emergency:
                    with self.lock:
                        if synthetic:
                            self.synthetic[_name] += 1
                        else:
                            self.emergency_callbacks += 1
                    return _original(*args, **kwargs)
                self.record(_name, args, kwargs)
            self.wrappers[name] = callback
            setattr(adapter, name, callback)

    def record(self, name, args, kwargs):
        with self.lock:
            self.ignored[name] += 1
            if name == 'mac_move':
                dx, dy = args[2:4]
                injected = args[4] if len(args) > 4 else kwargs.get('injected', False)
                self.events += 1
                self.injected += int(bool(injected))
                self.absolute_dx += abs(dx)
                self.absolute_dy += abs(dy)
                self.max_delta = max(self.max_delta, abs(dx), abs(dy))

    def snapshot(self):
        with self.lock:
            return dict(native_delta_callbacks=self.events, callbacks_flagged_injected=self.injected,
                        callbacks_not_flagged_injected=self.events - self.injected,
                        total_absolute_dx=self.absolute_dx, total_absolute_dy=self.absolute_dy,
                        max_absolute_component=self.max_delta, forwards_to_engine=False,
                        measures_physical_motion=False,
                        ignored_native_callbacks=dict(self.ignored),
                        synthetic_production_dispatches=dict(self.synthetic),
                        real_f12_callbacks_forwarded=self.emergency_callbacks,
                        records_key_values=False,
                        boundary='Permanent Input method wrappers dispatch only within a thread-local synthetic stimulus block. Native motion/buttons/scroll/non-F12 keys are aggregate-only. Real F12 always invokes the original production key callback. Native flags do not distinguish all setup-warp events from hardware events. This is not a physical-motion measurement.')


def start_with_isolated_input(adapter, isolation):
    """Bindings stay wrapped until all listeners stop; keyboard resolves self.key dynamically."""
    isolation.bind(adapter)
    adapter.start()
    if adapter.ml.on_delta is not isolation.wrappers['mac_move']:
        raise RuntimeError('Native input isolation was not bound before listener startup')


class FakeReceiverInput:
    """Receiver state only: none of these methods calls an OS input API."""
    def __init__(self):
        self.listeners = []
        self.position = None
        self.visible = False
        self.moves = self.entries = 0
        self.buttons = []
        self.held_keys, self.held_buttons = {}, set()
        self.mouse = SimpleNamespace(scroll=lambda *args: None)

    def move_to(self, position):
        self.position = tuple(position)
        self.moves += 1

    def set_remote_active(self, active):
        self.visible = bool(active)
        self.entries += int(bool(active))

    def inject_key(self, key, down):
        identity = tuple(sorted(key.items()))
        if down:
            self.held_keys[identity] = key
        else:
            self.held_keys.pop(identity, None)

    def inject_button(self, button, down):
        self.buttons.append(dict(button=button.name, down=down, position=self.position))
        if down:
            self.held_buttons.add(button)
        else:
            self.held_buttons.discard(button)

    def release(self):
        self.held_keys.clear()
        self.held_buttons.clear()

    def stop(self):
        self.release()
        self.visible = False


class RecordedChannel:
    def __init__(self, channel, name, events, lock):
        self.channel, self.name, self.events, self.lock = channel, name, events, lock

    def send(self, message):
        self.channel.send(message)
        with self.lock:
            self.events.append(dict(endpoint=self.name, type=message.get('type'),
                                    epoch=message.get('epoch'), delivery=message.get('_delivery'),
                                    packet=message.get('packet'), down=message.get('down'), at=time.monotonic()))

    def recv(self):
        return self.channel.recv()


class HandoffHarness:
    def __init__(self, screen, side='right'):
        from sharemouse.bluetooth_transport import BleStream
        from sharemouse.engine import Engine
        self.lock = threading.Lock()
        self.events, self.errors, self.status = [], [], []
        self.failures = []
        self.phase, self.current_cycle, self.current_frame = 'not-started', None, None
        self.cycle_progress = {}
        self.closed = threading.Event()
        self.threads = []
        password = 'local-handoff-probe-no-radio'
        def status(text):
            with self.lock:
                self.status.append(str(text))
        self.sender = Engine(True, screen, side, password, '', 0, status,
                             clipboard=False, transport='bluetooth')
        self.receiver = Engine(False, screen, side, password, '', 0, status,
                               clipboard=False, transport='bluetooth')
        self.receiver.input = FakeReceiverInput()
        self.sender.metrics, self.receiver.metrics = ProbeMetrics(), ProbeMetrics()
        left, right = BleStream(timeout=3), BleStream(timeout=3)
        left.set_writer(lambda data, timeout: right.feed(data))
        right.set_writer(lambda data, timeout: left.feed(data))
        self.streams = left, right
        self.password = password

    def _spawn(self, name, action):
        def run():
            try:
                action()
            except Exception as exc:
                if not self.closed.is_set():
                    with self.lock:
                        self.errors.append(name + ': ' + str(exc))
                    self.sender.stop_event.set()
                    self.receiver.stop_event.set()
                    self.sender.write_wake.set()
                    self.receiver.write_wake.set()
        thread = threading.Thread(target=run, name='handoff-probe-' + name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def start(self):
        from sharemouse.protocol import Channel
        channels = {}
        def handshake(name, stream, server):
            channels[name] = Channel.handshake(stream, self.password, server=server, compact=True)
        jobs = [self._spawn('source-handshake', lambda: handshake('source', self.streams[0], True)),
                self._spawn('receiver-handshake', lambda: handshake('receiver', self.streams[1], False))]
        for thread in jobs:
            thread.join(5)
        if self.errors or any(thread.is_alive() for thread in jobs):
            raise RuntimeError('Local compact handshake failed: ' + '; '.join(self.errors))
        for name, engine in (('source', self.sender), ('receiver', self.receiver)):
            engine.peer_screen = engine.local
            engine.connected = True
            engine.sock = self.streams[0 if name == 'source' else 1]
            channel = RecordedChannel(channels[name], name, self.events, self.lock)
            self._spawn(name + '-writer', lambda e=engine, c=channel: e.writer(c))
            def receive(e=engine, c=channel):
                while not self.closed.is_set() and not e.stop_event.is_set():
                    e.receive(c.recv())
            self._spawn(name + '-reader', receive)

    def wait(self, condition, description, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            if self.errors or self.sender.stop_event.is_set() or self.receiver.stop_event.is_set():
                raise RuntimeError(description + ': ' + '; '.join(self.errors + self.status[-4:]))
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            self.closed.wait(.005)

    def failure_snapshot(self, error):
        with self.sender.state_lock:
            cursor = self.sender.peer_cursor
            adapter = self.sender.input
            edge = getattr(adapter, 'edge_switch', None)
            source = dict(remote=self.sender.remote, connected=self.sender.connected,
                          epoch=self.sender.epoch, pending_return=self.sender.pending_return,
                          source_cursor=None if cursor is None else dict(x=cursor.x, y=cursor.y,
                              return_pressure=cursor.return_pressure, return_margin=cursor.return_margin),
                          pending_credit=self.sender.delivery.pending_count,
                          outgoing=self.sender.outgoing.qsize(),
                          physical_count=len(getattr(adapter, 'physical', ())),
                          button_count=len(getattr(adapter, 'physical_buttons', ())),
                          edge_armed=getattr(edge, 'armed', None), last_x=getattr(edge, 'last_x', None))
        with self.receiver.state_lock:
            receiver = dict(active=self.receiver.active, epoch=self.receiver.received_epoch,
                            receiver_position=self.receiver.input.position,
                            moves=self.receiver.input.moves,
                            buttons=list(self.receiver.input.buttons),
                            held_buttons=len(self.receiver.input.held_buttons))
        with self.lock:
            recent_events = list(self.events[-20:])
        return dict(cycle=self.current_cycle, phase=self.phase, frame=self.current_frame,
                    error=str(error), error_type=type(error).__name__, source=source,
                    receiver=receiver, partial_cycle=dict(self.cycle_progress),
                    source_metrics=self.sender.metrics.snapshot(), recent_wire_events=recent_events)

    def exercise_cycle(self, cycle, hid, snapshot, seconds=.2, frames=8):
        self.current_cycle, self.current_frame, self.phase = cycle, None, 'enter-setup'
        self.cycle_progress = dict(cycle=cycle, target_positions=[])
        try:
            return self._exercise_cycle(cycle, hid, snapshot, seconds, frames)
        except Exception as exc:
            self.failures.append(self.failure_snapshot(exc))
            raise

    def _exercise_cycle(self, cycle, hid, snapshot, seconds=.2, frames=8):
        adapter, screen = self.sender.input, self.sender.local
        sign = 1 if self.sender.side == 'right' else -1
        edge, inner = (screen.width - 1, screen.width - 41) if sign == 1 else (0, 40)
        fraction = (.05, .5, .95)[(cycle - 1) % 3]
        self.cycle_progress['fraction'] = fraction
        y = round((screen.height - 1) * fraction)
        actual_departure = []
        before_acks = self.sender.metrics.snapshot()['observations'].get('processed_ack_ms', 0)
        def enter():
            # Move the real local pointer only in explicit native capture mode;
            # self-test supplies a fake warp. Input.move remains production code.
            adapter.warp(inner, y)
            adapter.move(inner, y, dx=-sign * 40)
            adapter.warp(edge, y)
            actual_departure.append(tuple(adapter.mouse.position))
            self.cycle_progress['actual_departure_position'] = actual_departure[0]
            adapter.move(edge, y, dx=sign * 40)
        hid(enter)
        self.phase = 'waiting-enter'
        self.wait(lambda: self.receiver.active and self.sender.remote,
                  'Receiver did not enter through the production edge path')
        self.phase = 'ready-native-verification'
        departure = snapshot('ready', cycle)
        self.cycle_progress['actual_departure_position'] = actual_departure[0]
        if 'cursor_position' in departure and any(abs(a - b) > 1.5 for a, b in
                zip(departure['cursor_position'], actual_departure[0])):
            raise RuntimeError('Entry capture moved the local cursor away from its actual departure point')
        before_moves = self.receiver.input.moves
        before_buttons = len(self.receiver.input.buttons)
        positions = [self.receiver.input.position]
        self.cycle_progress['target_positions'] = positions
        step = min(8, (screen.width - 10) / (frames + 2))
        for frame in range(frames):
            self.phase, self.current_frame = 'outward-input-and-processed-ack', frame
            delta = (sign * step, 1 if frame % 2 else -1)
            self.cycle_progress['intended_delta'] = delta
            previous_moves = self.receiver.input.moves
            hid(lambda f=frame: adapter.mac_move(edge, y, sign * step, 1 if f % 2 else -1))
            self.wait(lambda: self.receiver.input.moves > previous_moves
                      and self.sender.delivery.pending_count == 0,
                      'Outward input did not receive its processed ACK')
            if not self.sender.remote or not self.receiver.active:
                raise RuntimeError('Outward input caused an unintended immediate return')
            position = self.receiver.input.position
            expected = self.receiver.local.clamp(positions[-1][0] + delta[0], positions[-1][1] + delta[1])
            self.cycle_progress['expected_target_position'] = expected
            if any(abs(actual - intended) > .001 for actual, intended in zip(position, expected)):
                raise RuntimeError('Target pointer did not match the isolated synthetic delta (see failure snapshot)')
            positions.append(position)
            if frame == frames // 2:
                self.phase = 'ordered-button-pair-and-processed-ack'
                from pynput.mouse import Button
                def click():
                    adapter.click(edge, y, Button.left, True)
                    adapter.click(edge, y, Button.left, False)
                hid(click)
                self.wait(lambda: len(self.receiver.input.buttons) == before_buttons + 2
                          and self.sender.delivery.pending_count == 0,
                          'Ordered mouse button events did not receive their processed ACKs')
                clicks = self.receiver.input.buttons[before_buttons:]
                if ([event['down'] for event in clicks] != [True, False]
                        or any(event['position'] != position for event in clicks)
                        or self.receiver.input.held_buttons):
                    raise RuntimeError('Mouse button order or its pointer barrier was lost')
            time.sleep(.01)
        self.wait(lambda: self.receiver.input.moves > before_moves,
                  'Fake receiver never injected the outward pointer snapshots')
        deadline = time.monotonic() + seconds
        self.phase = 'holding-active-handoff'
        while time.monotonic() < deadline:
            if not self.sender.remote or not self.receiver.active:
                raise RuntimeError('Handoff did not remain active while holding')
            snapshot('holding', cycle, departure)
            self.closed.wait(min(.05, max(0, deadline - time.monotonic())))
        with self.sender.state_lock:
            cursor = self.sender.peer_cursor
            distance = cursor.x if sign == 1 else screen.width - 1 - cursor.x
            gain = self.sender.mouse_speed * screen.scale / self.sender.local.scale
            reverse = -sign * (distance + cursor.return_margin + 16) / gain
        self.phase = 'explicit-reverse-return'
        hid(lambda: adapter.mac_move(edge, y, reverse, 0))
        self.phase = 'waiting-leave-left'
        self.wait(lambda: not self.sender.remote and self.sender.pending_return is None
                  and not self.receiver.active, 'Explicit reverse gesture did not complete leave/left')
        self.phase = 'native-restoration-verification'
        restored = snapshot('restored', cycle)
        after_acks = self.sender.metrics.snapshot()['observations'].get('processed_ack_ms', 0)
        if after_acks <= before_acks:
            raise RuntimeError('Cycle completed without a processed input ACK')
        self.phase = 'completed'
        return dict(cycle=cycle, epoch=self.sender.epoch, processed_acks=after_acks - before_acks,
                    pointer_injections=self.receiver.input.moves - before_moves,
                    fraction=fraction, actual_departure_position=actual_departure[0],
                    target_positions=positions, button_events=self.receiver.input.buttons[before_buttons:],
                    departure=departure, restored=restored)

    def close(self):
        self.closed.set()
        for engine in (self.sender, self.receiver):
            try:
                engine.stop()
            except Exception as exc:
                self.errors.append('cleanup: ' + str(exc))
        for stream in self.streams:
            stream.close()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(2)
        alive = [thread.name for thread in self.threads if thread.is_alive()]
        if alive:
            self.errors.append('Unstopped workers: ' + ', '.join(alive))


def self_test(cycles, side='right', frames=8):
    """Production edge/send/writer/receive/ACK logic; all native APIs are fake."""
    from unittest.mock import patch
    from enum import Enum
    button = Enum('Button', 'left right middle')
    key = Enum('Key', 'f12 shift')
    fake_mouse = SimpleNamespace(Button=button)
    fake_pynput = SimpleNamespace(keyboard=SimpleNamespace(Key=key), mouse=fake_mouse)
    with patch.dict(sys.modules, {'pynput': fake_pynput}):
        from sharemouse.input import Input
    from sharemouse.core import Screen
    from sharemouse.switching import EdgeSwitch
    harness = HandoffHarness(Screen(1920, 1080), side)
    adapter = Input.__new__(Input)
    adapter.engine = harness.sender
    adapter.listeners, adapter.physical, adapter.physical_buttons = [], set(), set()
    adapter.edge_switch = EdgeSwitch()
    adapter.ml = SimpleNamespace(_captured=False)
    adapter.capture_token = None
    adapter.mouse = SimpleNamespace(position=(960, 540))
    adapter.warp = lambda x, y: setattr(adapter.mouse, 'position', (x, y))
    adapter.capture = lambda: setattr(adapter.ml, '_captured', True)
    adapter.wait_capture_ready = lambda stop: not stop.is_set()
    adapter.release = lambda: None
    def restore(x, y):
        adapter.ml._captured = False
        adapter.edge_switch.returned(x, harness.sender.local.width, harness.sender.side)
    adapter.restore_local = restore
    adapter.stop = lambda: setattr(adapter.ml, '_captured', False)
    adapter.start = lambda: setattr(adapter.ml, 'on_delta', adapter.mac_move)
    harness.sender.input = adapter
    isolation = InputIsolation(key.f12)
    results = []
    try:
        with patch('sharemouse.input.sys', SimpleNamespace(platform='darwin')), \
                patch.dict(sys.modules, {'pynput': fake_pynput, 'pynput.mouse': fake_mouse}):
            start_with_isolated_input(adapter, isolation)
            harness.start()
            def fake_hid(action):
                # Mix every native callback kind with each stimulus. Holding
                # native Shift/left down must never close the entry gate.
                adapter.ml.on_delta(0, 0, 20000, -20000, False)
                adapter.move(1919, 100, dx=20000)
                adapter.click(0, 0, button.left, True)
                adapter.scroll(0, 0, 0, 100)
                adapter.key(key.shift, True)
                with isolation.stimulus():
                    action()
            for cycle in range(1, cycles + 1):
                results.append(harness.exercise_cycle(cycle, fake_hid,
                               lambda *args: {'native_apis_mocked': True}, seconds=.01, frames=frames))
                if adapter.physical or adapter.physical_buttons:
                    raise RuntimeError('Isolated native input leaked into the physical entry gate')
            adapter.key(key.f12, True)
            adapter.key(key.f12, False)
            if isolation.emergency_callbacks != 2 or adapter.physical:
                raise RuntimeError('Real F12 callbacks did not retain the production emergency path')
    finally:
        harness.close()
    if harness.errors:
        raise RuntimeError('; '.join(harness.errors))
    return dict(mode='self-test', cycles_completed=len(results), cycles=results,
                wire_events=harness.events, native_apis_mocked=True, errors=harness.errors,
                native_input_isolation=isolation.snapshot(),
                native_motion_isolation=isolation.snapshot(), failures=harness.failures,
                workers_alive=[t.name for t in harness.threads if t.is_alive()])


def native_probe(args):
    import AppKit as A
    import Foundation as F
    import Quartz as Q
    from sharemouse.mac_capture_ui import get_capture_coordinator
    app = A.NSApplication.sharedApplication()
    ui = get_capture_coordinator()
    before = ui.backend.diagnostics()
    evidence = dict(mode='native-handoff' if args.capture else 'read-only', before=before,
                    cycles_requested=args.cycles, cycles_completed=0, cycles=[], stages=[],
                    transport='in-memory BleStream with compact Channel',
                    receiver='fake; no OS input injection',
                    stimuli='synthetic production Input.move/mac_move calls on the real HID loop',
                    input_isolation='All native motion/buttons/scroll/non-F12 keyboard input is aggregate-only. Only thread-local synthetic stimulus blocks call production Input methods. F12 remains the production emergency callback; no physical-motion measurement.',
                    bluetooth_used=False, external_network_used=False)
    if not args.capture:
        return evidence
    if before['foreground_bundle'] == 'com.apple.loginwindow':
        raise RuntimeError('Unlock the Mac before --capture; secure desktop is never captured')
    if before['foreground_pid'] in (None, before['pid']):
        raise RuntimeError('Begin --capture while another unlocked application owns the foreground')
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    from sharemouse.display import primary_screen
    from sharemouse.input import Input
    harness = HandoffHarness(primary_screen(), args.side)
    harness.sender.input = adapter = Input(harness.sender)
    saved = tuple(before['cursor_position'])
    errors = []
    from pynput.keyboard import Key
    isolation = InputIsolation(Key.f12)

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
        if not completed.wait(2):
            raise TimeoutError('AppKit did not service the probe callback')
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
            raise RuntimeError('The real HID listener is not running')
        Q.CFRunLoopPerformBlock(adapter.ml._loop, Q.kCFRunLoopDefaultMode, invoke)
        Q.CFRunLoopWakeUp(adapter.ml._loop)
        if not completed.wait(2):
            raise TimeoutError('HID loop did not service the production input stimulus')
        if failures:
            raise failures[0]

    def sample(stage, cycle, departure=None):
        def take():
            state = ui.backend.diagnostics()
            state.update(stage=stage, cycle=cycle, source_remote=harness.sender.remote,
                         receiver_active=harness.receiver.active,
                         native_captured=adapter.ml._captured,
                         native_hidden_owned=adapter.ml._cursor_hidden,
                         native_disassociated_owned=adapter.ml._mouse_disassociated,
                         physical_count=len(adapter.physical), button_count=len(adapter.physical_buttons),
                         edge_armed=adapter.edge_switch.armed, last_x=adapter.edge_switch.last_x)
            return state
        if stage == 'restored':
            deadline = time.monotonic() + 2
            while True:
                state = main_call(take)
                if (not state['capture_panel_visible'] and not state['native_captured']
                        and not state['native_hidden_owned'] and not state['native_disassociated_owned']
                        and state['foreground_pid'] == before['foreground_pid']
                        and abs(state['cursor_position'][0] -
                                (harness.sender.local.width - 12 if args.side == 'right' else 12)) <= 1.5):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Leave/left did not restore native capture and previous foreground')
                threading.Event().wait(.01)
        else:
            state = main_call(take)
            if not (state['foreground_pid'] == state['pid'] and state['capture_panel_owner_pid'] == state['pid']
                    and state['capture_panel_key'] and state['capture_panel_visible']
                    and state['application_cursor_is_blank'] and state['native_captured']
                    and state['application_cursor_is_transparent']
                    and state['application_cursor_image'].get('verified')
                    and state['application_cursor_image'].get('max_alpha') == 0
                    and state['native_hidden_owned'] and state['native_disassociated_owned']
                    and state['source_remote'] and state['receiver_active']):
                raise RuntimeError('Full handoff did not retain actual foreground/panel/native capture')
            if departure is not None and any(abs(a - b) > 1.5 for a, b in
                    zip(state['cursor_position'], departure['cursor_position'])):
                raise RuntimeError('Local cursor moved while the remote handoff was active')
        evidence['stages'].append(state)
        return state

    def driver():
        try:
            for listener in adapter.listeners:
                listener.wait()
            harness.start()
            for cycle in range(1, args.cycles + 1):
                result = harness.exercise_cycle(cycle, hid, sample, args.seconds, args.frames)
                evidence['cycles'].append(result)
                evidence['cycles_completed'] = cycle
        except Exception as exc:
            errors.append(str(exc))
            if not harness.failures:
                harness.failures.append(harness.failure_snapshot(exc))
            harness.failures[-1]['native_input_isolation_at_failure'] = isolation.snapshot()
            try:
                harness.failures[-1]['native_state_before_cleanup'] = main_call(ui.backend.diagnostics)
            except Exception as sample_error:
                harness.failures[-1]['native_state_error'] = str(sample_error)
        finally:
            try:
                # Emergency release still uses the production release method;
                # final native recovery below also covers a failed listener.
                if adapter.ml.is_alive():
                    hid(lambda: harness.sender.return_local(confirmed=True))
            except Exception as exc:
                errors.append('Emergency return: ' + str(exc))
            harness.close()
            for listener in adapter.listeners:
                try:
                    if listener.is_alive():
                        listener.join(2)
                except Exception as exc:
                    errors.append('Listener cleanup: ' + str(exc))
            def finish():
                try:
                    if adapter.ml._captured or adapter.ml._cursor_hidden or adapter.ml._mouse_disassociated:
                        adapter.ml._release_native()
                    ui.close()
                    result = Q.CGWarpMouseCursorPosition(saved)
                    if result != Q.kCGErrorSuccess:
                        errors.append('Final original cursor restoration failed: ' + str(result))
                    evidence.update(after=ui.backend.diagnostics(), errors=errors + harness.errors,
                                    wire_events=harness.events, status=harness.status,
                                    source_metrics=harness.sender.metrics.snapshot(),
                                    receiver_metrics=harness.receiver.metrics.snapshot(),
                                    native_input_isolation=isolation.snapshot(),
                                    native_motion_isolation=isolation.snapshot(), failures=harness.failures,
                                    workers_alive=[t.name for t in harness.threads if t.is_alive()],
                                    listeners_alive=[type(l).__name__ for l in adapter.listeners if l.is_alive()])
                    if evidence['listeners_alive']:
                        evidence['errors'].append('Native listeners did not stop: ' + ', '.join(evidence['listeners_alive']))
                    if any(abs(a - b) > 1.5 for a, b in zip(evidence['after']['cursor_position'], saved)):
                        evidence['errors'].append('Final cursor position differs from its original saved position')
                except Exception as exc:
                    evidence['errors'] = errors + harness.errors + ['Final recovery: ' + str(exc)]
                finally:
                    app.stop_(None)
                    event = A.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                        A.NSEventTypeApplicationDefined, A.NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
                    app.postEvent_atStart_(event, True)
            F.NSOperationQueue.mainQueue().addOperationWithBlock_(finish)

    try:
        start_with_isolated_input(adapter, isolation)
        F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            .2, False, lambda timer: threading.Thread(target=driver, name='handoff-probe-driver', daemon=True).start())
        app.run()
    finally:
        harness.close()
        ui.close()
        # This is reached only with --capture; default mode never warps.
        Q.CGWarpMouseCursorPosition(saved)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--capture', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=.2)
    parser.add_argument('--frames', type=int, default=8)
    parser.add_argument('--side', choices=('left', 'right'), default='right')
    parser.add_argument('--output', type=Path, default=Path('/tmp/share-monkey-native-handoff.json'))
    args = parser.parse_args()
    if not 1 <= args.cycles <= 30 or not .01 <= args.seconds <= 5 or not 3 <= args.frames <= 30:
        parser.error('cycles 1..30, seconds .01..5, frames 3..30 required')
    if not args.self_test and sys.platform != 'darwin':
        parser.error('native diagnostics require macOS; --self-test is portable')
    evidence = self_test(args.cycles, args.side, args.frames) if args.self_test else native_probe(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(mode=evidence['mode'], cycles_completed=evidence.get('cycles_completed', 0),
                         errors=evidence.get('errors', []), output=str(args.output)), ensure_ascii=False))
    if evidence.get('errors') or args.capture and evidence['cycles_completed'] != args.cycles:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
