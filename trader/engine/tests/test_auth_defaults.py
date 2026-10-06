"""A public release must not accept its own placeholders.

    cd trader/engine && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import secrets
import unittest
from unittest import mock

from fastapi import HTTPException

from app.api import auth
from app.config import settings


class PlaceholderCredentialsTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_placeholder_password_never_logs_in(self):
        with mock.patch.object(settings, "panel_password_hash", ""), \
                mock.patch.object(settings, "panel_password", auth.DEFAULT_PANEL_PASSWORD):
            self.assertFalse(await auth.verify_credentials(settings.panel_username, auth.DEFAULT_PANEL_PASSWORD))

    async def test_an_empty_password_never_logs_in(self):
        with mock.patch.object(settings, "panel_password_hash", ""), \
                mock.patch.object(settings, "panel_password", ""):
            self.assertFalse(await auth.verify_credentials(settings.panel_username, ""))

    async def test_a_real_password_logs_in(self):
        with mock.patch.object(settings, "panel_password_hash", ""), \
                mock.patch.object(settings, "panel_password", "a-real-password-1"):
            self.assertTrue(await auth.verify_credentials(settings.panel_username, "a-real-password-1"))
            self.assertFalse(await auth.verify_credentials(settings.panel_username, "wrong"))


class JwtSecretTest(unittest.TestCase):
    def test_placeholder_and_short_secrets_sign_nothing(self):
        for secret in ("change-me", "change-me-to-a-long-random-string", "short"):
            with mock.patch.object(settings, "jwt_secret", secret):
                self.assertFalse(auth.jwt_secret_is_safe())
                with self.assertRaises(HTTPException) as ctx:
                    auth.create_access_token("admin", 1)
                self.assertEqual(ctx.exception.status_code, 503)

    def test_a_random_secret_signs(self):
        with mock.patch.object(settings, "jwt_secret", secrets.token_hex(32)):
            token, ttl, _ = auth.create_access_token("admin", 1)
            self.assertTrue(token)
            self.assertGreater(ttl, 0)


if __name__ == "__main__":
    unittest.main()
