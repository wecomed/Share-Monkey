"""Exercise the official build barrier without invoking probes or builders."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import release_client as release
from tools.validation_common import make_report, source_context, utc_now


class ReleaseClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release barrier ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(platform='macos', output_dir=self.root / 'output apps',
                                    evidence_dir=self.root / 'evidence', windows_version='0.5.4',
                                    gate_timeout=30, build_timeout=60)
        for name in ('mac_report', 'windows_report', 'paired_report', 'soak_report'):
            path = self.root / (name + '.json')
            path.write_text('{}')
            setattr(self.args, name, path)
        self.calls = []

    def gate_report(self):
        context = source_context()
        context['windows_target_version'] = self.args.windows_version
        return make_report('release', 'passed', context=context, evidence_scope='release_gate',
                           evidence={'checks': [{'check_id': name, 'status': 'passed'}
                                                for name in sorted(release.REQUIRED_CHECKS)]})

    def execute(self, argv, log, timeout):
        self.calls.append(list(argv))
        if 'verify_release.py' in argv[1]:
            (self.args.evidence_dir / 'release-gate/report.json').write_text(json.dumps(self.gate_report()))
        return {'status': 'passed', 'returncode': 0, 'command': list(argv), 'log': str(log)}

    def result(self):
        return json.loads((self.args.evidence_dir / 'release-client.json').read_text())

    def test_passed_fresh_gate_is_followed_by_structured_macos_build(self):
        with patch.object(release, '_execute', side_effect=self.execute):
            self.assertEqual(release.run_release(self.args), 0)
        self.assertEqual(len(self.calls), 2)
        gate, builder = self.calls
        self.assertEqual(gate[2:4], ['--profile', 'release'])
        self.assertNotIn('--allow-native', gate)
        self.assertTrue(builder[1].endswith('build_client.py'))
        self.assertEqual(builder[2], 'macos')
        self.assertEqual(builder[-1], str(self.args.output_dir.resolve()))
        self.assertIn(str(self.args.paired_report.resolve()), gate)
        self.assertEqual(self.result()['status'], 'passed')

    def test_windows_build_requires_and_passes_matching_source_target(self):
        self.args.platform = 'windows'
        self.args.windows_version = source_context()['harness_version']
        with patch.object(release, '_execute', side_effect=self.execute):
            self.assertEqual(release.run_release(self.args), 0)
        self.assertTrue(self.calls[1][1].endswith('build_windows_installer.py'))
        self.assertNotIn('windows', self.calls[1][2:])

    def test_windows_four_evidence_does_not_authorize_building_windows_six(self):
        self.args.platform = 'windows'
        with patch.object(release, '_execute') as execute:
            self.assertEqual(release.run_release(self.args), 2)
        execute.assert_not_called()
        self.assertIn('must match', self.result()['reason'])

    def test_missing_each_external_report_blocks_all_commands(self):
        for name in ('mac_report', 'windows_report', 'paired_report', 'soak_report'):
            value = getattr(self.args, name)
            setattr(self.args, name, None)
            with self.subTest(name=name), patch.object(release, '_execute') as execute:
                self.assertEqual(release.run_release(self.args), 2)
                execute.assert_not_called()
            setattr(self.args, name, value)

    def test_zero_exit_without_new_gate_output_does_not_reuse_old_pass(self):
        directory = self.args.evidence_dir / 'release-gate'
        directory.mkdir(parents=True)
        old = directory / 'report.json'
        old.write_text(json.dumps(self.gate_report()))
        with patch.object(release, '_execute', return_value={'status': 'passed', 'returncode': 0}) as execute:
            self.assertEqual(release.run_release(self.args), 2)
        self.assertEqual(execute.call_count, 1)
        self.assertFalse(old.exists())

    def test_gate_failure_skip_or_interruption_never_starts_build(self):
        for status, code in (('failed', 1), ('not_run', 2), ('interrupted', 1)):
            with self.subTest(status=status), patch.object(release, '_execute',
                    return_value={'status': status, 'reason': 'gate incomplete'}) as execute:
                self.assertEqual(release.run_release(self.args), code)
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(self.result()['status'], status)

    def test_status_passed_without_confirmed_zero_exit_cannot_build(self):
        for code in (1, 2, None):
            with self.subTest(code=code), patch.object(release, '_execute',
                    return_value={'status': 'passed', 'returncode': code}) as execute:
                self.assertEqual(release.run_release(self.args), 1)
                self.assertEqual(execute.call_count, 1)

    def run_mutated_gate(self, mutation):
        def execute(argv, log, timeout):
            report = self.gate_report()
            mutation(report)
            (self.args.evidence_dir / 'release-gate/report.json').write_text(json.dumps(report))
            return {'status': 'passed', 'returncode': 0}
        with patch.object(release, '_execute', side_effect=execute) as runner:
            self.assertEqual(release.run_release(self.args), 2)
            self.assertEqual(runner.call_count, 1)

    def test_zero_exit_with_failed_or_incomplete_json_cannot_build(self):
        for status in ('failed', 'not_run', 'interrupted'):
            with self.subTest(status=status):
                self.run_mutated_gate(lambda report: report.update(status=status))

    def test_errors_skips_or_missing_required_checks_cannot_build(self):
        for mutation in (lambda report: report['evidence'].update(errors=['native error']),
                         lambda report: report['evidence'].update(skipped='no peer'),
                         lambda report: report['evidence'].update(checks=[]),
                         lambda report: report['evidence']['checks'].pop()):
            with self.subTest(mutation=mutation):
                self.run_mutated_gate(mutation)

    def test_mismatched_source_version_platform_runtime_or_target_cannot_build(self):
        for key, value in (('source_fingerprint', {}), ('harness_version', 'old'),
                           ('tested_version', 'old'), ('platform', 'other'), ('runtime', {}),
                           ('windows_target_version', 'old')):
            with self.subTest(key=key):
                self.run_mutated_gate(lambda report, k=key, v=value: report['context'].update({k: v}))

    def test_old_or_unfinished_report_is_not_this_run(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for mutation in (lambda report: report.update(started_at=old),
                         lambda report: report.update(completed_at=None)):
            with self.subTest(mutation=mutation):
                self.run_mutated_gate(mutation)

    def test_invalid_gate_json_cannot_build(self):
        def execute(*args):
            (self.args.evidence_dir / 'release-gate/report.json').write_text('truncated')
            return {'status': 'passed', 'returncode': 0}
        with patch.object(release, '_execute', side_effect=execute) as runner:
            self.assertEqual(release.run_release(self.args), 2)
            self.assertEqual(runner.call_count, 1)

    def test_source_change_after_gate_blocks_packaging(self):
        context = source_context()
        changed = deepcopy(context)
        changed['source_fingerprint']['digest'] = 'changed'
        # gate fixture uses its module's common.source_context, not this patch.
        with patch.object(release, 'source_context', side_effect=[context, changed]), \
                patch.object(release, '_execute', side_effect=self.execute) as runner:
            self.assertEqual(release.run_release(self.args), 1)
            self.assertEqual(runner.call_count, 1)
        self.assertIn('Source changed', self.result()['reason'])

    def test_builder_failure_or_interruption_is_not_success(self):
        for status, code in (('failed', 1), ('interrupted', 1), ('not_run', 2)):
            def execute(argv, log, timeout):
                if 'verify_release.py' in argv[1]:
                    return self.execute(argv, log, timeout)
                return {'status': status, 'reason': 'builder did not complete'}
            with self.subTest(status=status), patch.object(release, '_execute', side_effect=execute) as runner:
                self.assertEqual(release.run_release(self.args), code)
                self.assertEqual(runner.call_count, 2)

    def test_old_parent_success_is_invalidated_before_gate(self):
        self.args.evidence_dir.mkdir()
        path = self.args.evidence_dir / 'release-client.json'
        path.write_text('{"status":"passed"}')
        def execute(*args):
            running = json.loads(path.read_text())
            self.assertEqual(running['status'], 'not_run')
            self.assertIsNone(running['completed_at'])
            raise RuntimeError('crash before gate output')
        with patch.object(release, '_execute', side_effect=execute), self.assertRaises(RuntimeError):
            release.run_release(self.args)
        self.assertEqual(json.loads(path.read_text())['status'], 'not_run')


class CommandExecutionTests(unittest.TestCase):
    def test_timeout_interrupt_and_signal_are_never_success(self):
        for effect, expected in ((subprocess.TimeoutExpired(['tool'], 1), 'failed'),
                                 (KeyboardInterrupt(), 'interrupted'), (None, 'interrupted')):
            process = Mock()
            process.wait.side_effect = effect
            if effect is None:
                process.wait.return_value = -15
            with self.subTest(effect=effect), tempfile.TemporaryDirectory() as name, \
                    patch.object(release.subprocess, 'Popen', return_value=process) as popen, \
                    patch.object(release, '_stop_process') as stop:
                result = release._execute(['tool', 'a path with spaces'], Path(name) / 'command.log', 1)
                self.assertEqual(result['status'], expected)
                self.assertEqual(popen.call_args.args[0], ['tool', 'a path with spaces'])
                self.assertNotIn('shell', popen.call_args.kwargs)
                if effect is not None:
                    stop.assert_called_once_with(process)


if __name__ == '__main__':
    unittest.main()
