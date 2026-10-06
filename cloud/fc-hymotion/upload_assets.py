"""Upload verified existing local HY-Motion files to private OSS without a second disk copy.

Default: local SHA-256 checks only. --upload explicitly enables OSS operations.
Credential values, provider errors and signed URLs are never printed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import quote, urlsplit

PART_SIZE = 16 * 1024 * 1024
REPOSITORIES = {'tencent/HY-Motion-1.0', 'Qwen/Qwen3-8B', 'openai/clip-vit-large-patch14'}


class UploadError(RuntimeError):
    pass


def read_plan(manifest_path, local_root):
    if manifest_path.stat().st_size > 1024 * 1024:
        raise UploadError('Asset manifest exceeds the allowed size')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        entries = manifest['files']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
            raise ValueError()
        plan, seen = [], set()
        root = local_root.resolve()
        for item in entries:
            relative, key = item['local_relative_path'], item['object_key']
            if (not isinstance(relative, str) or not relative.startswith('repo/ckpts/') or '\\' in relative
                    or any(not part or part in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9._-]+', part) for part in relative.split('/'))
                    or key != 'assets/hymotion/' + relative.removeprefix('repo/') or key in seen
                    or item['repo'] not in REPOSITORIES or not re.fullmatch('[a-f0-9]{40}', item['revision'])
                    or not re.fullmatch('[a-f0-9]{64}', item['sha256'])
                    or type(item['size_bytes']) is not int or not 0 < item['size_bytes'] <= PART_SIZE * 10000):
                raise ValueError()
            local = (root / relative).resolve()
            if not local.is_relative_to(root) or not local.is_file() or local.stat().st_size != item['size_bytes']:
                raise UploadError(f'Local file is missing or has changed size: {relative}')
            plan.append(dict(item, local_path=local))
            seen.add(key)
        if len(plan) != manifest['file_count'] or sum(item['size_bytes'] for item in plan) != manifest['total_bytes']:
            raise ValueError()
        return plan
    except UploadError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        raise UploadError('Asset manifest contains invalid paths, hashes, repositories or totals') from None


def verify_local(item):
    with item['local_path'].open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != item['sha256']:
        raise UploadError(f'Local SHA-256 differs: {item["local_relative_path"]}')


def make_bucket(bucket_name, region, internal=False):
    import oss2
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket_name)
            or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+', region)):
        raise UploadError('Invalid private OSS bucket or region')
    access_id = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_ID')
    access_secret = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
    token = os.getenv('ALIBABA_CLOUD_SECURITY_TOKEN')
    if not any((access_id, access_secret, token)):
        access_id, access_secret, token = (os.getenv('OSS_ACCESS_KEY_ID'), os.getenv('OSS_ACCESS_KEY_SECRET'), os.getenv('OSS_SESSION_TOKEN'))
    if any((access_id, access_secret, token)) and not (access_id and access_secret):
        raise UploadError('An environment credential pair is incomplete; do not mix providers')
    if access_id and access_secret:
        auth = oss2.StsAuth(access_id, access_secret, token) if token else oss2.Auth(access_id, access_secret)
    else:
        try:
            from alibabacloud_credentials.client import Client
            from oss2.credentials import Credentials
        except ImportError:
            raise UploadError('Provide existing environment credentials or install the official credentials SDK in the project cloud group') from None

        class Provider(oss2.CredentialsProvider):
            def __init__(self):
                self.client = Client()

            def get_credentials(self):
                value = self.client.get_credential()
                return Credentials(value.access_key_id, value.access_key_secret, value.security_token)

        # The official SDK's documented provider chain; never manually read or print CLI secret files.
        auth = oss2.ProviderAuth(Provider())
    suffix = '-internal' if internal else ''
    return oss2.Bucket(auth, f'https://oss-{region}{suffix}.aliyuncs.com', bucket_name, connect_timeout=60)


def verify_existing(bucket, item):
    """Return False only when absent; uncertain/unverifiable existing data must never be overwritten."""
    import oss2
    try:
        head = bucket.head_object(item['object_key'])
    except oss2.exceptions.NoSuchKey:
        return False
    if head.content_length != item['size_bytes'] or head.headers.get('x-oss-meta-sha256') != item['sha256']:
        raise UploadError(f'Existing OSS object is different or lacks the expected hash; refusing overwrite: {item["object_key"]}')
    result = bucket.get_object(item['object_key'])
    digest, total = hashlib.sha256(), 0
    try:
        while block := result.read(PART_SIZE):
            total += len(block)
            if total > item['size_bytes']:
                raise UploadError('Existing OSS object exceeded its declared size')
            digest.update(block)
    finally:
        result.close()
    if total != item['size_bytes'] or digest.hexdigest() != item['sha256']:
        raise UploadError(f'Existing OSS bytes failed verification; refusing overwrite: {item["object_key"]}')
    return True


def upload_one(bucket, item, *, part_size=PART_SIZE):
    import oss2
    if verify_existing(bucket, item):
        return 'verified_existing'
    # Opening before init avoids leaving a multipart upload when the local file cannot be read.
    with item['local_path'].open('rb') as stream:
        upload = bucket.init_multipart_upload(item['object_key'], headers={
            'x-oss-meta-sha256': item['sha256'], 'Content-Type': 'application/octet-stream'})
        upload_id, completed = upload.upload_id, False
        try:
            digest, total, parts = hashlib.sha256(), 0, []
            while block := stream.read(part_size):
                total += len(block)
                if total > item['size_bytes'] or len(parts) >= 10000:
                    raise UploadError('Local file grew or exceeds the multipart limit')
                digest.update(block)
                number = len(parts) + 1
                result = bucket.upload_part(item['object_key'], upload_id, number, block)
                parts.append(oss2.models.PartInfo(number, result.etag))
            if total != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                raise UploadError(f'Upload source failed size/SHA-256 validation: {item["local_relative_path"]}')
            # OSS publishes the object only after the full source checksum was checked.
            # A concurrent object created after the initial HEAD is never overwritten.
            bucket.complete_multipart_upload(item['object_key'], upload_id, parts,
                                             headers={'x-oss-forbid-overwrite': 'true'})
            completed = True
            if not verify_existing(bucket, item):
                raise UploadError('Completed OSS object could not be verified')
            return 'uploaded_and_verified'
        except BaseException:
            if not completed:
                try:
                    bucket.abort_multipart_upload(item['object_key'], upload_id)
                except Exception:
                    print('Multipart cleanup was not confirmed; inspect incomplete OSS uploads for this asset prefix.', file=sys.stderr)
            raise


def read_signed_plan(path, bucket, region, plan):
    """Validate every endpoint and signed header before any local bytes are sent."""
    if path.stat().st_size > 2 * 1024 * 1024:
        raise UploadError('Signed upload plan is too large')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value['bucket'] != bucket or value['region'] != region:
            raise ValueError()
        expires = value['expires_at_unix']
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time() + 30:
            raise UploadError('Signed upload plan has expired or is about to expire; obtain a new plan without re-uploading completed objects')
        signed = {}
        known = {item['object_key']: item for item in plan}
        for entry in value['files']:
            key = entry['object_key']
            if key in signed:
                raise ValueError()
            # A plan can include the full manifest when --max-files selects a local subset.
            if key not in known:
                continue
            expected_headers = {'content-type': 'application/octet-stream',
                                'x-oss-meta-sha256': known[key]['sha256'], 'x-oss-forbid-overwrite': 'true'}
            if {k.lower(): v for k, v in entry['headers'].items()} != expected_headers:
                raise ValueError()
            for label in ('put_url', 'head_url'):
                target = urlsplit(entry[label])
                if (target.scheme != 'https' or target.hostname != f'{bucket}.oss-{region}.aliyuncs.com'
                        or target.port is not None or target.username or target.password or target.fragment
                        or target.path != '/' + quote(key, safe='/') or not target.query):
                    raise ValueError()
            signed[key] = dict(entry, expires_at_unix=expires)
        if signed.keys() != known.keys():
            raise ValueError()
        return signed
    except UploadError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        raise UploadError('Signed plan has invalid destinations, paths, signed headers or missing objects') from None


def checked_stream(item, part_size=PART_SIZE, *, progress=None):
    """Do not send the final block until the entire stream matches the expected source hash."""
    digest, total = hashlib.sha256(), 0
    with item['local_path'].open('rb') as stream:
        pending = stream.read(part_size)
        while pending:
            following = stream.read(part_size)
            total += len(pending)
            digest.update(pending)
            if total > item['size_bytes'] or (total == item['size_bytes'] and following):
                raise UploadError('Local upload source grew during transfer')
            if not following and (total != item['size_bytes'] or digest.hexdigest() != item['sha256']):
                raise UploadError('Local upload source changed during transfer; final block was not sent')
            yield pending
            if progress is not None:
                progress(total)
            pending = following
    if total != item['size_bytes']:
        raise UploadError('Local upload source was truncated during transfer')


def signed_head_matches(client, entry, item):
    response = client.head(entry['head_url'])
    try:
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise UploadError(f'OSS HEAD rejected (HTTP {response.status_code}) for {item["local_relative_path"]}; check plan validity and permissions')
        if (response.headers.get('content-length') != str(item['size_bytes'])
                or response.headers.get('x-oss-meta-sha256') != item['sha256']):
            raise UploadError(f'Existing OSS object differs or lacks expected SHA metadata; refusing overwrite: {item["local_relative_path"]}')
        return True
    finally:
        response.close()


def upload_signed_one(item, entry, client):
    if time.time() + 30 >= entry['expires_at_unix']:
        raise UploadError('Signed plan expired before starting this file; obtain a new plan')
    if signed_head_matches(client, entry, item):
        return 'existing_size_and_sha256_metadata_match'
    # Check the whole original file before starting the request, then check it again while streaming.
    verify_local(item)
    headers = dict(entry['headers'], **{'Content-Length': str(item['size_bytes'])})
    last_report = time.monotonic()

    def progress(total):
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= 30:
            print(f'upload_streamed: {item["local_relative_path"]} ({total}/{item["size_bytes"]} bytes; awaiting OSS confirmation)', flush=True)
            last_report = now

    response = client.put(entry['put_url'], headers=headers, content=checked_stream(item, progress=progress))
    try:
        if response.status_code not in (200, 201):
            raise UploadError(f'OSS PUT rejected (HTTP {response.status_code}) for {item["local_relative_path"]}; no automatic retry was sent')
    finally:
        response.close()
    if not signed_head_matches(client, entry, item):
        raise UploadError('PUT result is uncertain because the completion HEAD did not find the object')
    return 'uploaded_source_sha256_verified_head_confirmed'


def upload_signed_files(plan, signed, workers):
    import httpx
    if workers not in (1, 2):
        raise UploadError('--workers must be 1 or 2')
    stopping = threading.Event()

    def one(item):
        if stopping.is_set():
            return
        try:
            # No redirects, .netrc, proxy environment or transport retry policy is inherited.
            with httpx.Client(follow_redirects=False, trust_env=False,
                              timeout=httpx.Timeout(connect=30, read=120, write=120, pool=30)) as client:
                status = upload_signed_one(item, signed[item['object_key']], client)
            print(f'{status}: {item["local_relative_path"]} ({item["size_bytes"]} bytes)', flush=True)
        except BaseException:
            stopping.set()
            raise

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = [executor.submit(one, item) for item in plan]
        try:
            for future in as_completed(pending):
                future.result()
        except BaseException:
            stopping.set()
            for future in pending:
                future.cancel()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('assets-manifest.json'))
    parser.add_argument('--local-root', type=Path, required=True)
    parser.add_argument('--bucket', help='Existing private OSS bucket; required with --upload')
    parser.add_argument('--region', default='cn-hangzhou')
    parser.add_argument('--internal', action='store_true', help='Use OSS internal endpoint only when running inside the same region')
    parser.add_argument('--upload', action='store_true', help='Enable real OSS uploads; default only verifies local files')
    parser.add_argument('--signed-plan', type=Path, help='Use scoped PUT/HEAD URLs with httpx; never load Alibaba Cloud credentials locally')
    parser.add_argument('--workers', type=int, default=2, help='Signed-plan upload concurrency, 1 or 2')
    parser.add_argument('--max-files', type=int, help='Process only the first N manifest entries for a controlled connectivity check')
    args = parser.parse_args(argv)
    try:
        plan = read_plan(args.manifest, args.local_root)
        if args.max_files is not None:
            if args.max_files < 1:
                raise UploadError('--max-files must be positive')
            plan = plan[:args.max_files]
        print(f'Plan: {len(plan)} files, {sum(item["size_bytes"] for item in plan)} bytes. Local streaming only; no second model copy.')
        if args.upload and not args.bucket:
            raise UploadError('--bucket is required with --upload')
        if args.signed_plan is not None:
            if not args.bucket or args.internal:
                raise UploadError('--signed-plan requires --bucket and public OSS endpoints (no --internal)')
            signed = read_signed_plan(args.signed_plan, args.bucket, args.region, plan)
            if args.upload:
                upload_signed_files(plan, signed, args.workers)
                print('Selected sources were streamed with SHA-256 verification; OSS HEAD confirmed size and SHA metadata. No full remote read-back was performed.')
                return 0
        bucket = make_bucket(args.bucket, args.region, args.internal) if args.upload else None
        for item in plan:
            if args.upload:
                status = upload_one(bucket, item)
            else:
                verify_local(item)
                status = 'local_sha256_verified'
            print(f'{status}: {item["local_relative_path"]} ({item["size_bytes"]} bytes)', flush=True)
        print('Selected assets verified in OSS.' if args.upload else 'Local check complete. No cloud request was sent.')
        return 0
    except UploadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Stopped. Completed OSS objects remain available; rerunning verifies them before skipping.', file=sys.stderr)
        return 130
    except Exception as exc:
        # SDK/provider diagnostics may contain credentials or signed URLs, so never interpolate them.
        print(f'Asset operation failed ({type(exc).__name__}). Check file access, credentials and OSS connectivity. Provider diagnostics were suppressed; existing objects were not intentionally overwritten.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
