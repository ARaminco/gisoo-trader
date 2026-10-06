"""Passkey (WebAuthn / FIDO2) endpoints: enrolment, login, the passkey-only switch.

Each ceremony is two requests. The first hands the browser its options plus
a random `state` id; the challenge behind it waits in Redis for five minutes
(security.store_passkey_state). The second brings the authenticator's answer
and the same `state`, which is consumed on the spot. Credentials are bound
to the RP ID of the host the request came in on, so the same key is
enrolled separately for localhost and for the public site.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api import passkeys, security
from app.api.auth import TokenInfo, create_access_token, current_token
from app.api.routers.auth import fail_login
from app.api.schemas import (
    PasskeyAvailableResponse, PasskeyListResponse, PasskeyLoginOptionsRequest,
    PasskeyLoginVerifyRequest, PasskeyOptionsResponse, PasskeyOut,
    PasskeyRegisterVerifyRequest, PasskeyRequiredRequest, TokenResponse,
)
from app.config import settings
from app.db import repo
from app.db.session import session_scope

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WRONG_HOST = "این نشانی برای کلید امنیتی پیکربندی نشده است (WEBAUTHN_ORIGINS)"
STATE_EXPIRED = "مهلت این درخواست تمام شد؛ دوباره تلاش کنید"
BAD_RESPONSE = "پاسخ کلید امنیتی پذیرفته نشد"
UNKNOWN_KEY = "کلید امنیتی شناخته نشد یا برای این نشانی ثبت نشده است"
DUPLICATE_KEY = "این کلید قبلاً ثبت شده است"
NOT_FOUND = "کلید پیدا نشد"
NO_PASSKEYS = "برای فعال کردن این گزینه ابتدا حداقل یک کلید امنیتی ثبت کنید"
LAST_KEY = "این آخرین کلید امنیتی است و «فقط ورود با کلید امنیتی» فعال است؛ ابتدا آن را خاموش کنید"
DEFAULT_NAME = "کلید امنیتی"

# /passkey/available answers after this long whatever it found.
AVAILABLE_DELAY_SECONDS = 0.3


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _rp_id(request: Request) -> str:
    rp_id = passkeys.rp_id_for_host(request.headers.get("host", ""))
    if rp_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WRONG_HOST)
    return rp_id


def _is_panel_user(username: str) -> bool:
    return hmac.compare_digest(username.encode(), settings.panel_username.encode())


def _out(row) -> PasskeyOut:
    return PasskeyOut(
        id=row.id,
        name=row.name,
        rp_id=row.rp_id,
        transports=list(row.transports or []),
        aaguid=row.aaguid,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        backup_eligible=bool(row.backup_eligible),
        backed_up=bool(row.backed_up),
    )


async def _listing(session) -> PasskeyListResponse:
    rows = await repo.list_passkeys(session)
    return PasskeyListResponse(
        passkeys=[_out(r) for r in rows], passkey_required=await passkeys.required_flag(session)
    )


# --------------------------------------------------------------------------
# enrolment (signed in)
# --------------------------------------------------------------------------


@router.post("/passkey/register/options", response_model=PasskeyOptionsResponse)
async def register_options(
    request: Request, info: TokenInfo = Depends(current_token)
) -> PasskeyOptionsResponse:
    rp_id = _rp_id(request)
    async with session_scope() as session:
        existing = await repo.list_passkeys(session, rp_id)
    options, challenge = passkeys.registration_options(
        rp_id, info.username, [(p.credential_id, p.transports or []) for p in existing]
    )
    state = security.new_state_id()
    await security.store_passkey_state(
        "register", state,
        {"challenge": passkeys.encode(challenge), "rp_id": rp_id, "username": info.username},
    )
    return PasskeyOptionsResponse(state=state, options=options)


@router.post("/passkey/register/verify", response_model=PasskeyListResponse)
async def register_verify(
    payload: PasskeyRegisterVerifyRequest, request: Request,
    info: TokenInfo = Depends(current_token),
) -> PasskeyListResponse:
    ip = await security.client_ip(request)
    pending = await security.pop_passkey_state("register", payload.state)
    if pending is None or pending.get("username") != info.username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=STATE_EXPIRED)
    rp_id = str(pending.get("rp_id", ""))
    if _rp_id(request) != rp_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WRONG_HOST)

    try:
        verified = passkeys.verify_registration(
            payload.credential, passkeys.decode(str(pending.get("challenge", ""))), rp_id
        )
    except passkeys.PasskeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_RESPONSE) from exc

    credential_id = passkeys.encode(verified.credential_id)
    name = payload.name.strip()[:64] or DEFAULT_NAME
    transports = passkeys.transports_of(payload.credential)
    async with session_scope() as session:
        if await repo.passkey_by_credential_id(session, credential_id) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE_KEY)
        row = await repo.add_passkey(
            session,
            credential_id=credential_id,
            public_key=verified.credential_public_key,
            sign_count=int(verified.sign_count),
            rp_id=rp_id,
            transports=transports,
            name=name,
            aaguid=str(verified.aaguid or "")[:36] or None,
            backup_eligible=verified.credential_device_type == "multi_device",
            backed_up=bool(verified.credential_backed_up),
        )
        await repo.log_event(
            session,
            f"Passkey '{name}' enrolled by {info.username} from {ip} for {rp_id}",
            level="warn",
            source="auth",
            data={
                "ip": ip, "passkey_id": row.id, "rp_id": rp_id, "transports": transports,
                "aaguid": row.aaguid, "user_verified": bool(verified.user_verified),
            },
        )
        return await _listing(session)


@router.get("/passkeys", response_model=PasskeyListResponse)
async def list_passkeys(_: TokenInfo = Depends(current_token)) -> PasskeyListResponse:
    async with session_scope() as session:
        return await _listing(session)


@router.delete("/passkeys/{passkey_id}", response_model=PasskeyListResponse)
async def delete_passkey(
    passkey_id: int, request: Request, info: TokenInfo = Depends(current_token)
) -> PasskeyListResponse:
    ip = await security.client_ip(request)
    async with session_scope() as session:
        if await passkeys.required_flag(session) and await repo.count_passkeys(session) <= 1:
            # never leave passkey-only mode with nothing to log in with
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_KEY)
        row = await repo.delete_passkey(session, passkey_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        await repo.log_event(
            session,
            f"Passkey '{row.name}' removed by {info.username} from {ip} ({row.rp_id})",
            level="warn",
            source="auth",
            data={"ip": ip, "passkey_id": passkey_id, "rp_id": row.rp_id},
        )
        return await _listing(session)


@router.patch("/passkey/required", response_model=PasskeyListResponse)
async def set_required(
    payload: PasskeyRequiredRequest, request: Request, info: TokenInfo = Depends(current_token)
) -> PasskeyListResponse:
    """Switch passkey-only login. Refused while no key is enrolled."""
    ip = await security.client_ip(request)
    async with session_scope() as session:
        if payload.enabled and await repo.count_passkeys(session) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NO_PASSKEYS)
        await repo.set_setting(session, passkeys.KEY_REQUIRED, bool(payload.enabled))
        await repo.log_event(
            session,
            f"Passkey-only login {'ENABLED' if payload.enabled else 'disabled'} "
            f"by {info.username} from {ip}",
            level="warn",
            source="auth",
            data={"ip": ip, "passkey_required": bool(payload.enabled)},
        )
        return await _listing(session)


# --------------------------------------------------------------------------
# login (anonymous)
# --------------------------------------------------------------------------


@router.get("/passkey/available", response_model=PasskeyAvailableResponse)
async def available(
    request: Request, username: str = Query("", max_length=128)
) -> PasskeyAvailableResponse:
    """Whether this username can use a key on this host. Answers after a fixed
    delay so the database lookup does not show in the timing."""
    started = time.monotonic()
    ip = await security.client_ip(request)
    username = username.strip()
    await security.ensure_not_locked(ip, username)
    rp_id = passkeys.rp_id_for_host(request.headers.get("host", ""))
    count = 0
    if rp_id and username and _is_panel_user(username):
        async with session_scope() as session:
            count = await repo.count_passkeys(session, rp_id)
    await asyncio.sleep(max(0.0, AVAILABLE_DELAY_SECONDS - (time.monotonic() - started)))
    return PasskeyAvailableResponse(available=count > 0)


@router.post("/passkey/login/options", response_model=PasskeyOptionsResponse)
async def login_options(
    payload: PasskeyLoginOptionsRequest, request: Request
) -> PasskeyOptionsResponse:
    ip = await security.client_ip(request)
    username = payload.username.strip()
    rp_id = _rp_id(request)
    await security.ensure_not_locked(ip, username)

    rows = []
    if username and _is_panel_user(username):
        async with session_scope() as session:
            rows = await repo.list_passkeys(session, rp_id)
    credentials = [(p.credential_id, p.transports or []) for p in rows]
    if not credentials:
        # An unknown name, or one without keys, gets a well-formed decoy so the
        # answer is the same shape and the assertion simply fails later.
        credentials = [(passkeys.encode(passkeys.decoy_credential_id(username)), ["usb", "nfc", "ble"])]

    options, challenge = passkeys.authentication_options(rp_id, credentials)
    state = security.new_state_id()
    await security.store_passkey_state(
        "login", state,
        {"challenge": passkeys.encode(challenge), "rp_id": rp_id, "username": username},
    )
    return PasskeyOptionsResponse(state=state, options=options)


@router.post("/passkey/login/verify", response_model=TokenResponse)
async def login_verify(payload: PasskeyLoginVerifyRequest, request: Request) -> TokenResponse:
    ip = await security.client_ip(request)
    pending = await security.pop_passkey_state("login", payload.state)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=STATE_EXPIRED)
    username = str(pending.get("username", ""))
    rp_id = str(pending.get("rp_id", ""))
    await security.ensure_not_locked(ip, username)
    if _rp_id(request) != rp_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WRONG_HOST)

    row = None
    credential_id = passkeys.credential_id_of(payload.credential)
    if credential_id and _is_panel_user(username):
        async with session_scope() as session:
            row = await repo.passkey_by_credential_id(session, credential_id)
        if row is not None and row.rp_id != rp_id:
            row = None  # a key enrolled for another host is not offered here either
    if row is None:
        await fail_login(ip, username, "passkey", status.HTTP_401_UNAUTHORIZED, UNKNOWN_KEY)

    try:
        verified = passkeys.verify_authentication(
            payload.credential, passkeys.decode(str(pending.get("challenge", ""))), rp_id,
            row.public_key, row.sign_count,
        )
    except passkeys.PasskeyError:
        await fail_login(ip, username, "passkey", status.HTTP_401_UNAUTHORIZED, BAD_RESPONSE)
    if not passkeys.user_handle_matches(payload.credential, username):
        await fail_login(ip, username, "passkey", status.HTTP_401_UNAUTHORIZED, BAD_RESPONSE)

    # The key proves possession of the user's hardware: that is the second
    # factor, so no TOTP step follows.
    await security.clear_failures(ip, username)
    version = await security.get_token_version()
    token, expires_in, _ = create_access_token(username, version)
    async with session_scope() as session:
        await repo.mark_passkey_used(
            session, row.id, verified.new_sign_count, verified.credential_backed_up
        )
        await repo.log_event(
            session,
            f"Login successful for {username} from {ip} (passkey '{row.name}')",
            source="auth",
            data={
                "ip": ip, "method": "passkey", "passkey_id": row.id, "rp_id": rp_id,
                "user_verified": bool(verified.user_verified),
            },
        )
    return TokenResponse(access_token=token, expires_in=expires_in)
