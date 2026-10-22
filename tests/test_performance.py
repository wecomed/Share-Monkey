import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sharemouse import __version__
from sharemouse.performance import TelemetrySession


class PerformanceTests(unittest.TestCase):
    def session(self, **kwargs):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        session = TelemetrySession('bluetooth', True, directory=folder.name, **kwargs)
        self.addCleanup(session.close)
        return session

    def records(self, session):
        return [json.loads(line) for line in session.path.read_text().splitlines()]

    def test_close_flushes_only_aggregates_and_metadata_once(self):
        s = self.session()
        s.observe('processed_ack_ms', 10)
        s.observe('processed_ack_ms', 20)
        s.observe('sample_to_ack_ms', 25)
        s.increment('acks', 2)
        s.gauge('queue_depth', 4)
        s.gauge('queue_depth', 1)
        self.assertIs(s.start(), s)
        self.assertIs(s.start(), s)
        s.close()
        s.close()
        records = self.records(s)
        self.assertEqual([r['kind'] for r in records], ['session_start', 'summary'])
        self.assertEqual(records[0]['version'], __version__)
        self.assertEqual(records[0]['role'], 'controller')
        self.assertIn('not_one_way', records[0]['latency_definition']['excludes'])
        summary = records[-1]
        observed = summary['observations']['processed_ack_ms']
        self.assertEqual(observed['mean_ms'], 15)
        self.assertEqual(observed['count'], 2)
        self.assertEqual(summary['counters'], {'acks': 2})
        self.assertEqual(summary['gauges'], {'queue_depth': 1})
        self.assertNotIn('samples', observed)
        self.assertEqual(len(list(s.directory.glob('*.jsonl'))), 1)

    def test_sample_buckets_and_metric_names_are_bounded(self):
        s = self.session()
        for i in range(1000):
            s.observe('processed_ack_ms', i)
        for i in range(100):
            s.gauge(f'metric_{i}', i)
        self.assertEqual(len(s._observations['processed_ack_ms']), 256)
        self.assertEqual(len(s._names), s.MAX_METRICS)
        s.start().close()
        bucket = self.records(s)[-1]['observations']['processed_ack_ms']
        self.assertEqual(bucket['count'], 1000)
        self.assertEqual(bucket['sample_count'], 256)
        self.assertEqual(bucket['min_ms'], 744)
        self.assertEqual(bucket['max_ms'], 999)

    def test_invalid_values_and_arbitrary_text_are_not_recorded(self):
        s = self.session()
        for value in ('clipboard text', {'password': 'secret'}, True, math.inf, math.nan, 10**400):
            s.observe('invalid', value)
            s.gauge('invalid', value)
            s.increment('invalid', value)
        for name in ('', 'password=secret', 'clipboard contents', 'a' * 65, 123):
            s.observe(name, 1)
            s.gauge(name, 1)
            s.increment(name)
        s.observe('negative', -1)
        s.increment('negative', -1)
        s.start().close()
        final = self.records(s)[-1]
        for field in ('observations', 'gauges', 'counters'):
            self.assertEqual(final[field], {})
        self.assertNotIn('secret', s.path.read_text())
        self.assertNotIn('clipboard', s.path.read_text())

    def test_periodic_write_and_all_file_io_use_background_thread(self):
        s = self.session()
        s.INTERVAL_SECONDS = .01
        original = Path.open
        writers = []
        wrote_interval = threading.Event()
        write = s._write

        def opened(path, *args, **kwargs):
            writers.append(threading.current_thread())
            return original(path, *args, **kwargs)

        def recording(handle, record, final=False):
            write(handle, record, final)
            if record['kind'] == 'interval':
                wrote_interval.set()

        with patch.object(Path, 'open', opened), patch.object(s, '_write', recording):
            s.start()
            s.observe('processed_ack_ms', 7)
            self.assertTrue(wrote_interval.wait(2))
            s.close()
        self.assertTrue(writers)
        self.assertTrue(all(worker is s._thread for worker in writers))
        self.assertIn('interval', [r['kind'] for r in self.records(s)])
        self.assertEqual(self.records(s)[-1]['kind'], 'summary')

    def test_close_from_worker_does_not_join_itself(self):
        s = self.session()
        write = s._write

        def closing(handle, record, final=False):
            if record['kind'] == 'session_start':
                s.close()
            write(handle, record, final)

        with patch.object(s, '_write', closing):
            s.start()
            s._thread.join(2)
        self.assertFalse(s._thread.is_alive())
        self.assertFalse(s._disabled)
        self.assertEqual(self.records(s)[-1]['kind'], 'summary')

    def test_close_before_start_and_observations_after_close_do_nothing(self):
        s = self.session()
        s.close()
        s.start()
        s.observe('late', 5)
        s.gauge('late', 5)
        s.increment('late')
        self.assertFalse(s.path.exists())
        self.assertEqual(s._names, set())

    def test_permissions_or_io_errors_disable_logging_without_raising(self):
        for failure in ('mkdir', 'open'):
            with self.subTest(failure=failure):
                s = self.session()
                with patch.object(Path, failure, side_effect=PermissionError('denied')):
                    s.start().close()
                self.assertTrue(s._disabled)
                self.assertFalse(s._thread.is_alive())
                s.observe('after_failure', 3)
                self.assertEqual(s._names, set())

    def test_disk_write_failure_stays_inside_worker(self):
        s = self.session()
        with patch.object(s, '_write', side_effect=OSError('disk full')):
            s.start().close()
        self.assertTrue(s._disabled)
        self.assertFalse(s._thread.is_alive())

    def test_slow_disk_does_not_block_close_indefinitely(self):
        s = self.session()
        s.CLOSE_TIMEOUT_SECONDS = .02
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        write = s._write

        def delayed(handle, record, final=False):
            if record['kind'] == 'session_start':
                entered.set()
                resume.wait(2)
            write(handle, record, final)

        with patch.object(s, '_write', delayed):
            s.start()
            self.assertTrue(entered.wait(2))
            before = time.monotonic()
            s.close()
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(s._thread.is_alive())
            resume.set()
            s._thread.join(2)
        self.assertEqual(self.records(s)[-1]['kind'], 'summary')

    def test_size_limit_reserves_space_for_final_summary(self):
        s = self.session()
        s.MAX_FILE_BYTES = 8192
        s.INTERVAL_SECONDS = 0
        for i in range(16):
            s.observe(f'metric_{i}', i)
        reached = threading.Event()
        write = s._write

        def limited(handle, record, final=False):
            write(handle, record, final)
            if s._limit_reached:
                reached.set()
                s.close()

        with patch.object(s, '_write', limited):
            s.start()
            self.assertTrue(reached.wait(2))
            s._thread.join(2)
        self.assertLessEqual(s.path.stat().st_size, s.MAX_FILE_BYTES)
        self.assertEqual(self.records(s)[-1]['kind'], 'summary')
        self.assertTrue(self.records(s)[-1]['file_limit_reached'])

    def test_retention_only_removes_own_old_files(self):
        s = self.session()
        for i in range(15):
            p = s.directory / f'connection-20200101-000000-{i}-12345678.jsonl'
            p.write_text('{}\n')
            os.utime(p, (i + 1, i + 1))
        unrelated = s.directory / 'connection-personal-notes.jsonl'
        unrelated.write_text('keep this')
        other = s.directory / 'other.jsonl'
        other.write_text('keep this too')
        s.start().close()
        own = [p for p in s.directory.iterdir() if s._LOG_NAME.fullmatch(p.name)]
        self.assertEqual(len(own), 10)
        self.assertEqual(unrelated.read_text(), 'keep this')
        self.assertEqual(other.read_text(), 'keep this too')

    def test_concurrent_observations_do_not_lose_counters(self):
        s = self.session()

        def produce():
            for _ in range(1000):
                s.observe('processed_ack_ms', 1)
                s.increment('acks')

        workers = [threading.Thread(target=produce) for _ in range(4)]
        s.start()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        s.close()
        final = self.records(s)[-1]
        self.assertEqual(final['counters']['acks'], 4000)
        self.assertEqual(final['observations']['processed_ack_ms']['count'], 4000)

    def test_default_paths_and_unknown_transport_do_not_create_files(self):
        with patch('sharemouse.performance.sys.platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/example/home')):
            s = TelemetrySession('secret password', False)
            self.assertEqual(s.directory, Path('/example/home/Library/Application Support/Share Monkey/logs'))
            self.assertEqual(s.transport, 'unknown')
        with patch('sharemouse.performance.sys.platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': '/example/local'}):
            s = TelemetrySession('lan', False)
            self.assertEqual(s.directory, Path('/example/local/Share Monkey/logs'))
        self.assertEqual(s.role, 'receiver')


if __name__ == '__main__':
    unittest.main()
