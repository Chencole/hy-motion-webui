import contextlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import worker


CONFIG = {'prompt': 'A person walks.', 'seconds': 1, 'seed': 42, 'steps': 2, 'threads': 8}


class WorkerOutputTests(unittest.TestCase):
    def test_relative_repository_output_preserves_original_artifacts_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, work = root / 'official', root / 'service-work'
            source.mkdir()
            work.mkdir()
            (work / 'generation.log').write_bytes(b'service-owned log')
            original = {'motion_000.npz': b'original-npz\x00\xff', 'motion_000.fbx': b'original-fbx\x00\xfe',
                        'motion_meta.json': b'{"num_samples":1}', 'motion_000.txt': b'original prompt'}
            generated_paths = []
            model_output = object()

            def generate(**kwargs):
                # Reproduces the official distinction: writes use cwd, HTML reads resolve from source.
                relative = Path(kwargs['output_dir'])
                self.assertFalse(relative.is_absolute())
                self.assertEqual(relative.parts[0], 'output')
                self.assertEqual(len(relative.parts), 2)
                self.assertNotIn('..', relative.parts)
                generated = relative.resolve()
                self.assertEqual(generated, (source / relative).resolve())
                self.assertTrue(generated.is_relative_to(source.resolve()))
                generated_paths.append(generated)
                for name, content in original.items():
                    (generated / name).write_bytes(content)
                (generated / 'unexpected-file.txt').write_bytes(b'not an artifact')
                (generated / 'generation.log').write_bytes(b'must not overwrite the service log')
                return '<!doctype html><html><body>embedded motion data</body></html>', [], model_output

            with contextlib.chdir(source):
                html, result = worker.generate_official_outputs(SimpleNamespace(generate_motion=generate), CONFIG, source, work)
            self.assertIs(result, model_output)
            self.assertIn('embedded motion data', html)
            for name, content in original.items():
                self.assertEqual((work / name).read_bytes(), content)
            self.assertFalse((work / 'unexpected-file.txt').exists())
            self.assertEqual((work / 'generation.log').read_bytes(), b'service-owned log')
            self.assertTrue(all(not path.exists() for path in generated_paths))

    def test_official_error_html_cannot_be_accepted_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            source, work = Path(folder) / 'official', Path(folder) / 'work'
            source.mkdir()
            work.mkdir()

            def generate(**kwargs):
                generated = source / kwargs['output_dir']
                (generated / 'motion_000.npz').write_bytes(b'valid-but-not-a-completed-job')
                return '<html><body><h1>Error generating visualization</h1><p>No SMPL data found</p></body></html>', [], {}

            with self.assertRaisesRegex(RuntimeError, 'invalid visualization HTML'):
                worker.generate_official_outputs(SimpleNamespace(generate_motion=generate), CONFIG, source, work)
            self.assertEqual(list((source / 'output').iterdir()), [])
            self.assertFalse((work / 'motion_000.npz').exists())

    def test_runtime_failure_cleans_repository_temporary_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, work = Path(folder) / 'official', Path(folder) / 'work'
            source.mkdir()
            work.mkdir()

            def generate(**kwargs):
                (source / kwargs['output_dir'] / 'partial.npz').write_bytes(b'partial')
                raise RuntimeError('official generation failed')

            with self.assertRaisesRegex(RuntimeError, 'official generation failed'):
                worker.generate_official_outputs(SimpleNamespace(generate_motion=generate), CONFIG, source, work)
            self.assertEqual(list((source / 'output').iterdir()), [])
            self.assertEqual(list(work.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
