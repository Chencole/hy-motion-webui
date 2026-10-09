"""Exercise CLI subprocess routing and check reports without loading model weights."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PAYLOAD = Path(__file__).resolve().parents[1] / 'deploy/windows-gpu/backend'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_module('gpu_cli_under_test', PAYLOAD / 'src/hy_motion_cpu/cli.py')
installation = load_module('gpu_installation_under_test', PAYLOAD / 'scripts/check_installation.py')


class InstallationReportTests(unittest.TestCase):
    def test_failure_replaces_old_success_atomically_and_preserves_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report_path = root / 'logs/installation-check.json'
            report_path.parent.mkdir()
            report_path.write_text(json.dumps({'status': 'installation_checks_passed', 'device': 'cuda:0'}))
            transitions = []
            original_replace = os.replace
            failure = RuntimeError('CUDA unavailable')

            def replace(source, destination):
                source, destination = Path(source), Path(destination)
                self.assertEqual(source.parent, destination.parent)
                transitions.append(json.loads(source.read_text(encoding='utf-8'))['status'])
                original_replace(source, destination)

            def fail(device, report):
                self.assertEqual(device, 'cuda:0')
                current = json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(current['status'], 'running')
                self.assertIsNone(current['actual_device'])
                raise failure

            with patch.object(installation, 'ROOT', root), \
                    patch.object(sys, 'argv', ['check_installation.py', '--device', 'cuda:0']), \
                    patch.object(installation, 'run_checks', side_effect=fail), \
                    patch.object(installation.os, 'replace', side_effect=replace):
                with self.assertRaises(RuntimeError) as caught:
                    installation.main()
            self.assertIs(caught.exception, failure)
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(transitions, ['running', 'failed'])
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['requested_device'], 'cuda:0')
            self.assertIsNone(report['device'])
            self.assertIsNone(report['actual_device'])
            self.assertEqual(report['error_type'], 'RuntimeError')
            self.assertEqual(report['error'], 'CUDA unavailable')
            self.assertFalse(report['inference_tested'])
            self.assertFalse(list(report_path.parent.glob('*.tmp')))

    def test_missing_repository_is_recorded_without_importing_model_dependencies(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(installation, 'ROOT', Path(folder)), \
                patch.object(sys, 'argv', ['check_installation.py', '--device', 'cuda:0']), \
                patch.dict(os.environ), patch.object(sys, 'path', list(sys.path)):
            with self.assertRaises(FileNotFoundError):
                installation.main()
            report = json.loads((Path(folder) / 'logs/installation-check.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['error_type'], 'FileNotFoundError')
            self.assertIsNone(report['actual_device'])

    def test_asset_failure_retains_only_the_device_already_verified(self):
        def fail_after_device_check(device, report):
            report.update(device=device, actual_device=device, gpu='mock GPU')
            raise FileNotFoundError('checkpoint missing')

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(installation, 'ROOT', Path(folder)), \
                patch.object(sys, 'argv', ['check_installation.py', '--device', 'cuda:0']), \
                patch.object(installation, 'run_checks', side_effect=fail_after_device_check):
            with self.assertRaises(FileNotFoundError):
                installation.main()
            report = json.loads((Path(folder) / 'logs/installation-check.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['actual_device'], 'cuda:0')
            self.assertFalse(report['inference_tested'])

    def test_success_keeps_existing_status_contract(self):
        def checked(device, report):
            report.update(device=device, actual_device=device)

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(installation, 'ROOT', Path(folder)), \
                patch.object(sys, 'argv', ['check_installation.py', '--device', 'cpu']), \
                patch.object(installation, 'run_checks', side_effect=checked), \
                contextlib.redirect_stdout(io.StringIO()):
            installation.main()
            report = json.loads((Path(folder) / 'logs/installation-check.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'installation_checks_passed')
            self.assertEqual(report['requested_device'], 'cpu')
            self.assertEqual(report['actual_device'], 'cpu')
            self.assertFalse(report['inference_tested'])


class GpuCliTests(unittest.TestCase):
    prompt = 'walk; $(echo no) & "pose"'

    def invoke(self, root, device='cuda:0', extra=()):
        argv = ['hymotion', '--prompt', self.prompt, '--device', device,
                '--seconds', '2.5', '--steps', '9', '--seed', '123', '--threads', '7',
                '--output', str(root / 'output'), *extra]
        with patch.object(cli, 'ROOT', root), patch.object(sys, 'argv', argv), \
                contextlib.redirect_stdout(io.StringIO()):
            return cli.main()

    def test_cuda_preflight_failure_returns_exact_code_and_never_encodes(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cli.subprocess, 'run', side_effect=subprocess.CalledProcessError(17, 'preflight')) as run:
            root = Path(folder)
            self.assertEqual(self.invoke(root), 17)
            run.assert_called_once_with([sys.executable, '-c', cli.CUDA_PREFLIGHT, 'cuda:0'], cwd=root, check=True)
            self.assertFalse((root / 'output').exists())
            self.assertFalse((root / 'cache').exists())

    def test_cuda_preflight_finishes_before_encoding_and_sampling(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli.subprocess, 'run') as run:
            root = Path(folder)
            self.assertEqual(self.invoke(root), 0)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], [sys.executable, '-c', cli.CUDA_PREFLIGHT, 'cuda:0'])
            self.assertEqual([command[2] for command in commands[1:]], ['encode', 'generate'])
            for command in commands[1:]:
                self.assertEqual(command[command.index('--prompt') + 1], self.prompt)
                self.assertEqual(command[command.index('--device') + 1], 'cuda:0')
                self.assertEqual(command[command.index('--threads') + 1], '7')
            generate = commands[-1]
            for key, value in (('--duration', '2.5'), ('--seed', '123'), ('--steps', '9'),
                               ('--output-dir', str(root / 'output'))):
                self.assertEqual(generate[generate.index(key) + 1], value)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs, {'cwd': root, 'check': True})

    def test_cache_hit_still_preflights_and_refresh_reencodes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / 'cache/text_features' / (hashlib.sha256(self.prompt.encode()).hexdigest() + '.pt')
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b'mocked cache; never loaded')
            for extra, stages in (((), ['generate']), (('--refresh-text',), ['encode', 'generate'])):
                with self.subTest(extra=extra), patch.object(cli.subprocess, 'run') as run:
                    self.assertEqual(self.invoke(root, extra=extra), 0)
                    self.assertEqual(run.call_args_list[0].args[0][1], '-c')
                    self.assertEqual([call.args[0][2] for call in run.call_args_list[1:]], stages)

    def test_cpu_retains_two_stages_without_cuda_preflight(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli.subprocess, 'run') as run:
            self.assertEqual(self.invoke(Path(folder), device='cpu'), 0)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual([command[2] for command in commands], ['encode', 'generate'])
            self.assertTrue(all('-c' not in command for command in commands))
            self.assertTrue(all(command[-2:] == ['--device', 'cpu'] for command in commands))

    def test_check_delegates_once_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(cli, 'ROOT', root), \
                    patch.object(sys, 'argv', ['hymotion', '--check', '--device', 'cuda:0']), \
                    patch.object(cli.subprocess, 'call', return_value=23) as call, \
                    patch.object(cli.subprocess, 'run') as run:
                self.assertEqual(cli.main(), 23)
            call.assert_called_once_with([sys.executable, str(root / 'scripts/check_installation.py'),
                                          '--device', 'cuda:0'], cwd=root)
            run.assert_not_called()

    def test_stage_failure_preserves_exit_code_and_stops_without_cpu_retry(self):
        for side_effects, code, count in (([None, subprocess.CalledProcessError(5, 'encode')], 5, 2),
                                          ([None, None, subprocess.CalledProcessError(9, 'generate')], 9, 3)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder, \
                    patch.object(cli.subprocess, 'run', side_effect=side_effects) as run:
                self.assertEqual(self.invoke(Path(folder)), code)
                self.assertEqual(run.call_count, count)
                self.assertTrue(all(call.args[0][-2:] == ['--device', 'cuda:0']
                                    for call in run.call_args_list[1:]))

    def test_preflight_code_fails_before_allocation_if_cuda_unavailable_or_initialization_fails(self):
        for available in (False, True):
            cuda = SimpleNamespace(is_available=Mock(return_value=available),
                                   set_device=Mock(side_effect=RuntimeError('driver initialization failed')))
            torch = SimpleNamespace(cuda=cuda, device=Mock(return_value='cuda:0'), tensor=Mock())
            message = 'driver initialization failed' if available else 'refusing a silent CPU fallback'
            with self.subTest(available=available), patch.dict(sys.modules, {'torch': torch}), \
                    patch.object(sys, 'argv', ['-c', 'cuda:0']):
                with self.assertRaisesRegex(RuntimeError, message):
                    exec(cli.CUDA_PREFLIGHT, {})
            torch.tensor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
