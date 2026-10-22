"""Encrypted local transport benchmark; never imports or injects OS input."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--rate', type=int, default=1000)
    parser.add_argument('--seconds', type=float, default=1)
    args = parser.parse_args()
    if not 1 <= args.rate <= 8000 or not .1 <= args.seconds <= 30:
        parser.error('rate must be 1–8000 and seconds .1–30')
    sys.path.insert(0, str(args.source.resolve()))
    from sharemouse import __version__
    from sharemouse.core import Screen
    from sharemouse.engine import Engine
    from sharemouse.protocol import Channel

    class Pointer:
        def __init__(self):
            self.moves = 0
        def move_to(self, position):
            self.moves += 1
        def release(self):
            pass

    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    salt = os.urandom(32)
    tx = Channel(a, 'benchmark-password', salt, False)
    rx = Channel(b, 'benchmark-password', salt, True)
    sender = Engine(True, Screen(1920, 1080), 'right', 'benchmark-password', '', 24809,
                    lambda message: None, clipboard=False)
    receiver = Engine(False, Screen(1920, 1080), 'right', 'benchmark-password', '', 24809,
                      lambda message: None, clipboard=False)
    sender.connected = receiver.connected = True
    sender.input = SimpleNamespace(listeners=[])
    receiver.input = Pointer()
    produced, latency, frames, errors = [], [], [], []
    done = threading.Event()

    def receive():
        count = 0
        try:
            while True:
                message = rx.recv()
                if message['type'] == 'done':
                    break
                receiver.receive(message)
                if message['type'] == 'move':
                    received_at = time.perf_counter()
                    samples = len(message.get('path', [0]))
                    latency.extend((received_at - produced[i]) * 1000
                                   for i in range(count, count + samples))
                    count += samples
                    frames.append(samples)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    class Writer:
        def send(self, message):
            tx.send(message)
            if message['type'] == 'done':
                sender.stop_event.set()

    writer = threading.Thread(target=sender.writer, args=(Writer(),), daemon=True)
    reader = threading.Thread(target=receive, daemon=True)
    started = time.perf_counter()
    cpu_started = time.process_time()
    writer.start()
    reader.start()
    total = int(args.rate * args.seconds)
    try:
        sender.send({'type': 'enter', 'side': 'right', 'fraction': .5})
        for index in range(total):
            delay = started + index / args.rate - time.perf_counter()
            if delay > 0:
                threading.Event().wait(delay)
            produced.append(time.perf_counter())
            sender.send({'type': 'move', 'dx': 1 if index % 2 == 0 else -1, 'dy': 0})
        sender.send({'type': 'done'})
        if not done.wait(5):
            raise RuntimeError('benchmark timed out')
        if errors:
            raise errors[0]
        if len(latency) != total or receiver.cursor.x != 2 + total % 2:
            raise RuntimeError('movement samples were lost or reordered')
        latency.sort()
        print(json.dumps({'version': __version__, 'rate': args.rate, 'samples': total,
                          'motion_frames': len(frames), 'cursor_updates': receiver.input.moves,
                          'cpu_ms': round((time.process_time() - cpu_started) * 1000, 2),
                          'latency_p50_ms': round(latency[len(latency) // 2], 2),
                          'latency_p95_ms': round(latency[int(len(latency) * .95)], 2),
                          'latency_max_ms': round(latency[-1], 2)}, ensure_ascii=False))
    finally:
        sender.stop_event.set()
        a.close()
        b.close()
        writer.join(2)
        reader.join(2)


if __name__ == '__main__':
    main()
