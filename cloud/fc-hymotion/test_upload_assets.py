import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import upload_assets as upload


class MissingObject(Exception):
    pass


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.parts = []
        self.metadata = {}
        self.completed = 0
        self.aborted = 0
        self.complete_headers = None

    def head_object(self, key):
        if key not in self.objects:
            raise MissingObject()
        return SimpleNamespace(content_length=len(self.objects[key]), headers=self.metadata)

    def get_object(self, key):
        return io.BytesIO(self.objects[key])

    def init_multipart_upload(self, key, headers):
        self.metadata = headers
        return SimpleNamespace(upload_id='fake-upload')

    def upload_part(self, key, upload_id, number, data):
        self.parts.append(data)
        return SimpleNamespace(etag=str(number))

    def complete_multipart_upload(self, key, upload_id, parts, headers):
        self.completed += 1
        self.complete_headers = headers
        self.objects[key] = b''.join(self.parts)

    def abort_multipart_upload(self, *_args):
        self.aborted += 1


class UploadTests(unittest.TestCase):
    def setUp(self):
        fake_sdk = SimpleNamespace(exceptions=SimpleNamespace(NoSuchKey=MissingObject),
                                   models=SimpleNamespace(PartInfo=lambda number, etag: (number, etag)))
        self.sdk = patch.dict('sys.modules', {'oss2': fake_sdk})
        self.sdk.start()
        self.addCleanup(self.sdk.stop)

    def make_item(self, folder, data=b'official-model-bytes'):
        path = Path(folder) / 'model.bin'
        path.write_bytes(data)
        return {'local_path': path, 'local_relative_path': 'repo/ckpts/Qwen3-8B/model.bin',
                'object_key': 'assets/hymotion/ckpts/Qwen3-8B/model.bin',
                'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    def test_commit_only_after_stream_hash_matches_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            result = upload.upload_one(bucket, item, part_size=5)
            self.assertEqual(result, 'uploaded_and_verified')
            self.assertEqual(bucket.objects[item['object_key']], item['local_path'].read_bytes())
            self.assertEqual(bucket.completed, 1)
            self.assertEqual(bucket.aborted, 0)
            self.assertEqual(bucket.complete_headers, {'x-oss-forbid-overwrite': 'true'})

    def test_changed_source_hash_aborts_without_completing(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            item['local_path'].write_bytes(b'x' * item['size_bytes'])
            with self.assertRaises(upload.UploadError):
                upload.upload_one(bucket, item, part_size=5)
            self.assertEqual(bucket.completed, 0)
            self.assertEqual(bucket.aborted, 1)
            self.assertFalse(bucket.objects)

    def test_changed_source_size_aborts_without_completing(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            item['local_path'].write_bytes(b'x')
            with self.assertRaises(upload.UploadError):
                upload.upload_one(bucket, item, part_size=5)
            self.assertEqual(bucket.completed, 0)
            self.assertEqual(bucket.aborted, 1)

    def test_matching_existing_object_is_read_verified_and_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            bucket.objects[item['object_key']] = item['local_path'].read_bytes()
            bucket.metadata = {'x-oss-meta-sha256': item['sha256']}
            result = upload.upload_one(bucket, item, part_size=5)
            self.assertEqual(result, 'verified_existing')
            self.assertEqual(bucket.completed, 0)
            self.assertFalse(bucket.parts)

    def test_matching_metadata_but_bad_existing_bytes_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            bucket.objects[item['object_key']] = b'x' * item['size_bytes']
            bucket.metadata = {'x-oss-meta-sha256': item['sha256']}
            with self.assertRaises(upload.UploadError):
                upload.upload_one(bucket, item)
            self.assertEqual(bucket.completed, 0)
            self.assertFalse(bucket.parts)

    def test_stream_error_aborts_and_keeps_provider_exception_out_of_our_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            item, bucket = self.make_item(folder), FakeBucket()
            bucket.upload_part = Mock(side_effect=OSError('secret-provider-diagnostic'))
            with self.assertRaises(OSError):
                upload.upload_one(bucket, item)
            self.assertEqual(bucket.aborted, 1)
            self.assertEqual(bucket.completed, 0)

    def test_local_default_mode_does_not_create_oss_client(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / 'repo/ckpts/Qwen3-8B/config.json'
            file.parent.mkdir(parents=True)
            file.write_bytes(b'{}')
            plan = {'file_count': 1, 'total_bytes': 2, 'files': [{
                'local_relative_path': 'repo/ckpts/Qwen3-8B/config.json',
                'object_key': 'assets/hymotion/ckpts/Qwen3-8B/config.json',
                'repo': 'Qwen/Qwen3-8B', 'revision': 'a' * 40, 'size_bytes': 2,
                'sha256': hashlib.sha256(b'{}').hexdigest()}]}
            manifest = root / 'assets.json'
            manifest.write_text(json.dumps(plan))
            with patch.object(upload, 'make_bucket', side_effect=AssertionError('No cloud')) as make:
                self.assertEqual(upload.main(['--local-root', str(root), '--manifest', str(manifest)]), 0)
                make.assert_not_called()
            plan['files'][0]['local_relative_path'] = 'repo/ckpts/../../escape'
            manifest.write_text(json.dumps(plan))
            with self.assertRaises(upload.UploadError):
                upload.read_plan(manifest, root)

    def signed_entry(self, item):
        base = 'https://private-hymotion-test.oss-cn-hangzhou.aliyuncs.com/' + item['object_key']
        return {'object_key': item['object_key'], 'put_url': base + '?Signature=fake-put',
                'head_url': base + '?Signature=fake-head', 'expires_at_unix': time.time() + 3600,
                'headers': {'Content-Type': 'application/octet-stream', 'x-oss-meta-sha256': item['sha256'], 'x-oss-forbid-overwrite': 'true'}}

    def test_signed_plan_rejects_foreign_hosts_paths_headers_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            item = self.make_item(folder)
            path = Path(folder) / 'signed.json'
            entry = self.signed_entry(item)
            value = {'bucket': 'private-hymotion-test', 'region': 'cn-hangzhou', 'expires_at_unix': time.time() + 3600, 'files': [entry]}
            path.write_text(json.dumps(value))
            self.assertIn(item['object_key'], upload.read_signed_plan(path, value['bucket'], value['region'], [item]))
            for field, bad in [('put_url', 'https://evil.example/model'), ('head_url', entry['head_url'].replace('model.bin', 'other.bin')),
                               ('headers', {'Content-Type': 'application/octet-stream'})]:
                changed = json.loads(json.dumps(value))
                changed['files'][0][field] = bad
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(upload.UploadError):
                    upload.read_signed_plan(path, value['bucket'], value['region'], [item])
            value['expires_at_unix'] = time.time() - 1
            path.write_text(json.dumps(value))
            with self.assertRaises(upload.UploadError):
                upload.read_signed_plan(path, value['bucket'], value['region'], [item])

    def test_checked_stream_withholds_final_bytes_if_hash_or_size_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            item = self.make_item(folder, b'12345678')
            item['local_path'].write_bytes(b'1234567x')
            stream = upload.checked_stream(item, part_size=4)
            self.assertEqual(next(stream), b'1234')
            with self.assertRaises(upload.UploadError):
                next(stream)
            item['local_path'].write_bytes(b'12345678x')
            stream = upload.checked_stream(item, part_size=4)
            self.assertEqual(next(stream), b'1234')
            with self.assertRaises(upload.UploadError):
                next(stream)

    def test_signed_put_streams_verified_bytes_and_confirms_head(self):
        import httpx
        with tempfile.TemporaryDirectory() as folder:
            item = self.make_item(folder)
            entry = self.signed_entry(item)
            transferred = []
            def handler(request):
                if request.method == 'HEAD':
                    if not transferred:
                        return httpx.Response(404)
                    return httpx.Response(200, headers={'Content-Length': str(item['size_bytes']), 'x-oss-meta-sha256': item['sha256']})
                transferred.append(request.read())
                self.assertEqual(request.headers['x-oss-forbid-overwrite'], 'true')
                self.assertEqual(request.headers['content-length'], str(item['size_bytes']))
                return httpx.Response(200)
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = upload.upload_signed_one(item, entry, client)
            self.assertEqual(result, 'uploaded_source_sha256_verified_head_confirmed')
            self.assertEqual(transferred, [item['local_path'].read_bytes()])

    def test_signed_put_network_failure_has_no_automatic_retry(self):
        import httpx
        with tempfile.TemporaryDirectory() as folder:
            item, attempts = self.make_item(folder), []
            def handler(request):
                if request.method == 'HEAD':
                    return httpx.Response(404)
                attempts.append(request.method)
                raise httpx.ReadError('private-signed-url-suppressed')
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(httpx.ReadError):
                    upload.upload_signed_one(item, self.signed_entry(item), client)
            self.assertEqual(attempts, ['PUT'])

    def test_signed_existing_metadata_match_skips_upload(self):
        import httpx
        with tempfile.TemporaryDirectory() as folder:
            item, methods = self.make_item(folder), []
            def handler(request):
                methods.append(request.method)
                return httpx.Response(200, headers={'Content-Length': str(item['size_bytes']), 'x-oss-meta-sha256': item['sha256']})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(upload.upload_signed_one(item, self.signed_entry(item), client), 'existing_size_and_sha256_metadata_match')
            self.assertEqual(methods, ['HEAD'])


if __name__ == '__main__':
    unittest.main()
