"""Synthetic-only tests; never read the production private key or configuration."""
import importlib.util
import json
from pathlib import Path
import unittest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

spec = importlib.util.spec_from_file_location('credential_import', Path(__file__).with_name('import-credentials.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=4096)

    def encrypt(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(',', ':'))
        return self.key.public_key().encrypt(text.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))

    def credentials(self):
        return {module.FIELDS[0]: 'SYNTHETIC_ACCESS_ID_1234', module.FIELDS[1]: 'SYNTHETIC_SECRET_12345678'}

    def test_roundtrip_and_optional_token(self):
        decoded = module.decrypt_credentials(self.key, self.encrypt(self.credentials()))
        self.assertEqual(decoded[module.FIELDS[2]], '')

    def test_unknown_field_rejected(self):
        value = self.credentials() | {'MOTION_REMOTE_ENDPOINT': 'forbidden'}
        with self.assertRaises(ValueError):
            module.decrypt_credentials(self.key, self.encrypt(value))

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(ValueError):
            module.decrypt_credentials(self.key, self.encrypt('{"ALIBABA_CLOUD_ACCESS_KEY_ID":"one","ALIBABA_CLOUD_ACCESS_KEY_ID":"two"}'))

    def test_newline_injection_rejected(self):
        value = self.credentials()
        value[module.FIELDS[1]] += '\nMOTION_MODE=local'
        with self.assertRaises(ValueError):
            module.decrypt_credentials(self.key, self.encrypt(value))

    def test_corrupt_ciphertext_rejected(self):
        blob = self.encrypt(self.credentials())
        with self.assertRaises(ValueError):
            module.decrypt_credentials(self.key, blob[:-1] + bytes([blob[-1] ^ 1]))

    def test_environment_preserved_and_stale_token_cleared(self):
        decoded = module.decrypt_credentials(self.key, self.encrypt(self.credentials()))
        old = '# unchanged\nMOTION_MODE=hosted\nMOTION_AUTH_PASSWORD_SHA256=synthetic\nALIBABA_CLOUD_SECURITY_TOKEN=stale\n'
        merged = module.merge_environment(old, decoded)
        self.assertTrue(merged.startswith('# unchanged\nMOTION_MODE=hosted\nMOTION_AUTH_PASSWORD_SHA256=synthetic\n'))
        self.assertNotIn('=stale', merged)
        self.assertEqual(merged.count('ALIBABA_CLOUD_SECURITY_TOKEN='), 1)


if __name__ == '__main__':
    unittest.main()
