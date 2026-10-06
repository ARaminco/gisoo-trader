"""Password hashing for the panel account (bcrypt).

Kept free of any settings / database import so the hash CLI works anywhere.
"""
from __future__ import annotations

from functools import lru_cache

import bcrypt

BCRYPT_ROUNDS = 12
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def hash_password(password: str, rounds: int = BCRYPT_ROUNDS) -> str:
    """Return a bcrypt hash string suitable for PANEL_PASSWORD_HASH."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of `password` against a bcrypt hash.

    Any malformed hash simply verifies as False - it never raises, so a bad
    PANEL_PASSWORD_HASH cannot turn into a 500 on the login route.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def looks_like_bcrypt(value: str) -> bool:
    return value.startswith(_BCRYPT_PREFIXES) and len(value) == 60


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A throwaway hash used to equalise timing when there is nothing to compare."""
    return hash_password("not-a-real-password")
