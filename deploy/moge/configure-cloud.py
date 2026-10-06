"""Merge only the five non-secret cloud settings; do not restart or contact FC."""
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

DIRECTORY = Path('/etc/hymotion-webui')
FIELDS = ('MOTION_REMOTE_ENDPOINT', 'MOTION_REMOTE_OSS_BUCKET', 'MOTION_REMOTE_OSS_REGION',
          'MOTION_REMOTE_OSS_PREFIX', 'MOTION_REMOTE_TIMEOUT_SECONDS')


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != set(FIELDS):
        raise ValueError('Exactly five cloud setting fields are required')
    if any(not isinstance(value, str) or not value or any(char in value for char in '\r\n\0') for value in settings.values()):
        raise ValueError('Setting values must be single-line strings')
    region = settings['MOTION_REMOTE_OSS_REGION']
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+', region):
        raise ValueError('Invalid region')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', settings['MOTION_REMOTE_OSS_BUCKET']):
        raise ValueError('Invalid bucket')
    if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', settings['MOTION_REMOTE_OSS_PREFIX']):
        raise ValueError('Invalid prefix')
    parsed = urlsplit(settings['MOTION_REMOTE_ENDPOINT'])
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port is not None
            or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.' + re.escape(region) + r'\.fcapp\.run', parsed.hostname or '')):
        raise ValueError('Invalid regional FC endpoint')
    if not settings['MOTION_REMOTE_TIMEOUT_SECONDS'].isdigit() or not 60 <= int(settings['MOTION_REMOTE_TIMEOUT_SECONDS']) <= 86460:
        raise ValueError('Invalid timeout')


def main():
    if os.geteuid() != 0:
        raise ValueError('Root required')
    spec = importlib.util.spec_from_file_location('credential_import', Path(__file__).with_name('import-credentials.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    text = sys.stdin.read(16385)
    if len(text) > 16384:
        raise ValueError('Settings document too large')
    settings = json.loads(text, object_pairs_hook=helper.unique_object)
    validate(settings)
    destination = DIRECTORY / 'app.env'
    old = helper.private_file(destination, 65536).decode('utf-8')
    kept_lines = [line for line in old.splitlines() if line.partition('=')[0].strip() not in FIELDS]
    updated = '\n'.join(kept_lines + [key + '=' + settings[key] for key in FIELDS]) + '\n'
    helper.atomic_private_write(destination, updated.encode('utf-8'))
    print(json.dumps({'settings_configured': list(FIELDS), 'other_environment_preserved': True, 'service_restarted': False, 'fc_called': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Cloud settings update failed. Configuration values and diagnostic details were suppressed.', file=sys.stderr)
        raise SystemExit(1) from None
