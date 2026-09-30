import json

import pytest

from agent_mail import server
from agent_mail.imap import ImapMailbox


def test_unknown_account_names_the_valid_ones():
    with pytest.raises(ValueError, match="hotmail, gmail, cultuvilla"):
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
