import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import contract
import service
import storage


REQUEST = {'job_id': 'job-1', 'config': {'prompt': 'A person walks.', 'seconds': 2, 'seed': 42, 'steps': 50, 'threads': 8}}


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.commits = []

    def get_json(self, job, name):
        return copy.deepcopy(self.values.get((job, name)))

    def put_json(self, job, name, value, **_kwargs):
        if (job, name) in self.values:
            return False
        self.values[job, name] = copy.deepcopy(value)
        self.commits.append(name)
        return True

    def commit_file(self, job, path):
        data = path.read_bytes()
        self.commits.append(path.name)
        return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'object_key': f'results/hymotion/{job}/{path.name}'}

    def with_urls(self, manifest):
        return dict(manifest, artifacts={name: dict(info, url='https://private-oss.example/fake')
                                        for name, info in manifest['artifacts'].items()})


def fake_worker(work):
    for name in contract.REQUIRED_FILES:
        (work / name).write_bytes(b'original fake output')
    (work / 'generation_report.json').write_text(json.dumps({'frames': 60, 'fps': 30}))
    (work / 'generation.log').write_text('fake GPU worker completed')


class ServiceTests(unittest.TestCase):
    def test_success_persists_completion_last_and_preserves_output_bytes(self):
        store = MemoryStore()
        worker = Mock(side_effect=fake_worker)
        result = service.generate(REQUEST, store, worker=worker, check=lambda: True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['backend'], 'tencent_hymotion_official')
        self.assertEqual(store.commits[-1], 'manifest.json')
        self.assertEqual(worker.call_count, 1)
        self.assertNotIn('url', store.values['job-1', 'manifest.json']['artifacts']['motion_000.npz'])

    def test_completed_same_request_is_idempotent_without_worker_or_preflight(self):
        store = MemoryStore()
        first = service.generate(REQUEST, store, worker=fake_worker, check=lambda: True)
        worker, check = Mock(), Mock()
        second = service.generate(REQUEST, store, worker=worker, check=check)
        self.assertEqual(first['request_sha256'], second['request_sha256'])
        worker.assert_not_called()
        check.assert_not_called()

    def test_same_id_different_request_is_rejected_without_generation(self):
        store = MemoryStore()
        service.generate(REQUEST, store, worker=fake_worker, check=lambda: True)
        changed = copy.deepcopy(REQUEST)
        changed['config']['seed'] += 1
        worker = Mock()
        with self.assertRaises(contract.RequestError) as error:
            service.generate(changed, store, worker=worker, check=lambda: True)
        self.assertEqual(error.exception.status, 409)
        worker.assert_not_called()

    def test_worker_failure_is_persistent_and_cannot_auto_restart(self):
        store = MemoryStore()
        def fail(work):
            (work / 'generation.log').write_text('fake worker failure')
            raise contract.RequestError(500, 'worker_failed', 'Worker failed')
        with self.assertRaises(contract.RequestError):
            service.generate(REQUEST, store, worker=fail, check=lambda: True)
        self.assertIn(('job-1', 'failure.json'), store.values)
        self.assertNotIn(('job-1', 'manifest.json'), store.values)
        worker = Mock()
        with self.assertRaises(contract.RequestError) as error:
            service.generate(REQUEST, store, worker=worker, check=lambda: True)
        self.assertEqual(error.exception.code, 'job_failed')
        worker.assert_not_called()

    def test_uncertain_reservation_write_never_starts_worker(self):
        store = MemoryStore()
        original = store.put_json
        def uncertain(job, name, value):
            original(job, name, value)
            raise OSError('Connection lost after put')
        store.put_json = uncertain
        worker = Mock()
        with self.assertRaises(OSError):
            service.generate(REQUEST, store, worker=worker, check=lambda: True)
        worker.assert_not_called()
        self.assertIn(('job-1', 'request.json'), store.values)

    def test_uncertain_completion_write_reconciles_without_regeneration(self):
        store = MemoryStore()
        original = store.put_json
        def uncertain(job, name, value):
            result = original(job, name, value)
            if name == 'manifest.json':
                raise OSError('Response lost after durable completion')
            return result
        store.put_json = uncertain
        first_worker = Mock(side_effect=fake_worker)
        with self.assertRaises(OSError):
            service.generate(REQUEST, store, worker=first_worker, check=lambda: True)
        self.assertEqual(first_worker.call_count, 1)
        self.assertNotIn(('job-1', 'failure.json'), store.values)
        later_worker = Mock()
        result = service.generate(REQUEST, store, worker=later_worker, check=lambda: True)
        self.assertEqual(result['status'], 'complete')
        later_worker.assert_not_called()

    def test_real_oss_adapter_reservation_cannot_overwrite_an_existing_object(self):
        class FakeOssError(Exception):
            status, code = 409, 'FileAlreadyExists'
        store = storage.OSSStore.__new__(storage.OSSStore)
        store.bucket = Mock()
        store.bucket.put_object.side_effect = FakeOssError()
        store.oss2 = SimpleNamespace(exceptions=SimpleNamespace(OssError=FakeOssError))
        store.prefix = 'results/hymotion'
        self.assertFalse(store.put_json('job-1', 'request.json', {'request_sha256': 'fake'}))
        self.assertEqual(store.bucket.put_object.call_args.kwargs['headers']['x-oss-forbid-overwrite'], 'true')

    def test_missing_execution_role_credentials_fail_before_oss_access(self):
        fake_sdk = SimpleNamespace(StsAuth=Mock(), Bucket=Mock())
        with patch.dict('sys.modules', {'oss2': fake_sdk}), patch.dict(os.environ, {'HYMOTION_OSS_BUCKET': 'test-hymotion-bucket'}):
            with self.assertRaises(contract.RequestError) as error:
                storage.OSSStore({})
        self.assertEqual(error.exception.code, 'role_credentials_missing')
        fake_sdk.Bucket.assert_not_called()

    def test_interrupted_reserved_job_does_not_restart(self):
        store = MemoryStore()
        _, _, digest = contract.validate_request(REQUEST)
        store.put_json('job-1', 'request.json', {'request_sha256': digest})
        worker = Mock()
        with self.assertRaises(contract.RequestError) as error:
            service.generate(REQUEST, store, worker=worker, check=lambda: True)
        self.assertEqual(error.exception.code, 'job_incomplete')
        worker.assert_not_called()

    def test_missing_mount_never_reserves_or_generates(self):
        store, worker = MemoryStore(), Mock()
        with self.assertRaises(contract.RequestError) as error:
            service.generate(REQUEST, store, worker=worker, check=lambda: False)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(store.values, {})
        worker.assert_not_called()

    def test_busy_rejects_without_reservation(self):
        store, worker = MemoryStore(), Mock()
        service.GATE.acquire()
        try:
            with self.assertRaises(contract.RequestError) as error:
                service.generate(REQUEST, store, worker=worker, check=lambda: True)
        finally:
            service.GATE.release()
        self.assertEqual(error.exception.status, 429)
        worker.assert_not_called()
        self.assertEqual(store.values, {})

    def test_missing_official_fbx_cannot_be_marked_complete(self):
        store = MemoryStore()
        def partial(work):
            (work / 'motion_000.npz').write_bytes(b'fake')
        with self.assertRaises(contract.RequestError):
            service.generate(REQUEST, store, worker=partial, check=lambda: True)
        self.assertNotIn(('job-1', 'manifest.json'), store.values)

    def test_output_storage_failure_cannot_be_marked_complete(self):
        store = MemoryStore()
        store.commit_file = Mock(side_effect=OSError('OSS unavailable'))
        with self.assertRaises(OSError):
            service.generate(REQUEST, store, worker=fake_worker, check=lambda: True)
        self.assertNotIn(('job-1', 'manifest.json'), store.values)
        self.assertIn(('job-1', 'failure.json'), store.values)

    def test_timeout_terminates_worker_and_strips_credentials(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired('worker', 1)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'never-pass', 'HYMOTION_WORKER_TIMEOUT_SECONDS': '1'}), \
                patch.object(service.subprocess, 'Popen', return_value=process) as popen, patch.object(service, 'terminate_worker') as stop:
            with self.assertRaises(contract.RequestError) as error:
                service.run_worker(Path(folder))
            self.assertEqual(error.exception.status, 504)
            stop.assert_called_once_with(process)
            self.assertNotIn('ALIBABA_CLOUD_ACCESS_KEY_SECRET', popen.call_args.kwargs['env'])

    def test_request_normalization_matches_remote_client(self):
        import remote_client
        _, normalized, digest = contract.validate_request(REQUEST)
        self.assertEqual(normalized, remote_client.normalize_config(REQUEST['config']))
        self.assertEqual(digest, hashlib.sha256(remote_client.canonical_bytes(normalized)).hexdigest())

    def test_invalid_requests_never_open_storage(self):
        for request in ({}, dict(REQUEST, job_id='../escape'), dict(REQUEST, unexpected=True),
                        dict(REQUEST, config=dict(REQUEST['config'], seed=True))):
            with self.subTest(request=request), self.assertRaises(contract.RequestError):
                contract.validate_request(request)


if __name__ == '__main__':
    unittest.main()
