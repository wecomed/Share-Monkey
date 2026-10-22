"""Official release build entry: verify all evidence before invoking a builder.

Development builders remain available separately. This entry never runs native
probes, installs a client or connects a peer. After verification, the existing
builder may download its normal packaging dependencies.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.validation_common import (ROOT, SCHEMA_VERSION, evidence_problem, make_report,
                                     parse_utc, source_context, utc_now, write_report)
from tools.verify_release import _stop_process, exit_code

REQUIRED_CHECKS = {'unittest', 'handoff-model', 'mac-native', 'windows-native', 'paired', 'soak'}


def _execute(argv, log_path, timeout):
    process = None
    result = dict(command=list(map(str, argv)), log=str(log_path), status='failed')
    try:
        with Path(log_path).open('w', encoding='utf-8') as stream:
            stream.write('Command (argv JSON): ' + json.dumps(result['command']) + '\n')
            stream.flush()
            process = subprocess.Popen(result['command'], cwd=str(ROOT), stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
            try:
                result['returncode'] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                result.update(reason='Command timed out', timed_out=True)
            else:
                code = result['returncode']
                result.update(status='passed' if code == 0 else
                              'interrupted' if code < 0 or code in (130, 143) else
                              'not_run' if code == 2 else 'failed',
                              reason=None if code == 0 else f'Command exited {code}')
    except KeyboardInterrupt:
        if process is not None:
            _stop_process(process)
        result.update(status='interrupted', reason='Release entry was interrupted')
    except OSError as exc:
        result.update(reason=type(exc).__name__ + ': ' + str(exc))
    return result


def gate_problem(report, context, started_at, windows_version):
    if (not isinstance(report, dict) or report.get('schema_version') != SCHEMA_VERSION
            or report.get('check_id') != 'release' or report.get('status') != 'passed'):
        return 'This run did not produce a passed release-gate report'
    if evidence_problem(report):
        return 'Gate report contains failed, skipped or incomplete evidence'
    observed = report.get('context')
    if not isinstance(observed, dict) or any(observed.get(name) != context.get(name) for name in
                                          ('source_fingerprint', 'harness_version', 'tested_version', 'platform', 'runtime')):
        return 'Gate source/version/runtime context does not match the build checkout'
    if observed.get('windows_target_version') != windows_version:
        return 'Gate Windows target version differs from the requested release target'
    try:
        started, completed = parse_utc(report.get('started_at')), parse_utc(report.get('completed_at'))
        if (started < parse_utc(started_at) or completed < started
                or (completed - datetime.now(timezone.utc)).total_seconds() > 5):
            return 'Gate report does not belong to this verification run'
    except (ValueError, TypeError):
        return 'Gate completion timestamps are missing or invalid'
    evidence = report.get('evidence')
    checks = evidence.get('checks') if isinstance(evidence, dict) else None
    if (not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks)
            or any(check.get('status') != 'passed' for check in checks)
            or not REQUIRED_CHECKS <= {check.get('check_id') for check in checks}):
        return 'Gate report lacks the complete set of passed release checks'
    return None


def run_release(args):
    directory = args.evidence_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    gate_dir = directory / 'release-gate'
    gate_dir.mkdir(exist_ok=True)
    report_path = directory / 'release-client.json'
    started = utc_now()
    context = source_context()
    context['windows_target_version'] = args.windows_version
    running = make_report('release-client', 'not_run', context=context, started_at=started,
                          evidence_scope='release_build', evidence=dict(running=True, checks=[]),
                          reason='Running verification; no build is authorized yet.')
    running['completed_at'] = None
    write_report(running, report_path)
    checks = []
    status = 'not_run'
    reason = None
    gate_path = gate_dir / 'report.json'
    gate_path.unlink(missing_ok=True)
    missing = [name for name in ('mac_report', 'windows_report', 'paired_report', 'soak_report')
               if getattr(args, name) is None or not getattr(args, name).is_file()]
    if missing:
        reason = 'Required external reports are missing: ' + ', '.join(missing)
    elif args.platform == 'windows' and args.windows_version != context['harness_version']:
        reason = ('Windows builder packages source version ' + context['harness_version'] +
                  '; --windows-version must match that version before building a new Windows release.')
    else:
        argv = [sys.executable, str(ROOT / 'tools/verify_release.py'), '--profile', 'release',
                '--output-dir', str(gate_dir), '--windows-version', args.windows_version]
        for option, value in (('--mac-report', args.mac_report), ('--windows-report', args.windows_report),
                              ('--paired-report', args.paired_report), ('--soak-report', args.soak_report)):
            if value is not None:
                argv.extend((option, str(value.resolve())))
        gate = _execute(argv, directory / 'gate-command.log', args.gate_timeout)
        gate['check_id'] = 'release-gate'
        checks.append(gate)
        status = gate['status']
        reason = gate.get('reason')
        if status == 'passed' and gate.get('returncode') != 0:
            status, reason = 'failed', 'Release gate did not confirm exit code zero'
            gate.update(status=status, reason=reason)
        if status == 'passed':
            try:
                report = json.loads(gate_path.read_text(encoding='utf-8'))
                reason = gate_problem(report, context, started, args.windows_version)
            except (OSError, ValueError, TypeError) as exc:
                reason = 'Cannot read this run\'s release-gate report: ' + str(exc)
            if reason:
                status = 'not_run'
                gate.update(status=status, reason=reason)
            elif source_context()['source_fingerprint'] != context['source_fingerprint']:
                status, reason = 'failed', 'Source changed after verification; build was blocked'
            else:
                builder = ROOT / 'tools' / ('build_client.py' if args.platform == 'macos' else 'build_windows_installer.py')
                command = [sys.executable, str(builder)]
                if args.platform == 'macos':
                    command.append('macos')
                command.extend(('--output-dir', str(args.output_dir.resolve())))
                build = _execute(command, directory / 'build-command.log', args.build_timeout)
                build['check_id'] = 'build'
                checks.append(build)
                status, reason = build['status'], build.get('reason')
                if status == 'passed' and source_context()['source_fingerprint'] != context['source_fingerprint']:
                    status, reason = 'failed', 'Source changed during packaging; artifacts are not a verified release'
    final = make_report('release-client', status, context=context, started_at=started,
                        evidence_scope='release_build', reason=reason,
                        evidence=dict(checks=checks, platform=args.platform,
                                      output_dir=str(args.output_dir.resolve()),
                                      gate_report=str(gate_dir / 'report.json'),
                                      native_actions=False, installs_client=False))
    write_report(final, report_path)
    print(json.dumps(dict(status=status, report=str(report_path), reason=reason)))
    return exit_code(status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('macos', 'windows'), required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/release')
    parser.add_argument('--evidence-dir', type=Path, default=ROOT / 'validation/release-build')
    parser.add_argument('--mac-report', type=Path)
    parser.add_argument('--windows-report', type=Path)
    parser.add_argument('--paired-report', type=Path)
    parser.add_argument('--soak-report', type=Path)
    parser.add_argument('--windows-version', default='0.5.4')
    parser.add_argument('--gate-timeout', type=float, default=600)
    parser.add_argument('--build-timeout', type=float, default=1800)
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or value <= 0 for value in (args.gate_timeout, args.build_timeout)):
        parser.error('Timeouts must be positive')
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_release(args)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
