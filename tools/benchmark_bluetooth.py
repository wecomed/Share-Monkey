"""Historical synchronous-link scaffold; not native BLE delivery evidence.

Use benchmark_bluetooth_delivery.py for the current OS-buffered model. This
earlier scaffold makes send completion coincide with peer delivery, which cannot
detect a backlog inside Core Bluetooth, WinRT, or the remote application.

Runs the production Engine.writer, Channel codec, and Engine.receive against a
virtual GATT link. Every encrypted frame costs ceil(packets / packets_per_event)
connection intervals. Hardware input continues arriving while a send is blocked.
These are configurable simulations, not measurements of a particular adapter.
"""
import argparse
from contextlib import ExitStack
import json
import math
from pathlib import Path
import queue
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sharemouse.bluetooth_transport import BleStream
from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.protocol import Channel


class VirtualWake:
    def __init__(self, model):
        self.model, self.flag = model, False

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        if not self.flag:
            if self.model.defer_return and self.model.sender.outgoing.empty():
                self.model.request_return(self.model.now)
            else:
                self.model.wait(1 if timeout is None else timeout)
        return self.flag


class SimulatedInput:
    def __init__(self, clock):
        self.clock = clock
        self.listeners = []
        self.positions, self.restores, self.actions = [], [], []
        self.presentation = []
        self.held_keys, self.held_buttons = set(), set()
        self.captured = False
        self.remote_active = False
        self.mouse = SimpleNamespace(scroll=lambda x, y: self.actions.append(('scroll', x, y)))

    def set_remote_active(self, active):
        self.remote_active = active
        self.presentation.append((self.clock(), 'visible', active))

    def release(self):
        self.held_keys.clear()
        self.held_buttons.clear()

    def stop(self):
        self.release()
        self.captured = False

    def move_to(self, position):
        self.positions.append((self.clock(), tuple(position)))
        self.presentation.append((self.clock(), 'move', tuple(position), self.remote_active))

    def restore_local(self, x, y):
        self.captured = False
        self.restores.append((self.clock(), (x, y)))

    def inject_key(self, key, down):
        identity = json.dumps(key, sort_keys=True)
        (self.held_keys.add if down else self.held_keys.discard)(identity)
        self.actions.append(('key', key, down))

    def inject_button(self, button, down):
        (self.held_buttons.add if down else self.held_buttons.discard)(button)
        self.actions.append(('button', button, down))


class BluetoothSchedulingModel:
    def __init__(self, mtu=20, interval_ms=15, packets_per_event=1,
                 rate=1000, samples=1000, trace='straight', drain_before_return=False):
        if mtu < 1 or interval_ms <= 0 or packets_per_event < 1 or rate < 1 or samples < 1:
            raise ValueError('Simulation parameters must be positive')
        if trace not in ('straight', 'foldback', 'return', 'curve'):
            raise ValueError('Unknown trace')
        self.mtu, self.interval = mtu, interval_ms / 1000
        self.packets_per_event = packets_per_event
        self.rate, self.samples, self.trace = rate, samples, trace
        self.drain_before_return = drain_before_return
        self.defer_return = False
        self.now = 0.
        self.events, self.next_event = [], 0
        self.frames, self.barriers, self.expected_barriers = [], [], []
        self.move_ages, self.delivered_samples = [], 0.
        self.peak_queue = 0
        self.local_return_at = self.leave_received_at = self.remote_return_at = None
        self.left_ack_received_at = None
        self.pending_return_snapshot = None
        self.return_cause = None
        self.receiver_inactive_at = None
        self.status = []
        self.sender = Engine(True, Screen(1920, 1080), 'right', 'simulated-ble-password',
                             '', 24809, self.status.append, clipboard=False, transport='bluetooth')
        self.receiver = Engine(False, Screen(1920, 1080), 'right', 'simulated-ble-password',
                               '', 24809, self.status.append, clipboard=False, transport='bluetooth')
        for engine in (self.sender, self.receiver):
            engine.input = SimulatedInput(lambda: self.now)
            engine.connected = True
            engine.peer_screen = Screen(1920, 1080)
        self.sender.remote, self.sender.epoch = True, 1
        self.sender.input.captured = True
        self.sender.write_wake = VirtualWake(self)
        for index in range(samples):
            dx = 1 if trace == 'straight' or index < samples // 2 else -1
            dy = 0
            if trace == 'return':
                dx = 1 if index < samples // 4 else -1
            if trace == 'curve':
                dx, dy = ((1, 0), (1, 1), (0, 1), (-1, 1),
                          (-1, 0), (-1, -1), (0, -1), (1, -1))[index % 8]
            self.events.append(((index + 1) / rate, {'type': 'move', 'dx': dx, 'dy': dy}))
        duration = samples / rate
        for fraction, kind, down in ((.25, 'key', True), (.3, 'button', True),
                                     (.5, 'key', False), (.8, 'button', False)):
            message = ({'type': kind, 'key': {'char': 'x'}, 'down': down} if kind == 'key'
                       else {'type': kind, 'button': 'left', 'down': down})
            self.events.append((duration * fraction + .0001, message))
        self.events.append((duration + .005, {'type': 'local_return'}))
        self.events.sort(key=lambda event: event[0])
        self.streams = [BleStream(), BleStream()]
        for index, stream in enumerate(self.streams):
            stream.set_writer(lambda data, timeout, i=index: self.transmit(i, data))
        salt = b'ShareMonkey deterministic timing!'
        self.channels = [Channel(self.streams[0], 'simulated-ble-password', salt, True, compact=True),
                         Channel(self.streams[1], 'simulated-ble-password', salt, False, compact=True)]

    def transmit(self, index, data):
        packet_count = math.ceil(len(data) / self.mtu)
        cost = math.ceil(packet_count / self.packets_per_event) * self.interval
        self.frames.append({'direction': index, 'bytes': len(data), 'packets': packet_count,
                            'begin': self.now, 'end': self.now + cost})
        self.advance(cost)
        for offset in range(0, len(data), self.mtu):
            self.streams[1 - index].feed(data[offset:offset + self.mtu])

    def advance(self, duration):
        target = self.now + max(0., duration)
        if target > 120:
            raise RuntimeError('Simulated BLE session did not drain within 120 seconds')
        while self.next_event < len(self.events) and self.events[self.next_event][0] <= target + 1e-12:
            timestamp, message = self.events[self.next_event]
            self.next_event += 1
            self.now = timestamp
            if message['type'] == 'local_return':
                if self.sender.remote:
                    if self.drain_before_return:
                        self.defer_return = True
                    else:
                        self.request_return(timestamp)
            elif self.sender.remote:
                self.sender.send(message)
                if not self.sender.remote and self.sender.pending_return and self.local_return_at is None:
                    self.local_return_at = timestamp
                    self.return_cause = 'mirrored edge'
                    self.pending_return_snapshot = dict(
                        pending=True, captured=self.sender.input.captured,
                        restores=len(self.sender.input.restores))
                if message['type'] in ('key', 'button'):
                    cursor = self.sender.peer_cursor
                    position = (cursor.x, cursor.y) if cursor is not None else None
                    self.expected_barriers.append((message['type'], message['down'], timestamp, position))
            self.peak_queue = max(self.peak_queue, self.sender.outgoing.qsize())
        self.now = target

    def request_return(self, timestamp):
        self.defer_return = False
        self.local_return_at = timestamp
        self.return_cause = 'F12'
        self.sender.return_local()
        self.pending_return_snapshot = dict(
            pending=bool(self.sender.pending_return), captured=self.sender.input.captured,
            restores=len(self.sender.input.restores))

    def wait(self, timeout):
        until = self.now + max(0., timeout)
        if self.next_event < len(self.events):
            until = min(until, self.events[self.next_event][0])
        self.advance(max(0., until - self.now))

    def deliver(self, message):
        self.channels[0].send(message)
        decoded = self.channels[1].recv()
        kind = decoded['type']
        if kind in ('key', 'button'):
            cursor = self.receiver.cursor
            position = (cursor.x, cursor.y) if cursor is not None else None
            self.barriers.append((kind, decoded['down'], self.now, position))
        if kind == 'pointer':
            self.move_ages.append(max(0., self.now - message['_sample_time']))
        elif kind == 'move':
            path = decoded.get('path', [[decoded.get('dx', 0), decoded.get('dy', 0)]])
            self.delivered_samples += sum(max(abs(sample[0]), abs(sample[1])) for sample in path)
            last_hardware_at = min(self.samples, self.delivered_samples) / self.rate
            self.move_ages.append(max(0., self.now - last_hardware_at))
        was_active = self.receiver.active
        self.receiver.receive(decoded)
        if was_active and not self.receiver.active and self.receiver_inactive_at is None:
            self.receiver_inactive_at = self.now
        while not self.receiver.outgoing.empty():
            reply = self.receiver.outgoing.get_nowait()
            if reply.get('type') == 'return':
                self.remote_return_at = self.now
            self.channels[1].send(reply)
            decoded_reply = self.channels[0].recv()
            if decoded_reply['type'] == 'left':
                self.left_ack_received_at = self.now
            self.sender.receive(decoded_reply)
        if kind == 'leave':
            self.leave_received_at = self.now
            self.sender.stop_event.set()

    def run(self):
        outgoing = self.sender.outgoing
        original_get = outgoing.get

        def get(block=True, timeout=None):
            try:
                return original_get(block=False)
            except queue.Empty:
                if not block or timeout is None:
                    raise
                self.wait(timeout)
                return original_get(block=False)

        # Replace only clocks and waits, preserving the production queue class
        # and any transport-specific coalescing/priority behavior it implements.
        mouse = ModuleType('pynput.mouse')
        mouse.Button = SimpleNamespace(left='left', right='right')
        pynput = ModuleType('pynput')
        pynput.__path__ = []
        time_proxy = SimpleNamespace(monotonic=lambda: self.now,
                                     monotonic_ns=lambda: round(self.now * 1_000_000_000))
        try:
            with ExitStack() as stack:
                stack.enter_context(patch('sharemouse.engine.time', time_proxy))
                stack.enter_context(patch('sharemouse.motion.time', time_proxy))
                stack.enter_context(patch.object(outgoing, 'get', side_effect=get))
                stack.enter_context(patch.dict(sys.modules, {'pynput': pynput, 'pynput.mouse': mouse}))
                self.sender.send({'type': 'enter', 'side': 'right', 'fraction': .5})
                self.sender.writer(SimpleNamespace(send=self.deliver))
        finally:
            for stream in self.streams:
                stream.finish()
        if self.leave_received_at is None:
            raise RuntimeError('BLE simulation did not deliver leave: ' + repr(self.status))
        ages = sorted(self.move_ages)
        p95 = ages[min(len(ages) - 1, int(len(ages) * .95))] if ages else 0
        barrier_ages = [max(0., received[2] - expected[2])
                        for received, expected in zip(self.barriers, self.expected_barriers)]
        barrier_errors = [max(abs(received[3][i] - expected[3][i]) for i in (0, 1))
                          for received, expected in zip(self.barriers, self.expected_barriers)
                          if received[3] is not None and expected[3] is not None]
        local_restored = self.sender.input.restores[0][0] if self.sender.input.restores else None
        overlap = (max(0., self.receiver_inactive_at - local_restored)
                   if self.receiver_inactive_at is not None and local_restored is not None else 0.)
        return dict(trace=self.trace, mtu=self.mtu, interval_ms=self.interval * 1000,
                    packets_per_event=self.packets_per_event, input_rate_hz=self.rate,
                    drain_before_return=self.drain_before_return,
                    input_samples=self.samples, simulated_seconds=round(self.now, 6),
                    encrypted_frames=len(self.frames), wire_bytes=sum(f['bytes'] for f in self.frames),
                    peak_queue=self.peak_queue, motion_p95_ms=round(p95 * 1000, 3),
                    control_max_ms=round(max(barrier_ages, default=0) * 1000, 3),
                    local_remote_overlap_ms=round(overlap * 1000, 3),
                    return_requested_at=self.local_return_at,
                    return_cause=self.return_cause,
                    receiver_inactive_at=self.receiver_inactive_at,
                    left_ack_received_at=self.left_ack_received_at,
                    local_restored_at=local_restored,
                    local_restore_delay_ms=(round((local_restored - self.local_return_at) * 1000, 3)
                                            if local_restored is not None and self.local_return_at is not None else None),
                    pending_return_kept_captured=(self.pending_return_snapshot is None or
                                                 self.pending_return_snapshot == dict(pending=True, captured=True, restores=0)),
                    return_control_ms=(round(max(0., self.receiver_inactive_at - self.local_return_at) * 1000, 3)
                                       if self.receiver_inactive_at is not None and self.local_return_at is not None else 0),
                    input_order_preserved=([event[:2] for event in self.barriers]
                                           == [event[:2] for event in self.expected_barriers]),
                    input_prefix_preserved=([event[:2] for event in self.barriers]
                                            == [event[:2] for event in self.expected_barriers[:len(self.barriers)]]),
                    control_position_max_error_px=max(barrier_errors, default=0.),
                    input_events_delivered=len(self.barriers),
                    input_events_cancelled_by_return=max(0, len(self.expected_barriers) - len(self.barriers)),
                    receiver_active=self.receiver.active,
                    receiver_cursor_visible=self.receiver.input.remote_active,
                    source_cursor_captured=self.sender.input.captured,
                    receiver_positioned_before_visible=(len(self.receiver.input.presentation) >= 3
                        and self.receiver.input.presentation[0][1:] == ('visible', False)
                        and self.receiver.input.presentation[1][1] == 'move'
                        and self.receiver.input.presentation[1][3] is False
                        and self.receiver.input.presentation[2][1:] == ('visible', True)),
                    held_keys=len(self.receiver.input.held_keys), held_buttons=len(self.receiver.input.held_buttons),
                    status=self.status[-3:])


def main():
    print('Historical synchronous-link model: use tools/benchmark_bluetooth_delivery.py '
          'to test separate OS acceptance and peer processing.', file=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--drain-before-return', action='store_true',
                        help='Let active input drain before returning; default simulates immediate F12')
    parser.add_argument('--both-return-modes', action='store_true',
                        help='Report immediate F12 and full input drain independently')
    parser.add_argument('--baseline', type=Path, help='Attach comparisons to an existing simulation report')
    args = parser.parse_args()
    modes = (False, True) if args.both_return_modes else (args.drain_before_return,)
    results = [BluetoothSchedulingModel(mtu=mtu, packets_per_event=packets,
                                       samples=args.samples, trace=trace,
                                       drain_before_return=drain).run()
               for drain in modes
               for mtu, packets in ((20, 1), (20, 4), (185, 1))
               for trace in ('straight', 'foldback', 'return', 'curve')]
    document = {
        'kind': 'deterministic simulation, not measured radio latency',
        'hardware_measurement': False,
        'historical_synchronous_link_model': True,
        'validates_native_os_backpressure': False,
        'wire_protocol': 'BLE v2 AES-GCM with absolute pointer snapshots',
        'model_assumptions': [
            'mtu is the maximum GATT value payload in bytes, not the ATT MTU including its header.',
            'Each encrypted frame consumes ceil(packet_count / packets_per_event) connection intervals.',
            'Input continues at 1000 Hz while sends block; the production Engine, input queue, Channel and BleStream run unchanged.',
            'Pointer age is measured from its original local sample time to receiver injection, not from summed displacement.',
            'Local presentation time is the recorded Input.restore_local callback, after remote leave acknowledgement.',
            'Host scheduling, actual Bluetooth contention, driver behavior and physical screen refresh are not measured.',
        ],
        'results': results,
    }
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        comparisons = []
        for after in results:
            if after['drain_before_return']:
                continue
            before = next((value for value in baseline['results']
                           if all(value.get(key) == after[key]
                                  for key in ('trace', 'mtu', 'packets_per_event', 'input_rate_hz', 'input_samples'))), None)
            if before is not None:
                comparisons.append({
                    'trace': after['trace'], 'mtu': after['mtu'], 'packets_per_event': after['packets_per_event'],
                    'before_motion_p95_ms': before['motion_p95_ms'], 'after_motion_p95_ms': after['motion_p95_ms'],
                    'before_return_control_ms': before['return_control_ms'], 'after_return_control_ms': after['return_control_ms'],
                    'before_cursor_overlap_ms': before['local_remote_overlap_ms'],
                    'after_cursor_overlap_ms': after['local_remote_overlap_ms'],
                    'after_local_restore_delay_ms': after['local_restore_delay_ms'],
                })
        document['baseline_path'] = str(args.baseline)
        document['comparisons'] = comparisons
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
