"""Explicit Mac-only smoke check. --capture briefly freezes/restores the pointer."""
import argparse
import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--cycles', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 1000:
        parser.error('--cycles must be between 1 and 1000')
    if sys.platform != 'darwin':
        parser.error('requires macOS')
    import Quartz
    from sharemouse.core import Screen
    from sharemouse.display import primary_screen
    from sharemouse.engine import Engine
    from sharemouse.input import Input
    engine = Engine(True, primary_screen(), 'right', 'local-diagnostic-only', '', 24809, print, clipboard=False)
    engine.input = Input(engine)
    adapter = engine.input
    try:
        adapter.start()
        for listener in adapter.listeners:
            listener.wait()
            if not listener.is_alive():
                raise RuntimeError('native listener failed to start')
        print('Native keyboard and HID mouse event taps started')
        if args.capture:
            saved = tuple(adapter.mouse.position)
            errors = []
            def capture():
                try:
                    adapter.ml.capture_mouse()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    ready.set()
            for _ in range(args.cycles):
                ready = threading.Event()
                loop = adapter.ml._loop
                Quartz.CFRunLoopPerformBlock(loop, Quartz.kCFRunLoopDefaultMode, capture)
                Quartz.CFRunLoopWakeUp(loop)
                if not ready.wait(2):
                    raise RuntimeError('capture callback timed out')
                if errors:
                    raise errors[0]
                if not adapter.ml._captured:
                    raise RuntimeError('capture state was not entered')
                threading.Event().wait(.15)
                adapter.ml.release_mouse(saved)
                restored = threading.Event()
                Quartz.CFRunLoopPerformBlock(loop, Quartz.kCFRunLoopDefaultMode, restored.set)
                Quartz.CFRunLoopWakeUp(loop)
                if not restored.wait(2) or adapter.ml._captured:
                    raise RuntimeError('native restore did not complete')
                if not adapter.ml.is_alive() or not Quartz.CGEventTapIsEnabled(adapter.ml._tap):
                    raise RuntimeError('mouse tap stopped after restore')
                if any(abs(actual - expected) > 1 for actual, expected in zip(adapter.mouse.position, saved)):
                    raise RuntimeError('cursor was not restored to its original position')
            print(f'Native capture/release completed {args.cycles} times on the same mouse event thread')
    finally:
        engine.stop()
        for listener in adapter.listeners:
            if listener.is_alive():
                listener.join(2)
        if hasattr(adapter, 'ml') and adapter.ml._captured:
            raise RuntimeError('mouse capture remained active after shutdown')
    print('Native listeners stopped; local input restored')


if __name__ == '__main__':
    main()
