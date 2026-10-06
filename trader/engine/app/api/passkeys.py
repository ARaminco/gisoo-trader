"""Passkey / FIDO2 (WebAuthn) support: RP ID resolution, option builders and
thin wrappers around py_webauthn's verification.

The option builders and verifiers are pure - no Redis, no request - so the
router owns every bit of state and this module can be exercised on its own.
Nothing here ever logs a challenge or a credential.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Sequence
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options, generate_registration_options,
    verify_authentication_response, verify_registration_response,
)
from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference, AuthenticatorSelectionCriteria,
    AuthenticatorTransport, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from app.config import settings
from app.db import repo

# Roaming keys need a while: the phone has to find the device over
# Bluetooth / NFC and the user confirms on the key itself.
TIMEOUT_MS = 120_000
# Longest credential id the spec allows, as base64url text.
MAX_CREDENTIAL_ID_CHARS = 1400
KEY_REQUIRED = "passkey_required"  # settings-table override of PASSKEY_REQUIRED

_TRANSPORTS = {t.value: t for t in AuthenticatorTransport}
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# What one stored credential contributes to an exclude/allow list.
Credential = tuple[str, Sequence[str]]  # (credential_id base64url, transports)


class PasskeyError(ValueError):
    """A response the library or this module rejected. The message is safe to show."""


# --------------------------------------------------------------------------
# relying party
# --------------------------------------------------------------------------


def host_of(value: str) -> str:
    """Bare lower-case host of an origin or a Host header (no scheme, no port)."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value if "://" in value else f"//{value}")
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _covers(rp_id: str, host: str) -> bool:
    """WebAuthn lets an RP ID be the host itself or a registrable parent of it."""
    return bool(rp_id) and (host == rp_id or host.endswith(f".{rp_id}"))


def rp_id_for_host(host_header: str) -> str | None:
    """The RP ID a request from this Host may use, or None if the host is not
    one of ours. The configured RP ID wins for hosts it covers; any other
    host that appears in the allowed origins binds credentials to itself
    (that is how localhost and the public site coexist)."""
    host = host_of(host_header)
    if not host:
        return None
    configured = settings.webauthn_default_rp_id
    if _covers(configured, host):
        return configured
    if host in {host_of(origin) for origin in settings.webauthn_allowed_origins}:
        return host
    return None


def origins_for(rp_id: str) -> list[str]:
    """Allowed origins that live under this RP ID - what clientDataJSON must carry."""
    return [o for o in settings.webauthn_allowed_origins if _covers(rp_id, host_of(o))]


# --------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------


def user_handle(username: str) -> bytes:
    """Stable, opaque user id (the spec forbids putting the name itself in it)."""
    return hashlib.sha256(b"gtr:passkey:user:" + username.strip().lower().encode()).digest()


def decoy_credential_id(username: str) -> bytes:
    """A well-formed credential id that matches nothing, stable per username so
    an unknown name always gets the same answer and reveals nothing."""
    key = settings.jwt_secret.encode()
    return hmac.new(key, b"gtr:passkey:decoy:" + username.strip().lower().encode(), "sha256").digest()


def _descriptor(credential_id: str, transports: Sequence[str]) -> PublicKeyCredentialDescriptor:
    known = [_TRANSPORTS[t] for t in transports if t in _TRANSPORTS]
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(credential_id), transports=known or None
    )


def credential_id_of(credential: dict[str, Any]) -> str | None:
    """The base64url id of a browser response, or None if it is not well-formed."""
    value = credential.get("id") or credential.get("rawId")
    if not isinstance(value, str) or not value or len(value) > MAX_CREDENTIAL_ID_CHARS:
        return None
    return value if _B64URL_RE.match(value) else None


def transports_of(credential: dict[str, Any]) -> list[str]:
    """`response.transports` from an attestation response, filtered to known values."""
    response = credential.get("response")
    raw = response.get("transports") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t in _TRANSPORTS][:8]


def _b64_field(source: dict[str, Any], key: str, limit: int) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value or len(value) > limit or not _B64URL_RE.match(value):
        raise PasskeyError(f"malformed field {key}")
    return value


def _sanitised(credential: dict[str, Any], response_keys: dict[str, int]) -> dict[str, Any]:
    """Rebuild the browser's JSON with only the fields the library reads, each
    checked to be base64url of a sane size - untrusted input never reaches
    the parser as-is."""
    response = credential.get("response")
    if not isinstance(response, dict) or credential.get("type") != "public-key":
        raise PasskeyError("malformed credential")
    clean_response: dict[str, Any] = {
        key: _b64_field(response, key, limit) for key, limit in response_keys.items()
    }
    handle = response.get("userHandle")
    if isinstance(handle, str) and handle:
        clean_response["userHandle"] = _b64_field(response, "userHandle", 128)
    transports = transports_of(credential)
    if transports:
        clean_response["transports"] = transports
    return {
        "id": _b64_field(credential, "id", MAX_CREDENTIAL_ID_CHARS),
        "rawId": _b64_field(credential, "rawId", MAX_CREDENTIAL_ID_CHARS),
        "type": "public-key",
        "response": clean_response,
    }


# --------------------------------------------------------------------------
# ceremonies
# --------------------------------------------------------------------------


def registration_options(
    rp_id: str, username: str, existing: Sequence[Credential]
) -> tuple[dict[str, Any], bytes]:
    """-> (options JSON for navigator.credentials.create, the challenge bytes).

    Non-discoverable credentials on purpose: some hardware FIDO2 keys do not
    store resident keys, so the login flow asks for the username first and offers
    the matching credential ids. Attestation is not requested - the device
    model is not a factor in trusting the user's own key.
    """
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=user_handle(username),
        user_name=username,
        user_display_name=username,
        timeout=TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[_descriptor(cid, transports) for cid, transports in existing],
    )
    return options_to_json_dict(options), options.challenge


def authentication_options(
    rp_id: str, credentials: Sequence[Credential]
) -> tuple[dict[str, Any], bytes]:
    """-> (options JSON for navigator.credentials.get, the challenge bytes)."""
    options = generate_authentication_options(
        rp_id=rp_id,
        timeout=TIMEOUT_MS,
        allow_credentials=[_descriptor(cid, transports) for cid, transports in credentials],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json_dict(options), options.challenge


def verify_registration(
    credential: dict[str, Any], challenge: bytes, rp_id: str
) -> VerifiedRegistration:
    origins = origins_for(rp_id)
    if not origins:
        raise PasskeyError("origin not configured")
    # attestation objects stay small with attestation "none"; 64 KB is generous
    clean = _sanitised(
        credential, {"clientDataJSON": 8_192, "attestationObject": 65_536}
    )
    try:
        return verify_registration_response(
            credential=clean,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origins,
            require_user_verification=False,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError("registration response rejected") from exc


def verify_authentication(
    credential: dict[str, Any],
    challenge: bytes,
    rp_id: str,
    public_key: bytes,
    sign_count: int,
) -> VerifiedAuthentication:
    """Signature, RP ID hash, origin and the sign counter (must grow, unless
    the authenticator never counts and both sides sit at 0)."""
    origins = origins_for(rp_id)
    if not origins:
        raise PasskeyError("origin not configured")
    clean = _sanitised(
        credential,
        {"clientDataJSON": 8_192, "authenticatorData": 4_096, "signature": 2_048},
    )
    try:
        return verify_authentication_response(
            credential=clean,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origins,
            credential_public_key=public_key,
            credential_current_sign_count=int(sign_count),
            require_user_verification=False,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError("authentication response rejected") from exc


def user_handle_matches(credential: dict[str, Any], username: str) -> bool:
    """A non-resident credential returns no user handle; one that does must be ours."""
    response = credential.get("response")
    handle = response.get("userHandle") if isinstance(response, dict) else None
    if not handle:
        return True
    if not isinstance(handle, str) or not _B64URL_RE.match(handle):
        return False
    try:
        return hmac.compare_digest(base64url_to_bytes(handle), user_handle(username))
    except ValueError:
        return False


def encode(value: bytes) -> str:
    return bytes_to_base64url(value)


def decode(value: str) -> bytes:
    return base64url_to_bytes(value)


# --------------------------------------------------------------------------
# passkey-only mode
# --------------------------------------------------------------------------


async def required_flag(session: AsyncSession) -> bool:
    """The panel switch if it was ever touched, else PASSKEY_REQUIRED from the env."""
    stored = await repo.get_setting(session, KEY_REQUIRED, None)
    return settings.passkey_required if stored is None else bool(stored)


async def login_required(session: AsyncSession) -> bool:
    """True when the password form must be refused: the switch is on AND a key exists."""
    return await required_flag(session) and await repo.count_passkeys(session) > 0
