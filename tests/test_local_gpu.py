"""GPU routing/adapter contract tests; no model or GPU inference is performed."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backend

SCRIPTS = Path(__file__).resolve().parents[1] / 'deploy/windows-gpu/backend/scripts'
spec = importlib.util.spec_from_file_location('staged_gpu_adapter_test', SCRIPTS / 'staged_infer.py')
staged = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staged)


class LocalGpuTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'MOTION_DEVICE': 'cpu', 'MOTION_BACKEND': 'local'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_cpu_command_remains_compatible_with_legacy_cli(self):
        with patch.dict(os.environ, {'MOTION_DEVICE': 'cpu', 'MOTION_BACKEND': 'local'}):
            command = backend.command_for(Path('job'), backend.validate_config({'prompt': 'walk'}), {'python': 'python', 'root': 'model'})
        self.assertNotIn('--device', command)

    def test_gpu_command_explicitly_selects_cuda_without_shell(self):
        prompt = 'walk; $(echo no) & more'
        with patch.dict(os.environ, {'MOTION_DEVICE': 'cuda:0', 'MOTION_BACKEND': 'local'}):
            command = backend.command_for(Path('job'), backend.validate_config({'prompt': prompt}), {'python': 'python', 'root': 'model'})
        self.assertEqual(command[-2:], ['--device', 'cuda:0'])
        self.assertIn(prompt, command)

    def test_legacy_backend_not_reported_as_gpu_capable(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MOTION_DEVICE': 'cuda:0', 'MOTION_BACKEND': 'local', 'MOTION_BACKEND_ROOT': folder}):
            state = backend.inspect_backend()
            self.assertFalse(state['ready'])
            self.assertTrue(any('staged CUDA adapter' in name for name in state['missing']))
            self.assertEqual(state['device'], 'cuda:0')
            self.assertIn('CPU text', state['model'])

    def test_invalid_device_is_a_complete_not_ready_state(self):
        with patch.dict(os.environ, {'MOTION_DEVICE': 'cuda:99'}):
            state = backend.inspect_backend()
        self.assertFalse(state['ready'])
        self.assertFalse(state['inference_verified'])
        self.assertIsNone(state['device'])
        self.assertEqual(state['requested_device'], 'cuda:99')
        self.assertIn('MOTION_DEVICE', state['message'])

    def test_malformed_adapter_markers_do_not_crash_inspection(self):
        cases = ('null', '[]', '{broken', '{"schema":1,"motion_devices":null}',
                 '{"schema":1,"motion_devices":"cuda:0"}',
                 '{"schema":true,"motion_devices":["cuda:0"]}',
                 '{"schema":1,"motion_devices":["cuda:0",42]}')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MOTION_DEVICE': 'cuda:0', 'MOTION_BACKEND_ROOT': folder}):
            marker = Path(folder) / 'runtime-adapter.json'
            for content in cases:
                with self.subTest(content=content):
                    marker.write_text(content, encoding='utf-8')
                    state = backend.inspect_backend()
                    self.assertFalse(state['ready'])
                    self.assertEqual(state['device'], 'cuda:0')
                    self.assertTrue(any('staged CUDA adapter' in reason for reason in state['missing']))

    def test_cpu_backend_needs_no_gpu_marker_and_ready_is_not_inference_verified(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MOTION_BACKEND_ROOT': folder}), \
                patch.object(backend, 'SUPPORT_FILES', ()), patch.object(backend, 'WEIGHT_SIZES', {}):
            root = Path(folder)
            python = backend.backend_python(root)
            python.parent.mkdir(parents=True)
            python.touch()
            (root / '.venv/pyvenv.cfg').write_text('version = 3.11.14\n', encoding='utf-8')
            cpu = backend.inspect_backend()
            self.assertTrue(cpu['ready'])
            self.assertEqual(cpu['device'], 'cpu')
            self.assertFalse(cpu['inference_verified'])
            (root / 'runtime-adapter.json').write_text('{"schema":1,"motion_devices":["cpu","cuda:0"]}', encoding='utf-8')
            with patch.dict(os.environ, {'MOTION_DEVICE': 'cuda:0'}):
                cuda = backend.inspect_backend()
            self.assertTrue(cuda['ready'])
            self.assertEqual(cuda['device'], 'cuda:0')
            self.assertFalse(cuda['inference_verified'])

    def test_fc_route_ignores_local_device_settings(self):
        import sys
        remote = SimpleNamespace(inspect_remote=lambda: {'ready': True, 'remote': True})
        with patch.dict(os.environ, {'MOTION_BACKEND': 'fc', 'MOTION_DEVICE': 'invalid'}), \
                patch.dict(sys.modules, {'remote_client': remote}):
            self.assertTrue(backend.inspect_backend()['remote'])
            command = backend.command_for(Path('job'), {}, {})
        self.assertIn(str(backend.APP_ROOT / 'remote_client.py'), command)
        self.assertNotIn('--device', command)

    def test_cuda_unavailable_fails_instead_of_cpu_fallback(self):
        class Cuda:
            @staticmethod
            def is_available():
                return False
        class Torch:
            cuda = Cuda
        with self.assertRaisesRegex(RuntimeError, 'refusing a silent CPU fallback'):
            staged.motion_device(Torch, 'cuda:0')

    def test_all_cached_features_move_device_and_keep_dtype(self):
        class Feature:
            def __init__(self, dtype):
                self.dtype = dtype
                self.device = 'cpu'
            def to(self, *, device):
                result = Feature(self.dtype)
                result.device = device
                return result
        features = dict(text_vec_raw=Feature('float32'), text_ctxt_raw=Feature('bfloat16'), text_ctxt_raw_length=Feature('int64'))
        with patch.object(staged, 'validate_features'):
            moved = staged.features_on_device(features, object(), 'cuda:0')
        self.assertTrue(all(value.device == 'cuda:0' for value in moved.values()))
        self.assertEqual(moved['text_ctxt_raw_length'].dtype, 'int64')
        self.assertTrue(all(value.device == 'cpu' for value in features.values()))

    def test_generation_failure_keeps_requested_and_actual_device_separate(self):
        args = SimpleNamespace(stage='generate', device='cuda:0', threads=1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args.repo, args.output_dir = root / 'repo', root / 'output'
            args.repo.mkdir()
            with patch.object(staged, 'BASE', root), patch.object(staged, 'parse_args', return_value=args), \
                    patch.object(staged.os, 'chdir'), patch.dict(os.environ), \
                    patch.object(staged.subprocess, 'check_output', return_value='test-commit'), \
                    patch.object(staged, 'generate', side_effect=RuntimeError('CUDA out of memory')) as generate, \
                    patch.object(staged, 'encode') as encode:
                with self.assertRaisesRegex(RuntimeError, 'CUDA out of memory'):
                    staged.main()
            report = json.loads((args.output_dir / 'run_metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['requested_motion_device'], 'cuda:0')
            self.assertIsNone(report['device'])
            self.assertIsNone(report['model_device'])
            self.assertEqual(report['error'], 'CUDA out of memory')
            generate.assert_called_once()
            encode.assert_not_called()


class GpuHttpErrors(unittest.TestCase):
    def test_invalid_device_and_marker_reject_jobs_as_not_ready(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MOTION_DATA_DIR': str(Path(folder) / 'data'), 'MOTION_BACKEND_ROOT': folder,
                'MOTION_MODE': 'local', 'MOTION_BACKEND': 'local', 'MOTION_SHOWCASE': '0'}):
            app_spec = importlib.util.spec_from_file_location('gpu_errors_app', backend.APP_ROOT / 'app.py')
            application = importlib.util.module_from_spec(app_spec)
            app_spec.loader.exec_module(application)
            (Path(folder) / 'runtime-adapter.json').write_text('null', encoding='utf-8')
            client = TestClient(application.app)
            self.addCleanup(client.close)
            for device in ('invalid', 'cuda:0'):
                with self.subTest(device=device), patch.dict(os.environ, {'MOTION_DEVICE': device}):
                    state = client.get('/api/status').json()['backend']
                    self.assertFalse(state['ready'])
                    self.assertIn('requested_device', state)
                    self.assertEqual(client.post('/api/jobs', data={'config': '{"prompt":"walk"}'}).status_code, 409)
                    self.assertEqual(client.post('/api/batches', json={
                        'name': 'test', 'prompts': ['walk'], 'repeat': 1, 'seed_strategy': 'fixed', 'config': {}
                    }).status_code, 409)
            self.assertEqual(application.jobs, {})


if __name__ == '__main__':
    unittest.main()
