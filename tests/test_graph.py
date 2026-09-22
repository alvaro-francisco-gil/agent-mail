import pytest

from agent_mail.graph import GraphMailbox
from agent_mail.models import Message, MessageSummary

MESSAGE = {
    "id": "abc",
    "subject": "Clean drafts from counsel",
    "from": {"emailAddress": {"name": "Brendan", "address": "b@example.com"}},
    "receivedDateTime": "2026-09-22T14:03:11Z",
    "bodyPreview": "Matt has finished",
}


class FakeTransport:
    """Records the request. Asserting on the URL we build matters more than on
    the response we already control."""

    def __init__(self, *pages):
        self.pages = list(pages) or [{"value": [MESSAGE]}]
        self.urls = []
        self.headers = []

    def __call__(self, url, headers):
        self.urls.append(url)
        self.headers.append(headers)
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    @property
    def last_url(self):
        return self.urls[-1]

    @property
    def last_headers(self):
        return self.headers[-1]


class FakeAuth:
    def access_token(self):
        return "token"


def mailbox(transport):
    return GraphMailbox(FakeAuth(), account="hotmail", transport=transport)


def test_read_requests_plain_text_body():
    """Graph returns HTML unless asked otherwise, and HTML is not what a model
    should be reading."""
    t = FakeTransport({"id": "abc", "body": {"content": "hello"}})
    mailbox(t).read("abc")
    assert t.last_headers["Prefer"] == 'outlook.body-content-type="text"'


def test_read_returns_a_full_message():
    t = FakeTransport(dict(MESSAGE, body={"content": "hello there"}))
    m = mailbox(t).read("abc")
    assert isinstance(m, Message)
    assert m.body_text == "hello there"
    assert m.account == "hotmail"


def test_authorization_header_carries_the_token():
    t = FakeTransport()
    mailbox(t).list_recent()
    assert t.last_headers["Authorization"] == "Bearer token"


def test_list_recent_caps_top_at_graphs_documented_ceiling():
    t = FakeTransport()
    mailbox(t).list_recent(limit=5000)
    assert "%24top=1000" in t.last_url or "$top=1000" in t.last_url


def test_list_recent_selects_an_explicit_field_list():
    """The docs warn that fat pages trigger HTTP 504."""
    t = FakeTransport()
    mailbox(t).list_recent()
    assert "select" in t.last_url
    assert "bodyPreview" in t.last_url


def test_list_recent_returns_summaries_not_full_messages():
    t = FakeTransport()
    result = mailbox(t).list_recent()
    assert [type(m) for m in result] == [MessageSummary]


def test_search_passes_the_query_through():
    t = FakeTransport()
    mailbox(t).search("insurance")
    assert "insurance" in t.last_url


def test_paging_follows_odata_nextlink_verbatim():
    """Never reconstruct $skip: Graph uses it to count items it has walked past,
    so it can exceed the page size even in the first response."""
    t = FakeTransport(
        {"value": [MESSAGE], "@odata.nextLink": "https://graph.microsoft.com/next-page-token"},
        {"value": [dict(MESSAGE, id="def")]},
    )
    result = mailbox(t).list_recent(limit=2)
    assert t.urls[1] == "https://graph.microsoft.com/next-page-token"
    assert [m.id for m in result] == ["abc", "def"]


def test_paging_stops_once_the_limit_is_met():
    t = FakeTransport({"value": [MESSAGE, dict(MESSAGE, id="def")], "@odata.nextLink": "https://x/2"})
    result = mailbox(t).list_recent(limit=2)
    assert len(result) == 2
    assert len(t.urls) == 1


def test_no_write_methods_exist():
    """The read-only promise is the absence of a code path, not a flag."""
    surface = {name for name in dir(GraphMailbox) if not name.startswith("_")}
    assert surface == {"list_recent", "search", "read"}
