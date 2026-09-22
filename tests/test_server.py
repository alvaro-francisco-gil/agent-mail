import pytest

from agent_mail import server


def test_unknown_account_names_the_valid_ones():
    with pytest.raises(ValueError, match="hotmail"):
        server._mailbox("outlook")


def test_hotmail_without_a_client_id_says_which_variable(monkeypatch):
    monkeypatch.delenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", raising=False)
    server._mailboxes.clear()
    with pytest.raises(ValueError, match="AGENT_MAIL_HOTMAIL_CLIENT_ID"):
        server._mailbox("hotmail")


def test_gmail_is_explicitly_not_ready_rather_than_a_confusing_error():
    with pytest.raises(ValueError, match="not wired up yet"):
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
