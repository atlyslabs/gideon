"""
Encryption Service — Fernet symmetric encryption for OAuth tokens at rest.
"""

from cryptography.fernet import Fernet, InvalidToken
import structlog

from config import ENCRYPTION_KEY

logger = structlog.get_logger()

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError(
                "ENCRYPTION_KEY not set. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return base64-encoded ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt base64-encoded ciphertext and return plaintext."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt token — invalid key or corrupted data")
        raise
