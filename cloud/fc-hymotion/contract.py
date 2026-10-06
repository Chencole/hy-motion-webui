"""Small transport contract; no model or cloud SDK imports."""
import hashlib
import json
import math
import re

JOB_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
FILES = ('motion_000.npz', 'motion_meta.json', 'motion_000.fbx', 'motion_000.txt',
         'preview.html', 'generation_report.json', 'generation.log')
REQUIRED_FILES = ('motion_000.npz', 'motion_meta.json', 'motion_000.fbx', 'generation_report.json')
MAX_FILE_BYTES = 256 * 1024 * 1024
UPSTREAM_COMMIT = '4e426f5a1021cbcf7f375458c37b840ee7225229'


class RequestError(Exception):
    def __init__(self, status, code, detail):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) != {'job_id', 'config'}:
        raise RequestError(400, 'invalid_request', 'Expected job_id and config only')
    job, config = payload['job_id'], payload['config']
    if not isinstance(job, str) or not JOB_RE.fullmatch(job):
        raise RequestError(400, 'invalid_request', 'Invalid job_id')
    if not isinstance(config, dict) or set(config) - {'prompt', 'seconds', 'seed', 'steps', 'threads'}:
        raise RequestError(400, 'invalid_request', 'Unsupported configuration field')
    prompt = config.get('prompt')
    seconds = config.get('seconds', 4.0)
    if not isinstance(prompt, str) or not prompt.strip() or '\0' in prompt or len(prompt) > 4000:
        raise RequestError(400, 'invalid_request', 'Invalid prompt')
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 12:
        raise RequestError(400, 'invalid_request', 'seconds must be in (0, 12]')
    normalized = {'prompt': prompt, 'seconds': float(seconds)}
    for key, default, lower, upper in [('seed', 42, 0, 2**32 - 1), ('steps', 50, 1, 200), ('threads', 8, 1, 64)]:
        value = config.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise RequestError(400, 'invalid_request', f'{key} must be an integer in {lower}..{upper}')
        normalized[key] = value
    return job, normalized, hashlib.sha256(canonical_bytes(normalized)).hexdigest()
