"""Focus diagnostics are bounded, read-only and preserve probe behavior."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tools.probe_mac_residency import FocusTrace


class FocusTraceTests(unittest.TestCase):
    def trace(self, **kwargs):
        return FocusTrace(100, 200, location=lambda: dict(phase='holding', cycle=2), **kwargs)

    def test_activation_intent_precedes_call_and_records_return_without_changing_it(self):
        trace = self.trace()
        def activate(options):
            self.assertEqual(trace.evidence['records'][-1]['event'], 'original_app_activate_intent')
            self.assertEqual(options, 7)
            return True
        app = SimpleNamespace(activateWithOptions_=activate)
        self.assertIs(trace.activate(app, 7, 'hidden-background-setup'), True)
        self.assertEqual([record['event'] for record in trace.evidence['records']],
                         ['original_app_activate_intent', 'original_app_activate_return'])
        self.assertEqual(trace.evidence['records'][0]['phase'], 'holding')
        self.assertEqual(trace.evidence['records'][0]['target_pid'], 200)

    def test_activation_exception_is_recorded_and_propagates(self):
        trace = self.trace()
        app = SimpleNamespace(activateWithOptions_=Mock(side_effect=RuntimeError('failure')))
        with self.assertRaises(RuntimeError):
            trace.activate(app, 7, 'setup')
        self.assertEqual(trace.evidence['records'][-1]['event'], 'original_app_activate_raised')

    def test_false_ownership_is_snapshotted_before_return_and_never_recurses(self):
        trace = self.trace()
        original = Mock(return_value=False)
        snapshot = Mock(return_value=dict(foreground_pid=100, capture_panel_key=False,
                                          capture_panel_visible=True, current_token=dict(prepared=True)))
        observed = trace.wrap_owns_foreground(original, snapshot)
        self.assertIs(observed(), False)
        original.assert_called_once_with()
        snapshot.assert_called_once_with()
        record = trace.evidence['records'][-1]
        self.assertEqual(record['event'], 'owns_foreground_false')
        self.assertEqual(record['foreground_pid'], 100)
        self.assertTrue(record['current_token']['prepared'])

    def test_successful_ownership_does_not_do_extra_native_reads(self):
        trace = self.trace()
        snapshot = Mock(side_effect=AssertionError('Should not read native state'))
        self.assertIs(trace.wrap_owns_foreground(lambda: True, snapshot)(), True)
        snapshot.assert_not_called()
        self.assertEqual(trace.evidence['records'], [])

    def test_diagnostic_failure_cannot_turn_lost_ownership_into_success(self):
        trace = self.trace()
        result = trace.wrap_owns_foreground(lambda: False, Mock(side_effect=RuntimeError('native read')))()
        self.assertIs(result, False)
        self.assertEqual(trace.evidence['diagnostic_faults'], 1)

    def test_original_ownership_exception_remains_unchanged(self):
        trace = self.trace()
        original = Mock(side_effect=RuntimeError('original failure'))
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            trace.wrap_owns_foreground(original, Mock())()
        self.assertEqual(trace.evidence['records'], [])

    def test_workspace_filters_other_processes_and_records_only_numeric_identity(self):
        trace = self.trace()
        center = Mock()
        trace.observe_workspace(center, 'activated', 'application')
        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[-1]
        for pid in (300, 100, 200):
            app = SimpleNamespace(processIdentifier=lambda value=pid: value)
            callback(SimpleNamespace(userInfo=lambda value=app: {'application': value}))
        self.assertEqual([item['activated_pid'] for item in trace.evidence['records']], [100, 200])
        self.assertTrue(all('bundle' not in item and 'title' not in item for item in trace.evidence['records']))

    def test_application_notifications_and_all_observers_are_removed_once(self):
        trace = self.trace()
        center = Mock()
        app = object()
        trace.observe_application(center, 'resigned', app, 'application_did_resign_active')
        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[-1]
        callback(object())
        self.assertEqual(trace.evidence['records'][-1]['event'], 'application_did_resign_active')
        self.assertIs(center.addObserverForName_object_queue_usingBlock_.call_args.args[1], app)
        trace.close(); trace.close()
        center.removeObserver_.assert_called_once_with(center.addObserverForName_object_queue_usingBlock_.return_value)
        self.assertEqual(trace.evidence['observers_removed'], 1)

    def test_trace_size_is_bounded_without_hiding_drop_count(self):
        trace = self.trace(limit=2)
        for _ in range(10):
            trace.record('owns_foreground_false')
        self.assertEqual(len(trace.evidence['records']), 2)
        self.assertEqual(trace.evidence['dropped_records'], 8)

    def test_observer_install_failure_is_diagnostic_only(self):
        trace = self.trace()
        center = Mock()
        center.addObserverForName_object_queue_usingBlock_.side_effect = RuntimeError('unavailable')
        trace.observe_workspace(center, 'activated', 'application')
        trace.close()
        self.assertEqual(trace.evidence['diagnostic_faults'], 1)
        center.removeObserver_.assert_not_called()


if __name__ == '__main__':
    unittest.main()
