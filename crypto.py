"""Encrypt/decrypt Burhill credentials stored in the database."""
import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    secret = os.environ.get("SECRET_KEY", "dev-insecure-key-set-SECRET_KEY-in-prod")
    # Derive a 32-byte URL-safe base64 key from the secret
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
