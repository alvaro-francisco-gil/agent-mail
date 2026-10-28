"""The server side of the handled-mail ledger; the file itself is in test_ledger.py."""

import pytest

from agent_mail import server
from agent_mail.models import Message, MessageSummary



class FakeMailbox:
    def __init__(self, summaries):
        self.summaries = summaries
        self.reads = []

    def list_recent(self, limit=20):
        return self.summaries[:limit]

    def search(self, query, limit=20):
        return self.summaries[:limit]

    def read(self, message_id):
        self.reads.append(message_id)
        s = next(m for m in self.summaries if m.id == message_id)
        return Message(**s.__dict__, body_text="body")


def summary(n):
    return MessageSummary(id=f"INBOX:1:{n}", account="gmail", sender_name="", sender_address="a@b",
                          subject=f"s{n}", received="2026-10-28T00:00:00+00:00", snippet="",
                          internet_id=f"<{n}@b>")


@pytest.fixture
def fake_gmail(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    box = FakeMailbox([summary(n) for n in (3, 2, 1)])
    monkeypatch.setitem(server._mailboxes, "gmail", box)
    server._internet_ids.clear()
    yield box
    server._mailboxes.clear()
    server._internet_ids.clear()


def test_summaries_say_whether_a_message_was_handled(fake_gmail):
    server.mail_list_recent("gmail")
    server.mail_mark_handled("gmail", "INBOX:1:2", "filed", where="professional/x.md")
    rows = {r["id"]: r for r in server.mail_list_recent("gmail")}
    assert rows["INBOX:1:2"]["handled"]["outcome"] == "filed"
    assert rows["INBOX:1:3"]["handled"] is None


def test_handled_mail_can_be_left_out(fake_gmail):
    server.mail_list_recent("gmail")
    server.mail_mark_handled("gmail", "INBOX:1:2", "ignored")
    assert [r["id"] for r in server.mail_list_recent("gmail", include_handled=False)] == [
        "INBOX:1:3", "INBOX:1:1"]
    assert [r["id"] for r in server.mail_search("x", "gmail", include_handled=False)] == [
        "INBOX:1:3", "INBOX:1:1"]


def test_marking_a_listed_message_does_not_reread_it(fake_gmail):
    server.mail_search("x", "gmail")
    server.mail_mark_handled("gmail", "INBOX:1:2", "acted")
    assert fake_gmail.reads == []


def test_marking_an_unseen_message_reads_it_once_for_its_message_id(fake_gmail):
    entry = server.mail_mark_handled("gmail", "INBOX:1:1", "filed")
    assert fake_gmail.reads == ["INBOX:1:1"]
    assert entry["internet_id"] == "<1@b>"


def test_a_read_message_carries_its_handled_state(fake_gmail):
    server.mail_list_recent("gmail")
    server.mail_mark_handled("gmail", "INBOX:1:3", "filed")
    assert server.mail_read("INBOX:1:3", "gmail")["handled"]["outcome"] == "filed"


def test_the_ledger_lives_in_the_cache_dir_not_the_repo(fake_gmail, tmp_path):
    server.mail_list_recent("gmail")
    server.mail_mark_handled("gmail", "INBOX:1:2", "filed")
    assert (tmp_path / "handled.jsonl").exists()


def test_marking_is_the_one_tool_that_is_not_read_only():
    """It writes a local file, never the mailbox; the annotation must say so honestly."""
    assert server.LEDGER_WRITE.read_only_hint is False
    assert server.LEDGER_WRITE.destructive_hint is False
    assert server.LEDGER_WRITE.open_world_hint is False
