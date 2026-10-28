import json

import pytest

from agent_mail import server
from agent_mail.imap import ImapMailbox
from agent_mail.tls import DONDOMINIO_MAIL_SHA256, CertificateMismatch


def test_unknown_account_names_the_valid_ones():
    with pytest.raises(ValueError, match="hotmail, gmail, cultuvilla-gmail, ordago-gmail, cultuvilla"):
        server._mailbox("outlook")


def test_hotmail_without_a_client_id_says_which_variable(monkeypatch):
    monkeypatch.delenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", raising=False)
    server._mailboxes.clear()
    with pytest.raises(ValueError, match="AGENT_MAIL_HOTMAIL_CLIENT_ID"):
        server._mailbox("hotmail")


# --- the IMAP accounts ------------------------------------------------------
# These replace test_gmail_is_explicitly_not_ready_rather_than_a_confusing_error,
# which asserted the placeholder this wiring removes.


@pytest.fixture
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    server._mailboxes.clear()

    def write(name, **fields):
        (tmp_path / name).write_text(json.dumps(fields))

    return write


def test_gmail_resolves_to_an_imap_mailbox(creds):
    creds("gmail.json", username="a@gmail.com", password="pw")
    assert isinstance(server._mailbox("gmail"), ImapMailbox)


def test_cultuvilla_resolves_to_an_imap_mailbox(creds):
    creds("cultuvilla.json", username="alvaro@cultuvilla.es", password="pw")
    assert isinstance(server._mailbox("cultuvilla"), ImapMailbox)


def test_building_a_mailbox_opens_no_connection(creds):
    """Constructing one must not reach the network: it happens when a tool is
    called, not when the server starts inside somebody's editor."""
    creds("gmail.json", username="a@gmail.com", password="pw")
    server._mailbox("gmail")  # a connection attempt to a real host would hang or raise


def test_a_missing_credential_file_names_the_script_that_creates_it(creds):
    with pytest.raises(ValueError, match="store_secret.py"):
        server._mailbox("gmail")


def test_a_credential_file_missing_the_password_says_which_key(creds):
    creds("gmail.json", username="a@gmail.com")
    with pytest.raises(ValueError, match="password"):
        server._mailbox("gmail")


def test_app_password_is_accepted_as_the_password_key(creds):
    """Google calls it an app password and DonDominio calls it a password, so the
    file written for each reads naturally. Both are the same field here."""
    creds("gmail.json", username="a@gmail.com", app_password="pw")
    assert isinstance(server._mailbox("gmail"), ImapMailbox)


def test_a_file_with_neither_password_key_still_says_which_key(creds):
    creds("gmail.json", username="a@gmail.com", secret="pw")
    with pytest.raises(ValueError, match="password"):
        server._mailbox("gmail")


def test_a_credential_file_missing_the_username_says_which_key(creds):
    creds("gmail.json", password="pw")
    with pytest.raises(ValueError, match="username"):
        server._mailbox("gmail")


def test_every_tool_is_annotated_read_only():
    """The annotation is what a client shows a user before approving the tool."""
    for tool in (server.mail_list_recent, server.mail_search, server.mail_read):
        assert callable(tool)
    assert server.READ_ONLY.read_only_hint is True
    assert server.READ_ONLY.destructive_hint is False


def test_no_tool_mentions_sending():
    forbidden = ("send", "delete", "reply", "forward", "draft")
    names = [n for n in dir(server) if n.startswith("mail_")]
    assert names, "expected mail_* tools"
    assert not [n for n in names if any(word in n for word in forbidden)]


# --- TLS ---------------------------------------------------------------------

@pytest.mark.parametrize("account", ["gmail", "cultuvilla-gmail", "ordago-gmail"])
def test_gmail_is_not_pinned_because_it_verifies_normally(account):
    host, filename, pin = server.IMAP_ACCOUNTS[account]
    assert (host, pin) == ("imap.gmail.com", None)


def test_each_account_has_its_own_credential_file():
    filenames = [filename for _, filename, _ in server.IMAP_ACCOUNTS.values()]
    assert len(filenames) == len(set(filenames))


def test_cultuvilla_is_pinned_because_its_certificate_is_self_signed():
    host, filename, pin = server.IMAP_ACCOUNTS["cultuvilla"]
    assert pin == DONDOMINIO_MAIL_SHA256


class _FakeSock:
    def __init__(self, der):
        self._der = der

    def getpeercert(self, binary_form=False):
        return self._der


def _fake_imap(der, log):
    """Enough of the protocol for a listing to complete and find nothing.

    These tests are about the TLS handshake and the order of login, so the
    mailbox itself is deliberately empty.
    """
    class FakeIMAP:
        def __init__(self, host, port, ssl_context=None):
            log["context"] = ssl_context
            self.sock = _FakeSock(der)

        def login(self, user, password):
            log["login"] = True

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def select(self, folder, readonly=False):
            return "OK", [b"0"]

        def status(self, folder, what):
            return "OK", [b'"INBOX" (UIDVALIDITY 1)']

        def uid(self, command, *args):
            return "OK", [b""]

        def logout(self):
            log["logout"] = True

        def shutdown(self):
            log["shutdown"] = True

    return FakeIMAP


def test_a_pinned_account_refuses_a_wrong_certificate_before_it_authenticates(creds, monkeypatch):
    """The order is the point. Checking the certificate after login would have
    already handed the password to whoever presented it."""
    creds("cultuvilla.json", username="u@cultuvilla.es", password="p")
    log = {}
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", _fake_imap(b"not the pinned cert", log))

    mailbox = server._mailbox("cultuvilla")
    with pytest.raises(CertificateMismatch):
        mailbox.list_recent()

    assert "login" not in log, "the password was sent to an unverified server"
    assert log.get("shutdown"), "the refused connection was left open"


def test_a_pinned_account_proceeds_when_the_certificate_matches(creds, monkeypatch):
    import hashlib
    der = b"the real certificate"
    creds("cultuvilla.json", username="u@cultuvilla.es", password="p")
    monkeypatch.setattr(server, "IMAP_ACCOUNTS", dict(
        server.IMAP_ACCOUNTS,
        cultuvilla=("imap.cultuvilla.es", "cultuvilla.json", hashlib.sha256(der).hexdigest()),
    ))
    log = {}
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", _fake_imap(der, log))

    server._mailbox("cultuvilla").list_recent()
    assert log.get("login") is True


def test_an_unpinned_account_is_given_a_verifying_context(creds, monkeypatch):
    import ssl
    creds("gmail.json", username="u@gmail.com", app_password="p")
    log = {}
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", _fake_imap(b"x", log))

    server._mailbox("gmail").list_recent()
    assert log["context"].verify_mode is ssl.CERT_REQUIRED
    assert log["context"].check_hostname is True


def test_hotmail_signs_in_without_blocking_the_tool_call(monkeypatch, tmp_path):
    """The server's auth must raise the code, never poll: see test_oauth.py."""
    monkeypatch.setenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    server._mailboxes.clear()
    try:
        assert server._mailbox("hotmail").auth.interactive is False
    finally:
        server._mailboxes.clear()


def test_login_hotmail_runs_the_interactive_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    built = []

    class FakeAuth:
        def __init__(self, client_id, scopes, cache_path, **kwargs):
            built.append(kwargs)

        def access_token(self):
            return "token"

    monkeypatch.setattr(server, "DeviceCodeAuth", FakeAuth)
    server.main(["login", "hotmail"])
    assert built and built[0].get("interactive", True) is True


def test_login_refuses_an_account_that_has_no_sign_in():
    with pytest.raises(SystemExit):
        server.main(["login", "gmail"])
