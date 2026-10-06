import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import remote_client as remote


ENV = {'MOTION_REMOTE_ENDPOINT': 'https://hymotion-demo.cn-hangzhou.fcapp.run',
       'MOTION_REMOTE_OSS_BUCKET': 'private-hymotion-test',
       'ALIBABA_CLOUD_ACCESS_KEY_ID': 'fake-id', 'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'fake-secret'}
CONFIG = {'prompt': 'A person walks.', 'seconds': 2, 'seed': 42, 'steps': 50, 'threads': 8}
DATA = b'fake official artifact bytes'


def make_manifest(job='job-1'):
    return {'job_id': job, 'status': 'complete', 'backend': 'tencent_hymotion_official',
            'request_sha256': hashlib.sha256(remote.canonical_bytes(remote.normalize_config(CONFIG))).hexdigest(),
            'artifacts': {name: {'size': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest(),
                                'object_key': f'results/hymotion/{job}/{name}',
                                'url': f'https://private-hymotion-test.oss-cn-hangzhou.aliyuncs.com/results/hymotion/{job}/{name}?Signature=fake'}
                          for name in remote.REQUIRED_FILES}}


class FakeResponse:
    def __init__(self, body=DATA, status=200, headers=None):
        self.stream, self.status = io.BytesIO(body), status
        self.headers = {'Content-Length': str(len(body))} if headers is None else headers

    def read(self, count):
        return self.stream.read(count)

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class RemoteClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_inspection_never_contacts_cloud_or_exposes_credentials(self):
        with patch.object(remote.http.client, 'HTTPSConnection', side_effect=AssertionError('network forbidden')):
            state = remote.inspect_remote()
        self.assertTrue(state['credentials_present']['ALIBABA_CLOUD_ACCESS_KEY_ID'])
        self.assertFalse(state['inference_verified'])
        self.assertNotIn('fake-secret', json.dumps(state))
        self.assertNotIn('fake-id', json.dumps(state))

    def test_missing_credentials_not_ready(self):
        os.environ.pop('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
        self.assertFalse(remote.inspect_remote()['ready'])

    def test_endpoint_is_fixed_https_fc_region(self):
        invalid = ['http://hymotion-demo.cn-hangzhou.fcapp.run', 'https://evil.test',
                   'https://hymotion-demo.cn-beijing.fcapp.run',
                   'https://hymotion-demo.cn-hangzhou.fcapp.run/v1/generate',
                   'https://user:password@hymotion-demo.cn-hangzhou.fcapp.run',
                   'https://hymotion-demo.cn-hangzhou.fcapp.run:443']
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {'MOTION_REMOTE_ENDPOINT': endpoint}):
                with self.assertRaises(ValueError):
                    remote._settings()

    def test_invalid_generation_fails_before_http(self):
        for extra in ({'seed': -1}, {'steps': 201}, {'threads': 65}, {'seconds': float('nan')},
                      {'seconds': True}, {'prompt': 'x' * 4001}, {'backend': 'kimodo'}):
            with self.subTest(extra=extra), patch.object(remote, '_invoke', side_effect=AssertionError('network forbidden')):
                with self.assertRaises(ValueError):
                    remote.generate_remote('job-1', dict(CONFIG, **extra), Path('unused'))

    def test_manifest_rejects_host_path_job_model_and_size_changes(self):
        conf = remote._settings()
        good = make_manifest()
        digest = good['request_sha256']
        for field, value in [('url', 'https://evil.test/file?Signature=fake'),
                             ('url', 'https://private-hymotion-test.oss-cn-hangzhou.aliyuncs.com/results/hymotion/another/motion_000.npz?Signature=fake'),
                             ('url', 'https://private-hymotion-test.oss-cn-hangzhou.aliyuncs.com/results/hymotion/job-1/%2e%2e/motion_000.npz?Signature=fake'),
                             ('url', 'https://private-hymotion-test.oss-cn-hangzhou.aliyuncs.com:443/results/hymotion/job-1/motion_000.npz?Signature=fake'),
                             ('object_key', 'results/kimodo/job-1/motion_000.npz'),
                             ('size', True), ('size', remote.MAX_FILE_BYTES + 1), ('sha256', 'invalid')]:
            changed = copy.deepcopy(good)
            changed['artifacts']['motion_000.npz'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(remote.RemoteGenerationError):
                remote.validate_manifest(changed, conf, 'job-1', digest)
        for field, value in [('job_id', 'another'), ('backend', 'kimodo'), ('request_sha256', 'wrong'), ('status', 'running')]:
            with self.subTest(field=field), self.assertRaises(remote.RemoteGenerationError):
                remote.validate_manifest(dict(good, **{field: value}), conf, 'job-1', digest)

    def test_download_rejects_redirect_truncation_extra_data_and_wrong_hash(self):
        info = make_manifest()['artifacts']['motion_000.npz']
        for response in (FakeResponse(status=302), FakeResponse(DATA[:-1]),
                         FakeResponse(DATA + b'extra', headers={}), FakeResponse(b'x' * len(DATA))):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as folder:
                connection = FakeConnection(response)
                with patch.object(remote.http.client, 'HTTPSConnection', return_value=connection):
                    with self.assertRaises(remote.RemoteGenerationError):
                        remote._download(info, Path(folder) / 'artifact')
                self.assertEqual(len(connection.calls), 1)

    def test_download_verifies_exact_bytes(self):
        info = make_manifest()['artifacts']['motion_000.npz']
        with tempfile.TemporaryDirectory() as folder, patch.object(remote.http.client, 'HTTPSConnection',
                                                                   return_value=FakeConnection(FakeResponse())):
            path = Path(folder) / 'artifact'
            remote._download(info, path)
            self.assertEqual(path.read_bytes(), DATA)

    def test_success_downloads_without_more_fc_calls_or_saved_urls(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, '_invoke', return_value=make_manifest()) as invoke:
            def download(info, destination):
                destination.write_bytes(DATA)
            with patch.object(remote, '_download', side_effect=download) as fetch:
                output = Path(folder) / 'output'
                result = remote.generate_remote('job-1', CONFIG, output)
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(fetch.call_count, len(remote.REQUIRED_FILES))
            self.assertTrue((output / 'motion_000.fbx').is_file())
            self.assertNotIn('Signature', (output / 'remote_manifest.json').read_text())
            self.assertNotIn('url', result['artifacts']['motion_000.npz'])
            self.assertFalse((Path(folder) / 'remote-recovery.json').exists())

    def test_failed_download_leaves_no_partial_output_and_preserves_private_recovery(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, '_invoke', return_value=make_manifest()) as invoke:
            output = Path(folder) / 'output'
            with patch.object(remote, '_download', side_effect=remote.RemoteGenerationError('download failed')):
                with self.assertRaises(remote.RemoteGenerationError):
                    remote.generate_remote('job-1', CONFIG, output)
            self.assertEqual(invoke.call_count, 1)
            self.assertFalse(output.exists())
            self.assertTrue((Path(folder) / 'remote-recovery.json').is_file())

    def test_recovery_downloads_never_resubmit_generation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, '_invoke', side_effect=AssertionError('FC forbidden')):
            recovery = Path(folder) / 'remote-recovery.json'
            recovery.write_text(json.dumps(make_manifest()))
            with patch.object(remote, '_download', side_effect=lambda _info, path: path.write_bytes(DATA)):
                remote.generate_remote('job-1', CONFIG, Path(folder) / 'output', recovery_manifest=recovery)
            self.assertTrue((Path(folder) / 'output/motion_000.fbx').exists())
            self.assertFalse(recovery.exists())

    def test_existing_output_fails_before_invocation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, '_invoke') as invoke:
            with self.assertRaises(remote.RemoteGenerationError):
                remote.generate_remote('job-1', CONFIG, folder)
            invoke.assert_not_called()

    def test_timeout_does_not_retry_or_echo_secret_errors(self):
        connection = FakeConnection(FakeResponse())
        connection.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('fake-secret signed-url'))
        with patch.object(remote, '_signed_headers', return_value={}), patch.object(remote.http.client, 'HTTPSConnection', return_value=connection) as connect:
            with self.assertRaises(remote.RemoteGenerationError) as error:
                remote._invoke(remote._settings(), {'job_id': 'job-1', 'config': CONFIG})
            self.assertEqual(connect.call_count, 1)
            self.assertNotIn('fake-secret', str(error.exception))

    def test_http_error_does_not_echo_untrusted_body(self):
        response = FakeResponse(b'fake-secret signed Authorization', status=500)
        with patch.object(remote, '_signed_headers', return_value={}), patch.object(remote.http.client, 'HTTPSConnection', return_value=FakeConnection(response)):
            with self.assertRaises(remote.RemoteGenerationError) as error:
                remote._invoke(remote._settings(), {})
            self.assertNotIn('fake-secret', str(error.exception))

    def test_official_sdk_signing_uses_test_credentials_without_network(self):
        if remote.importlib.util.find_spec('alibabacloud_credentials') is None:
            self.skipTest('Official SDK is not installed in this environment')
        with patch.object(remote.http.client, 'HTTPSConnection', side_effect=AssertionError('network forbidden')):
            headers = remote._signed_headers('unused', b'{}')
        self.assertTrue(headers['authorization'].startswith('ACS3-HMAC-SHA256'))
        self.assertNotIn('fake-secret', headers['authorization'])


if __name__ == '__main__':
    unittest.main()
