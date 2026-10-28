import json
import os
import stat
import time

import pytest

from agent_mail import oauth
from agent_mail.oauth import AuthorizationDeclined, DeviceCodeAuth, SignInRequired


def write_cache(path, **overrides):
    tokens = {"access_token": "cached", "refresh_token": "r", "expires_at": time.time() + 600}
    tokens.update(overrides)
    path.write_text(json.dumps(tokens))
    return path


def test_uses_cached_access_token_when_fresh(tmp_path):
    cache = write_cache(tmp_path / "hotmail.json")
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache)
    assert auth.access_token() == "cached"


def test_refreshes_when_access_token_expired(tmp_path, monkeypatch):
    cache = write_cache(tmp_path / "hotmail.json", access_token="stale", expires_at=time.time() - 1)
    calls = []

    def fake_post(url, data):
        calls.append(data)
        return {"access_token": "fresh", "refresh_token": "r2", "expires_in": 3599}

    monkeypatch.setattr("agent_mail.oauth._post_form", fake_post)
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache)

    assert auth.access_token() == "fresh"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "r"
    # Microsoft rotates the refresh token on every refresh. Failing to write the
    # new one back means the connection dies silently at the next expiry.
    assert json.loads(cache.read_text())["refresh_token"] == "r2"


def test_refreshes_shortly_before_expiry_not_after(tmp_path, monkeypatch):
    """A token valid for another 10 seconds will expire mid-request."""
    cache = write_cache(tmp_path / "hotmail.json", expires_at=time.time() + 10)
    monkeypatch.setattr(
        "agent_mail.oauth._post_form",
        lambda url, data: {"access_token": "fresh", "refresh_token": "r2", "expires_in": 3599},
    )
    assert DeviceCodeAuth("cid", ["Mail.Read"], cache).access_token() == "fresh"


def test_device_flow_when_no_cache_polls_through_authorization_pending(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    responses = [
        {"device_code": "dc", "user_code": "ABCD-EFGH",
         "verification_uri": "https://microsoft.com/devicelogin", "interval": 0, "expires_in": 900},
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "new", "refresh_token": "r1", "expires_in": 3599},
    ]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    shown = []

    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache, prompt=shown.append)
    assert auth.access_token() == "new"
    # The user cannot complete the flow without seeing both of these.
    assert "ABCD-EFGH" in shown[0]
    assert "https://microsoft.com/devicelogin" in shown[0]
    assert json.loads(cache.read_text())["access_token"] == "new"


def test_declined_raises_rather_than_polling_forever(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    responses = [
        {"device_code": "dc", "user_code": "A", "verification_uri": "u", "interval": 0, "expires_in": 900},
        {"error": "authorization_declined", "error_description": "user said no"},
    ]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    with pytest.raises(AuthorizationDeclined, match="user said no"):
        DeviceCodeAuth("cid", ["Mail.Read"], cache, prompt=lambda m: None).access_token()


def test_cache_is_not_world_readable(tmp_path, monkeypatch):
    """A refresh token to a mailbox holding contract negotiations is the whole
    risk of this project sitting in one file."""
    cache = tmp_path / "nested" / "hotmail.json"
    monkeypatch.setattr(
        "agent_mail.oauth._post_form",
        lambda url, data: {"access_token": "new", "refresh_token": "r", "expires_in": 3599},
    )
    responses = [
        {"device_code": "dc", "user_code": "A", "verification_uri": "u", "interval": 0, "expires_in": 900},
        {"access_token": "new", "refresh_token": "r", "expires_in": 3599},
    ]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    DeviceCodeAuth("cid", ["Mail.Read"], cache, prompt=lambda m: None).access_token()

    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cache.parent).st_mode) == 0o700


def test_offline_access_is_always_requested(tmp_path, monkeypatch):
    """Without it Microsoft returns no refresh token and every session reprompts."""
    cache = tmp_path / "hotmail.json"
    seen = {}
    responses = [
        {"device_code": "dc", "user_code": "A", "verification_uri": "u", "interval": 0, "expires_in": 900},
        {"access_token": "new", "refresh_token": "r", "expires_in": 3599},
    ]

    def fake_post(url, data):
        seen.setdefault("first", data)
        return responses.pop(0)

    monkeypatch.setattr("agent_mail.oauth._post_form", fake_post)
    DeviceCodeAuth("cid", ["Mail.Read"], cache, prompt=lambda m: None).access_token()
    assert "offline_access" in seen["first"]["scope"]


def test_authority_matches_a_personal_accounts_only_registration():
    """The Entra app is registered "Personal accounts only", the narrowest type
    that can sign in an @hotmail.com mailbox. /common would also work, but it
    advertises willingness to accept sign-ins from every Entra tenant for no
    benefit. If this assertion fails, the registration and the code disagree and
    sign-in will fail confusingly."""
    from agent_mail import oauth

    assert "/consumers/" in oauth.AUTHORITY
    assert oauth.DEVICECODE_URL.endswith("/devicecode")
    assert oauth.TOKEN_URL.endswith("/token")


# --- the server's non-interactive mode ----------------------------------------
# Under an MCP client nobody reads the server's stdout, so a sign-in that polls
# inside the tool call is a fifteen-minute hang with the code shown to no one.
# The server's auth raises the code instead, and finishes on a later call.

STARTED = {"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 5, "expires_in": 900,
           "verification_uri": "https://microsoft.com/devicelogin",
           "message": "To sign in, use https://microsoft.com/devicelogin and enter ABCD-EFGH."}


def no_sleep(monkeypatch):
    def refuse(seconds):
        raise AssertionError("a non-interactive sign-in must never sleep")
    monkeypatch.setattr("agent_mail.oauth.time.sleep", refuse)


def test_a_missing_token_raises_the_code_without_polling(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: calls.append(data) or dict(STARTED))
    no_sleep(monkeypatch)
    auth = DeviceCodeAuth("cid", ["Mail.Read"], tmp_path / "hotmail.json", interactive=False)

    with pytest.raises(SignInRequired) as raised:
        auth.access_token()
    assert "ABCD-EFGH" in str(raised.value)
    assert "https://microsoft.com/devicelogin" in str(raised.value)
    assert len(calls) == 1, "only the devicecode request, no token poll"


def test_a_dead_refresh_token_raises_the_code_too(tmp_path, monkeypatch):
    cache = write_cache(tmp_path / "hotmail.json", expires_at=time.time() - 1)
    responses = [{"error": "invalid_grant"}, dict(STARTED)]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    no_sleep(monkeypatch)
    with pytest.raises(SignInRequired, match="ABCD-EFGH"):
        DeviceCodeAuth("cid", ["Mail.Read"], cache, interactive=False).access_token()


def test_the_next_call_polls_once_and_repeats_the_same_code_while_pending(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    responses = [dict(STARTED), {"error": "authorization_pending"}]
    calls = []

    def fake_post(url, data):
        calls.append(data)
        return responses.pop(0)

    monkeypatch.setattr("agent_mail.oauth._post_form", fake_post)
    no_sleep(monkeypatch)
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache, interactive=False)
    with pytest.raises(SignInRequired):
        auth.access_token()
    with pytest.raises(SignInRequired, match="ABCD-EFGH"):
        auth.access_token()
    assert calls[1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert calls[1]["device_code"] == "dc"


def test_a_completed_sign_in_saves_tokens_and_clears_the_pending_code(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    responses = [dict(STARTED), {"access_token": "new", "refresh_token": "r1", "expires_in": 3599}]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    no_sleep(monkeypatch)
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache, interactive=False)
    with pytest.raises(SignInRequired):
        auth.access_token()
    assert auth.access_token() == "new"
    assert json.loads(cache.read_text())["refresh_token"] == "r1"
    assert not auth.pending_path.exists()


def test_an_expired_pending_code_starts_a_new_sign_in(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache, interactive=False)
    auth.pending_path.write_text(json.dumps(
        {"device_code": "old", "message": "old code", "interval": 5, "expires_at": time.time() - 1}))
    calls = []

    def fake_post(url, data):
        calls.append(url)
        return dict(STARTED)

    monkeypatch.setattr("agent_mail.oauth._post_form", fake_post)
    no_sleep(monkeypatch)
    with pytest.raises(SignInRequired, match="ABCD-EFGH"):
        auth.access_token()
    assert calls == [oauth.DEVICECODE_URL]


def test_a_declined_pending_code_raises_and_is_forgotten(tmp_path, monkeypatch):
    cache = tmp_path / "hotmail.json"
    responses = [dict(STARTED), {"error": "authorization_declined", "error_description": "no"}]
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: responses.pop(0))
    no_sleep(monkeypatch)
    auth = DeviceCodeAuth("cid", ["Mail.Read"], cache, interactive=False)
    with pytest.raises(SignInRequired):
        auth.access_token()
    with pytest.raises(AuthorizationDeclined, match="no"):
        auth.access_token()
    assert not auth.pending_path.exists()


def test_the_pending_code_is_not_world_readable(tmp_path, monkeypatch):
    """A device code is half a sign-in: whoever completes it holds the mailbox."""
    monkeypatch.setattr("agent_mail.oauth._post_form", lambda url, data: dict(STARTED))
    auth = DeviceCodeAuth("cid", ["Mail.Read"], tmp_path / "nested" / "hotmail.json", interactive=False)
    with pytest.raises(SignInRequired):
        auth.access_token()
    assert stat.S_IMODE(os.stat(auth.pending_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(auth.pending_path.parent).st_mode) == 0o700
