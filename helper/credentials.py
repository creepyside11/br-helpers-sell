import hmac
import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = "game-passwords.key"


def _read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as key_file:
        info = os.fstat(key_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError(f"{KEY_FILE} должен быть обычным файлом с правами 0600.")
        return key_file.read(512).strip()


def _save_once(path, key):
    # Publish only a complete key. If two processes start together,
    # the second reads the key the first one installed.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".game-key-", delete=False) as tmp:
        staged = Path(tmp.name)
        try:
            tmp.write(key + b"\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(staged, path)
            except FileExistsError:
                pass
        finally:
            staged.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return _read_key(path)


def load_cipher(data_dir, legacy_key=None, *, has_pending_passwords=lambda: False):
    root = Path(data_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / KEY_FILE
    legacy = legacy_key.strip().encode() if legacy_key and legacy_key.strip() else None
    if legacy is not None:
        Fernet(legacy)

    try:
        key = _read_key(path)
    except FileNotFoundError:
        if legacy is None and has_pending_passwords():
            raise RuntimeError(
                "Есть заказы с зашифрованными паролями, но ключ отсутствует. "
                "Восстанови файл game-passwords.key или запусти сервис с прежним DATABASE_KEY."
            ) from None
        key = _save_once(path, legacy if legacy is not None else Fernet.generate_key())

    if legacy is not None and not hmac.compare_digest(key, legacy):
        raise RuntimeError(
            "DATABASE_KEY отличается от сохранённого ключа. Проверь DATA_DIR и переменные окружения."
        )
    return Fernet(key)
