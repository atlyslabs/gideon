"""
SQLAlchemy TypeDecorators for column-level Fernet encryption.

Strict mode: all stored values must be Fernet ciphertext (starts with "gAAAAA").
A plaintext value on read raises ValueError — this catches buggy writes that
bypass the ORM, manual DB edits, and partially-restored dumps.

On write: always encrypt (unless value is already a Fernet token, which keeps
the migration script idempotent).
"""

import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from services.encryption_service import decrypt, encrypt

_FERNET_PREFIX = "gAAAAA"


def _is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_FERNET_PREFIX)


class EncryptedText(TypeDecorator):
    """TEXT column with transparent Fernet encryption. Rejects plaintext on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        if _is_encrypted(value):
            return value  # already encrypted — migration re-run safety
        return encrypt(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if _is_encrypted(value):
            return decrypt(value)
        raise ValueError(
            "EncryptedText read a non-encrypted value — data integrity violation. "
            "Every row must be Fernet ciphertext."
        )


class EncryptedJSON(TypeDecorator):
    """TEXT column holding an encrypted JSON-serialized value. Rejects plaintext on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        # If caller handed us a pre-encrypted string, pass through
        if isinstance(value, str) and _is_encrypted(value):
            return value
        serialized = json.dumps(value)
        return encrypt(serialized)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if _is_encrypted(value):
            return json.loads(decrypt(value))
        raise ValueError(
            "EncryptedJSON read a non-encrypted value — data integrity violation. "
            "Every row must be Fernet ciphertext."
        )
