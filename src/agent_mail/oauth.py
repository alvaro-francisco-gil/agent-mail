"""OAuth 2.0 device authorization grant against the Microsoft identity platform.

Two form POSTs and a poll loop, which is why there is no `msal` dependency here:
the library would be a far larger attack surface than the eighty lines it saves,
and the point of this server is that it can be read in one sitting.

Endpoints and error codes are from Microsoft's documentation, fetched
2026-09-22: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code

`/common` accepts personal Microsoft accounts. The app is a public client, so
there is no client secret anywhere in this file -- if you find yourself wanting
one, the app registration is wrong.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
DEVICECODE_URL = f"{AUTHORITY}/devicecode"
TOKEN_URL = f"{AUTHORITY}/token"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Refresh this far before the token actually dies, so a token with four seconds
# left is not handed to a request that takes five.
EXPIRY_SKEW_SECONDS = 120


class AuthError(RuntimeError):
    """Authentication failed in a way that retrying will not fix."""


class AuthorizationDeclined(AuthError):
    """The user denied the request, or the device code expired unused."""


def _post_form(url: str, data: dict) -> dict:
    """POST a form and return the parsed JSON body.

    The token endpoint signals `authorization_pending` with HTTP 400 and a JSON
    body, so an error status is not exceptional here -- the body is the answer.
    """
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:
            raise AuthError(f"{url} returned HTTP {exc.code} with no JSON body") from exc


class DeviceCodeAuth:
    """Holds a refresh token and turns it into access tokens on demand.

    Prompts only when there is no usable refresh token. In practice that is once
    per app registration, because Microsoft's refresh tokens do not carry
    Google's 7-day testing-mode expiry.
    """

    def __init__(
        self,
        client_id: str,
        scopes: Iterable[str],
        cache_path: str | Path,
        *,
        prompt: Callable[[str], None] = print,
    ) -> None:
        self.client_id = client_id
        # offline_access is what makes Microsoft return a refresh token at all.
        # Without it every session reprompts, which no one would tolerate.
        self.scopes = list(dict.fromkeys([*scopes, "offline_access"]))
        self.cache_path = Path(cache_path)
        self.prompt = prompt

    def access_token(self) -> str:
        tokens = self._load()
        if tokens.get("access_token") and tokens.get("expires_at", 0) - time.time() > EXPIRY_SKEW_SECONDS:
            return tokens["access_token"]
        if tokens.get("refresh_token"):
            return self._save(self._refresh(tokens["refresh_token"]))["access_token"]
        return self._save(self._device_flow())["access_token"]

    def _refresh(self, refresh_token: str) -> dict:
        response = _post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(self.scopes),
            },
        )
        if "access_token" not in response:
            # An expired or revoked refresh token lands here. Fall back to a
            # fresh sign-in rather than failing the tool call.
            return self._device_flow()
        # Microsoft rotates the refresh token. Keep the old one if this response
        # omits it, but never assume the old one still works.
        response.setdefault("refresh_token", refresh_token)
        return response

    def _device_flow(self) -> dict:
        started = _post_form(
            DEVICECODE_URL, {"client_id": self.client_id, "scope": " ".join(self.scopes)}
        )
        if "device_code" not in started:
            raise AuthError(started.get("error_description") or str(started))

        self.prompt(
            started.get("message")
            or f"Go to {started['verification_uri']} and enter the code {started['user_code']}"
        )

        interval = int(started.get("interval", 5))
        deadline = time.time() + int(started.get("expires_in", 900))
        while time.time() < deadline:
            response = _post_form(
                TOKEN_URL,
                {
                    "grant_type": DEVICE_CODE_GRANT,
                    "client_id": self.client_id,
                    "device_code": started["device_code"],
                },
            )
            if "access_token" in response:
                return response

            error = response.get("error")
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            if error in ("authorization_declined", "expired_token", "bad_verification_code"):
                raise AuthorizationDeclined(response.get("error_description") or error)
            raise AuthError(response.get("error_description") or str(response))

        raise AuthorizationDeclined("the device code expired before sign-in completed")

    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, response: dict) -> dict:
        tokens = {
            "access_token": response["access_token"],
            "refresh_token": response.get("refresh_token", ""),
            "expires_at": time.time() + int(response.get("expires_in", 3599)),
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.cache_path.parent, 0o700)
        # Create with 0600 rather than writing then chmod-ing: between those two
        # calls the token would sit on disk world-readable.
        fd = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(tokens, handle)
        return tokens
