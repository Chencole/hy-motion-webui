"""Server-only FC client. Import/inspection never contacts FC or loads a model."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
from urllib.parse import quote, urlsplit

MODEL_NAME = 'Tencent HY-Motion-1.0 (standard, FC GPU)'
JOB_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
FILES = frozenset(('motion_000.npz', 'motion_meta.json', 'motion_000.fbx',
                   'motion_000.txt', 'preview.html', 'generation_report.json', 'generation.log'))
REQUIRED_FILES = frozenset(('motion_000.npz', 'motion_meta.json', 'motion_000.fbx', 'generation_report.json'))
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class RemoteGenerationError(RuntimeError):
    """Safe to display: raw HTTP bodies, URLs and SDK exceptions are never included."""


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def normalize_config(config):
    if not isinstance(config, dict) or set(config) - {'prompt', 'seconds', 'seed', 'steps', 'threads'}:
        raise ValueError('Expected prompt, seconds, seed, steps and threads only')
    prompt = config.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or '\0' in prompt or len(prompt) > 4000:
        raise ValueError('prompt must contain 1..4000 characters without NUL')
    seconds = config.get('seconds', 4.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 12:
        raise ValueError('seconds must be in (0, 12]')
    result = {'prompt': prompt, 'seconds': float(seconds)}
    for key, default, lower, upper in [('seed', 42, 0, 2**32 - 1), ('steps', 50, 1, 200), ('threads', 8, 1, 64)]:
        value = config.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f'{key} must be an integer in {lower}..{upper}')
        result[key] = value
    return result


def _settings():
    region = os.getenv('MOTION_REMOTE_OSS_REGION', 'cn-hangzhou')
    bucket = os.getenv('MOTION_REMOTE_OSS_BUCKET', '')
    prefix = os.getenv('MOTION_REMOTE_OSS_PREFIX', 'results/hymotion')
    endpoint = os.getenv('MOTION_REMOTE_ENDPOINT', '').rstrip('/')
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+', region):
        raise ValueError('MOTION_REMOTE_OSS_REGION is invalid')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ValueError('MOTION_REMOTE_OSS_BUCKET must name the private OSS bucket')
    if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', prefix):
        raise ValueError('MOTION_REMOTE_OSS_PREFIX is invalid')
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError('MOTION_REMOTE_ENDPOINT is invalid') from None
    if (parsed.scheme != 'https' or parsed.username or parsed.password or port is not None
            or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.' + re.escape(region) + r'\.fcapp\.run', parsed.hostname or '')):
        raise ValueError('MOTION_REMOTE_ENDPOINT must be the HTTPS root URL of the HY-Motion FC HTTP trigger in the configured region')
    timeout = int(os.getenv('MOTION_REMOTE_TIMEOUT_SECONDS', '1820'))
    if not 60 <= timeout <= 86460:
        raise ValueError('MOTION_REMOTE_TIMEOUT_SECONDS must be in 60..86460')
    return {'endpoint': endpoint, 'host': parsed.hostname, 'bucket': bucket, 'region': region,
            'prefix': prefix, 'timeout': timeout}


def inspect_remote():
    """Only inspect configuration and module availability; never resolve credentials over a network."""
    missing = []
    conf = None
    try:
        conf = _settings()
    except (ValueError, TypeError):
        missing.append('valid MOTION_REMOTE_ENDPOINT / OSS_BUCKET / OSS_REGION / OSS_PREFIX / TIMEOUT_SECONDS')
    present = {key: bool(os.getenv(key)) for key in ('ALIBABA_CLOUD_ACCESS_KEY_ID', 'ALIBABA_CLOUD_ACCESS_KEY_SECRET', 'ALIBABA_CLOUD_SECURITY_TOKEN')}
    missing.extend(key for key in ('ALIBABA_CLOUD_ACCESS_KEY_ID', 'ALIBABA_CLOUD_ACCESS_KEY_SECRET') if not present[key])
    for module in ('alibabacloud_credentials', 'alibabacloud_openapi_util'):
        if importlib.util.find_spec(module) is None:
            missing.append(module)
    return {'ready': not missing, 'model': MODEL_NAME, 'mode': 'fc', 'missing': missing,
            'message': ('FC client configuration is present; this configuration-only check does not invoke the GPU or test generation.' if not missing
                        else 'Configure the HY-Motion FC endpoint, private OSS bucket and server credentials; install the project dependencies.'),
            'credentials_present': present, 'inference_verified': False,
            'root': str(Path(__file__).resolve().parent), 'python': sys.executable,
            'region': conf['region'] if conf else None,
            'limitations': ['Inspection checks local configuration only and does not invoke FC or verify generation.',
                             'FC must have minimum instances 0, delayed release off, and maximum concurrency 1.',
                             'A timeout or disconnect is an uncertain result; the client never automatically resubmits generation.',
                             'OSS storage and network charges are separate from GPU usage.']}


def _signed_headers(host, body):
    try:
        from alibabacloud_credentials.client import Client
        from alibabacloud_credentials.models import Config
        from alibabacloud_openapi_util.client import Client as Signer
        from Tea.request import TeaRequest
        access_id = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_ID', '')
        access_secret = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET', '')
        token = os.getenv('ALIBABA_CLOUD_SECURITY_TOKEN', '')
        if not access_id or not access_secret:
            raise ValueError('Missing server credentials')
        # Explicit environment credentials: no hidden CLI profiles, metadata probes or remote providers.
        credential = Client(Config(type='sts' if token else 'access_key', access_key_id=access_id,
                                   access_key_secret=access_secret, security_token=token)).get_credential()
        headers = {'content-type': 'application/json', 'content-length': str(len(body)),
                   'x-acs-date': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                   'x-acs-security-token': credential.security_token or ''}
        request = TeaRequest()
        request.method, request.pathname, request.query, request.headers = 'POST', '/v1/generate', {}, headers
        # FC's official HTTP-trigger example uses an empty hashed-payload argument.
        headers['authorization'] = Signer.get_authorization(request, 'ACS3-HMAC-SHA256', '',
                                                            credential.access_key_id, credential.access_key_secret)
        return headers
    except Exception:
        raise RemoteGenerationError('Unable to sign the FC request; check server-only Alibaba Cloud credentials and SDK dependencies.') from None


def _invoke(conf, payload):
    body = canonical_bytes(payload)
    headers = _signed_headers(conf['host'], body)
    connection = http.client.HTTPSConnection(conf['host'], timeout=conf['timeout'], context=ssl.create_default_context())
    try:
        connection.request('POST', '/v1/generate', body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RemoteGenerationError('FC returned an oversized response; execution state is uncertain. No retry was sent.')
        if response.status != 200:
            # Never echo provider diagnostics, signed URLs, headers or SDK exception text.
            explanations = {409: 'Job ID already exists with a conflicting, failed or incomplete request.',
                            429: 'GPU function is busy; no automatic retry was sent.',
                            503: 'GPU function configuration or model storage is not ready.',
                            504: 'GPU request timed out; inspect the original job ID before retrying.',
                            401: 'FC authentication was rejected.', 403: 'FC invocation was not authorized.'}
            raise RemoteGenerationError(f'FC HTTP {response.status}. ' + explanations.get(response.status,
                                        'Generation failed or its result is uncertain; inspect the original job ID.'))
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise RemoteGenerationError('FC returned invalid JSON; no retry was sent.') from None
    except (OSError, http.client.HTTPException, socket.timeout):
        raise RemoteGenerationError('FC connection ended without a confirmed result. No retry was sent; inspect the original job ID.') from None
    finally:
        connection.close()


def validate_manifest(manifest, conf, job_id, digest):
    if (not isinstance(manifest, dict) or manifest.get('job_id') != job_id or manifest.get('status') != 'complete'
            or manifest.get('request_sha256') != digest or manifest.get('backend') != 'tencent_hymotion_official'):
        raise RemoteGenerationError('FC completion manifest does not match the requested HY-Motion job.')
    artifacts = manifest.get('artifacts')
    if not isinstance(artifacts, dict) or not REQUIRED_FILES <= artifacts.keys() or not artifacts.keys() <= FILES:
        raise RemoteGenerationError('FC manifest contains missing or unsupported artifacts.')
    total = 0
    for name, info in artifacts.items():
        if not isinstance(info, dict):
            raise RemoteGenerationError('Invalid artifact metadata.')
        size, sha = info.get('size'), info.get('sha256')
        if type(size) is not int or not 0 < size <= MAX_FILE_BYTES or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
            raise RemoteGenerationError('Invalid artifact size or SHA-256.')
        expected_key = f"{conf['prefix']}/{job_id}/{name}"
        if info.get('object_key') != expected_key or not isinstance(info.get('url'), str):
            raise RemoteGenerationError('Artifact object key does not belong to this job.')
        try:
            parsed = urlsplit(info['url'])
            if (parsed.scheme != 'https' or parsed.hostname != f"{conf['bucket']}.oss-{conf['region']}.aliyuncs.com"
                    or parsed.port is not None or parsed.username or parsed.password or parsed.fragment
                    or parsed.path != '/' + quote(expected_key, safe='/') or not parsed.query):
                raise ValueError()
        except ValueError:
            raise RemoteGenerationError('Artifact download URL is outside the configured private OSS job directory.') from None
        total += size
    if total > MAX_TOTAL_BYTES:
        raise RemoteGenerationError('Artifact set exceeds the download limit.')
    return artifacts


def _download(info, destination):
    parsed = urlsplit(info['url'])
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=120, context=ssl.create_default_context())
    try:
        # Direct connection without redirects: no credentials or requests can escape the validated OSS host.
        connection.request('GET', parsed.path + '?' + parsed.query, headers={'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding', 'identity') not in ('', 'identity'):
            raise RemoteGenerationError('OSS download was rejected or expired; no generation retry was sent.')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdigit() or int(length) != info['size']):
            raise RemoteGenerationError('OSS Content-Length does not match the artifact manifest.')
        digest, received = hashlib.sha256(), 0
        with destination.open('xb') as stream:
            while block := response.read(min(1024 * 1024, info['size'] - received + 1)):
                received += len(block)
                if received > info['size']:
                    raise RemoteGenerationError('OSS artifact exceeded its declared size.')
                stream.write(block)
                digest.update(block)
        if received != info['size'] or digest.hexdigest() != info['sha256']:
            raise RemoteGenerationError('OSS artifact size or SHA-256 verification failed.')
    except (OSError, http.client.HTTPException):
        raise RemoteGenerationError('OSS artifact download failed; no generation retry was sent.') from None
    finally:
        connection.close()


def generate_remote(job_id, config, output_dir, log_callback=None, *, recovery_manifest=None):
    if not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
        raise ValueError('Invalid job_id')
    normalized = normalize_config(config)
    conf = _settings()
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise RemoteGenerationError('Output directory already exists; preserve and inspect it before another request.')
    output.parent.mkdir(parents=True, exist_ok=True)
    log = log_callback or (lambda _message: None)
    digest = hashlib.sha256(canonical_bytes(normalized)).hexdigest()
    if recovery_manifest is None:
        log('Submitting one HY-Motion generation request to FC. Automatic retries are disabled.')
        manifest = _invoke(conf, {'job_id': job_id, 'config': normalized})
    else:
        recovery_path = Path(recovery_manifest)
        if recovery_path.stat().st_size > MAX_RESPONSE_BYTES:
            raise RemoteGenerationError('Recovery manifest is too large.')
        try:
            manifest = json.loads(recovery_path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            raise RemoteGenerationError('Recovery manifest is not valid JSON.') from None
        log('Recovering verified OSS downloads from the saved manifest. No FC request will be sent.')
    artifacts = validate_manifest(manifest, conf, job_id, digest)
    # Keep the signed URLs server-side for manual recovery, never print them or expose them through the UI.
    recovery = output.parent / 'remote-recovery.json'
    # Create with owner-only permissions from the first byte, before any signed URL reaches disk.
    descriptor = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        if os.name == 'posix':
            os.fchmod(stream.fileno(), 0o600)
        json.dump(manifest, stream, ensure_ascii=False, allow_nan=False)
    try:
        with tempfile.TemporaryDirectory(prefix='.hymotion-download-', dir=output.parent) as folder:
            staged = Path(folder)
            for name, info in artifacts.items():
                _download(info, staged / name)
                log(f'Verified {name} ({info["size"]} bytes).')
            sanitized = {key: manifest[key] for key in ('job_id', 'status', 'request_sha256', 'backend',
                         'model', 'frames', 'fps', 'device', 'elapsed_seconds') if key in manifest}
            sanitized['artifacts'] = {name: {key: info[key] for key in ('size', 'sha256', 'object_key')}
                                      for name, info in artifacts.items()}
            (staged / 'remote_manifest.json').write_bytes(canonical_bytes(sanitized))
            # The output appears only after every artifact was verified. rename must not merge with an existing directory.
            if output.exists():
                raise RemoteGenerationError('Output directory appeared during download; refusing to overwrite it.')
            staged.rename(output)
        recovery.unlink(missing_ok=True)
        log('All outputs were downloaded directly from OSS and verified. No additional GPU requests were sent.')
        return sanitized
    except RemoteGenerationError:
        log('Generation is committed in OSS, but local download was not completed. Do not create a new generation to recover files.')
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Submit one job to the configured HY-Motion FC function, then verify OSS downloads.')
    parser.add_argument('--job-id')
    parser.add_argument('--request', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--recover', type=Path, help='Download from a saved remote-recovery.json; never invoke FC')
    parser.add_argument('--check', action='store_true', help='Inspect local configuration only; never send an HTTP request')
    args = parser.parse_args(argv)
    if args.check:
        state = inspect_remote()
        print(json.dumps(state, ensure_ascii=False))
        return 0 if state['ready'] else 2
    if not args.job_id or args.request is None or args.output is None:
        parser.error('--job-id, --request and --output are required')
    try:
        if args.request.stat().st_size > 32768:
            raise ValueError('Request JSON is too large')
        config = json.loads(args.request.read_text(encoding='utf-8-sig'))
        generate_remote(args.job_id, config, args.output, log_callback=lambda message: print(message, flush=True), recovery_manifest=args.recover)
        return 0
    except (RemoteGenerationError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except Exception:
        print('Remote client failed locally; inspect the original job before resubmitting. Diagnostic secrets were suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
