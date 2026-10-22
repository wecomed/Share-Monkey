"""Passively observe a running Mac client and physical mouse reports.

Never captures, hides, moves or injects input, activates an application, requests
privacy access, or opens a network/Bluetooth connection. Mouse-only observations
are aggregate buckets. A listen-only head tap observes before the client's
swallowing tap when installed after it; it is reinstalled on new session logs.
IOHID is opened nonexclusively only if access was already granted.

Remote processed ACKs are evidence of the peer processing the input. They do not
measure physical display latency or verify the appearance of its cursor.
"""
import argparse
import ctypes as C
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time


def axes():
    return dict(n=0, dx=0, dy=0, abs_dx=0, abs_dy=0, max_component=0)


def add_axis(bucket, dx, dy):
    bucket['n'] += 1
    bucket['dx'] += dx
    bucket['dy'] += dy
    bucket['abs_dx'] += abs(dx)
    bucket['abs_dy'] += abs(dy)
    bucket['max_component'] = max(bucket['max_component'], abs(dx), abs(dy))


class RawMouse:
    def __init__(self, sink):
        import Foundation as F
        import objc
        self.sink = sink
        self.manager = None
        self.status = dict(open=False, devices=0, errors=0)
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        signatures = {
            'IOHIDCheckAccess': (C.c_int, [C.c_int]),
            'IOHIDManagerCreate': (C.c_void_p, [C.c_void_p, C.c_uint32]),
            'IOHIDManagerSetDeviceMatching': (None, [C.c_void_p, C.c_void_p]),
            'IOHIDManagerOpen': (C.c_int, [C.c_void_p, C.c_uint32]),
            'IOHIDManagerClose': (C.c_int, [C.c_void_p, C.c_uint32]),
            'IOHIDManagerCopyDevices': (C.c_void_p, [C.c_void_p]),
            'IOHIDManagerScheduleWithRunLoop': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'IOHIDManagerUnscheduleFromRunLoop': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'IOHIDValueGetElement': (C.c_void_p, [C.c_void_p]),
            'IOHIDValueGetIntegerValue': (C.c_long, [C.c_void_p]),
            'IOHIDElementGetUsagePage': (C.c_uint32, [C.c_void_p]),
            'IOHIDElementGetUsage': (C.c_uint32, [C.c_void_p]),
            'IOHIDElementIsRelative': (C.c_uint8, [C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.io, name)
            function.restype, function.argtypes = result, arguments
        self.cf.CFRunLoopGetCurrent.restype = C.c_void_p
        self.cf.CFSetGetCount.restype, self.cf.CFSetGetCount.argtypes = C.c_long, [C.c_void_p]
        self.cf.CFRelease.argtypes = [C.c_void_p]
        # IOHIDRequestTypeListenEvent == 1; IOHIDAccessTypeGranted == 0.
        self.status['access'] = int(self.io.IOHIDCheckAccess(1))
        if self.status['access'] != 0:
            self.status['reason'] = 'existing_input_monitoring_access_not_granted'
            return
        self.manager = self.io.IOHIDManagerCreate(None, 0)
        if not self.manager:
            self.status['reason'] = 'manager_creation_failed'
            return
        self.matching = F.NSDictionary.dictionaryWithDictionary_({'DeviceUsagePage': 1, 'DeviceUsage': 2})
        self.io.IOHIDManagerSetDeviceMatching(self.manager, objc.pyobjc_id(self.matching))
        callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.c_void_p, C.c_void_p)
        self.callback = callback_type(self.receive)
        self.io.IOHIDManagerRegisterInputValueCallback.argtypes = [C.c_void_p, callback_type, C.c_void_p]
        self.io.IOHIDManagerRegisterInputValueCallback.restype = None
        self.io.IOHIDManagerRegisterInputValueCallback(self.manager, self.callback, None)
        self.loop = self.cf.CFRunLoopGetCurrent()
        self.mode = C.c_void_p.in_dll(self.cf, 'kCFRunLoopDefaultMode').value
        self.io.IOHIDManagerScheduleWithRunLoop(self.manager, self.loop, self.mode)
        result = int(self.io.IOHIDManagerOpen(self.manager, 0))  # shared, never SeizeDevice
        self.status.update(open=result == 0, open_result=result)
        devices = self.io.IOHIDManagerCopyDevices(self.manager)
        if devices:
            try:
                self.status['devices'] = int(self.cf.CFSetGetCount(devices))
            finally:
                self.cf.CFRelease(devices)

    def receive(self, context, result, sender, value):
        if result or not value:
            return
        try:
            element = self.io.IOHIDValueGetElement(value)
            if (self.io.IOHIDElementGetUsagePage(element) != 1
                    or not self.io.IOHIDElementIsRelative(element)):
                return
            usage = self.io.IOHIDElementGetUsage(element)
            if usage not in (0x30, 0x31):
                return
            amount = int(self.io.IOHIDValueGetIntegerValue(value))
            if amount:
                self.sink(amount if usage == 0x30 else 0, amount if usage == 0x31 else 0)
        except Exception:
            self.status['errors'] += 1

    def close(self):
        if self.manager:
            self.io.IOHIDManagerUnscheduleFromRunLoop(self.manager, self.loop, self.mode)
            self.io.IOHIDManagerClose(self.manager, 0)
            self.cf.CFRelease(self.manager)
            self.manager = None


class Observer:
    def __init__(self, pid):
        import AppKit as A
        import Quartz as Q
        self.A, self.Q, self.pid = A, Q, pid
        self.bucket = self.new_bucket()
        self.tap = self.source = None
        self.tap_errors = self.tap_disabled = self.tap_installs = 0
        self.log_offsets, self.session_headers = {}, {}
        self.logs = Path.home() / 'Library' / 'Application Support' / 'Share Monkey' / 'logs'
        self.raw = RawMouse(lambda dx, dy: add_axis(self.bucket['hid'], dx, dy))
        self.install_tap()

    @staticmethod
    def new_bucket():
        return dict(hid=axes(), quartz=axes(), quartz_injected=axes(), buttons=0, scrolls=0)

    def install_tap(self):
        Q = self.Q
        self.remove_tap()
        if not Q.CGPreflightListenEventAccess():
            return
        types = (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDragged, Q.kCGEventRightMouseDragged,
                 Q.kCGEventOtherMouseDragged, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
                 Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGEventOtherMouseDown,
                 Q.kCGEventOtherMouseUp, Q.kCGEventScrollWheel)
        self.tap = Q.CGEventTapCreate(Q.kCGHIDEventTap, Q.kCGHeadInsertEventTap,
                                     Q.kCGEventTapOptionListenOnly, sum(1 << t for t in types),
                                     self.event, None)
        if self.tap is not None:
            self.source = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
            Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), self.source, Q.kCFRunLoopDefaultMode)
            Q.CGEventTapEnable(self.tap, True)
            self.tap_installs += 1

    def remove_tap(self):
        Q = self.Q
        if self.tap is not None:
            Q.CGEventTapEnable(self.tap, False)
            if self.source is not None:
                Q.CFRunLoopRemoveSource(Q.CFRunLoopGetCurrent(), self.source, Q.kCFRunLoopDefaultMode)
            Q.CFMachPortInvalidate(self.tap)
        self.tap = self.source = None

    def event(self, proxy, event_type, event, context):
        Q = self.Q
        try:
            if event_type in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                self.tap_disabled += 1
                if self.tap is not None:
                    Q.CGEventTapEnable(self.tap, True)
            elif event_type in (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDragged,
                                Q.kCGEventRightMouseDragged, Q.kCGEventOtherMouseDragged):
                dx = int(Q.CGEventGetIntegerValueField(event, Q.kCGMouseEventDeltaX))
                dy = int(Q.CGEventGetIntegerValueField(event, Q.kCGMouseEventDeltaY))
                injected = Q.CGEventGetIntegerValueField(event, Q.kCGEventSourceUnixProcessID) != 0
                add_axis(self.bucket['quartz_injected' if injected else 'quartz'], dx, dy)
            elif event_type == Q.kCGEventScrollWheel:
                self.bucket['scrolls'] += 1
            else:
                self.bucket['buttons'] += 1
        except Exception:
            self.tap_errors += 1
        return event

    def sample(self):
        Q = self.Q
        front = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        point = Q.CGEventGetLocation(Q.CGEventCreate(None))
        panels = []
        main_visible = False
        info = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID)
        for item in info or ():
            if int(item.get(Q.kCGWindowOwnerPID, -1)) != self.pid:
                continue
            rect = dict(item.get(Q.kCGWindowBounds, {}))
            if (int(item.get(Q.kCGWindowLayer, 0)) == 0
                    and rect.get('Width', 0) >= 800 and rect.get('Height', 0) >= 500):
                main_visible = True
            if rect.get('Width') == 96 and rect.get('Height') == 96:
                panels.append(dict(number=int(item[Q.kCGWindowNumber]), bounds=rect,
                                   layer=int(item.get(Q.kCGWindowLayer, 0))))
        result, self.bucket = self.bucket, self.new_bucket()
        result.update(source_foreground=bool(front and front.processIdentifier() == self.pid),
                      main_window_visible=main_visible,
                      position=[point.x, point.y], panels=panels,
                      cg_visible_advisory_only=bool(Q.CGCursorIsVisible()))
        return result

    def metrics(self):
        records, new_session = [], False
        for path in sorted(self.logs.glob(f'connection-*-{self.pid}-*.jsonl')):
            if path.is_symlink():
                continue
            if path.name not in self.log_offsets:
                self.log_offsets[path.name] = 0
                new_session = True
            with path.open('r', encoding='utf-8') as handle:
                handle.seek(self.log_offsets[path.name])
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        break  # retry a partial line at the next sample
                    records.append(dict(file=path.name, record=record))
                    self.log_offsets[path.name] += len(line.encode('utf-8'))
                    if record.get('kind') == 'session_start':
                        self.session_headers[path.name] = record
        if new_session:
            self.install_tap()  # production Input.start precedes performance log creation
        return records

    def close(self):
        self.remove_tap()
        self.raw.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin' or args.pid <= 0 or not 1 <= args.duration <= 600:
        parser.error('Requires macOS, a positive source PID and duration 1..600 seconds')
    observer = Observer(args.pid)
    evidence = dict(kind='passive_real_input_observation', pid=args.pid,
                    started_utc=datetime.now(timezone.utc).isoformat(),
                    duration_requested=args.duration, raw_mouse=observer.raw.status,
                    does_not_inject_or_capture=True, sample_interval_seconds=.25,
                    limitation='No remote screen, physical display latency or remote cursor appearance measurement. '
                               'HID axis reports and Quartz accelerated points have different units. '
                               'CGCursorIsVisible is advisory, not a pixel visibility check. '
                               'Source PID flags are not proof that every unflagged Quartz event is physical.',
                    samples=[], production_metrics=[])
    totals = dict(hid=axes(), quartz=axes(), quartz_injected=axes(), panels_samples=0)
    start, last_print = time.monotonic(), 0
    print(json.dumps(dict(started=True, pid=args.pid, raw_mouse=observer.raw.status,
                          listen_only_tap=observer.tap is not None)), flush=True)
    try:
        while time.monotonic() - start < args.duration:
            observer.Q.CFRunLoopRunInMode(observer.Q.kCFRunLoopDefaultMode, .25, False)
            sample = observer.sample()
            sample['t'] = round(time.monotonic() - start, 4)
            evidence['samples'].append(sample)
            evidence['production_metrics'].extend(observer.metrics())
            for name in ('hid', 'quartz', 'quartz_injected'):
                for key, value in sample[name].items():
                    totals[name][key] = (max(totals[name][key], value) if key == 'max_component'
                                         else totals[name][key] + value)
            totals['panels_samples'] += int(bool(sample['panels']))
            if sample['t'] - last_print >= 10:
                last_print = sample['t']
                print(json.dumps(dict(t=sample['t'], totals=totals,
                                      production_sessions=len(observer.log_offsets))), flush=True)
    except KeyboardInterrupt:
        evidence['interrupted'] = True
    finally:
        observer.close()
        evidence.update(duration_observed=round(time.monotonic() - start, 4), totals=totals,
                        tap_errors=observer.tap_errors, tap_disabled=observer.tap_disabled,
                        tap_installs=observer.tap_installs,
                        production_sessions=observer.session_headers,
                        closed=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({k: evidence[k] for k in ('duration_observed', 'totals', 'tap_errors',
                                                  'tap_disabled', 'tap_installs', 'closed')}), flush=True)


if __name__ == '__main__':
    main()
