from agent_mail.models import Message, MessageSummary

GRAPH_MESSAGE = {
    "id": "AAMkAGUAAAwTW09AAA=",
    "subject": "Clean drafts from counsel",
    "from": {"emailAddress": {"name": "Brendan", "address": "b@example.com"}},
    "receivedDateTime": "2026-09-22T14:03:11Z",
    "bodyPreview": "Matt has finished the review",
}


def test_from_graph_normalises_sender_and_date():
    s = MessageSummary.from_graph(GRAPH_MESSAGE, account="hotmail")
    assert s.sender_address == "b@example.com"
    assert s.sender_name == "Brendan"
    assert s.received == "2026-09-22T14:03:11Z"
    assert s.subject == "Clean drafts from counsel"
    assert s.account == "hotmail"


def test_from_graph_tolerates_missing_sender():
    """Graph omits `from` on drafts and some calendar items. A KeyError here
    surfaces to the caller as an opaque MCP failure, so absence must be a
    normal case rather than an error."""
    s = MessageSummary.from_graph({"id": "x"}, account="hotmail")
    assert s.sender_address == ""
    assert s.sender_name == ""
    assert s.subject == ""
    assert s.received == ""


def test_from_graph_tolerates_null_sender_rather_than_missing():
    """Graph sends an explicit null often enough that `.get("from", {})`
    is not sufficient on its own."""
    raw = dict(GRAPH_MESSAGE, **{"from": None})
    s = MessageSummary.from_graph(raw, account="hotmail")
    assert s.sender_address == ""


def test_message_carries_body_text():
    raw = dict(GRAPH_MESSAGE, body={"contentType": "text", "content": "Hi Alvaro,\n\nMatt has"})
    m = Message.from_graph(raw, account="hotmail")
    assert m.body_text.startswith("Hi Alvaro,")
    assert m.subject == "Clean drafts from counsel"


def test_message_is_a_summary():
    """server.py must never branch on provider or on summary-vs-full."""
    raw = dict(GRAPH_MESSAGE, body={"content": "x"})
    assert isinstance(Message.from_graph(raw, account="hotmail"), MessageSummary)


def test_frozen():
    s = MessageSummary.from_graph(GRAPH_MESSAGE, account="hotmail")
    try:
        s.subject = "tampered"
    except Exception as exc:
        assert "frozen" in str(exc).lower() or exc.__class__.__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("MessageSummary must be immutable")


# --- RFC 2047 encoded headers ----------------------------------------------
# Non-ASCII headers travel encoded. Graph hands them over already decoded, so the
# IMAP side must too or Spanish subjects arrive as =?utf-8?b?...?= -- which is what
# the real gmail mailbox returned on 2026-09-30.

def _headers(raw: str):
    import email
    return email.message_from_string(raw)


def test_an_encoded_subject_is_decoded():
    msg = _headers("Subject: Nuevo inicio de =?utf-8?b?c2VzacOzbg==?=\n\n")
    assert MessageSummary.from_imap(msg, "gmail", "i").subject == "Nuevo inicio de sesión"


def test_an_encoded_sender_name_is_decoded():
    msg = _headers("From: =?utf-8?q?Jos=C3=A9?= <j@example.com>\n\n")
    summary = MessageSummary.from_imap(msg, "gmail", "i")
    assert (summary.sender_name, summary.sender_address) == ("José", "j@example.com")


def test_an_undecodable_header_is_passed_through_rather_than_raising():
    msg = _headers("Subject: =?bogus-charset?q?x?=\n\n")
    assert MessageSummary.from_imap(msg, "gmail", "i").subject
