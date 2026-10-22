import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import collect_paired_evidence as collector


def session(*, sent=4, received=4, timestamp='2026-10-16T10:00:00+00:00', ended=False):
    return dict(file='connection-20261016-173213-12345-50f8bf73.jsonl', pid=12345,
                header=dict(version='0.5.6', platform='darwin', role='controller',
                            transport='bluetooth', timestamp_utc='2026-10-16T09:00:00+00:00'),
                counters=dict(sent_frames=sent, received_frames=received),
                latest_timestamp=timestamp, ended=ended, telemetry_full=False)


def progressed():
    return session(sent=8, received=9, timestamp='2026-10-16T10:00:10+00:00')


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / session()['file']
        self.header = dict(kind='session_start', schema_version=1, **session()['header'])
        self.interval = dict(kind='interval', timestamp_utc=session()['latest_timestamp'],
                             counters=session()['counters'], file_limit_reached=False)
        self.path.write_text(json.dumps(self.header) + '\n' + json.dumps(self.interval) + '\n')

    def test_bounded_known_aggregates_only(self):
        self.interval['unexpected_user_text'] = 'must not appear in evidence'
        self.interval['counters']['unexpected_counter'] = 40
        self.path.write_text(json.dumps(self.header) + '\n' + json.dumps(self.interval) + '\n')
        result = collector.read_session(self.path)
        self.assertEqual(result['pid'], 12345)
        self.assertEqual(result['counters']['sent_frames'], 4)
        self.assertNotIn('unexpected', json.dumps(result))
        self.assertNotIn('must not appear', json.dumps(result))

    def test_partial_live_tail_is_not_parsed(self):
        with self.path.open('a') as handle:
            handle.write('{"kind":"interval",')
        self.assertEqual(collector.read_session(self.path)['complete_records'], 2)

    def test_complete_invalid_line_fails(self):
        with self.path.open('a') as handle:
            handle.write('not-json\n')
        with self.assertRaises(ValueError):
            collector.read_session(self.path)

    def test_summary_marks_session_ended(self):
        with self.path.open('a') as handle:
            handle.write(json.dumps(dict(self.interval, kind='summary')) + '\n')
        self.assertTrue(collector.read_session(self.path)['ended'])

    def test_symlink_is_rejected(self):
        link = self.path.with_name('connection-20261016-173213-12345-aaaaaaaa.jsonl')
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            collector.read_session(link)

    def test_oversized_log_is_rejected_before_read(self):
        with self.path.open('wb') as handle:
            handle.truncate(1100 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'size bound'):
            collector.read_session(self.path)

    def test_bool_negative_and_large_counters_are_rejected(self):
        for count in (True, -1, 2**64):
            with self.subTest(count=count):
                self.interval['counters']['sent_frames'] = count
                self.path.write_text(json.dumps(self.header) + '\n' + json.dumps(self.interval) + '\n')
                with self.assertRaises(ValueError):
                    collector.read_session(self.path)

    def test_select_requires_alive_known_client_and_matching_version(self):
        inspector = SimpleNamespace(inspect=mock.Mock(return_value={'metadata_version': '0.5.5'}))
        with mock.patch.object(collector.sys, 'platform', 'darwin'):
            self.assertIsNone(collector.select_session(self.path.parent, inspector))
            inspector.inspect.return_value = {'metadata_version': '0.5.6', 'alive': True}
            self.assertEqual(collector.select_session(self.path.parent, inspector)[1]['pid'], 12345)
            inspector.inspect.return_value = None
            self.assertIsNone(collector.select_session(self.path.parent, inspector))


class ObservationTests(unittest.TestCase):
    def evaluate(self, before=None, after=None, samples=None, duration=10):
        return collector.evaluate_observation(
            before or session(), after or progressed(),
            samples if samples is not None else [dict(alive=True, main_window_visible=False)], duration)

    def test_pass_covers_only_current_observation_delta(self):
        status, reason, delta = self.evaluate()
        self.assertEqual(status, 'passed')
        self.assertEqual(delta['sent_frames'], 4)
        self.assertEqual(delta['received_frames'], 5)
        self.assertEqual(delta['enter_sent'], 0)

    def test_started_session_alone_does_not_prove_connection(self):
        status, _, _ = self.evaluate(before=session(sent=0, received=0),
                                     after=session(sent=0, received=0, timestamp=progressed()['latest_timestamp']))
        self.assertEqual(status, 'not_run')

    def test_one_way_progress_is_insufficient(self):
        after = progressed()
        after['counters']['received_frames'] = 4
        self.assertEqual(self.evaluate(after=after)[0], 'not_run')

    def test_ended_session_or_dead_process_fails(self):
        self.assertEqual(self.evaluate(after=dict(progressed(), ended=True))[0], 'failed')
        self.assertEqual(self.evaluate(samples=[{'alive': True}, {'alive': False}])[0], 'failed')

    def test_different_session_and_backwards_counter_fail(self):
        self.assertEqual(self.evaluate(after=dict(progressed(), pid=54321))[0], 'failed')
        after = progressed()
        after['counters']['sent_frames'] = 3
        self.assertEqual(self.evaluate(after=after)[0], 'failed')

    def test_short_empty_stale_and_full_observations_do_not_pass(self):
        for kwargs in (dict(duration=2), dict(samples=[]), dict(after=session()),
                       dict(after=dict(progressed(), telemetry_full=True))):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.evaluate(**kwargs)[0], 'not_run')

    def test_unsupported_platform_never_initializes_native_reader(self):
        args = SimpleNamespace(duration=3, run_id=None, installed_root=None, pid=None)
        with mock.patch.object(collector.sys, 'platform', 'linux'), mock.patch.object(collector, 'MacClient') as mac:
            result = collector.observe(args)
        self.assertEqual(result['status'], 'not_run')
        self.assertEqual(result['evidence_scope'], 'observational')
        mac.assert_not_called()

    def test_overlapping_complementary_hosts_cannot_satisfy_paired_release(self):
        reports = []
        for platform, role in (('darwin', 'controller'), ('win32', 'receiver')):
            reports.append(dict(schema_version=1, check_id='peer-observation', status='passed',
                context={'platform': platform}, evidence_scope='observational',
                started_at='2026-10-16T10:00:00Z', completed_at='2026-10-16T10:00:10Z',
                evidence={'run_id': 'same-label', 'before': {'header': {'role': role}}}))
        result = collector.combine_reports(reports)
        self.assertEqual(result['status'], 'not_run')
        self.assertEqual(result['check_id'], 'paired')
        self.assertEqual(result['evidence_scope'], 'observational')
        self.assertFalse(result['evidence']['authenticated_peer_identity_verified'])
        self.assertEqual(result['evidence']['overlap_seconds'], 10)
        self.assertTrue(all(value == 'not_run' for value in result['evidence']['coverage'].values()))

    def test_missing_duplicate_or_invalid_host_report_remains_not_run(self):
        result = collector.combine_reports([{}, {}])
        self.assertEqual(result['status'], 'not_run')
        self.assertTrue(result['evidence']['diagnostic_problems'])


if __name__ == '__main__':
    unittest.main()
