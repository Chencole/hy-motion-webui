"""One-time RSA-OAEP credential import. Never print plaintext or provider errors."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DIRECTORY = Path('/etc/hymotion-webui')
FIELDS = ('ALIBABA_CLOUD_ACCESS_KEY_ID', 'ALIBABA_CLOUD_ACCESS_KEY_SECRET', 'ALIBABA_CLOUD_SECURITY_TOKEN')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


def decrypt_credentials(private_key, ciphertext):
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size != 4096:
        raise ValueError('Unexpected key type')
    if len(ciphertext) != 512:
        raise ValueError('Unexpected ciphertext length')
    plaintext = private_key.decrypt(ciphertext, padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    credentials = json.loads(plaintext.decode('utf-8'), object_pairs_hook=unique_object)
    if not isinstance(credentials, dict) or set(credentials) - set(FIELDS):
        raise ValueError('Unexpected credential fields')
    for field in FIELDS:
        value = credentials.get(field, '')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+/=.\-]*', value):
            raise ValueError('Invalid credential value')
        if field != FIELDS[2] and not 16 <= len(value) <= 256:
            raise ValueError('Missing or invalid required credential')
        credentials[field] = value
    return credentials


def merge_environment(existing, credentials):
    lines = [line for line in existing.splitlines() if line.partition('=')[0].strip() not in FIELDS]
    return '\n'.join(lines + [field + '=' + credentials[field] for field in FIELDS]) + '\n'


def private_file(path, max_size):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > max_size:
            raise ValueError('Unsafe input file permissions or size')
        return stream.read()


def atomic_private_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(ciphertext_path):
    import fcntl
    if os.geteuid() != 0:
        raise ValueError('Root required')
    directory_info = DIRECTORY.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != 0 or stat.S_IMODE(directory_info.st_mode) & 0o077:
        raise ValueError('Unsafe configuration directory')
    ciphertext_path = Path(ciphertext_path)
    if ciphertext_path.parent.resolve() != DIRECTORY or ciphertext_path.name in {'app.env', 'login.txt', 'credential-import-private.pem'}:
        raise ValueError('Place ciphertext in the private configuration directory')
    lock_descriptor = os.open(DIRECTORY / 'credential-import.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_descriptor, 'w') as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = DIRECTORY / 'credential-import-completed.json'
        if marker.exists():
            raise ValueError('One-time import already consumed')
        key_path = DIRECTORY / 'credential-import-private.pem'
        private_key = serialization.load_pem_private_key(private_file(key_path, 8192), password=None)
        credentials = decrypt_credentials(private_key, private_file(ciphertext_path, 512))
        environment_path = DIRECTORY / 'app.env'
        existing = private_file(environment_path, 65536).decode('utf-8')
        merged = merge_environment(existing, credentials)
        public_der = private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        receipt = json.dumps({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'public_key_sha256': hashlib.sha256(public_der).hexdigest(),
            'fields_present': {field: bool(credentials[field]) for field in FIELDS},
            'service_restarted': False,
        }, indent=2).encode('utf-8')
        atomic_private_write(environment_path, merged.encode('utf-8'))
        key_path.unlink()
        atomic_private_write(marker, receipt)
    print('Credential import complete. Configuration is root-only; one-time private key removed. Service was not restarted.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ciphertext', help='Root-only raw 512-byte RSA-OAEP ciphertext under /etc/hymotion-webui')
    arguments = parser.parse_args()
    try:
        run(arguments.ciphertext)
    except Exception:
        print('Credential import failed. Plaintext and diagnostic details were suppressed; inspect file ownership, format and import status.', file=sys.stderr)
        raise SystemExit(1) from None
