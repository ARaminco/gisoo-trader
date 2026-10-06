"""Single-user authentication for the panel: credentials, JWTs, dependencies."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hmac
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.api import security
from app.api.passwords import dummy_hash, verify_password
from app.config import settings

ALGORITHM = "HS256"
# Shipped placeholders. A public release must never accept them: logins are
# refused while the password is the placeholder, and no token is signed with
# a placeholder or short secret (start.sh / start.ps1 generate real ones).
DEFAULT_PANEL_PASSWORD = "change-me-now"
DEFAULT_JWT_SECRETS = ("change-me", "change-me-to-a-long-random-string")
MIN_JWT_SECRET_LENGTH = 32
TYPE_ACCESS = "access"
TYPE_CHALLENGE = "totp_challenge"
CHALLENGE_TTL_SECONDS = 300

bearer = HTTPBearer(auto_error=False)


@dataclasses.dataclass(frozen=True)
class TokenInfo:
    username: str
    jti: str
    ver: int
    typ: str
    issued_at: dt.datetime
    expires_at: dt.datetime

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.expires_at - _now()).total_seconds()))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    )


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


async def verify_credentials(username: str, password: str) -> bool:
    """Both fields are always checked, whatever the outcome of the first, so a
    wrong username and a wrong password take the same time."""
    user_ok = hmac.compare_digest(username.encode(), settings.panel_username.encode())
    if not settings.panel_password_hash and (
        not settings.panel_password or settings.panel_password == DEFAULT_PANEL_PASSWORD
    ):
        # Placeholder password: refuse every login (same cost as a real check).
        await asyncio.to_thread(verify_password, password, dummy_hash())
        return False
    if settings.panel_password_hash:
        # bcrypt is deliberately slow - keep it off the event loop
        pass_ok = await asyncio.to_thread(
            verify_password, password, settings.panel_password_hash
        )
    else:
        pass_ok = hmac.compare_digest(password.encode(), settings.panel_password.encode())
        # match the cost of the hashed path so the mode is not observable
        await asyncio.to_thread(verify_password, password, dummy_hash())
    return user_ok and pass_ok


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------


def jwt_secret_is_safe() -> bool:
    secret = settings.jwt_secret or ""
    return secret not in DEFAULT_JWT_SECRETS and len(secret) >= MIN_JWT_SECRET_LENGTH


def _mint(username: str, ver: int, typ: str, ttl: int) -> tuple[str, int, dt.datetime]:
    if not jwt_secret_is_safe():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT_SECRET is a placeholder or shorter than 32 characters; set a random one in .env",
        )
    now = _now()
    expires_at = now + dt.timedelta(seconds=ttl)
    payload = {
        "sub": username,
        "typ": typ,
        "jti": secrets.token_urlsafe(16),
        "ver": int(ver),
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM), ttl, expires_at


def create_access_token(username: str, ver: int) -> tuple[str, int, dt.datetime]:
    """-> (token, expires_in seconds, expires_at)."""
    return _mint(username, ver, TYPE_ACCESS, settings.jwt_expire_hours * 3600)


def create_challenge_token(username: str, ver: int) -> tuple[str, int]:
    """Short-lived proof that the password step passed; only /login/totp takes it."""
    token, ttl, _ = _mint(username, ver, TYPE_CHALLENGE, CHALLENGE_TTL_SECONDS)
    return token, ttl


async def decode_token(token: str, expected_type: str = TYPE_ACCESS) -> TokenInfo:
    """Validate signature, expiry, type, subject, revocation and version.

    Used by the HTTP dependency and by the WebSocket handshake alike.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise _invalid() from exc

    subject = payload.get("sub")
    jti = payload.get("jti")
    ver = payload.get("ver")
    typ = payload.get("typ")
    exp = payload.get("exp")
    if not subject or not jti or ver is None or typ != expected_type or exp is None:
        raise _invalid()
    if not hmac.compare_digest(str(subject).encode(), settings.panel_username.encode()):
        raise _invalid()
    if await security.is_revoked(str(jti)):
        raise _invalid()
    if int(ver) != await security.get_token_version():
        raise _invalid()

    expires_at = dt.datetime.fromtimestamp(int(exp), tz=dt.timezone.utc)
    iat = payload.get("iat")
    issued_at = (
        dt.datetime.fromtimestamp(int(iat), tz=dt.timezone.utc) if iat else _now()
    )
    return TokenInfo(
        username=str(subject),
        jti=str(jti),
        ver=int(ver),
        typ=str(typ),
        issued_at=issued_at,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------


async def current_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> TokenInfo:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await decode_token(credentials.credentials)


async def current_user(info: TokenInfo = Depends(current_token)) -> str:
    return info.username
