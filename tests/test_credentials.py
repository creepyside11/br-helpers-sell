import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from helper.credentials import KEY_FILE, load_cipher


class CredentialsAtRestTests(unittest.TestCase):
    def test_generated_key_is_private_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            ciphertext = load_cipher(folder).encrypt(b"game-password")
            self.assertEqual(stat.S_IMODE((Path(folder) / KEY_FILE).stat().st_mode), 0o600)
            self.assertEqual(load_cipher(folder).decrypt(ciphertext), b"game-password")

    def test_adopts_legacy_key_and_rejects_unintended_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            old_key = Fernet.generate_key()
            ciphertext = Fernet(old_key).encrypt(b"queued-password")
            load_cipher(folder, old_key.decode())
            self.assertEqual(load_cipher(folder).decrypt(ciphertext), b"queued-password")
            with self.assertRaisesRegex(RuntimeError, "отличается"):
                load_cipher(folder, Fernet.generate_key().decode())
            self.assertEqual(load_cipher(folder).decrypt(ciphertext), b"queued-password")

    def test_missing_key_does_not_strand_queued_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "ключ отсутствует"):
                load_cipher(folder, has_pending_passwords=lambda: True)
            self.assertFalse((Path(folder) / KEY_FILE).exists())
