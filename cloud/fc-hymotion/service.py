"""Synchronous FC HTTP service with immutable OSS job reservations and lazy GPU loading."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from contract import FILES, JOB_RE, MAX_FILE_BYTES, REQUIRED_FILES, RequestError, canonical_bytes, validate_request
from storage import OSSStore

ROOT = Path(__file__).resolve().parent
GATE = threading.Lock()


def preflight():
    assets = Path(os.getenv('HYMOTION_ASSETS_MOUNT', '/mnt/hymotion-assets')).resolve()
    source = Path(os.getenv('HYMOTION_OFFICIAL_SOURCE', '/opt/hymotion')).resolve()
    required = [assets / name for name in (
        'ckpts/tencent/HY-Motion-1.0/config.yml', 'ckpts/tencent/HY-Motion-1.0/latest.ckpt',
        'ckpts/Qwen3-8B/config.json', 'ckpts/Qwen3-8B/tokenizer.json',
        'ckpts/Qwen3-8B/tokenizer_config.json', 'ckpts/Qwen3-8B/model.safetensors.index.json',
        'ckpts/clip-vit-large-patch14/config.json', 'ckpts/clip-vit-large-patch14/model.safetensors',
        'ckpts/clip-vit-large-patch14/tokenizer_config.json', 'ckpts/clip-vit-large-patch14/vocab.json',
        'ckpts/clip-vit-large-patch14/merges.txt')]
    required += [source / name for name in ('stats/Mean.npy', 'stats/Std.npy',
        'hymotion/utils/t2m_runtime.py', 'assets/wooden_models/boy_Rigging_smplx_tex.fbx',
        'scripts/gradio/templates/index_wooden_static.html')]
    try:
        index = json.loads((assets / 'ckpts/Qwen3-8B/model.safetensors.index.json').read_text(encoding='utf-8'))
        shards = set(index['weight_map'].values())
        if not shards or any(not isinstance(name, str) or '/' in name or '\\' in name or not name.endswith('.safetensors') or '..' in name for name in shards):
            return False
        required += [assets / 'ckpts/Qwen3-8B' / name for name in shards]
        if not os.path.ismount(assets) or (source / 'ckpts').resolve() != assets / 'ckpts':
            return False
        for path in required:
            if not path.is_file() or path.stat().st_size == 0:
                return False
            if path.stat().st_size < 1024 and path.read_bytes().startswith(b'version https://git-lfs.github.com/spec/v1'):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def existing_result(store, job, digest):
    request = store.get_json(job, 'request.json')
    if request is None:
        return None
    if request.get('request_sha256') != digest:
        raise RequestError(409, 'job_conflict', 'job_id belongs to a different request')
    manifest = store.get_json(job, 'manifest.json')
    if manifest is not None:
        if manifest.get('status') != 'complete' or manifest.get('request_sha256') != digest:
            raise RequestError(409, 'job_incomplete', 'Stored completion metadata is inconsistent')
        return store.with_urls(manifest)
    failure = store.get_json(job, 'failure.json')
    raise RequestError(409, 'job_failed' if failure is not None else 'job_incomplete',
                       'Job already started or failed; inspect persisted state. No automatic regeneration is allowed')


def terminate_worker(process):
    if process.poll() is None:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def run_worker(work):
    # Do not leak cloud execution credentials, OSS URLs or local HF tokens into the inference subprocess/logs.
    worker_env = {key: value for key, value in os.environ.items()
                  if not any(word in key.upper() for word in ('ACCESS_KEY', 'SECRET', 'TOKEN', 'CREDENTIAL'))}
    timeout = int(os.getenv('HYMOTION_WORKER_TIMEOUT_SECONDS', '1740'))
    if not 1 <= timeout <= 86300:
        raise RequestError(503, 'invalid_timeout', 'Worker timeout is invalid')
    with (work / 'generation.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-B', '-u', str(ROOT / 'worker.py'), str(work)],
                                   stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=worker_env,
                                   start_new_session=os.name == 'posix')
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_worker(process)
            raise RequestError(504, 'worker_timeout', 'Worker timed out and was terminated; inspect this job before retrying') from None
        except BaseException:
            terminate_worker(process)
            raise
    if result != 0:
        raise RequestError(500, 'worker_failed', 'Official GPU worker failed; inspect the private OSS job log')


def generate(payload, store, worker=run_worker, check=preflight):
    job, config, digest = validate_request(payload)
    existing = existing_result(store, job, digest)
    if existing is not None:
        return existing
    if not GATE.acquire(blocking=False):
        raise RequestError(429, 'busy', 'A generation is already running')
    reserved = False
    started = time.time()
    try:
        if not check():
            raise RequestError(503, 'preflight_failed', 'Official model files or persistent mount are missing')
        # Conditional OSS PUT is the durable reservation; uncertain writes never start inference.
        reservation = {'job_id': job, 'config': config, 'request_sha256': digest, 'started_at_unix': started}
        if not store.put_json(job, 'request.json', reservation):
            completed = existing_result(store, job, digest)
            if completed is None:
                raise RequestError(409, 'job_incomplete', 'A reservation conflict has an uncertain stored state; do not automatically retry')
            return completed
        reserved = True
        with tempfile.TemporaryDirectory(prefix='hymotion-') as folder:
            work = Path(folder)
            (work / 'request.json').write_bytes(canonical_bytes(config))
            try:
                worker(work)
                for name in REQUIRED_FILES:
                    if not (work / name).is_file():
                        raise RequestError(500, 'artifacts_missing', 'Official runtime did not export all required artifacts')
                artifacts = {}
                for name in FILES:
                    path = work / name
                    if path.is_file():
                        if path.is_symlink() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
                            raise RequestError(500, 'invalid_artifact', 'Worker artifact size or type is invalid')
                        artifacts[name] = store.commit_file(job, path)
                report = json.loads((work / 'generation_report.json').read_text(encoding='utf-8'))
                manifest = {'job_id': job, 'status': 'complete', 'request_sha256': digest,
                            'backend': 'tencent_hymotion_official', 'model': 'Tencent HY-Motion-1.0 standard',
                            'frames': report['frames'], 'fps': report['fps'], 'device': 'cuda:0',
                            'elapsed_seconds': round(time.time() - started, 3), 'artifacts': artifacts}
                if not store.put_json(job, 'manifest.json', manifest):
                    raise RequestError(409, 'manifest_conflict', 'Completion metadata already exists; inspect the original job')
                return store.with_urls(manifest)
            except Exception:
                # Best effort logs cannot replace the final immutable completion marker.
                log_path = work / 'generation.log'
                if log_path.is_file() and 0 < log_path.stat().st_size <= MAX_FILE_BYTES:
                    try:
                        store.commit_file(job, log_path)
                    except Exception:
                        pass
                raise
    except Exception as exc:
        if reserved:
            try:
                if store.get_json(job, 'manifest.json') is None:
                    store.put_json(job, 'failure.json', {'job_id': job, 'status': 'failed', 'request_sha256': digest,
                        'code': exc.code if isinstance(exc, RequestError) else 'storage_or_worker_failure',
                        'detail': 'Generation or persistence did not complete. Do not automatically regenerate this job.'})
            except Exception:
                # A persisted reservation without manifest is intentionally left incomplete after storage failure.
                pass
        raise
    finally:
        GATE.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'HYMotionFC/1'

    def log_message(self, *_args):
        pass  # HTTP headers, bodies and signed download URLs are never logged.

    def send_json(self, status, value):
        body = canonical_bytes(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authenticate(self):
        if os.getenv('HYMOTION_TRUST_FC_AUTH', '0') != '1':
            raise RequestError(503, 'auth_not_configured', 'Signed FC gateway authentication must be configured before enabling the service')

    def handle_error(self, exc):
        try:
            if isinstance(exc, RequestError):
                self.send_json(exc.status, {'status': 'error', 'code': exc.code, 'detail': exc.detail})
            else:
                self.send_json(500, {'status': 'error', 'code': 'internal_failure', 'detail': 'Storage or worker operation failed; inspect the original job ID'})
        except (BrokenPipeError, ConnectionError):
            pass

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == '/healthz':
                self.send_json(200, {'status': 'ok', 'model_loaded': False, 'inference_verified': False})
                return
            self.authenticate()
            if path.startswith('/v1/jobs/') and JOB_RE.fullmatch(path.removeprefix('/v1/jobs/')):
                job = path.removeprefix('/v1/jobs/')
                store = OSSStore(self.headers)
                request = store.get_json(job, 'request.json')
                if request is None:
                    raise RequestError(404, 'not_found', 'Job not found')
                self.send_json(200, existing_result(store, job, request['request_sha256']))
                return
            raise RequestError(404, 'not_found', 'Route not found')
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self):
        try:
            self.authenticate()
            if self.path != '/v1/generate':
                raise RequestError(404, 'not_found', 'Route not found')
            length = self.headers.get('Content-Length', '')
            if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 32768:
                raise RequestError(400, 'invalid_request', 'Expected a bounded JSON request body')
            self.connection.settimeout(30)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise RequestError(400, 'invalid_request', 'Truncated request body')
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RequestError(400, 'invalid_request', 'Invalid JSON') from None
            validate_request(payload)
            self.send_json(200, generate(payload, OSSStore(self.headers)))
        except Exception as exc:
            self.handle_error(exc)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '9000'))), Handler).serve_forever()
