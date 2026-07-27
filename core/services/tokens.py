"""Control-token helpers for the web dashboard (generate + hash).

Only the SHA-256 hash is ever stored or compared; the plaintext token is
shown once by the tgbot and never persisted.
"""

from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    """Return a fresh URL-safe control token (plaintext, shown once)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a control token."""
    return hashlib.sha256(token.encode()).hexdigest()
