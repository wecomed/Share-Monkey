"""Evidence integrity, interruption and duration checks; no native actions."""
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools.soak_stability import Config, Step, cycle_steps, required_checks, run, run_child, signal_stop


CONTEXT = dict(source_fingerprint=dict(algorithm='sha256', digest='test-source'),
               harness_version='test', tested_version='test', platform=sys.platform,
               runtime=dict(kind='source', path='test'))


class SoakTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / 'report.json'

    def config(self, **kwargs):
        return Config(self.output, **kwargs)

    @staticmethod
    def success(step, log, timeout, stop):
        return dict(check_id=step.check_id, status='passed', elapsed_seconds=1.0,
                    log=str(log), reason=None)

    def test_short_run_passes_only_after_required_duration_and_full_mixed_cycle(self):
        report = run(self.config(duration=4), runner=self.success, context=CONTEXT)
        evidence = report['evidence']
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(evidence['completed_cycles'], 2)
        self.assertEqual(evidence['duration_seconds'], 6)
        self.assertEqual(evidence['required_duration_seconds'], 4)
        self.assertEqual(set(evidence['coverage']), required_checks(self.config()))
        self.assertFalse(evidence['real_bluetooth'])
        self.assertFalse(evidence['physical_input'])
        self.assertFalse(evidence['bidirectional'])
        self.assertEqual(report['evidence_scope'], 'model')
        self.assertEqual(len(self.output.with_suffix('.events.jsonl').read_text().splitlines()), 6)
        self.assertTrue(self.output.with_suffix('.md').is_file())

    def test_cycle_limit_cannot_turn_fifteen_seconds_into_two_hours(self):
        report = run(self.config(cycles=1), runner=self.success, context=CONTEXT)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['evidence']['required_duration_seconds'], 7200)
        self.assertEqual(report['evidence']['duration_seconds'], 3)

    def test_subprocess_failure_cannot_become_passed_after_duration_reached(self):
        calls = []
        def runner(step, log, timeout, stop):
            calls.append(step.check_id)
            result = self.success(step, log, timeout, stop)
            if len(calls) == 2:
                result.update(status='failed', reason='Injected regression')
            return result
        report = run(self.config(duration=.1), runner=runner, context=CONTEXT)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['reason'], 'Injected regression')
        self.assertEqual(report['evidence']['completed_cycles'], 0)
        self.assertEqual(len(calls), 2)

    def test_ctrl_c_retains_completed_steps_but_never_passes(self):
        calls = []
        def runner(step, log, timeout, stop):
            calls.append(step.check_id)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return self.success(step, log, timeout, stop)
        report = run(self.config(duration=3), runner=runner, context=CONTEXT)
        self.assertEqual(report['status'], 'interrupted')
        saved = json.loads(self.output.with_suffix('.checkpoint.json').read_text())
        self.assertEqual(saved['successful_seconds'], 1)
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(saved['active_step'], 'mixed_model')

    def test_stop_after_last_child_is_interrupted_even_when_duration_is_met(self):
        stop = threading.Event()
        def runner(step, log, timeout, token):
            if step.check_id == 'handoff_right':
                token.set()
            return self.success(step, log, timeout, token)
        report = run(self.config(duration=1), runner=runner, stop=stop, context=CONTEXT)
        self.assertEqual(report['status'], 'interrupted')

    def test_checkpoint_and_report_are_incomplete_while_child_runs(self):
        captured = []
        def runner(step, log, timeout, stop):
            saved = json.loads(self.output.read_text())
            checkpoint = json.loads(self.output.with_suffix('.checkpoint.json').read_text())
            captured.append((saved['status'], saved['completed_at'], checkpoint['active_step']))
            return self.success(step, log, timeout, stop)
        run(self.config(duration=1), runner=runner, context=CONTEXT)
        self.assertTrue(all(status == 'not_run' and completed is None for status, completed, _ in captured))
        self.assertEqual(captured[0][2], 'handoff_left')

    def test_resume_counts_successful_work_only_and_discloses_noncontinuous_run(self):
        stop = threading.Event()
        def runner(step, log, timeout, token):
            token.set()
            return dict(self.success(step, log, timeout, token), status='interrupted', elapsed_seconds=999)
        first = run(self.config(duration=4), runner=runner, stop=stop, context=CONTEXT)
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(first['evidence']['duration_seconds'], 0)
        resumed = run(self.config(duration=4, resume=True), runner=self.success, context=CONTEXT)
        self.assertEqual(resumed['status'], 'passed')
        self.assertEqual(resumed['evidence']['duration_seconds'], 6)
        self.assertFalse(resumed['evidence']['continuous_run'])
        self.assertEqual(resumed['evidence']['resume_count'], 1)

    def test_resume_rejects_changed_source_and_keeps_original_artifacts(self):
        stop = threading.Event(); stop.set()
        run(self.config(), stop=stop, context=CONTEXT)
        original = self.output.read_bytes()
        changed = dict(CONTEXT, source_fingerprint=dict(digest='different-source'))
        with patch('tools.soak_stability.run_child') as child:
            report = run(self.config(resume=True), context=changed)
        self.assertEqual(report['status'], 'not_run')
        self.assertIn('differs', report['reason'])
        self.assertEqual(self.output.read_bytes(), original)
        child.assert_not_called()

    def test_failed_run_cannot_resume_into_a_clean_pass(self):
        def failure(step, log, timeout, stop):
            return dict(self.success(step, log, timeout, stop), status='failed', reason='Regression')
        run(self.config(duration=1), runner=failure, context=CONTEXT)
        report = run(self.config(duration=1, resume=True), runner=self.success, context=CONTEXT)
        self.assertEqual(report['status'], 'not_run')
        self.assertIn('failed or completed', report['reason'])

    def test_source_changed_during_run_cannot_pass_with_stale_fingerprint(self):
        changed = dict(CONTEXT, source_fingerprint=dict(algorithm='sha256', digest='new-source'))
        with patch('tools.soak_stability.source_context', side_effect=[CONTEXT, changed]):
            report = run(self.config(duration=1), runner=self.success)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Source changed', report['reason'])

    def test_native_without_explicit_permission_does_not_start_children(self):
        def forbidden(*_):
            self.fail('Native child was started without explicit permission')
        report = run(self.config(scope='mac-native'), runner=forbidden, context=CONTEXT)
        self.assertEqual(report['status'], 'not_run')
        self.assertIn('--allow-native', report['reason'])

    def test_default_plan_never_starts_native_ui_or_taps(self):
        steps = cycle_steps(self.config(), 1, self.directory)
        commands = ' '.join(' '.join(step.command) for step in steps)
        self.assertNotIn('--native', commands)
        self.assertNotIn('--capture', commands)
        self.assertNotIn('probe_mac_keyboard.py', commands)
        self.assertNotIn('probe_mac_residency.py', commands)

    def test_native_requires_both_keyboard_sizes_but_ui_remains_opt_in(self):
        config = self.config(scope='mac-native', allow_native=True)
        steps = cycle_steps(config, 1, self.directory) + cycle_steps(config, 2, self.directory)
        self.assertEqual({step.pairs for step in steps if step.pairs}, {64, 512})
        self.assertTrue(all('probe_mac_residency.py' not in ' '.join(step.command) for step in steps))
        self.assertIn('native_keyboard_512', required_checks(config))

    def test_model_cannot_enable_foreground_capture(self):
        report = run(self.config(include_window_capture=True), runner=self.success, context=CONTEXT)
        self.assertEqual(report['status'], 'not_run')

    def test_skipped_required_child_stops_run_as_not_run(self):
        def skipped(step, log, timeout, stop):
            return dict(self.success(step, log, timeout, stop), status='not_run', reason='Missing permission')
        report = run(self.config(duration=1), runner=skipped, context=CONTEXT)
        self.assertEqual(report['status'], 'not_run')
        self.assertEqual(report['evidence']['duration_seconds'], 0)

    def test_invalid_duration_is_not_run(self):
        for duration in (float('nan'), float('inf'), 0, -1):
            with self.subTest(duration=duration):
                report = run(Config(self.directory / str(duration) / 'report.json', duration=duration), context=CONTEXT)
                self.assertEqual(report['status'], 'not_run')


class ChildProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / 'child.log'

    def child(self, script, **kwargs):
        return run_child(Step('test', [sys.executable, '-c', script], **kwargs),
                         self.log, .3, threading.Event())

    def test_success_exit_without_expected_evidence_is_failure(self):
        result = self.child('pass', artifact=self.directory / 'missing.json', mode='self-test')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('missing', result['reason'])

    def test_explicit_skipped_probe_is_not_success_even_with_zero_exit(self):
        artifact = self.directory / 'probe.json'
        artifact.write_text(json.dumps(dict(mode='native-keyboard', skipped='Input Monitoring is missing')))
        result = self.child('pass', artifact=artifact, mode='native-keyboard')
        self.assertEqual(result['status'], 'not_run')

    def test_probe_errors_override_zero_exit(self):
        artifact = self.directory / 'probe.json'
        artifact.write_text(json.dumps(dict(mode='self-test', errors=['Receiver worker failed'])))
        result = self.child('pass', artifact=artifact, mode='self-test')
        self.assertEqual(result['status'], 'failed')

    def test_independent_timeout_stops_hung_process(self):
        started = time.monotonic()
        result = self.child('import time; time.sleep(20)')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('timeout', result['reason'])
        self.assertLess(time.monotonic() - started, 3)

    def test_stop_request_terminates_running_child(self):
        stop = threading.Event()
        timer = threading.Timer(.1, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = run_child(Step('test', [sys.executable, '-c', 'import time; time.sleep(20)']),
                           self.log, 10, stop)
        self.assertEqual(result['status'], 'interrupted')

    def test_empty_or_skipped_unittest_batch_cannot_pass(self):
        for output in ('Ran 0 tests\nOK', 'Ran 8 tests\nOK (skipped=1)'):
            with self.subTest(output=output):
                result = self.child('print(' + repr(output) + ')')
                self.assertNotEqual(result['status'], 'passed')

    def test_signal_handlers_are_restored(self):
        original = signal.getsignal(signal.SIGTERM)
        stop = threading.Event()
        with signal_stop(stop):
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)


if __name__ == '__main__':
    unittest.main()
