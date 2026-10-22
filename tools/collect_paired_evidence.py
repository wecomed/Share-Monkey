"""Read-only observations of installed Share Monkey clients, never an E2E claim.

Observe on each host, then combine the two JSON reports. No input is injected,
window activated, connection created, password read or peer command accepted.
Existing v0.5.x aggregate logs do not contain a shared authenticated session ID;
even overlapping observations therefore cannot prove that these exact two
clients were paired, or that every release scenario was executed.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.validation_common import (SCHEMA_VERSION, make_report, parse_utc,
                                     source_context, utc_now, write_report)

LOG_NAME = re.compile(r'connection-\d{8}-\d{6}-(\d+)-[0-9a-f]{8}\.jsonl\Z')
COUNTERS = ('sent_frames', 'received_frames', 'enter_sent', 'returns_completed',
            'source_edge_returns', 'peer_edge_returns', 'return_requests',
            'late_consumed_acks', 'capture_wait_cancelled')
SCENARIOS = ('mac-to-windows', 'windows-to-mac', 'window-close', 'window-reopen',
             'disconnect-recovery', 'mixed-input')
BOUNDARIES = [
    'Read-only aggregate observation; no native input, UI action or connection was initiated.',
    'Frame progress includes heartbeat traffic and does not establish mouse smoothness or display latency.',
    'Existing telemetry has no authenticated shared peer ID; a common run label is not peer identity.',
    'Hidden-window samples alone do not verify that closing and reopening preserved the connection.',
    'This evidence cannot satisfy real paired scenario coverage or the two-hour mixed-input release soak.',
]


def log_directory():
    base = (Path(os.environ.get('LOCALAPPDATA', Path.home())) if sys.platform == 'win32'
            else Path.home() / 'Library' / 'Application Support')
    return base / 'Share Monkey' / 'logs'


def read_session(path):
    """Bound reads, discard incomplete tails, and retain only known aggregates."""
    path = Path(path)
    if not LOG_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
        raise ValueError('Not a regular Share Monkey connection log')
    if path.stat().st_size > 1100 * 1024:
        raise ValueError('Connection log exceeds the product size bound')
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b'\n'):
        lines.pop()  # The product may currently be writing this line.
    records = []
    for line in lines:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError('Connection log record is not an object')
        records.append(record)
    if not records or records[0].get('kind') != 'session_start':
        raise ValueError('Connection log has no session header')
    header = records[0]
    if (header.get('schema_version') != 1 or header.get('platform') not in ('darwin', 'win32')
            or header.get('role') not in ('controller', 'receiver')
            or header.get('transport') not in ('lan', 'bluetooth')
            or not re.fullmatch(r'\d+\.\d+\.\d+', str(header.get('version', '')))):
        raise ValueError('Connection header schema, platform, role, transport or version is invalid')
    parse_utc(header.get('timestamp_utc'))
    snapshots = [item for item in records if item.get('kind') in ('interval', 'summary')]
    latest = snapshots[-1] if snapshots else None
    counts = {}
    if latest is not None:
        parse_utc(latest.get('timestamp_utc'))
        for name in COUNTERS:
            value = latest.get('counters', {}).get(name, 0)
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError('Invalid aggregate counter: ' + name)
            counts[name] = value
    return dict(file=path.name, pid=int(LOG_NAME.fullmatch(path.name)[1]),
                header={key: header[key] for key in
                        ('version', 'platform', 'role', 'transport', 'timestamp_utc')},
                latest_timestamp=latest.get('timestamp_utc') if latest else None,
                counters=counts, ended=any(item.get('kind') == 'summary' for item in snapshots),
                telemetry_full=any(item.get('file_limit_reached') for item in snapshots),
                complete_records=len(records), sha256=hashlib.sha256(raw).hexdigest())


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


class MacClient:
    def inspect(self, pid):
        import AppKit as A
        import Quartz as Q
        running = A.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if (running is None or running.isTerminated()
                or running.bundleIdentifier() != 'local.sharemouselab.mac'):
            return None
        bundle = Path(str(running.bundleURL().path())).resolve()
        executable = Path(str(running.executableURL().path())).resolve()
        if not _inside(executable, bundle / 'Contents' / 'MacOS'):
            return None
        with (bundle / 'Contents' / 'Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        if info.get('CFBundleIdentifier') != 'local.sharemouselab.mac':
            return None
        visible = False
        for item in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly,
                                               Q.kCGNullWindowID) or ():
            if int(item.get(Q.kCGWindowOwnerPID, -1)) != pid:
                continue
            bounds = item.get(Q.kCGWindowBounds, {})
            if (int(item.get(Q.kCGWindowLayer, 0)) == 0
                    and bounds.get('Width', 0) >= 800 and bounds.get('Height', 0) >= 500):
                visible = True
        return dict(pid=pid, alive=True, executable=str(executable), bundle=str(bundle),
                    metadata_version=str(info.get('CFBundleShortVersionString', '')),
                    main_window_visible=visible,
                    identity_method='running bundle identifier, executable path and Info.plist')


class WindowsClient:
    def __init__(self, installed_root=None):
        from ctypes import wintypes as W
        self.W = W
        self.root = (Path(installed_root) if installed_root else
                     Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Programs' / 'Share Monkey')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        for name, args, result in (
                ('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
                ('GetExitCodeProcess', [W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
                ('QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL),
                ('CloseHandle', [W.HANDLE], W.BOOL)):
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result
        self.callback = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, W.LPARAM]
        self.user.EnumWindows.restype = W.BOOL
        for name, args, result in (
                ('GetWindowThreadProcessId', [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
                ('IsWindowVisible', [W.HWND], W.BOOL),
                ('GetWindowRect', [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL)):
            method = getattr(self.user, name)
            method.argtypes, method.restype = args, result

    def inspect(self, pid):
        W = self.W
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code, size = W.DWORD(), W.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if (not self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259
                    or not self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))):
                return None
            executable = Path(name.value).resolve()
            if not _inside(executable, self.root) or executable.name.lower() not in (
                    'share monkey.exe', 'python.exe', 'pythonw.exe'):
                return None
        finally:
            self.kernel.CloseHandle(handle)
        module = self.root / 'pkgs' / 'sharemouse' / '__init__.py'
        if module.is_symlink() or not module.is_file():
            return None
        match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", module.read_text(encoding='utf-8'), re.M)
        visible = False

        @self.callback
        def window(hwnd, _):
            nonlocal visible
            owner, rect = W.DWORD(), W.RECT()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if (owner.value == pid and self.user.IsWindowVisible(hwnd)
                    and self.user.GetWindowRect(hwnd, ctypes.byref(rect))
                    and rect.right - rect.left >= 500 and rect.bottom - rect.top >= 350):
                visible = True
            return True

        if not self.user.EnumWindows(window, 0):
            raise OSError('Cannot inspect own client windows')
        return dict(pid=pid, alive=True, executable=str(executable), installed_root=str(self.root.resolve()),
                    metadata_version=match[1] if match else '', main_window_visible=visible,
                    identity_method='live executable inside installed root and installed module version')


def select_session(directory, inspector, pid=None):
    candidates = []
    for path in Path(directory).glob('connection-*.jsonl'):
        match = LOG_NAME.fullmatch(path.name)
        if match and not path.is_symlink() and (pid is None or int(match[1]) == pid):
            candidates.append(path)
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        session = read_session(path)
        if session['ended'] or session['pid'] == os.getpid():
            continue
        process = inspector.inspect(session['pid'])
        if (process and session['header']['platform'] == sys.platform
                and process['metadata_version'] == session['header']['version']):
            return path, session, process
    return None


def evaluate_observation(before, after, samples, duration):
    """Only a same-log, same-process, progressing observation may pass locally."""
    if not samples or duration < 3:
        return 'not_run', 'Observation needs at least three seconds and process samples', {}
    if before['file'] != after['file'] or before['pid'] != after['pid'] or before['header'] != after['header']:
        return 'failed', 'Session identity changed while observing', {}
    if any(not sample.get('alive') for sample in samples) or after['ended']:
        return 'failed', 'Observed client exited or the selected session ended', {}
    if before['telemetry_full'] or after['telemetry_full']:
        return 'not_run', 'Aggregate log reached its size limit; liveness cannot be measured', {}
    delta = {name: after['counters'].get(name, 0) - before['counters'].get(name, 0) for name in COUNTERS}
    if any(value < 0 for value in delta.values()):
        return 'failed', 'Session aggregate counters moved backwards', delta
    if not after.get('latest_timestamp') or not before.get('latest_timestamp'):
        return 'not_run', 'Insufficient aggregate intervals', delta
    if parse_utc(after['latest_timestamp']) <= parse_utc(before['latest_timestamp']):
        return 'not_run', 'Aggregate log made no progress during observation', delta
    if delta['sent_frames'] <= 0 or delta['received_frames'] <= 0:
        return 'not_run', 'No two-way frame progress; starting a session is not proof of a connection', delta
    return 'passed', 'Observed the same installed client and session with two-way frame progress', delta


def observe(args):
    started = utc_now()
    context = source_context()
    evidence = dict(run_id=args.run_id or str(uuid.uuid4()), limitations=BOUNDARIES,
                    requested_seconds=args.duration, duration_seconds=0,
                    real_bluetooth=False, real_peer_scenarios_executed=False,
                    password_read=False, user_input_recorded=False, mutations=False,
                    samples=[], coverage={name: 'not_run' for name in SCENARIOS})
    status, reason = 'not_run', 'Unsupported platform'
    stopped = []

    def stop(signum, _):
        stopped.append(signum)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if sys.platform not in ('darwin', 'win32'):
            return make_report('peer-observation', status, evidence=evidence, context=context,
                               evidence_scope='observational', started_at=started, reason=reason)
        inspector = MacClient() if sys.platform == 'darwin' else WindowsClient(args.installed_root)
        chosen = select_session(log_directory(), inspector, args.pid)
        if chosen is None:
            reason = 'No running installed Share Monkey client with an unended matching-version session log'
        else:
            path, before, process = chosen
            evidence.update(process=process, before=before, transport=before['header']['transport'])
            context = source_context(tested_version=before['header']['version'], runtime=dict(
                kind='installed', path=process.get('bundle', process.get('installed_root')),
                version_verified=False, metadata_matches_log=True, binary_verified=False))
            origin = time.monotonic()
            while not stopped:
                current = inspector.inspect(before['pid'])
                evidence['samples'].append(dict(at=utc_now(), alive=bool(current),
                    main_window_visible=current.get('main_window_visible') if current else None))
                elapsed = time.monotonic() - origin
                if not current or elapsed >= args.duration:
                    break
                time.sleep(min(.5, args.duration - elapsed))
            evidence['duration_seconds'] = round(time.monotonic() - origin, 3)
            after = read_session(path)
            evidence['after'] = after
            status, reason, delta = evaluate_observation(before, after, evidence['samples'],
                                                        evidence['duration_seconds'])
            evidence['counter_delta_within_observation'] = delta
            evidence['main_window_hidden_samples'] = sum(sample['main_window_visible'] is False
                                                       for sample in evidence['samples'])
            evidence['telemetry_transport_bluetooth'] = before['header']['transport'] == 'bluetooth'
        if stopped:
            status, reason = 'interrupted', 'Observation interrupted by signal ' + str(stopped[-1])
    except (OSError, ValueError, TypeError, ImportError, KeyError) as exc:
        status, reason = 'failed', type(exc).__name__ + ': ' + str(exc)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return make_report('peer-observation', status, evidence=evidence, context=context,
                       evidence_scope='observational', started_at=started, reason=reason)


def combine_reports(reports):
    observations, problems = {}, []
    for report in reports:
        if (not isinstance(report, dict) or report.get('schema_version') != SCHEMA_VERSION
                or report.get('check_id') != 'peer-observation'
                or report.get('evidence_scope') != 'observational'):
            problems.append('Invalid observation report schema or scope')
            continue
        platform = report.get('context', {}).get('platform')
        if platform not in ('darwin', 'win32') or platform in observations:
            problems.append('Missing/duplicate host platform')
            continue
        observations[platform] = report
        if report.get('status') != 'passed':
            problems.append(platform + ': observation did not pass')
    overlap = 0.0
    if set(observations) == {'darwin', 'win32'}:
        mac, windows = observations['darwin'], observations['win32']
        try:
            overlap = max(0, (min(parse_utc(mac['completed_at']), parse_utc(windows['completed_at']))
                              - max(parse_utc(mac['started_at']), parse_utc(windows['started_at']))).total_seconds())
        except (ValueError, KeyError, TypeError):
            problems.append('Observation timestamps are invalid')
        roles = {item.get('evidence', {}).get('before', {}).get('header', {}).get('role')
                 for item in observations.values()}
        if roles != {'controller', 'receiver'}:
            problems.append('Observed roles are not complementary')
        if overlap <= 0:
            problems.append('Host observations did not overlap in time')
    else:
        problems.append('Both Mac and Windows observations are required')
    return make_report('paired', 'not_run', evidence_scope='observational',
        reason='Full paired release validation is not implemented by a passive log collector',
        evidence=dict(observations=observations, diagnostic_problems=problems,
                      overlap_seconds=overlap, real_bluetooth=False, bidirectional=False,
                      authenticated_peer_identity_verified=False,
                      coverage={name: 'not_run' for name in SCENARIOS}, limitations=BOUNDARIES))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    local = sub.add_parser('observe', help='Read only the current installed client and its aggregate log')
    local.add_argument('--duration', type=float, default=15)
    local.add_argument('--output', type=Path, required=True)
    local.add_argument('--pid', type=int)
    local.add_argument('--installed-root', type=Path)
    local.add_argument('--run-id', help='Optional label only; never evidence of exact peer identity')
    combine = sub.add_parser('combine', help='Collect two host observations without promoting their scope')
    combine.add_argument('--mac', type=Path, required=True)
    combine.add_argument('--windows', type=Path, required=True)
    combine.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'observe':
        if not math.isfinite(args.duration) or not 3 <= args.duration <= 600 or (args.pid is not None and args.pid <= 0):
            parser.error('duration must be 3..600 seconds and PID must be positive')
        result = observe(args)
    else:
        try:
            result = combine_reports([json.loads(path.read_text(encoding='utf-8'))
                                      for path in (args.mac, args.windows)])
        except (OSError, ValueError) as exc:
            result = make_report('paired', 'not_run', evidence_scope='observational', reason=str(exc))
    write_report(result, args.output)
    print(json.dumps(dict(status=result['status'], reason=result['reason'], output=str(args.output)), ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 2 if result['status'] == 'not_run' else 1


if __name__ == '__main__':
    raise SystemExit(main())
