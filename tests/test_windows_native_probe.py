"""Model-only safety regressions; these never prove native Windows execution."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import Mock

from tools.probe_windows_native import (OwnWindowGuard, ProbeAborted, Step, parser,
                                        perform_step, steps_for_cycle)
from tools.build_validation_bundle import BOOTSTRAP, WINDOWS_CMD, build


class OwnWindowGuardTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.is_window.return_value = True
        self.api.foreground.return_value = 77
        self.api.modifiers_down.return_value = False
        self.api.pointer_is_ours.return_value = True
        self.guard = OwnWindowGuard(self.api, 77)
        self.adapter = Mock()
        self.pointer = Mock()

    def perform(self, operation, argument=None):
        return perform_step(Step('fixed', operation, argument), guard=self.guard,
                            adapter=self.adapter, pointer=self.pointer,
                            position=(120, 130), button='left')

    def test_import_is_inert_even_on_mac(self):
        subprocess.run([sys.executable, '-c',
                        'import sys; import tools.probe_windows_native; '
                        'assert "pynput" not in sys.modules; '
                        'assert "PySide6" not in sys.modules; '
                        'assert "sharemouse.input" not in sys.modules'], check=True)

    def test_default_cli_records_not_run_instead_of_a_native_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            result = subprocess.run([sys.executable, 'tools/probe_windows_native.py',
                                     '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['check_id'], 'windows-native')
            self.assertEqual(report['status'], 'not_run')
            self.assertFalse(report['evidence']['native_actions'])
            self.assertFalse(report['evidence']['executed_on_windows'])
            self.assertTrue(report['evidence']['unsupported'])
            self.assertTrue(output.with_suffix('.md').is_file())

    @unittest.skipIf(sys.platform == 'win32', 'This negative control is for other platforms')
    def test_explicit_native_on_non_windows_is_still_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            result = subprocess.run([sys.executable, 'tools/probe_windows_native.py', '--native',
                                     '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'not_run')
            self.assertFalse(report['evidence']['executed_on_windows'])

    def test_focus_loss_prevents_every_new_input_type(self):
        self.api.foreground.return_value = 88
        for operation in ('move', 'key_down', 'key_up', 'button_down', 'button_up', 'scroll'):
            with self.subTest(operation=operation), self.assertRaises(ProbeAborted):
                self.perform(operation, {'char': 'a'})
        self.assertFalse(self.adapter.mock_calls)
        self.assertFalse(self.pointer.mock_calls)

    def test_foreground_is_rechecked_between_press_and_release(self):
        self.perform('key_down', {'char': 'a'})
        self.api.foreground.return_value = 88
        with self.assertRaises(ProbeAborted):
            self.perform('key_up', {'char': 'a'})
        self.adapter.inject_key.assert_called_once_with({'char': 'a'}, True)

    def test_escape_closed_window_and_modifiers_prevent_injection(self):
        for condition in ('escape', 'closed', 'modifier'):
            with self.subTest(condition=condition):
                self.api.is_window.return_value = condition != 'closed'
                self.api.modifiers_down.return_value = condition == 'modifier'
                self.guard.aborted = lambda: condition == 'escape'
                with self.assertRaises(ProbeAborted):
                    self.perform('key_down', {'char': 'a'})
        self.assertFalse(self.adapter.mock_calls)

    def test_mouse_and_scroll_require_cursor_belongs_to_own_window(self):
        self.api.pointer_is_ours.return_value = False
        for operation in ('button_down', 'button_up', 'scroll'):
            with self.subTest(operation=operation), self.assertRaises(ProbeAborted):
                self.perform(operation)
        self.assertFalse(self.adapter.mock_calls)

    def test_movement_outside_own_client_never_calls_setcursorpos(self):
        self.api.point_is_ours.return_value = False
        with self.assertRaises(ProbeAborted):
            self.perform('move')
        self.pointer.move_to.assert_not_called()

    def test_native_readback_mismatch_fails_instead_of_claiming_success(self):
        self.api.cursor_position.return_value = (119, 130)
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.perform('move')
        self.pointer.move_to.assert_called_once_with((120, 130))

    def test_successful_movement_records_fixed_coordinate_readback(self):
        self.api.cursor_position.return_value = (120, 130)
        result = self.perform('move')
        self.assertEqual(result, {'requested': [120, 130], 'readback': [120, 130]})
        self.assertEqual(self.guard.checks, 1)

    def test_fixed_stimuli_call_production_receiver_methods(self):
        self.perform('key_down', {'name': 'f8'})
        self.perform('key_up', {'name': 'f8'})
        self.perform('button_down')
        self.perform('button_up')
        self.perform('scroll')
        self.assertEqual(self.adapter.inject_key.call_args_list[0].args, ({'name': 'f8'}, True))
        self.assertEqual(self.adapter.inject_key.call_args_list[1].args, ({'name': 'f8'}, False))
        self.assertEqual(self.adapter.inject_button.call_args_list[0].args, ('left', True))
        self.assertEqual(self.adapter.inject_button.call_args_list[1].args, ('left', False))
        self.adapter.mouse.scroll.assert_called_once_with(0, 1)

    def test_fixed_cycle_contains_no_system_shortcut_or_state_toggle(self):
        steps = steps_for_cycle(0)
        self.assertEqual(len(steps), 8)
        self.assertEqual([step.argument for step in steps if step.operation == 'key_down'],
                         [{'char': 'a'}, {'name': 'f8'}])
        self.assertEqual(sum(step.operation == 'key_down' for step in steps),
                         sum(step.operation == 'key_up' for step in steps))
        self.assertEqual(sum(step.operation == 'button_down' for step in steps),
                         sum(step.operation == 'button_up' for step in steps))

    def test_target_is_explicit_and_defaults_to_source(self):
        self.assertEqual(parser().parse_args([]).target, 'source')
        args = parser().parse_args(['--target', 'installed', '--installed-root', 'fixed-runtime'])
        self.assertEqual(args.target, 'installed')
        self.assertEqual(args.installed_root, Path('fixed-runtime'))
        self.assertFalse(args.native)


class ValidationBundleTests(unittest.TestCase):
    def test_bootstrap_is_valid_python_and_uses_explicit_native_opt_in(self):
        compile(BOOTSTRAP, '_bootstrap_validation.py', 'exec')
        self.assertIn("parser.add_argument('--native', action='store_true')", BOOTSTRAP)
        self.assertIn('if args.native:', BOOTSTRAP)
        self.assertIn('Python\\python.exe', WINDOWS_CMD)
        self.assertIn('--native --installed-root', WINDOWS_CMD)

    def test_bundle_needs_collector_and_never_rebuilds_or_ships_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            for name in ('tools/probe_windows_native.py', 'tools/validation_common.py',
                         'sharemouse/input.py', 'sharemouse/windows_pointer.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# test fixture\n')
            output = Path(directory) / 'bundle.zip'
            with self.assertRaisesRegex(FileNotFoundError, 'collect_paired_evidence'):
                build(output, root)
            (root / 'tools/collect_paired_evidence.py').write_text('# fixture\n')
            result = build(output, root)
            self.assertFalse(result['product_rebuilt'])
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any(name.endswith('.exe') for name in archive.namelist()))
                self.assertIn('运行 Windows 自检.cmd', archive.namelist())
                self.assertIn('_bootstrap_validation.py', archive.namelist())
                manifest = json.loads(archive.read('bundle-manifest.json'))
                for item in manifest['files']:
                    data = archive.read(item['path'])
                    self.assertEqual(len(data), item['bytes'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
            self.assertEqual(result['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_bootstrap_retains_text_exit_and_packs_only_generated_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tools').mkdir()
            (root / 'tools/__init__.py').write_text('')
            (root / 'runtime/pkgs').mkdir(parents=True)
            (root / 'runtime/password-settings.txt').write_text('must-never-be-packed')
            (root / 'tools/collect_paired_evidence.py').write_text(
                "def main(argv):\n    raise SystemExit('fixed collector failure')\n")
            (root / 'tools/probe_windows_native.py').write_text(
                "from pathlib import Path\n"
                "def main(argv):\n"
                "    assert argv[argv.index('--target') + 1] == 'installed'\n"
                "    output = Path(argv[argv.index('--output') + 1])\n"
                "    output.write_text('{\"status\": \"not_run\"}')\n"
                "    output.with_suffix('.md').write_text('No native action')\n"
                "    return 2\n")
            bootstrap = root / '_bootstrap_validation.py'
            bootstrap.write_text(BOOTSTRAP)
            result = subprocess.run([sys.executable, str(bootstrap), '--native',
                                     '--installed-root', str(root / 'runtime')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            archives = list((root / 'reports').glob('Share-Monkey-Windows-Results-*.zip'))
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {'windows-native.json', 'windows-native.md', 'one-click-summary.json'})
                summary = json.loads(archive.read('one-click-summary.json'))
                self.assertEqual(summary['observation_exit_code'], 1)
                self.assertEqual(summary['native_exit_code'], 2)
                self.assertEqual(summary['execution_errors'][0]['error'], 'SystemExit')
                self.assertEqual(summary['execution_errors'][0]['message'], 'fixed collector failure')
            self.assertIn('Result ZIP: ', result.stdout)


if __name__ == '__main__':
    unittest.main()
