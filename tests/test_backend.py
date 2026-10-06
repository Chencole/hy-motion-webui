import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend

class BackendTests(unittest.TestCase):
    def test_environment_root_takes_precedence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MOTION_BACKEND_ROOT': folder}):
            self.assertEqual(backend.backend_root(), Path(folder).resolve())

    def test_missing_backend_does_not_claim_ready(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MOTION_BACKEND_ROOT': folder}):
            state = backend.inspect_backend()
            self.assertFalse(state['ready'])
            self.assertFalse(state['inference_verified'])
            self.assertTrue(state['missing'])

    def test_prompt_is_data_and_jobs_do_not_share_output(self):
        prompt = 'A person moves; $(untrusted) "hello" & echo test'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {'ready': True, 'python': str(root / 'python'), 'root': str(root / 'model')}
            with patch.object(backend, 'inspect_backend', return_value=state):
                config = {'prompt': prompt, 'seconds': 2, 'seed': 73, 'steps': 27, 'threads': 3}
                a = backend.build_command(root / 'job a', config)
                b = backend.build_command(root / 'job b', {'prompt': prompt, 'seconds': 2})
                self.assertIsInstance(a, list)
                self.assertEqual(a, [str(root / 'python'), '-u', str(root / 'model/src/hy_motion_cpu/cli.py'),
                                     '--prompt', prompt, '--seconds', '2.0', '--seed', '73',
                                     '--steps', '27', '--threads', '3', '--output', str(root / 'job a/output')])
                self.assertNotEqual(a, b)
                request = json.loads((root / 'job a/request.json').read_text(encoding='utf-8'))
                self.assertEqual(request['prompt'], prompt)
                self.assertEqual(request['steps'], 27)
                self.assertFalse((root / 'job a/output').exists())
                with self.assertRaises(FileExistsError):
                    backend.build_command(root / 'job a', {'prompt': prompt})

    def test_invalid_input_fails_before_job_creation(self):
        for data in ({'prompt': ''}, {'prompt': 'walk', 'seconds': float('nan')},
                     {'prompt': 'walk', 'seconds': 0}, {'prompt': 'walk', 'steps': True},
                     {'prompt': 'walk', 'seed': 1.5}, {'prompt': 'walk', 'threads': 0}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as folder:
                job = Path(folder) / 'job'
                with self.assertRaises(ValueError):
                    backend.build_command(job, data)
                self.assertFalse(job.exists())

    def test_video_rejected_for_text_model(self):
        with self.assertRaises(ValueError):
            backend.build_command(Path('unused'), {'prompt': 'walk'}, Path('video.mp4'))

    def test_backend_unavailable_has_no_job_side_effect(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(backend, 'inspect_backend', return_value={'ready': False, 'message': 'missing'}):
            job = Path(folder) / 'job'
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                backend.build_command(job, {'prompt': 'walk'})
            self.assertFalse(job.exists())

if __name__ == '__main__':
    unittest.main()
