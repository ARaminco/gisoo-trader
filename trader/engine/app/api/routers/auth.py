"""Login, two-factor, session and account-security endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api import passkeys, security, totp
from app.api.auth import (
    TYPE_CHALLENGE, TokenInfo, create_access_token, create_challenge_token,
    current_token, decode_token, verify_credentials,
)
from app.api.schemas import (
    LoginRequest, MeResponse, TokenResponse, TotpChallengeResponse,
    TotpCodeRequest, TotpDisableResponse, TotpEnableResponse, TotpLoginRequest,
    TotpSetupResponse,
)
from app.db import repo
from app.db.session import session_scope

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WRONG_CREDENTIALS = "نام کاربری یا رمز عبور اشتباه است"
WRONG_CODE = "کد تأیید نامعتبر است"
CHALLENGE_EXPIRED = "مهلت ورود تمام شد؛ دوباره وارد شوید"
TOTP_ALREADY_ENABLED = "احراز هویت دو مرحله‌ای از قبل فعال است؛ ابتدا آن را غیرفعال کنید"
TOTP_SETUP_FIRST = "ابتدا مرحلهٔ راه‌اندازی را انجام دهید"
PASSKEY_ONLY = "ورود با رمز عبور غیرفعال است؛ با کلید امنیتی (Passkey) وارد شوید"

FAILURE_DELAY_SECONDS = 1.0

# settings-table keys
KEY_SECRET = "totp_secret"
KEY_ENABLED = "totp_enabled"
KEY_BACKUP = "totp_backup_codes"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _shown(username: str) -> str:
    """Username as it appears in audit events - bounded, never a password."""
    return username.strip()[:64]


async def fail_login(ip: str, username: str, stage: str, status_code: int, detail: str) -> None:
    """Count a failed attempt, write the audit event, then raise 401/423.

    Shared with the passkey router: a rejected assertion is a login failure
    like a wrong password and feeds the same lockout.
    """
    lock_seconds = await security.record_failure(ip, username)
    async with session_scope() as session:
        if lock_seconds:
            await repo.log_event(
                session,
                f"Login locked for {lock_seconds // 60} min after repeated failures "
                f"(user '{_shown(username)}', ip {ip})",
                level="warn",
                source="auth",
                data={
                    "ip": ip, "username": _shown(username), "stage": stage,
                    "retry_after_seconds": lock_seconds,
                },
            )
        else:
            await repo.log_event(
                session,
                f"Failed login for '{_shown(username)}' from {ip} ({stage})",
                level="warn",
                source="auth",
                data={"ip": ip, "username": _shown(username), "stage": stage},
            )
    await asyncio.sleep(FAILURE_DELAY_SECONDS)
    if lock_seconds:
        raise security.LoginLocked(lock_seconds)
    raise HTTPException(status_code=status_code, detail=detail)


async def _totp_state(session) -> tuple[bool, str | None, list[str]]:
    enabled = bool(await repo.get_setting(session, KEY_ENABLED, False))
    secret = await repo.get_setting(session, KEY_SECRET, None) or None
    hashes = await repo.get_setting(session, KEY_BACKUP, None) or []
    return enabled, secret, list(hashes)


async def _check_second_factor(session, username: str, code: str) -> str | None:
    """Verify a TOTP code or consume a backup code.

    Returns "totp", "backup" or None. A backup code that matches is removed
    from the stored list right here.
    """
    enabled, secret, hashes = await _totp_state(session)
    if not enabled or not secret:
        return None

    if totp.is_totp_shaped(code):
        normalised = totp.normalise_code(code)
        if not totp.verify_totp(secret, normalised):
            return None
        # a code that was already accepted inside its window is a replay
        if not await security.mark_totp_used(username, normalised):
            return None
        return "totp"

    index = await asyncio.to_thread(totp.match_backup_code, code, hashes)
    if index is None:
        return None
    del hashes[index]
    await repo.set_setting(session, KEY_BACKUP, hashes)
    return "backup"


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


@router.post("/login", response_model=TokenResponse | TotpChallengeResponse)
async def login(payload: LoginRequest, request: Request):
    ip = await security.client_ip(request)
    username = payload.username.strip()

    await security.ensure_not_locked(ip, username)
    async with session_scope() as session:
        # Passkey-only mode: the form is refused before the password is even
        # looked at, so it cannot be used to guess the password either.
        if await passkeys.login_required(session):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PASSKEY_ONLY)
    if not await verify_credentials(username, payload.password):
        await fail_login(ip, username, "password", status.HTTP_401_UNAUTHORIZED, WRONG_CREDENTIALS)

    await security.clear_failures(ip, username)
    version = await security.get_token_version()

    async with session_scope() as session:
        totp_enabled = bool(await repo.get_setting(session, KEY_ENABLED, False))
        if totp_enabled:
            challenge, ttl = create_challenge_token(username, version)
            await repo.log_event(
                session,
                f"Password accepted for {username} from {ip}; waiting for the 2FA code",
                source="auth",
                data={"ip": ip, "stage": "password"},
            )
            return TotpChallengeResponse(challenge=challenge, expires_in=ttl)

        token, expires_in, _ = create_access_token(username, version)
        await repo.log_event(
            session,
            f"Login successful for {username} from {ip}",
            source="auth",
            data={"ip": ip, "totp": False},
        )
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/login/totp", response_model=TokenResponse)
async def login_totp(payload: TotpLoginRequest, request: Request) -> TokenResponse:
    ip = await security.client_ip(request)
    try:
        challenge = await decode_token(payload.challenge, expected_type=TYPE_CHALLENGE)
    except HTTPException as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=CHALLENGE_EXPIRED) from exc
    username = challenge.username

    await security.ensure_not_locked(ip, username)
    async with session_scope() as session:
        method = await _check_second_factor(session, username, payload.code)
    if method is None:
        await fail_login(ip, username, "totp", status.HTTP_401_UNAUTHORIZED, WRONG_CODE)

    # the challenge is single-use
    await security.revoke_token(challenge.jti, challenge.remaining_seconds + 5)
    await security.clear_failures(ip, username)
    token, expires_in, _ = create_access_token(username, challenge.ver)

    async with session_scope() as session:
        if method == "backup":
            _, _, remaining = await _totp_state(session)
            await repo.log_event(
                session,
                f"Login successful for {username} from {ip} using a BACKUP code "
                f"({len(remaining)} left)",
                level="warn",
                source="auth",
                data={"ip": ip, "totp": True, "method": "backup",
                      "backup_codes_left": len(remaining)},
            )
        else:
            await repo.log_event(
                session,
                f"Login successful for {username} from {ip} (2FA)",
                source="auth",
                data={"ip": ip, "totp": True, "method": "totp"},
            )
    return TokenResponse(access_token=token, expires_in=expires_in)


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(info: TokenInfo = Depends(current_token)) -> Response:
    await security.revoke_token(info.jti, info.remaining_seconds + 5)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(info: TokenInfo = Depends(current_token)) -> Response:
    async with session_scope() as session:
        version = await security.bump_token_version(session)
        await repo.log_event(
            session,
            f"All sessions signed out by {info.username}",
            level="warn",
            source="auth",
            data={"token_version": version},
        )
    await security.cache_token_version(version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeResponse)
async def me(info: TokenInfo = Depends(current_token)) -> MeResponse:
    async with session_scope() as session:
        enabled = bool(await repo.get_setting(session, KEY_ENABLED, False))
        enrolled = await repo.count_passkeys(session)
        required = await passkeys.required_flag(session)
    return MeResponse(
        username=info.username,
        totp_enabled=enabled,
        token_expires_at=info.expires_at,
        passkeys_enrolled=enrolled,
        passkey_required=required,
    )


# --------------------------------------------------------------------------
# two-factor management
# --------------------------------------------------------------------------


@router.post("/totp/setup", response_model=TotpSetupResponse)
async def totp_setup(info: TokenInfo = Depends(current_token)) -> TotpSetupResponse:
    """Create a pending secret. Nothing is enforced until /totp/enable."""
    async with session_scope() as session:
        enabled, _, _ = await _totp_state(session)
        if enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TOTP_ALREADY_ENABLED)
        secret = totp.new_secret()
        await repo.set_setting(session, KEY_SECRET, secret)
        await repo.set_setting(session, KEY_ENABLED, False)
        await repo.set_setting(session, KEY_BACKUP, [])

    uri = totp.otpauth_uri(secret, info.username)
    png = await asyncio.to_thread(totp.qr_png_base64, uri)
    return TotpSetupResponse(secret=secret, otpauth_uri=uri, qr_png_base64=png)


@router.post("/totp/enable", response_model=TotpEnableResponse)
async def totp_enable(
    payload: TotpCodeRequest, request: Request, info: TokenInfo = Depends(current_token)
) -> TotpEnableResponse:
    ip = await security.client_ip(request)
    async with session_scope() as session:
        enabled, secret, _ = await _totp_state(session)
        if enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TOTP_ALREADY_ENABLED)
        if not secret:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=TOTP_SETUP_FIRST)
        if not totp.verify_totp(secret, payload.code):
            # 400, not 401: the session itself is fine, only the code is wrong
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WRONG_CODE)

        codes = totp.generate_backup_codes()
        hashes = await asyncio.to_thread(lambda: [totp.hash_backup_code(c) for c in codes])
        await repo.set_setting(session, KEY_BACKUP, hashes)
        await repo.set_setting(session, KEY_ENABLED, True)
        await repo.log_event(
            session,
            f"Two-factor authentication ENABLED by {info.username} from {ip}",
            level="warn",
            source="auth",
            data={"ip": ip, "backup_codes": len(codes)},
        )
    return TotpEnableResponse(backup_codes=codes)


@router.post("/totp/disable", response_model=TotpDisableResponse)
async def totp_disable(
    payload: TotpCodeRequest, request: Request, info: TokenInfo = Depends(current_token)
) -> TotpDisableResponse:
    ip = await security.client_ip(request)
    async with session_scope() as session:
        enabled, _, _ = await _totp_state(session)
        if not enabled:
            return TotpDisableResponse()

    # Disabling 2FA with a stolen session is an escalation, so wrong codes
    # here count towards the same lockout as the login form.
    await security.ensure_not_locked(ip, info.username)
    async with session_scope() as session:
        method = await _check_second_factor(session, info.username, payload.code)
    if method is None:
        await fail_login(ip, info.username, "totp_disable", status.HTTP_400_BAD_REQUEST, WRONG_CODE)

    await security.clear_failures(ip, info.username)
    async with session_scope() as session:
        await repo.set_setting(session, KEY_ENABLED, False)
        await repo.set_setting(session, KEY_SECRET, None)
        await repo.set_setting(session, KEY_BACKUP, [])
        await repo.log_event(
            session,
            f"Two-factor authentication DISABLED by {info.username} from {ip} "
            f"(verified with {method})",
            level="warn",
            source="auth",
            data={"ip": ip, "method": method},
        )
    return TotpDisableResponse()

