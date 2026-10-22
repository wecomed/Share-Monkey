"""Fail-closed release verification. Native actions require --allow-native.

Examples:
  python tools/verify_release.py --profile quick --output-dir validation/current
  python tools/verify_release.py --profile mac-native --allow-native --output-dir validation/mac
  python tools/verify_release.py --profile release --output-dir validation/release \
    --mac-report validation/mac/report.json --windows-report Windows/report.json \
    --paired-report paired/report.json --soak-report soak/report.json

Release imports evidence; it never installs software, connects a peer or runs
native UI actions implicitly. Missing, stale, skipped or mismatched evidence
exits 2. Failures/interruption exit 1. Only every required check passed exits 0.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.validation_common import (ROOT, SCHEMA_VERSION, evidence_problem, make_report,
                                     parse_utc, source_context, utc_now, write_report)

PROFILES = ('quick', 'mac-native', 'windows-native', 'release')
PAIR_SCENARIOS = ('mac-to-windows', 'windows-to-mac', 'window-close', 'window-reopen',
                  'disconnect-recovery', 'mixed-input')
KEYBOARD_PHASES = {'media-batch', 'normal-batch', 'caps-batch', 'stale-before-main-decode',
                   'stale-after-main-decode', 'stop-before-main-decode', 'stop-after-main-decode'}
WINDOWS_PHASES = {'move': 'move', 'a-down': 'key_down', 'a-up': 'key_up',
                  'f8-down': 'key_down', 'f8-up': 'key_up', 'left-down': 'button_down',
                  'left-up': 'button_up', 'scroll': 'wheel'}


def positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def aggregate_status(checks):
    statuses = [c.get('status') for c in checks]
    if 'interrupted' in statuses:
        return 'interrupted'
    if 'failed' in statuses:
        return 'failed'
    if not statuses or any(status != 'passed' for status in statuses):
        return 'not_run'
    return 'passed'


def exit_code(status):
    return 0 if status == 'passed' else 2 if status == 'not_run' else 1


def inspect_probe(payload, kind, *, cycles=2):
    if not isinstance(payload, dict) or not payload:
        return 'not_run', 'Probe JSON is empty or is not an object'
    problem = evidence_problem(payload)
    if problem:
        return problem
    if kind == 'unittest':
        if not positive_int(payload.get('tests_run')):
            return 'not_run', 'No unit tests ran'
    elif kind == 'handoff-model':
        if payload.get('mode') != 'self-test' or not payload.get('native_apis_mocked'):
            return 'failed', 'Portable model scope was not confirmed'
        if (not positive_int(payload.get('cycles_completed')) or payload['cycles_completed'] < cycles
                or not isinstance(payload.get('cycles'), list) or len(payload['cycles']) < cycles):
            return 'not_run', 'Model cycles did not complete'
    elif kind == 'keyboard':
        if payload.get('mode') != 'native-keyboard' or not payload.get('native_objects'):
            return 'not_run', 'Native keyboard probe did not start'
        phases = payload.get('phases', [])
        if (not isinstance(phases, list) or len(phases) != 7
                or any(not isinstance(phase, dict) for phase in phases)
                or {phase.get('name') for phase in phases} != KEYBOARD_PHASES
                or not positive_int(payload.get('pairs_requested')) or payload['pairs_requested'] < 512):
            return 'not_run', 'All seven keyboard phases at 512 pairs are required'
        for phase in phases:
            batch = phase['name'] in ('media-batch', 'normal-batch', 'caps-batch')
            callbacks = payload['pairs_requested'] * 2 if batch else 0
            conversions = (callbacks if phase['name'] == 'media-batch' else
                           1 if phase['name'] in ('stale-after-main-decode', 'stop-after-main-decode') else 0)
            if (phase.get('passed') is not True or phase.get('actual_callbacks') != callbacks
                    or phase.get('expected_callbacks') != callbacks or phase.get('nsevent_main_conversions') != conversions):
                return 'not_run', 'Keyboard phase assertions were not completed'
    elif kind == 'residency':
        if payload.get('mode') != 'native-synthetic-residency':
            return 'not_run', 'Native AppKit capture did not start'
        if (not positive_int(payload.get('cycles_completed')) or payload['cycles_completed'] < cycles
                or not isinstance(payload.get('cycles'), list) or len(payload['cycles']) < cycles):
            return 'not_run', 'Required native window cycles did not complete'
        for index, cycle in enumerate(payload['cycles']):
            if (not isinstance(cycle, dict) or cycle.get('cycle') != index + 1
                    or not positive_int(cycle.get('processed_acks')) or not positive_int(cycle.get('pointer_injections'))):
                return 'not_run', 'Native window cycle did not include input injection and a processed ACK'
    elif kind == 'windows':
        if payload.get('schema_version') != SCHEMA_VERSION or payload.get('check_id') != 'windows-native':
            return 'not_run', 'Windows native report schema/check identity is missing'
        if payload.get('status') != 'passed':
            return payload.get('status', 'not_run'), payload.get('reason') or 'Windows native check did not pass'
        if payload.get('evidence_scope') not in ('native_synthetic', 'windows-native'):
            return 'not_run', 'Windows report did not verify native APIs'
        evidence = payload.get('evidence', {})
        phases = evidence.get('phases')
        if not isinstance(phases, list) or not phases or any(not isinstance(phase, dict) for phase in phases):
            return 'not_run', 'Windows native phases are missing'
        if (evidence.get('executed_on_windows') is not True or evidence.get('native_actions') is not True
                or any(phase.get('received') is not True for phase in phases)):
            return 'not_run', 'Native Windows input was not received and asserted'
        completed = evidence.get('completed_cycles')
        if not positive_int(completed) or completed < 2 or len(phases) != completed * len(WINDOWS_PHASES):
            return 'not_run', 'At least two complete Windows input cycles are required'
        expected = {f'cycle-{cycle}-{name}': event for cycle in range(1, completed + 1)
                    for name, event in WINDOWS_PHASES.items()}
        if ({phase.get('name'): phase.get('event') for phase in phases} != expected
                or evidence.get('foreground_guard_checks', 0) < len(phases)):
            return 'not_run', 'Windows input phase identities or foreground assertions are incomplete'
        coverage = evidence.get('coverage', {})
        if not isinstance(coverage, dict) or any(coverage.get(name) is not True for name in
                                               ('own_window_input', 'physical_coordinates')):
            return 'not_run', 'Own-window input and physical-coordinate assertions are incomplete'
        for phase in phases:
            if phase.get('event') == 'move':
                point = phase.get('requested')
                if (not isinstance(point, list) or len(point) != 2 or phase.get('readback') != point
                        or any(not isinstance(value, int) or isinstance(value, bool) for value in point)):
                    return 'not_run', 'Physical cursor position was not read back and asserted'
    return 'passed', None


def _stop_process(process):
    if process.poll() is not None:
        return
    # SIGINT gives native probes a chance to restore cursor/taps in finally.
    for sig, grace in ((signal.SIGINT, 4), (signal.SIGTERM, 2)):
        try:
            if os.name == 'posix':
                os.killpg(process.pid, sig)
            else:
                process.terminate()
            process.wait(timeout=grace)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            pass
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait(timeout=5)


def run_check(check_id, argv, output_dir, *, timeout, kind, cycles=2):
    """Run a structured argv and retain its own log, even after interruption."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log = output_dir / (check_id + '.log')
    output = output_dir / (check_id + '.json')
    output.unlink(missing_ok=True)  # Never accept an earlier run's artifact.
    started = utc_now()
    check = dict(check_id=check_id, status='not_run', started_at=started,
                 command=list(map(str, argv)), log=str(log.resolve()), output=str(output.resolve()))
    process = None
    try:
        with log.open('w', encoding='utf-8') as stream:
            stream.write('Command (argv JSON): ' + json.dumps(check['command']) + '\n')
            stream.flush()
            process = subprocess.Popen(check['command'], cwd=str(ROOT), stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                check.update(status='failed', reason=f'Exceeded timeout ({timeout:g} seconds)', timed_out=True)
            else:
                check['returncode'] = code
                if code < 0 or code in (130, 143):
                    check.update(status='interrupted', reason=f'Child terminated by signal {-code}')
                elif code != 0:
                    # Permission skips commonly exit 2; inspect that JSON too.
                    check.update(status='not_run' if code == 2 else 'failed', reason=f'Child exited {code}')
                if output.is_file():
                    try:
                        payload = json.loads(output.read_text(encoding='utf-8'))
                        check['evidence'] = payload
                        observed, reason = inspect_probe(payload, kind, cycles=cycles)
                        if code == 0 or observed in ('failed', 'interrupted'):
                            check.update(status=observed, reason=reason)
                    except (OSError, ValueError, TypeError) as exc:
                        check.update(status='failed', reason='Invalid probe output: ' + str(exc))
                elif code == 0:
                    check.update(status='not_run', reason='Child exited zero without its required JSON artifact')
    except KeyboardInterrupt:
        if process is not None:
            _stop_process(process)
        check.update(status='interrupted', reason='Validation interrupted; remaining checks were not run')
    except OSError as exc:
        check.update(status='failed', reason=type(exc).__name__ + ': ' + str(exc))
    finally:
        check['completed_at'] = utc_now()
    return check


def _version_runtime_problem(context, expected_version):
    if context.get('tested_version') != expected_version:
        return 'Tested version does not match required target ' + expected_version
    runtime = context.get('runtime', {})
    if not isinstance(runtime, dict):
        return 'Runtime identity is malformed'
    if context.get('tested_version') != context.get('harness_version'):
        if runtime.get('kind') not in ('installed', 'bundled') or runtime.get('version_verified') is not True:
            return 'Different product/harness versions require an independently verified installed/bundled runtime'
    return None


def validate_import(report, check_id, context, *, expected_version=None, now=None, _depth=0):
    """Validate same-source, fresh external evidence without trusting status alone."""
    if _depth > 1:
        return 'not_run', 'Nested native report depth is unsupported'
    if not isinstance(report, dict) or report.get('schema_version') != SCHEMA_VERSION:
        return 'not_run', 'Unsupported or missing report schema'
    if report.get('check_id') != check_id:
        return 'not_run', 'Report check_id does not match ' + check_id
    if report.get('status') not in ('passed', 'failed', 'not_run', 'interrupted'):
        return 'not_run', 'Missing final status'
    problem = evidence_problem(report)
    if problem:
        return problem
    if report['status'] != 'passed':
        return report['status'], report.get('reason') or 'Required check has not passed'
    try:
        finished = parse_utc(report.get('completed_at'))
        started = parse_utc(report.get('started_at'))
    except (ValueError, TypeError):
        return 'not_run', 'Missing or invalid UTC timestamps'
    now = now or datetime.now(timezone.utc)
    if finished < started or (now - finished).total_seconds() > 86400 or (finished - now).total_seconds() > 300:
        return 'not_run', 'Report is stale, future-dated or has invalid chronology'
    observed = report.get('context', {})
    if not isinstance(observed, dict):
        return 'not_run', 'Report context is malformed'
    fingerprint = observed.get('source_fingerprint', {})
    if not isinstance(fingerprint, dict):
        return 'not_run', 'Source fingerprint is malformed'
    if fingerprint != context['source_fingerprint']:
        return 'not_run', 'Source/harness fingerprint differs from the current checkout'
    if observed.get('harness_version') != context['harness_version']:
        return 'not_run', 'Harness version differs from the current checkout'
    if expected_version:
        mismatch = _version_runtime_problem(observed, expected_version)
        if mismatch:
            return 'not_run', mismatch
    evidence = report.get('evidence', {})
    if not isinstance(evidence, dict) or not evidence:
        return 'not_run', 'External report contains no evidence'
    if check_id in ('mac-native', 'windows-native'):
        platform = 'darwin' if check_id == 'mac-native' else 'win32'
        if observed.get('platform') != platform:
            return 'not_run', 'Native platform does not match its report'
        if report.get('evidence_scope') not in ('native_synthetic', check_id):
            return 'not_run', 'Native APIs were not verified'
        if check_id == 'mac-native':
            checks = evidence.get('checks', [])
            if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
                return 'not_run', 'Native child checks are malformed'
            required = {'mac-keyboard'} | {'mac-' + name for name in
                       ('hidden-entry', 'close-active', 'reopen-active', 'focus-loss-active', 'anchor-drift')}
            found = {item.get('check_id') for item in checks if item.get('status') == 'passed'}
            if not required <= found:
                return 'not_run', 'Required keyboard/window native checks are missing'
            for item in checks:
                if item.get('check_id') in required:
                    kind = 'keyboard' if item['check_id'] == 'mac-keyboard' else 'residency'
                    status, reason = inspect_probe(item.get('evidence'), kind)
                    if status != 'passed':
                        return status, 'Imported ' + item['check_id'] + ': ' + str(reason)
                    if kind == 'residency' and item['evidence'].get('scenario') != item['check_id'][4:]:
                        return 'not_run', 'Native residency scenario identity does not match'
        else:
            native_report = report
            if not evidence.get('phases'):
                nested = evidence.get('checks', [])
                if not isinstance(nested, list):
                    return 'not_run', 'Native Windows child checks are malformed'
                matches = [item.get('evidence') for item in nested
                           if isinstance(item, dict) and item.get('check_id') == 'windows-native']
                if len(matches) != 1 or not isinstance(matches[0], dict):
                    return 'not_run', 'Native Windows phases are missing'
                native_report = matches[0]
                status, reason = validate_import(native_report, 'windows-native', context,
                                                 expected_version=expected_version, now=now, _depth=_depth + 1)
                if status != 'passed':
                    return status, 'Nested Windows report: ' + str(reason)
            status, reason = inspect_probe(native_report, 'windows')
            if status != 'passed':
                return status, reason
            if (observed.get('tested_version') != observed.get('harness_version')
                    and (native_report['evidence'].get('adapter_provenance_verified') is not True
                         or not isinstance(native_report['evidence'].get('tested_runtime'), dict)
                         or not all(native_report['evidence']['tested_runtime'].get(name) for name in
                                    ('adapter_module', 'pointer_module', 'source_files')))):
                return 'not_run', 'Installed target adapter provenance was not verified by the native probe'
    if check_id in ('paired', 'soak'):
        if (report.get('evidence_scope') != 'real_peer' or evidence.get('transport') != 'bluetooth'
                or evidence.get('real_bluetooth') is not True or evidence.get('bidirectional') is not True):
            return 'not_run', 'Actual bidirectional Bluetooth peers were not verified'
        coverage = evidence.get('coverage', {})
        if not isinstance(coverage, dict):
            return 'not_run', 'Actual paired scenario coverage is malformed'
        if any(coverage.get(name) != 'passed' for name in PAIR_SCENARIOS):
            return 'not_run', 'Actual paired scenario coverage is incomplete'
        peers = evidence.get('peers', {})
        if not isinstance(peers, dict):
            return 'not_run', 'Peer identities are malformed'
        versions = {'mac': context['harness_version'], 'windows': context.get('windows_target_version', '0.5.4')}
        for name, version in versions.items():
            peer = peers.get(name, {})
            if not isinstance(peer, dict):
                return 'not_run', 'Peer identity is malformed'
            runtime = peer.get('runtime', {})
            if not isinstance(runtime, dict):
                return 'not_run', 'Peer runtime identity is malformed'
            if (peer.get('tested_version') != version or runtime.get('kind') not in ('installed', 'bundled')
                    or runtime.get('version_verified') is not True):
                return 'not_run', 'Actual ' + name + ' product identity/version is unverified'
        if check_id == 'soak':
            duration = evidence.get('duration_seconds', 0)
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                    or not math.isfinite(duration) or duration < 7200 or evidence.get('mixed_input') is not True
                    or (finished - started).total_seconds() < 7199):
                return 'not_run', 'Real mixed-input soak must last at least two hours'
    return 'passed', None


def import_check(check_id, path, output_dir, context, *, expected_version=None):
    log = Path(output_dir) / (check_id + '.log')
    check = dict(check_id=check_id, status='not_run', imported_from=str(path) if path else None,
                 log=str(log.resolve()), started_at=utc_now())
    if not path or not Path(path).is_file():
        check['reason'] = 'Required external report is missing'
    else:
        try:
            payload = json.loads(Path(path).read_text(encoding='utf-8'))
            check['evidence'] = payload
            check['status'], check['reason'] = validate_import(payload, check_id, context, expected_version=expected_version)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            check.update(status='failed', reason='Cannot validate imported report: ' + str(exc))
    check['completed_at'] = utc_now()
    log.write_text(json.dumps(check, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return check


def unittest_report(output):
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = dict(tests_run=result.testsRun, errors=[text for _, text in result.errors],
                    failures=[text for _, text in result.failures],
                    skipped=[dict(test=str(test), reason=reason) for test, reason in result.skipped],
                    expected_failures=[str(test) for test, _ in result.expectedFailures],
                    unexpected_successes=[str(test) for test in result.unexpectedSuccesses])
    status = ('failed' if not result.wasSuccessful() else
              'not_run' if result.skipped or result.expectedFailures or not result.testsRun else 'passed')
    Path(output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return exit_code(status)


def run_profile(args):
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    context = source_context()
    context['windows_target_version'] = args.windows_version
    # Invalidate any previous success before launching a child. SIGKILL and
    # unexpected interpreter failure cannot execute finally; a run with no
    # completion timestamp must therefore remain explicitly incomplete.
    running = make_report(args.profile, 'not_run', context=context,
                          evidence_scope='pending', started_at=started,
                          evidence=dict(checks=[], running=True),
                          reason='Running verification; no completed result exists for this run.')
    running['completed_at'] = None
    write_report(running, directory / 'report.json')
    (directory / 'report.md').replace(directory / 'REPORT.md')
    checks = []
    commands = []
    scope = 'model'
    if args.profile in ('quick', 'release'):
        commands.extend([
            ('unittest', [sys.executable, str(Path(__file__).resolve()), '--_unittest-report', str(directory / 'unittest.json')], 'unittest', 240),
            ('handoff-model', [sys.executable, str(ROOT / 'tools/probe_mac_handoff.py'), '--self-test', '--cycles', '4', '--output', str(directory / 'handoff-model.json')], 'handoff-model', 60),
        ])
    if args.profile == 'mac-native':
        scope = 'native_synthetic'
        commands.append(('mac-keyboard', [sys.executable, str(ROOT / 'tools/probe_mac_keyboard.py'), '--native', '--pairs', '512', '--output', str(directory / 'mac-keyboard.json')], 'keyboard', 150))
        for scenario in ('hidden-entry', 'close-active', 'reopen-active', 'focus-loss-active', 'anchor-drift'):
            check_id = 'mac-' + scenario
            commands.append((check_id, [sys.executable, str(ROOT / 'tools/probe_mac_residency.py'), '--capture', '--scenario', scenario, '--cycles', '2', '--output', str(directory / (check_id + '.json'))], 'residency', 150))
    elif args.profile == 'windows-native':
        scope = 'native_synthetic'
        commands.append(('windows-native', [sys.executable, str(ROOT / 'tools/probe_windows_native.py'), '--native', '--output', str(directory / 'windows-native.json')], 'windows', 150))
    interrupted = False
    native_allowed = args.allow_native and ((args.profile == 'mac-native' and sys.platform == 'darwin') or
                                            (args.profile == 'windows-native' and sys.platform == 'win32'))
    try:
        for check_id, command, kind, timeout in commands:
            if interrupted or (args.profile.endswith('-native') and not native_allowed):
                reason = ('Earlier check was interrupted' if interrupted else
                          'Native profile requires --allow-native on its target OS; no native action was performed')
                log = directory / (check_id + '.log')
                log.write_text(reason + '\n', encoding='utf-8')
                checks.append(dict(check_id=check_id, status='not_run', reason=reason, log=str(log)))
                continue
            check = run_check(check_id, command, directory, timeout=timeout, kind=kind, cycles=4 if kind == 'handoff-model' else 2)
            checks.append(check)
            interrupted = check['status'] == 'interrupted'
        if args.profile == 'release':
            scope = 'release_gate'
            for check_id, path, version in (
                ('mac-native', args.mac_report, context['harness_version']),
                ('windows-native', args.windows_report, args.windows_version),
                ('paired', args.paired_report, None), ('soak', args.soak_report, None)):
                checks.append(import_check(check_id, path, directory, context, expected_version=version))
    except KeyboardInterrupt:
        checks.append(dict(check_id='gate-interruption', status='interrupted', reason='Interrupted outside a child check'))
    final_context = source_context()
    if final_context['source_fingerprint']['digest'] != context['source_fingerprint']['digest']:
        log = directory / 'source-snapshot.log'
        reason = 'Source/harness changed during verification; rerun against a stable checkout'
        log.write_text(reason + '\n', encoding='utf-8')
        checks.append(dict(check_id='source-snapshot', status='failed', reason=reason, log=str(log)))
    status = aggregate_status(checks)
    report = make_report(args.profile, status, context=context, evidence_scope=scope, started_at=started,
                         evidence=dict(checks=checks, native_actions_authorized=bool(args.allow_native),
                                       limitations=['Model/native synthetic checks do not prove real Windows/Bluetooth peer operation.']),
                         reason=None if status == 'passed' else 'One or more required checks failed, were interrupted or were not run.')
    write_report(report, directory / 'report.json')
    (directory / 'report.md').replace(directory / 'REPORT.md')
    print(json.dumps(dict(profile=args.profile, status=status, report=str(directory / 'report.json'))))
    return exit_code(status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', choices=PROFILES, default='quick')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'validation/current')
    parser.add_argument('--allow-native', action='store_true')
    parser.add_argument('--mac-report', type=Path)
    parser.add_argument('--windows-report', type=Path)
    parser.add_argument('--paired-report', type=Path)
    parser.add_argument('--soak-report', type=Path)
    parser.add_argument('--windows-version', default='0.5.4')
    parser.add_argument('--_unittest-report', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._unittest_report:
        return unittest_report(args._unittest_report)
    old_handler = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_profile(args)
    finally:
        signal.signal(signal.SIGTERM, old_handler)


if __name__ == '__main__':
    raise SystemExit(main())
