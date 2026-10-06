"""Private OSS persistence using the FC execution role, never caller-supplied paths."""
from __future__ import annotations

import hashlib
import json
import os
import re

from contract import RequestError, canonical_bytes


class OSSStore:
    def __init__(self, headers):
        import oss2
        bucket = os.getenv('HYMOTION_OSS_BUCKET', '')
        region = os.getenv('HYMOTION_OSS_REGION', 'cn-hangzhou')
        prefix = os.getenv('HYMOTION_RESULTS_PREFIX', 'results/hymotion')
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket)
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+', region)
                or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', prefix)):
            raise RequestError(503, 'storage_not_configured', 'Private OSS storage is not configured')
        values = [headers.get(name, '') for name in ('x-fc-access-key-id', 'x-fc-access-key-secret', 'x-fc-security-token')]
        if not all(values):
            raise RequestError(503, 'role_credentials_missing', 'FC execution-role credentials are missing')
        # FC injects these headers in custom containers. The port must only be reachable through the signed FC gateway.
        auth = oss2.StsAuth(*values)
        self.bucket = oss2.Bucket(auth, f'https://oss-{region}-internal.aliyuncs.com', bucket, connect_timeout=30)
        self.public_bucket = oss2.Bucket(auth, f'https://oss-{region}.aliyuncs.com', bucket, connect_timeout=30)
        self.prefix = prefix
        self.oss2 = oss2

    def key(self, job, name):
        return f'{self.prefix}/{job}/{name}'

    def get_json(self, job, name):
        try:
            result = self.bucket.get_object(self.key(job, name))
            try:
                data = result.read(1024 * 1024 + 1)
            finally:
                result.close()
            if len(data) > 1024 * 1024:
                raise ValueError('Oversized stored metadata')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError('Invalid stored metadata')
            return value
        except self.oss2.exceptions.NoSuchKey:
            return None

    def put_json(self, job, name, value, *, create_only=True):
        content = canonical_bytes(value)
        headers = {'Content-Type': 'application/json'}
        if create_only:
            headers['x-oss-forbid-overwrite'] = 'true'
        try:
            self.bucket.put_object(self.key(job, name), content, headers=headers)
        except self.oss2.exceptions.OssError as exc:
            if create_only and exc.status == 409 and exc.code == 'FileAlreadyExists':
                return False
            raise
        if self.get_json(job, name) != value:
            raise OSError('OSS metadata verification failed')
        return True

    def commit_file(self, job, path):
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        size = path.stat().st_size
        key = self.key(job, path.name)
        self.bucket.put_object_from_file(key, str(path), headers={
            'x-oss-forbid-overwrite': 'true', 'x-oss-meta-sha256': digest,
            'Content-Type': 'application/octet-stream', 'Cache-Control': 'private, no-store'})
        # A remote read, rather than an OSS mount cache read, confirms durable bytes before the manifest is published.
        result = self.bucket.get_object(key)
        saved, count = hashlib.sha256(), 0
        try:
            while block := result.read(1024 * 1024):
                saved.update(block)
                count += len(block)
        finally:
            result.close()
        if count != size or saved.hexdigest() != digest:
            raise OSError('OSS artifact verification failed')
        return {'size': size, 'sha256': digest, 'object_key': key}

    def with_urls(self, manifest):
        # Only URLs are refreshed on an idempotent completed retry; existing motion bytes never change.
        return dict(manifest, artifacts={name: dict(info, url=self.public_bucket.sign_url(
            'GET', info['object_key'], 3600, slash_safe=True)) for name, info in manifest['artifacts'].items()})
