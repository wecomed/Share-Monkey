"""Run interruptible, resumable stability checks without claiming a real peer.

Default: two hours of model checks, with no input taps, windows or injection.
--scope mac-native --allow-native adds a listen-only native keyboard probe.
--include-window-capture additionally permits foreground changes and cursor
capture; use that option only while the desktop is not in use. All children
run sequentially. Neither scope connects Bluetooth or verifies physical input.

Example short check: python tools/soak_stability.py --duration 15 --output
validation/soak-smoke/report.json. --cycles is an upper bound, not permission
to report a duration requirement as passed before that duration is achieved.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.validation_common import (evidence_problem, make_report, source_context,
                                     utc_now, write_report)


@dataclass
class Config:
    output: Path
    duration: float = 7200
    cycles: int | None = None
    scope: str = 'model'
    allow_native: bool = False
    include_window_capture: bool = False
    step_timeout: float = 150
    resume: bool = False
    python: str = sys.executable

    @property
    def checkpoint(self):
        return self.output.with_suffix('.checkpoint.json')

    def identity(self):
        return dict(duration=self.duration, cycles=self.cycles, scope=self.scope,
                    include_window_capture=self.include_window_capture,
                    step_timeout=self.step_timeout, python=self.python)

    def problem(self):
        if not math.isfinite(self.duration) or self.duration <= 0:
            return 'duration must be a finite positive number'
        if not math.isfinite(self.step_timeout) or self.step_timeout <= 0:
            return 'step-timeout must be a finite positive number'
        if self.cycles is not None and self.cycles < 1:
            return 'cycles must be positive'
        if self.scope not in ('model', 'mac-native'):
            return 'Unknown scope'
        if self.scope == 'mac-native' and not self.allow_native:
            return 'mac-native requires explicit --allow-native; no native actions ran'
        if self.scope == 'mac-native' and sys.platform != 'darwin':
            return 'mac-native requires macOS; no native actions ran'
        if self.include_window_capture and self.scope != 'mac-native':
            return '--include-window-capture requires mac-native and --allow-native'
        return None


@dataclass
class Step:
    check_id: str
    command: list
    artifact: Path | None = None
    mode: str | None = None
    expected_cycles: int | None = None
    pairs: int | None = None


MODEL_TESTS = (
    'tests.test_engine', 'tests.test_switching', 'tests.test_motion',
    'tests.test_delivery', 'tests.test_clipboard_locking',
    'tests.test_bluetooth_transport',
    'tests.test_mac_capture_ui.CaptureUITests.test_cancel_before_main_queue_never_activates',
    'tests.test_mac_capture_ui.CaptureUITests.test_old_release_and_prepare_cannot_close_or_ready_new_capture',
    'tests.test_mac_capture_ui.CaptureUITests.test_focus_loss_after_capture_fails_closed_without_activation_loop',
    'tests.test_mac_capture_ui.CaptureUITests.test_native_completion_aligns_panel_to_actual_cursor_before_ready',
    'tests.test_mac_capture_ui.CaptureUITests.test_continuous_cursor_drift_has_three_retries_then_fails',
    'tests.test_mac_capture_ui.CaptureUITests.test_cancelled_alignment_retry_never_prepares_or_reactivates',
)
WINDOW_SCENARIOS = ('hidden-entry', 'close-active', 'reopen-active',
                    'focus-loss-active', 'anchor-drift')


def required_checks(config):
    checks = {'handoff_left', 'handoff_right', 'mixed_model'}
    if config.scope == 'mac-native':
        checks.update(('native_keyboard_64', 'native_keyboard_512'))
    if config.include_window_capture:
        checks.update('window_' + item for item in WINDOW_SCENARIOS)
    return checks


def cycle_steps(config, cycle, directory):
    def handoff(side):
        artifact = directory / ('handoff-' + side + '.json')
        return Step('handoff_' + side, [config.python, str(ROOT / 'tools/probe_mac_handoff.py'),
                    '--self-test', '--cycles', '4', '--side', side, '--output', str(artifact)],
                    artifact, 'self-test', 4)
    steps = [handoff('left'), Step('mixed_model',
             [config.python, '-m', 'unittest', *MODEL_TESTS, '-q']), handoff('right')]
    if config.scope == 'mac-native':
        pairs = 64 if cycle % 2 else 512
        artifact = directory / 'keyboard.json'
        steps.append(Step('native_keyboard_' + str(pairs),
                     [config.python, str(ROOT / 'tools/probe_mac_keyboard.py'), '--native',
                      '--pairs', str(pairs), '--output', str(artifact)],
                     artifact, 'native-keyboard', pairs=pairs))
    if config.include_window_capture:
        scenario = WINDOW_SCENARIOS[(cycle - 1) % len(WINDOW_SCENARIOS)]
        artifact = directory / 'window.json'
        steps.append(Step('window_' + scenario,
                     [config.python, str(ROOT / 'tools/probe_mac_residency.py'), '--capture',
                      '--scenario', scenario, '--cycles', '2', '--output', str(artifact)],
                     artifact, 'native-synthetic-residency', 2))
    return steps


@contextmanager
def signal_stop(stop):
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGINT', 'SIGTERM'):
            sig = getattr(signal, name, None)
            if sig is not None:
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_child(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            # Python's SIGINT raises KeyboardInterrupt, so a native probe can
            # run its existing finally blocks and restore capture/listeners.
            # SIGTERM would normally terminate it before that cleanup runs.
            os.killpg(process.pid, signal.SIGINT)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)


def run_child(step, log, timeout, stop):
    """Independent timeout, aggregate-only logs, and process-tree cleanup."""
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    result = dict(check_id=step.check_id, status='failed', log=str(log),
                  artifact=str(step.artifact) if step.artifact else None,
                  command=step.command, elapsed_seconds=0.0)
    if stop.is_set():
        return dict(result, status='interrupted', reason='Interrupted before child startup')
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(step.command, cwd=ROOT, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
        try:
            while process.poll() is None:
                if stop.is_set():
                    result.update(status='interrupted', reason='Stop requested; child terminated')
                    break
                if time.monotonic() - started >= timeout:
                    result.update(reason='Subprocess exceeded its independent timeout')
                    break
                stop.wait(.05)
            else:
                result.update(exit_code=process.returncode)
                if process.returncode == 0:
                    result.update(status='passed', reason=None)
                else:
                    result.update(reason='Subprocess exited with code ' + str(process.returncode))
        finally:
            stop_child(process)
            result['elapsed_seconds'] = time.monotonic() - started
    # A missing native permission is an explicit incomplete check, even when
    # the native script happens to return zero in some future revision.
    if result['status'] != 'interrupted' and step.artifact and step.artifact.is_file():
        try:
            evidence = json.loads(step.artifact.read_text(encoding='utf-8'))
            problem = evidence_problem(evidence)
            if problem:
                result.update(status=problem[0], reason=problem[1])
            elif result['status'] == 'passed':
                if evidence.get('mode') != step.mode:
                    result.update(status='failed', reason='Unexpected probe mode')
                elif step.expected_cycles and evidence.get('cycles_completed') != step.expected_cycles:
                    result.update(status='failed', reason='Probe did not complete every requested cycle')
                elif step.pairs and (evidence.get('pairs_requested') != step.pairs or
                                     len(evidence.get('phases', [])) != 7 or
                                     not all(item.get('passed') is True for item in evidence['phases'])):
                    result.update(status='failed', reason='Native keyboard phases were incomplete')
        except (ValueError, OSError, TypeError) as exc:
            result.update(status='failed', reason='Invalid probe artifact: ' + str(exc))
    elif result['status'] == 'passed' and step.artifact:
        result.update(status='failed', reason='Probe artifact is missing')
    if result['status'] == 'passed' and not step.artifact:
        text = log.read_text(encoding='utf-8')
        match = re.search(r'Ran (\d+) tests?', text)
        if not match or int(match.group(1)) == 0:
            result.update(status='failed', reason='Model batch did not execute any tests')
        elif re.search(r'\bskipped\s*=\s*[1-9]', text):
            result.update(status='not_run', reason='Model batch skipped required tests')
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run(config, *, runner=run_child, stop=None, context=None):
    """A successful child alone can never promote an unfinished soak."""
    config.output = Path(config.output).resolve()
    stop = stop or threading.Event()
    verify_live_source = context is None
    context = context or source_context()
    problem = config.problem()
    state = dict(schema_version=1, check_id='soak', run_id=uuid.uuid4().hex,
                 started_at=utc_now(), config=config.identity(), context=context,
                 status='not_run', run_state='running', completed_cycles=0,
                 successful_seconds=0.0, resume_count=0, coverage={}, checks={}, recent=[],
                 attempts=0, active_step=None)
    if config.resume:
        try:
            saved = json.loads(config.checkpoint.read_text(encoding='utf-8'))
            if (saved['schema_version'] != 1 or saved['check_id'] != 'soak' or
                    saved['config'] != config.identity() or saved['context'] != context):
                raise ValueError('Checkpoint source, runtime or configuration differs')
            if saved['status'] in ('failed', 'passed'):
                raise ValueError('A failed or completed run requires a fresh output directory')
            state = saved
            state.update(status='not_run', run_state='running', active_step=None,
                         resume_count=state['resume_count'] + 1)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problem = 'Cannot resume: ' + str(exc)
    elif config.checkpoint.exists():
        problem = 'Output already contains a checkpoint; use --resume or a fresh output path'
    journal = config.output.with_suffix('.events.jsonl')

    def persist(status, reason=None, final=False):
        state.update(status=status, run_state=status if final else 'running', updated_at=utc_now())
        evidence = dict(required_duration_seconds=config.duration,
                        duration_seconds=state['successful_seconds'],
                        duration_semantics='sum_of_completed_successful_subprocess_time; excludes gaps, idle and interrupted checks',
                        continuous_run=state['resume_count'] == 0,
                        requested_cycles=config.cycles, completed_cycles=state['completed_cycles'],
                        coverage=state['coverage'], required_checks=sorted(required_checks(config)),
                        checks=list(state['checks'].values()), recent_checks=state['recent'],
                        run_state=state['run_state'], resume_count=state['resume_count'],
                        active_step=state['active_step'], journal=str(journal),
                        checkpoint=str(config.checkpoint), real_bluetooth=False,
                        physical_input=False, bidirectional=False, mixed_input=True,
                        transport='in-memory', scopes=['model'] +
                        (['mac-native'] if config.scope == 'mac-native' else []),
                        window_capture_enabled=config.include_window_capture,
                        limitation='Local synthetic/model checks only; does not qualify as a real Mac/Windows Bluetooth soak')
        report = make_report('soak', status, context=context, evidence=evidence,
                             evidence_scope=config.scope, started_at=state['started_at'], reason=reason)
        if not final:
            report['completed_at'] = None
        # Never overwrite someone else's finished run on invalid --resume.
        if problem and config.checkpoint.exists():
            return report
        atomic_json(config.checkpoint, state)
        write_report(report, config.output)
        return report

    if problem:
        return persist('not_run', problem, final=True)
    persist('not_run', 'Soak is running; duration and coverage are not yet complete')
    try:
        with signal_stop(stop):
            while True:
                if stop.is_set():
                    return persist('interrupted', 'Stop requested; unfinished work was not counted', final=True)
                if (state['successful_seconds'] >= config.duration and
                        required_checks(config) <= set(state['coverage'])):
                    return persist('passed', final=True)
                if config.cycles is not None and state['completed_cycles'] >= config.cycles:
                    return persist('failed', 'Cycle limit reached before duration or required coverage was achieved', final=True)
                cycle = state['completed_cycles'] + 1
                state['attempts'] += 1
                directory = config.output.parent / 'steps' / state['run_id'] / str(state['attempts'])
                for step in cycle_steps(config, cycle, directory):
                    state['active_step'] = step.check_id
                    persist('not_run', 'Soak is running; current child has not completed')
                    result = runner(step, directory / (step.check_id + '.log'), config.step_timeout, stop)
                    with journal.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(dict(cycle=cycle, completed_at=utc_now(), **result),
                                                ensure_ascii=False) + '\n')
                        stream.flush()
                    state['recent'] = (state['recent'] + [result])[-12:]
                    summary = state['checks'].setdefault(step.check_id, dict(
                        check_id=step.check_id, status='not_run', executions=0, elapsed_seconds=0.0))
                    summary.update(status=result['status'], log=result['log'], reason=result.get('reason'))
                    summary['executions'] += 1
                    summary['elapsed_seconds'] += result['elapsed_seconds']
                    state['active_step'] = None
                    if result['status'] != 'passed':
                        return persist(result['status'], result.get('reason'), final=True)
                    elapsed = result['elapsed_seconds']
                    if not math.isfinite(elapsed) or elapsed < 0:
                        return persist('failed', 'Child reported an invalid duration', final=True)
                    state['successful_seconds'] += elapsed
                    state['coverage'][step.check_id] = 'passed'
                    persist('not_run', 'Soak is running; current cycle has not completed')
                state['completed_cycles'] = cycle
                if verify_live_source:
                    live = source_context()
                    if live['source_fingerprint'] != context['source_fingerprint']:
                        return persist('failed', 'Source changed during the soak; mixed revisions cannot pass', final=True)
                persist('not_run', 'Soak is running; checking duration and coverage')
    except KeyboardInterrupt:
        stop.set()
        return persist('interrupted', 'Keyboard interrupt; unfinished work was not counted', final=True)
    except Exception as exc:
        return persist('failed', type(exc).__name__ + ': ' + str(exc), final=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('validation/soak/report.json'))
    parser.add_argument('--duration', type=float, default=7200, help='Required successful check runtime in seconds')
    parser.add_argument('--cycles', type=int, help='Optional maximum number of completed mixed cycles')
    parser.add_argument('--scope', choices=('model', 'mac-native'), default='model')
    parser.add_argument('--allow-native', action='store_true')
    parser.add_argument('--include-window-capture', action='store_true')
    parser.add_argument('--step-timeout', type=float, default=150)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    report = run(Config(**vars(args)))
    print(json.dumps(dict(status=report['status'], reason=report.get('reason'),
                          evidence_scope=report['evidence_scope'], output=str(args.output),
                          duration_seconds=report['evidence']['duration_seconds'],
                          required_duration_seconds=args.duration), ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 130 if report['status'] == 'interrupted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
