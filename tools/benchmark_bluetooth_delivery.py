"""OS acceptance and delayed peer processing are separate in this BLE model.

Runs the production sender Engine.writer, both Engine.receive handlers, Channel,
and BleStream. The native endpoints accept into a configurable packet FIFO first;
only later virtual connection events deliver bytes. This is a scheduling stress
model, not a measurement of any radio, driver, OS buffer capacity, or display.
"""
import argparse
import heapq
import json
import math
from pathlib import Path
import random
import struct
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_bluetooth import BluetoothSchedulingModel


class DeliveryWake:
    def __init__(self, model):
        self.model, self.flag = model, False

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        model = self.model
        if not self.flag:
            if (model.defer_return and model.sender.outgoing.empty()
                    and model.sender.background_outgoing.empty()
                    and model.sender.delivery.pending_count == 0):
                model.request_return(model.now)
            else:
                model.wait(1.0 if timeout is None else timeout)
        return self.flag


class BufferedDeliveryModel(BluetoothSchedulingModel):
    def __init__(self, *args, clipboard=False, credit_limit=2,
                 os_packet_capacity=128, **kwargs):
        super().__init__(*args, **kwargs)
        if type(os_packet_capacity) is not int or os_packet_capacity < 2:
            raise ValueError('OS packet capacity must be at least two')
        self.os_packet_capacity = os_packet_capacity
        self.sender.delivery.limit = credit_limit
        self.credit_limit = credit_limit
        self.sender.write_wake = DeliveryWake(self)
        self._wire_events = []
        self._event_number = 0
        self._next_slot = [0, 0]
        self._pending_packets = [0, 0]
        self._pending_bytes = [0, 0]
        self.os_peak_packets = [0, 0]
        self.os_peak_bytes = [0, 0]
        self._frames_waiting = [bytearray(), bytearray()]
        self._metadata = [[], []]
        self._application_busy = [False, False]
        self._context = [None, None]
        self.accepted_frames = 0
        self.arrived_frames = 0
        self.accepted_before_first_arrival = 0
        self.ack_rtts = []
        self._accepted_delivery = {}
        self.background_peak = 0
        self._background_pending = set()
        self.clipboard_values = []
        self.with_clipboard = clipboard
        if clipboard:
            self.sender.clipboard_enabled = self.receiver.clipboard_enabled = True
            rng = random.Random(271828)
            self.clipboard_text = ''.join(chr(rng.randrange(33, 127)) for _ in range(8192))
            self.sender.queue_clipboard(self.clipboard_text)

    def _next_wire_time(self):
        return self._wire_events[0][0] if self._wire_events else float('inf')

    def transmit(self, index, data):
        """Accept locally; do not feed the remote stream or advance to arrival."""
        metadata = dict(self._context[index] or {})
        self._metadata[1 - index].append(metadata)
        packet_count = math.ceil(len(data) / self.mtu)
        frame = dict(direction=index, bytes=len(data), packets=packet_count,
                     accepted_at=self.now, delivered_at=None, type=metadata.get('type'))
        self.frames.append(frame)
        self.accepted_frames += 1
        if self.accepted_frames > 6000:
            raise RuntimeError(f'Buffered model did not converge: now={self.now}, '
                               f'pending={self.sender.delivery.pending_count}, '
                               f'fifo={self._pending_packets}, '
                               f'wire_events={len(self._wire_events)}')
        if self.arrived_frames == 0:
            self.accepted_before_first_arrival += 1
        if index == 0 and '_delivery' in metadata:
            self._accepted_delivery[metadata['_delivery']] = self.now
            if metadata['type'] == 'clipboard_part':
                self._background_pending.add(metadata['_delivery'])
                self.background_peak = max(self.background_peak, len(self._background_pending))
        for offset in range(0, len(data), self.mtu):
            while self._pending_packets[index] >= self.os_packet_capacity:
                self.advance(max(0., self._next_wire_time() - self.now))
            chunk = bytes(data[offset:offset + self.mtu])
            # At an exact interval boundary, newly accepted data waits for the
            # following event; previously queued data may fill the current one.
            first_slot = math.floor((self.now + 1e-12) / self.interval) * self.packets_per_event
            slot = max(self._next_slot[index], first_slot)
            self._next_slot[index] = slot + 1
            arrival = (slot // self.packets_per_event + 1) * self.interval
            self._event_number += 1
            heapq.heappush(self._wire_events, (arrival, self._event_number, index, chunk, frame,
                                             offset + len(chunk) == len(data)))
            self._pending_packets[index] += 1
            self._pending_bytes[index] += len(chunk)
            self.os_peak_packets[index] = max(self.os_peak_packets[index], self._pending_packets[index])
            self.os_peak_bytes[index] = max(self.os_peak_bytes[index], self._pending_bytes[index])

    def _send(self, index, message):
        self._context[index] = message
        try:
            self.channels[index].send(message)
        finally:
            self._context[index] = None

    def deliver(self, message):
        # Engine.writer returns from this as soon as the local FIFO accepted it.
        self._send(0, message)

    def _consume_application_frames(self, index):
        if self._application_busy[index]:
            return
        self._application_busy[index] = True
        try:
            buffered = self._frames_waiting[index]
            while len(buffered) >= 4:
                size, = struct.unpack('!I', buffered[:4])
                if len(buffered) < size + 4:
                    break
                del buffered[:size + 4]
                metadata = self._metadata[index].pop(0)
                message = self.channels[index].recv()
                self.arrived_frames += 1
                if index == 1:
                    kind = message['type']
                    if kind in ('key', 'button'):
                        cursor = self.receiver.cursor
                        self.barriers.append((kind, message['down'], self.now,
                                              (cursor.x, cursor.y) if cursor else None))
                    if kind == 'pointer':
                        self.move_ages.append(max(0., self.now - metadata['_sample_time']))
                    was_active = self.receiver.active
                    self.receiver.receive(message)
                    if was_active and not self.receiver.active and self.receiver_inactive_at is None:
                        self.receiver_inactive_at = self.now
                    if kind == 'leave':
                        self.leave_received_at = self.now
                    # These are actual production reply messages (consumed,
                    # left, return), all exempt from the data credit window.
                    while not self.receiver.outgoing.empty():
                        reply = self.receiver.outgoing.get_nowait()
                        self._send(1, reply)
                else:
                    if message['type'] == 'consumed':
                        packet = message['packet']
                        sent_at = self._accepted_delivery.pop(packet, None)
                        if sent_at is not None:
                            self.ack_rtts.append(self.now - sent_at)
                        self._background_pending.discard(packet)
                    if message['type'] == 'left':
                        self.left_ack_received_at = self.now
                    self.sender.receive(message)
                    if self.leave_received_at is not None and self.sender.pending_return is None:
                        self.sender.stop_event.set()
        finally:
            self._application_busy[index] = False

    def advance(self, duration):
        target = self.now + max(0., duration)
        if target > 120:
            raise RuntimeError('Buffered BLE model exceeded 120 virtual seconds')
        while True:
            hardware_time = self.events[self.next_event][0] if self.next_event < len(self.events) else float('inf')
            wire_time = self._next_wire_time()
            timestamp = min(hardware_time, wire_time)
            if timestamp > target + 1e-12:
                break
            self.now = max(self.now, timestamp)
            if hardware_time <= wire_time:
                # The parent drives exactly one source hardware event at this
                # instant. It does not know or advance the OS packet FIFO.
                BluetoothSchedulingModel.advance(self, max(0., timestamp - self.now))
            else:
                _, _, source, chunk, frame, last = heapq.heappop(self._wire_events)
                self._pending_packets[source] -= 1
                self._pending_bytes[source] -= len(chunk)
                destination = 1 - source
                self.streams[destination].feed(chunk)
                self._frames_waiting[destination].extend(chunk)
                if last:
                    frame['delivered_at'] = self.now
                self._consume_application_frames(destination)
        self.now = max(self.now, target)

    def wait(self, timeout):
        hardware_time = self.events[self.next_event][0] if self.next_event < len(self.events) else float('inf')
        deadline = self.now + max(0., timeout)
        if timeout > 0 and deadline <= self.now:
            deadline = math.nextafter(self.now, float('inf'))
        target = min(deadline, hardware_time, self._next_wire_time())
        self.advance(max(0., target - self.now))

    def run(self):
        with patch('pyperclip.copy', side_effect=self.clipboard_values.append):
            result = super().run()
        ack = sorted(self.ack_rtts)
        result.update(
            model='local accepted bytes stay in an independent delayed OS FIFO until connection events',
            credit_limit=self.credit_limit, peak_inflight_data=self.sender.delivery.peak,
            os_packet_capacity_assumption=self.os_packet_capacity,
            peak_os_packets=self.os_peak_packets, peak_os_bytes=self.os_peak_bytes,
            accepted_frames_before_first_peer_frame=self.accepted_before_first_arrival,
            processed_ack_p95_ms=round(ack[min(len(ack) - 1, int(len(ack) * .95))] * 1000, 3) if ack else 0,
            peak_inflight_background=self.background_peak,
            clipboard_complete=self.clipboard_values == [self.clipboard_text] if self.with_clipboard else None,
        )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--compare-no-credit', action='store_true')
    args = parser.parse_args()
    cases = []
    for mtu, packets in ((20, 1), (20, 4), (185, 1)):
        for clipboard in (False, True):
            for limit in ((2, 1000000) if args.compare_no_credit else (2,)):
                result = BufferedDeliveryModel(mtu=mtu, packets_per_event=packets, samples=args.samples,
                    trace='curve', drain_before_return=True, clipboard=clipboard, credit_limit=limit).run()
                result['background_clipboard'] = clipboard
                cases.append(result)
    report = dict(kind='OS-buffered deterministic stress model, not measured radio latency',
                  hardware_measurement=False,
                  assumptions=[
                      'Local send acceptance is independent of peer packet delivery and native input processing.',
                      'A configurable 128-packet local FIFO is a stress assumption, not a measured CoreBluetooth queue size.',
                      'Configured packet capacity is per direction per connection event; duplex scheduling is idealized.',
                      'Native endpoints, real operating-system scheduling and physical display refresh are not exercised.',
                      'Receiver Engine.receive and its processed ACKs run only after complete encrypted bytes arrive.',
                  ], results=cases)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
