"""Token encryption/decryption using AES-256-GCM."""

import base64
import os

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger()

_NONCE_BYTES = 12
_KEY_BYTES = 32
_VERSION_PREFIX = "v1"


class TokenEncryptor:
    """Encrypt and decrypt sensitive tokens using AES-256-GCM with authenticated encryption."""

    def __init__(self, master_key: str):
        if not master_key:
            raise ValueError("master_key is required for TokenEncryptor")

        try:
            raw_key = base64.urlsafe_b64decode(master_key + "=" * (-len(master_key) % 4))
            if len(raw_key) != _KEY_BYTES:
                raise ValueError(f"Key must be {_KEY_BYTES} bytes, got {len(raw_key)}")
            self._aesgcm = AESGCM(raw_key)
        except Exception as e:
            raise ValueError(f"Invalid encryption key format: {e}") from e

    def encrypt(self, plaintext: str, aad: str | None = None) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        aad_bytes = aad.encode() if aad else None
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode(), aad_bytes)

        encoded_nonce = base64.urlsafe_b64encode(nonce).rstrip(b"=").decode()
        encoded_ct = base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode()
        return f"{_VERSION_PREFIX}:{encoded_nonce}:{encoded_ct}"

    def decrypt(self, ciphertext: str, aad: str | None = None) -> str:
        parts = ciphertext.split(":")
        if len(parts) != 3 or parts[0] != _VERSION_PREFIX:
            raise ValueError(f"Invalid ciphertext format: expected '{_VERSION_PREFIX}:nonce:ciphertext'")

        nonce = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        ct_bytes = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        aad_bytes = aad.encode() if aad else None

        return self._aesgcm.decrypt(nonce, ct_bytes, aad_bytes).decode()


def generate_encryption_key() -> str:
    """Generate a new 32-byte AES-256 key, returned as a base64url string (no padding)."""
    raw_key = os.urandom(_KEY_BYTES)
    return base64.urlsafe_b64encode(raw_key).rstrip(b"=").decode()
