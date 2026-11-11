import tempfile
import unittest
from pathlib import Path

from services.sub2api_totp import TOTPSecretStore, normalize_totp_secret, totp_code


SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
SERVER = {"id": "fixture", "base_url": "https://source.example.test/", "email": "admin@example.test"}


class TOTPTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        self.assertEqual("287082", totp_code(SEED, 59))

    def test_input_validation_does_not_echo_secret(self):
        self.assertEqual(SEED, normalize_totp_secret(SEED.lower()))
        for value in ["invalid-secret!", "x" * 2049, "123456", None]:
            with self.assertRaises(ValueError) as caught:
                normalize_totp_secret(value)
            self.assertNotIn(str(value), str(caught.exception))

    def test_secret_is_encrypted_bound_to_source_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TOTPSecretStore(Path(directory) / "key")
            encrypted = store.seal(SEED, SERVER)
            self.assertNotIn(SEED, encrypted)
            self.assertEqual(SEED, store.open(encrypted, SERVER))
            self.assertEqual(SEED, TOTPSecretStore(store.path).open(encrypted, SERVER))
            self.assertEqual(0o600, store.path.stat().st_mode & 0o777)
            with self.assertRaises(ValueError):
                store.open(encrypted, {**SERVER, "base_url": "https://different.example.test"})

    def test_successive_codes_use_distinct_time_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [45.0]
            store = TOTPSecretStore(Path(directory) / "key", clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n))
            server = {**SERVER, "totp_secret_encrypted": store.seal(SEED, SERVER)}
            first = store.next_code(server)
            second = store.next_code(server)
            self.assertEqual("287082", first)
            self.assertNotEqual(first, second)
            self.assertGreaterEqual(now[0], 60)
