"""Exercise real Mac input-hook restart through Session, with transport faults.

No radio, peer, clipboard, OS input injection, window or cursor capture. Real
input is allowed through locally; no key values are recorded. --native required.
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--cycles', type=int, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.native or sys.platform != 'darwin' or not 1 <= args.cycles <= 100:
        parser.error('Explicit --native on macOS with 1–100 cycles is required')
    import objc
    import Foundation as F
    from sharemouse.client import MAC
    from sharemouse.engine import Engine
    from sharemouse.session import Session, Settings

    engines, completed = [], []
    clock = [0.]
    telemetry = Mock()
    telemetry.start.return_value = telemetry
    def factory(*values, **options):
        engine = Engine(*values, **options)
        def fault():
            for listener in engine.input.listeners:
                listener.wait()
                if not listener.is_alive():
                    engine.retryable = False
                    engine.last_error = 'Native listener did not start'
                    engine.stop()
                    return
            completed.append(len(engines))
            engine.stop('自检模拟传输断线，本机输入保留')
        engine.run = fault
        engines.append(engine)
        return engine

    with ExitStack() as stack:
        stack.enter_context(patch('sharemouse.session.Engine', side_effect=factory))
        stack.enter_context(patch('sharemouse.engine.TelemetrySession', return_value=telemetry))
        session = Session(MAC, discovery=False, persist=False,
                          settings=Settings(manual=True, host='127.0.0.1', clipboard=False),
                          clock=lambda: clock[0])
        try:
            with objc.autorelease_pool():
                session.connect('0042')
            deadline = time.monotonic() + 45
            while len(completed) < args.cycles and time.monotonic() < deadline:
                with objc.autorelease_pool():
                    session.poll()
                    if session.reconnecting:
                        clock[0] += 20
                    F.NSRunLoop.currentRunLoop().runMode_beforeDate_(
                        F.NSDefaultRunLoopMode, F.NSDate.dateWithTimeIntervalSinceNow_(.01))
            session.disconnect()
            deadline = time.monotonic() + 3
            while not all(engine.quiescent for engine in engines) and time.monotonic() < deadline:
                with objc.autorelease_pool():
                    F.NSRunLoop.currentRunLoop().runMode_beforeDate_(
                        F.NSDefaultRunLoopMode, F.NSDate.dateWithTimeIntervalSinceNow_(.01))
            if len(completed) < args.cycles or not all(e.quiescent for e in engines):
                raise RuntimeError('Native restart incomplete or hooks remain alive')
        finally:
            session.close()
    result = dict(passed=True, requested_cycles=args.cycles, completed_cycles=len(completed),
                  engines_created=len(engines), workers_and_hooks_stopped=True,
                  source='production Session, Engine and Mac keyboard/mouse hooks',
                  radio_or_peer_used=False, clipboard_read=False, records_key_values=False,
                  posts_os_input=False, captures_or_moves_cursor=False,
                  physical_windows_sleep_verified=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
