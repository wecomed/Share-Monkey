"""Release evidence must fail closed; these tests never execute native APIs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import validation_common as common
from tools import verify_release as gate


class EvidenceContractTests(unittest.TestCase):
    def test_fingerprint_changes_only_for_tracked_sources(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'sharemouse').mkdir()
            (root / 'sharemouse/__init__.py').write_text("__version__ = '0.5.6'\n")
            (root / 'tools').mkdir()
            (root / 'tools/harness.py').write_text('VALUE = 1\n')
            first = common.source_context(root)
            (root / 'report.json').write_text('{"status":"passed"}')
            self.assertEqual(first['source_fingerprint'], common.source_context(root)['source_fingerprint'])
            (root / 'tools/harness.py').write_text('VALUE = 2\n')
            self.assertNotEqual(first['source_fingerprint']['digest'], common.source_context(root)['source_fingerprint']['digest'])
            self.assertEqual(first['runtime']['kind'], 'source')

    def test_report_writes_json_and_readable_sibling(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'report.json'
            report = common.make_report('example', 'not_run', context={}, reason='No paired device')
            common.write_report(report, path)
            self.assertEqual(json.loads(path.read_text())['status'], 'not_run')
            self.assertIn('No paired device', path.with_suffix('.md').read_text())

    def test_unknown_or_running_status_is_not_final(self):
        for status in ('running', 'skipped', '', None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                common.make_report('x', status, context={})

    def test_skip_cannot_hide_a_later_failure(self):
        result = common.evidence_problem({'checks': [{'status': 'not_run'}, {'errors': ['bad']}]})
        self.assertEqual(result[0], 'failed')

    def test_nested_errors_skips_empty_phases_and_workers_are_not_success(self):
        for marker, expected in (({'errors': ['native error']}, 'failed'),
                                 ({'skipped': 'missing permission'}, 'not_run'),
                                 ({'phases': []}, 'not_run'),
                                 ({'workers_alive': ['stuck']}, 'failed'),
                                 ({'driver_alive': True}, 'failed'),
                                 ({'phases': [{'passed': False}]}, 'failed'),
                                 ({'status': 'interrupted'}, 'interrupted')):
            with self.subTest(marker=marker):
                self.assertEqual(common.evidence_problem({'child': marker})[0], expected)


class GateImportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
        self.context = dict(harness_version='0.5.6', tested_version='0.5.6', platform='darwin',
                            runtime=dict(kind='source'), windows_target_version='0.5.4',
                            source_fingerprint=dict(algorithm='sha256', digest='a' * 64,
                                                    files=[dict(path='sharemouse/input.py', sha256='b' * 64)]))

    def report(self, check_id='paired'):
        report = common.make_report(check_id, 'passed', context=deepcopy(self.context), evidence_scope='real_peer')
        report.update(started_at=(self.now - timedelta(hours=2)).isoformat(), completed_at=self.now.isoformat())
        report['evidence'] = dict(transport='bluetooth', real_bluetooth=True, bidirectional=True,
                                  duration_seconds=7200, mixed_input=True,
                                  coverage={name: 'passed' for name in gate.PAIR_SCENARIOS},
                                  peers={name: dict(tested_version=version,
                                                   runtime=dict(kind='installed', version_verified=True))
                                         for name, version in (('mac', '0.5.6'), ('windows', '0.5.4'))})
        return report

    def windows_evidence(self):
        return dict(phases=[dict(name=f'cycle-{cycle}-{name}', event=event, received=True,
                                 **({'requested': [100, 200], 'readback': [100, 200]} if event == 'move' else {}))
                            for cycle in (1, 2) for name, event in gate.WINDOWS_PHASES.items()],
                    errors=[], completed_cycles=2, foreground_guard_checks=32,
                    coverage=dict(own_window_input=True, physical_coordinates=True),
                    executed_on_windows=True, native_actions=True, application_binary_verified=False)

    def validate(self, report, check_id='paired', **kwargs):
        return gate.validate_import(report, check_id, self.context, now=self.now, **kwargs)

    def test_complete_real_peer_contract_is_accepted(self):
        self.assertEqual(self.validate(self.report())[0], 'passed')

    def test_missing_schema_identity_or_evidence_is_incomplete(self):
        for name in ('schema_version', 'check_id', 'context', 'completed_at', 'started_at', 'evidence'):
            report = self.report()
            report.pop(name)
            with self.subTest(name=name):
                self.assertNotEqual(self.validate(report)[0], 'passed')

    def test_malformed_external_structures_do_not_crash_or_pass(self):
        for name, value in (('context', None), ('context', []), ('evidence', [])):
            report = self.report()
            report[name] = value
            with self.subTest(name=name, value=value):
                self.assertNotEqual(self.validate(report)[0], 'passed')
        for name in ('coverage', 'peers'):
            report = self.report()
            report['evidence'][name] = None
            with self.subTest(name=name):
                self.assertNotEqual(self.validate(report)[0], 'passed')

    def test_stale_future_invalid_or_reversed_timestamps_are_incomplete(self):
        for value in ((self.now - timedelta(hours=25)).isoformat(),
                      (self.now + timedelta(hours=1)).isoformat(), '2026-10-16', 'invalid'):
            report = self.report()
            report['completed_at'] = value
            with self.subTest(value=value):
                self.assertEqual(self.validate(report)[0], 'not_run')

    def test_modified_source_or_harness_version_is_incomplete(self):
        for name in ('source_fingerprint', 'harness_version'):
            report = self.report()
            report['context'][name] = {} if name == 'source_fingerprint' else '0.5.5'
            with self.subTest(name=name):
                self.assertEqual(self.validate(report)[0], 'not_run')
        report = self.report()
        report['context']['source_fingerprint']['files'] = [{'path': 'wrong.py', 'sha256': 'not-a-hash'}]
        self.assertEqual(self.validate(report)[0], 'not_run')

    def test_model_or_observational_reports_never_prove_real_peer(self):
        for scope in ('model', 'native_synthetic', 'observational', 'mac-native'):
            report = self.report()
            report['evidence_scope'] = scope
            with self.subTest(scope=scope):
                self.assertEqual(self.validate(report)[0], 'not_run')

    def test_each_required_actual_scenario_is_mandatory(self):
        for scenario in gate.PAIR_SCENARIOS:
            report = self.report()
            del report['evidence']['coverage'][scenario]
            with self.subTest(scenario=scenario):
                self.assertEqual(self.validate(report)[0], 'not_run')

    def test_no_ble_no_direction_or_peer_version_never_passes(self):
        for key in ('real_bluetooth', 'bidirectional'):
            report = self.report()
            report['evidence'][key] = False
            with self.subTest(key=key):
                self.assertEqual(self.validate(report)[0], 'not_run')
        report = self.report()
        report['evidence']['peers']['windows']['tested_version'] = '0.5.6'
        self.assertEqual(self.validate(report)[0], 'not_run')

    def test_raw_skipped_nested_probe_cannot_be_laundered_by_passed_parent(self):
        report = self.report()
        report['evidence']['child'] = {'skipped': 'not on Windows'}
        self.assertEqual(self.validate(report)[0], 'not_run')
        report['evidence']['child'] = {'errors': ['native error']}
        self.assertEqual(self.validate(report)[0], 'failed')

    def test_two_hour_soak_requires_real_wall_time_and_mixed_input(self):
        report = self.report('soak')
        self.assertEqual(self.validate(report, 'soak')[0], 'passed')
        for duration in (15, 7199, float('nan'), float('inf'), True, '7200'):
            short = deepcopy(report)
            short['evidence']['duration_seconds'] = duration
            with self.subTest(duration=duration):
                self.assertEqual(self.validate(short, 'soak')[0], 'not_run')
        report['started_at'] = (self.now - timedelta(seconds=15)).isoformat()
        self.assertEqual(self.validate(report, 'soak')[0], 'not_run')

    def test_borrowing_windows_four_dependencies_does_not_test_four_binary(self):
        report = self.report('windows-native')
        report['evidence_scope'] = 'native_synthetic'
        report['context']['platform'] = 'win32'
        report['evidence'] = self.windows_evidence()
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.4')[0], 'not_run')
        report['context']['tested_version'] = '0.5.4'
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.4')[0], 'not_run')
        report['context']['runtime'] = {'kind': 'installed', 'version_verified': True}
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.4')[0], 'not_run')
        report['evidence']['adapter_provenance_verified'] = True
        report['evidence']['tested_runtime'] = {'adapter_module': 'pkgs/sharemouse/input.py',
                                               'pointer_module': 'pkgs/sharemouse/windows_pointer.py',
                                               'source_files': {'input.py': 'sha256'}}
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.4')[0], 'passed')

    def test_windows_string_phases_cannot_claim_native_execution(self):
        report = self.report('windows-native')
        report['evidence_scope'] = 'native_synthetic'
        report['context']['platform'] = 'win32'
        report['evidence'] = {'phases': ['send-input'], 'errors': []}
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.6')[0], 'not_run')

    def test_windows_incomplete_or_duplicate_phases_never_pass(self):
        report = self.report('windows-native')
        report['evidence_scope'] = 'native_synthetic'
        report['context']['platform'] = 'win32'
        report['evidence'] = self.windows_evidence()
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.6')[0], 'passed')
        report['evidence']['phases'][-1] = deepcopy(report['evidence']['phases'][0])
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.6')[0], 'not_run')

    def test_nested_windows_report_must_be_fresh_and_match_its_parent(self):
        child = self.report('windows-native')
        child['evidence_scope'] = 'native_synthetic'
        child['context']['platform'] = 'win32'
        child['evidence'] = self.windows_evidence()
        report = deepcopy(child)
        report['evidence'] = {'checks': [{'check_id': 'windows-native', 'status': 'passed', 'evidence': child}]}
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.6')[0], 'passed')
        child['completed_at'] = (self.now - timedelta(hours=25)).isoformat()
        self.assertEqual(self.validate(report, 'windows-native', expected_version='0.5.6')[0], 'not_run')

    def test_mac_native_requires_real_nested_probe_payloads(self):
        report = self.report('mac-native')
        report['evidence_scope'] = 'native_synthetic'
        names = ['mac-keyboard'] + ['mac-' + s for s in
                ('hidden-entry', 'close-active', 'reopen-active', 'focus-loss-active', 'anchor-drift')]
        report['evidence'] = {'checks': [dict(check_id=name, status='passed') for name in names]}
        self.assertEqual(self.validate(report, 'mac-native', expected_version='0.5.6')[0], 'not_run')

    def test_missing_external_report_has_its_own_log(self):
        with tempfile.TemporaryDirectory() as name:
            check = gate.import_check('windows-native', None, name, self.context)
            self.assertEqual(check['status'], 'not_run')
            self.assertTrue(Path(check['log']).is_file())


class GateExecutionTests(unittest.TestCase):
    def run_probe(self, body, *, kind='generic', timeout=5, exit_status=0):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'test.json'
            script = ('import json,pathlib,time,sys\n'
                      + body.replace('OUTPUT', repr(str(output)))
                      + '\nsys.exit(' + str(exit_status) + ')\n')
            result = gate.run_check('test', [sys.executable, '-c', script], name,
                                    timeout=timeout, kind=kind)
            self.assertTrue(Path(result['log']).is_file())
            return result

    def test_zero_exit_without_output_is_incomplete(self):
        self.assertEqual(self.run_probe('pass')['status'], 'not_run')

    def test_previous_output_cannot_make_zero_exit_pass(self):
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / 'test.json').write_text('{"errors":[]}')
            result = gate.run_check('test', [sys.executable, '-c', 'pass'], name, timeout=5, kind='generic')
            self.assertEqual(result['status'], 'not_run')

    def test_zero_exit_errors_skips_empty_phases_or_empty_object_never_pass(self):
        for evidence, expected in (({'errors': ['bad']}, 'failed'), ({'skipped': 'permissions'}, 'not_run'),
                                   ({'phases': []}, 'not_run'), ({}, 'not_run')):
            with self.subTest(evidence=evidence):
                body = 'pathlib.Path(OUTPUT).write_text(' + repr(json.dumps(evidence)) + ')'
                self.assertEqual(self.run_probe(body)['status'], expected)

    def test_nonzero_exit_overrides_success_looking_json(self):
        result = self.run_probe('pathlib.Path(OUTPUT).write_text(\'{"errors":[]}\')', exit_status=1)
        self.assertEqual(result['status'], 'failed')

    def test_invalid_json_is_failure(self):
        self.assertEqual(self.run_probe('pathlib.Path(OUTPUT).write_text("truncated")')['status'], 'failed')

    def test_timeout_is_failure_and_child_is_reaped(self):
        result = self.run_probe('time.sleep(30)', timeout=.1)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['timed_out'])

    def test_child_signal_is_interruption(self):
        result = self.run_probe('import os,signal\nos.kill(os.getpid(),signal.SIGTERM)')
        self.assertEqual(result['status'], 'interrupted')

    def test_partial_cycle_or_empty_keyboard_phases_is_incomplete(self):
        self.assertEqual(gate.inspect_probe({'mode': 'self-test', 'native_apis_mocked': True,
                                             'cycles_completed': 1, 'cycles': [{}]}, 'handoff-model', cycles=4)[0], 'not_run')
        self.assertEqual(gate.inspect_probe({'mode': 'native-keyboard', 'native_objects': True,
                                             'pairs_requested': 512, 'phases': []}, 'keyboard')[0], 'not_run')

    def test_non_integer_test_count_is_never_accepted(self):
        for count in (float('nan'), float('inf'), True, '12', 0, -1):
            with self.subTest(count=count):
                self.assertEqual(gate.inspect_probe({'tests_run': count}, 'unittest')[0], 'not_run')

    def test_all_pass_only_exit_zero(self):
        for checks, status, code in (([{'status': 'passed'}], 'passed', 0),
                                     ([{'status': 'passed'}, {'status': 'not_run'}], 'not_run', 2),
                                     ([{'status': 'failed'}, {'status': 'not_run'}], 'failed', 1),
                                     ([{'status': 'interrupted'}], 'interrupted', 1), ([], 'not_run', 2)):
            with self.subTest(checks=checks):
                self.assertEqual(gate.aggregate_status(checks), status)
                self.assertEqual(gate.exit_code(status), code)

    def args(self, name, profile):
        return SimpleNamespace(profile=profile, output_dir=Path(name), allow_native=False,
                               windows_version='0.5.4', mac_report=None, windows_report=None,
                               paired_report=None, soak_report=None)

    def test_native_default_has_no_subprocess_and_keeps_all_not_run_checks(self):
        with tempfile.TemporaryDirectory() as name, patch.object(gate, 'run_check') as runner:
            self.assertEqual(gate.run_profile(self.args(name, 'mac-native')), 2)
            runner.assert_not_called()
            report = json.loads((Path(name) / 'report.json').read_text())
            self.assertEqual(len(report['evidence']['checks']), 6)
            self.assertTrue(all(c['status'] == 'not_run' for c in report['evidence']['checks']))
            self.assertTrue((Path(name) / 'REPORT.md').is_file())

    def test_release_without_windows_paired_or_soak_never_passes(self):
        with tempfile.TemporaryDirectory() as name, patch.object(gate, 'run_check', return_value={'status': 'passed'}):
            self.assertEqual(gate.run_profile(self.args(name, 'release')), 2)
            report = json.loads((Path(name) / 'report.json').read_text())
            missing = [c for c in report['evidence']['checks'] if c.get('check_id') in
                       ('mac-native', 'windows-native', 'paired', 'soak')]
            self.assertEqual(len(missing), 4)
            self.assertTrue(all(c['status'] == 'not_run' for c in missing))

    def test_interrupted_child_stops_remaining_actions_without_silently_passing(self):
        with tempfile.TemporaryDirectory() as name, patch.object(gate, 'run_check',
                return_value={'check_id': 'unittest', 'status': 'interrupted'}) as runner:
            self.assertEqual(gate.run_profile(self.args(name, 'quick')), 1)
            self.assertEqual(runner.call_count, 1)
            report = json.loads((Path(name) / 'report.json').read_text())
            self.assertEqual([c['status'] for c in report['evidence']['checks']], ['interrupted', 'not_run'])

    def test_previous_success_is_invalidated_before_any_child_starts(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'report.json'
            output.write_text(json.dumps({'status': 'passed', 'completed_at': common.utc_now()}))
            observed = []
            def inspect_running(*args, **kwargs):
                report = json.loads(output.read_text())
                observed.append(report)
                self.assertEqual(report['status'], 'not_run')
                self.assertIsNone(report['completed_at'])
                self.assertTrue(report['evidence']['running'])
                self.assertIn('Running', (Path(name) / 'REPORT.md').read_text())
                return {'check_id': args[0], 'status': 'passed'}
            with patch.object(gate, 'run_check', side_effect=inspect_running):
                self.assertEqual(gate.run_profile(self.args(name, 'quick')), 0)
            self.assertEqual(len(observed), 2)
            self.assertEqual(json.loads(output.read_text())['status'], 'passed')

    def test_unhandled_failure_cannot_leave_previous_success_on_disk(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'report.json'
            output.write_text(json.dumps({'status': 'passed', 'completed_at': common.utc_now()}))
            with patch.object(gate, 'run_check', side_effect=RuntimeError('unexpected probe driver failure')):
                with self.assertRaises(RuntimeError):
                    gate.run_profile(self.args(name, 'quick'))
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'not_run')
            self.assertIsNone(report['completed_at'])


if __name__ == '__main__':
    unittest.main()
