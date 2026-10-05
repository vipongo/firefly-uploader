"""Passwords, login sessions, and encryption of the Firefly tokens kept in the database."""

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# scrypt cost: about 16 MB of memory and a few dozen milliseconds per password check
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(digest)])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except ValueError:
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def new_secret() -> str:
    """For session cookies and form tokens."""
    return secrets.token_urlsafe(32)


def fingerprint(secret: str) -> str:
    """What the database keeps of a session cookie, so a copy of the database can't log in."""
    return hashlib.sha256(secret.encode()).hexdigest()


class TokenCipher:
    """Encrypts Firefly tokens, so the database alone doesn't give them away."""

    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key)

    @staticmethod
    def new_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, data: str) -> str | None:
        """None when the token was encrypted with another key."""
        try:
            return self._fernet.decrypt(data.encode()).decode()
        except InvalidToken:
            return None


def load_key(configured: str | None, key_file: Path) -> str:
    """The configured key (UPLOADER_SECRET_KEY), else the key file, made on first start."""
    if configured:
        return configured
    if key_file.exists():
        return key_file.read_text().strip()
    key = TokenCipher.new_key()
    key_file.write_text(key + "\n")
    os.chmod(key_file, 0o600)
    return key
