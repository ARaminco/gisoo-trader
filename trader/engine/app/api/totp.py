"""TOTP second factor: secrets, QR provisioning, backup codes."""
from __future__ import annotations

import base64
import io
import re
import secrets

import bcrypt
import pyotp
import qrcode

ISSUER = "Gisoo Trader"
BACKUP_CODE_COUNT = 8
# No 0/o, 1/l/i - the user will be typing these from a printout.
_BACKUP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_BACKUP_LENGTH = 10
_BACKUP_ROUNDS = 10  # ~50 ms each; eight of them are checked on a backup login
_TOTP_RE = re.compile(r"^\d{6}$")


def new_secret() -> str:
    return pyotp.random_base32()


def otpauth_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_png_base64(uri: str) -> str:
    image = qrcode.make(uri, box_size=6, border=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def normalise_code(code: str) -> str:
    return code.strip().replace(" ", "").replace("-", "")


def is_totp_shaped(code: str) -> bool:
    return bool(_TOTP_RE.match(normalise_code(code)))


def verify_totp(secret: str, code: str) -> bool:
    code = normalise_code(code)
    if not _TOTP_RE.match(code):
        return False
    # valid_window=1 tolerates one 30 s step of clock drift either way
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(_BACKUP_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_backup_code(code: str) -> str:
    normalised = normalise_code(code).lower().encode("ascii", "ignore")
    return bcrypt.hashpw(normalised, bcrypt.gensalt(_BACKUP_ROUNDS)).decode("ascii")


def match_backup_code(code: str, hashes: list[str]) -> int | None:
    """Index of the hash that `code` matches, or None. Checks every hash."""
    normalised = normalise_code(code).lower().encode("ascii", "ignore")
    found: int | None = None
    for index, hashed in enumerate(hashes):
        try:
            if bcrypt.checkpw(normalised, hashed.encode("ascii")):
                found = index
        except (ValueError, TypeError):
            continue
    return found
