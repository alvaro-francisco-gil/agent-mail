"""OAuth 2.0 device authorization grant against the Microsoft identity platform.

Two form POSTs and a poll loop, which is why there is no `msal` dependency here:
the library would be a far larger attack surface than the eighty lines it saves,
and the point of this server is that it can be read in one sitting.

Endpoints and error codes are from Microsoft's documentation, fetched
2026-09-22: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code

The app is a public client, so there is no client secret anywhere in this file.
If you find yourself wanting one, the app registration is wrong.
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

# /consumers, not /common. The app registration is "Personal accounts only",
# which is the narrowest type that can sign in an @hotmail.com mailbox, and
# /consumers is the authority that matches it. /common would also work but
# advertises a willingness to accept sign-ins from every Entra tenant there is,
# which buys nothing here.
#
# Override only if the registration's supported account types change.
AUTHORITY = os.environ.get(
    "AGENT_MAIL_MS_AUTHORITY", "https://login.microsoftonline.com/consumers/oauth2/v2.0"
)
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


class SignInRequired(AuthError):
    """A person must sign in; the message carries Microsoft's URL and code.

    Raised only by the non-interactive mode the server uses. The tool call fails
    at once with the code in its error, where a person can read it, and a later
    call finishes the sign-in.
    """


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

    `interactive=False` is for the MCP server. Its stdout goes to the client's
    log, so a prompt there reaches nobody, and polling inside a tool call hangs
    it for up to fifteen minutes. Instead the device code is saved beside the
    token cache and raised as `SignInRequired`; each later call polls the token
    endpoint once, and the first one after the person signs in succeeds.
    """

    def __init__(
        self,
        client_id: str,
        scopes: Iterable[str],
        cache_path: str | Path,
        *,
        prompt: Callable[[str], None] = print,
        interactive: bool = True,
    ) -> None:
        self.client_id = client_id
        # offline_access is what makes Microsoft return a refresh token at all.
        # Without it every session reprompts, which no one would tolerate.
        self.scopes = list(dict.fromkeys([*scopes, "offline_access"]))
        self.cache_path = Path(cache_path)
        self.prompt = prompt
        self.interactive = interactive
        self.pending_path = self.cache_path.with_name(self.cache_path.stem + ".pending.json")

    def access_token(self) -> str:
        tokens = self._load()
        if tokens.get("access_token") and tokens.get("expires_at", 0) - time.time() > EXPIRY_SKEW_SECONDS:
            return tokens["access_token"]
        if tokens.get("refresh_token"):
            return self._save(self._refresh(tokens["refresh_token"]))["access_token"]
        return self._save(self._sign_in())["access_token"]

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
            return self._sign_in()
        # Microsoft rotates the refresh token. Keep the old one if this response
        # omits it, but never assume the old one still works.
        response.setdefault("refresh_token", refresh_token)
        return response

    def _sign_in(self) -> dict:
        return self._device_flow() if self.interactive else self._sign_in_once()

    def _start_device_flow(self) -> dict:
        started = _post_form(
            DEVICECODE_URL, {"client_id": self.client_id, "scope": " ".join(self.scopes)}
        )
        if "device_code" not in started:
            raise AuthError(started.get("error_description") or str(started))
        started.setdefault(
            "message",
            f"Go to {started['verification_uri']} and enter the code {started['user_code']}",
        )
        return started

    def _poll(self, device_code: str) -> dict:
        """One token request. Returns tokens, or the error code to wait on."""
        response = _post_form(
            TOKEN_URL,
            {"grant_type": DEVICE_CODE_GRANT, "client_id": self.client_id, "device_code": device_code},
        )
        if "access_token" in response:
            return response
        error = response.get("error")
        if error in ("authorization_pending", "slow_down"):
            return {"pending": error}
        if error in ("authorization_declined", "expired_token", "bad_verification_code"):
            raise AuthorizationDeclined(response.get("error_description") or error)
        raise AuthError(response.get("error_description") or str(response))

    def _device_flow(self) -> dict:
        started = self._start_device_flow()
        self.prompt(started["message"])

        interval = int(started.get("interval", 5))
        deadline = time.time() + int(started.get("expires_in", 900))
        while time.time() < deadline:
            response = self._poll(started["device_code"])
            if "access_token" in response:
                return response
            if response["pending"] == "slow_down":
                interval += 5
            time.sleep(interval)

        raise AuthorizationDeclined("the device code expired before sign-in completed")

    def _sign_in_once(self) -> dict:
        """The non-blocking half: poll a saved code once, or start one and raise."""
        pending = self._load_pending()
        if pending:
            try:
                response = self._poll(pending["device_code"])
            except AuthError:
                self.pending_path.unlink(missing_ok=True)
                raise
            if "access_token" in response:
                self.pending_path.unlink(missing_ok=True)
                return response
            raise SignInRequired(pending["message"])

        started = self._start_device_flow()
        self._write_private(
            self.pending_path,
            {
                "device_code": started["device_code"],
                "message": started["message"],
                "expires_at": time.time() + int(started.get("expires_in", 900)),
            },
        )
        raise SignInRequired(started["message"])

    def _load_pending(self) -> dict:
        try:
            pending = json.loads(self.pending_path.read_text())
        except (OSError, ValueError):
            return {}
        if pending.get("expires_at", 0) <= time.time() or not pending.get("device_code"):
            self.pending_path.unlink(missing_ok=True)
            return {}
        return pending

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
        self._write_private(self.cache_path, tokens)
        return tokens

    @staticmethod
    def _write_private(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        # Create with 0600 rather than writing then chmod-ing: between those two
        # calls the token would sit on disk world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
