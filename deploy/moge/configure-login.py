"""Create a root-only service environment from the supplied login file."""
import hashlib
import os
from pathlib import Path

assert os.geteuid() == 0, "Run as root"
directory = Path('/etc/hymotion-webui')
login = directory / 'login.txt'
values = dict(line.split('=', 1) for line in login.read_text().splitlines() if '=' in line)
assert values.get('username') == 'hymotion'
assert len(values.get('password', '')) >= 40
destination = directory / 'app.env'
content = '\n'.join([
    'MOTION_MODE=hosted',
    'MOTION_BACKEND=fc',
    'MOTION_DATA_DIR=/var/lib/hymotion-webui',
    'MOTION_ROOT_PATH=/motion',
    'MOTION_AUTH_USER=hymotion',
    'MOTION_AUTH_PASSWORD_SHA256=' + hashlib.sha256(values['password'].encode()).hexdigest(),
    '',
])
descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    stream.write(content)
os.chmod(login, 0o600)
print('Created root-only service configuration; cloud endpoint and credentials remain unset.')
