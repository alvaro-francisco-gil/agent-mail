import json
import os
import stat

import pytest

from agent_mail.ledger import OUTCOMES, Ledger


def test_an_unknown_message_has_no_entry(tmp_path):
    assert Ledger(tmp_path / "handled.jsonl").lookup("gmail", "INBOX:1:2", "<a@b>") is None


def test_a_marked_message_is_found_by_its_id(tmp_path):
    ledger = Ledger(tmp_path / "handled.jsonl")
    ledger.mark("gmail", "INBOX:1:2", "<a@b>", "filed", where="professional/identity/history.md")
    entry = ledger.lookup("gmail", "INBOX:1:2", "")
    assert entry["outcome"] == "filed"
    assert entry["where"] == "professional/identity/history.md"
    assert entry["handled"]


def test_a_moved_message_is_still_found_by_its_message_id(tmp_path):
    """IMAP and Graph ids change when a message changes folder; Message-ID does not."""
    ledger = Ledger(tmp_path / "handled.jsonl")
    ledger.mark("gmail", "INBOX:1:2", "<a@b>", "ignored")
    assert ledger.lookup("gmail", "[Gmail]/All Mail:9:77", "<a@b>")["outcome"] == "ignored"


def test_accounts_do_not_share_entries(tmp_path):
    """The same Message-ID can sit in two mailboxes; handling one is not handling both."""
    ledger = Ledger(tmp_path / "handled.jsonl")
    ledger.mark("gmail", "INBOX:1:2", "<a@b>", "filed")
    assert ledger.lookup("ordago-gmail", "INBOX:1:2", "<a@b>") is None


def test_the_latest_entry_wins(tmp_path):
    ledger = Ledger(tmp_path / "handled.jsonl")
    ledger.mark("hotmail", "AAMk", "", "ignored")
    ledger.mark("hotmail", "AAMk", "", "acted", note="paid the recibo")
    assert ledger.lookup("hotmail", "AAMk", "")["outcome"] == "acted"
    assert len((tmp_path / "handled.jsonl").read_text().splitlines()) == 2


def test_an_outcome_outside_the_list_is_refused(tmp_path):
    with pytest.raises(ValueError, match=", ".join(OUTCOMES)):
        Ledger(tmp_path / "handled.jsonl").mark("gmail", "INBOX:1:2", "", "done")


def test_the_file_is_private(tmp_path):
    """Entries name senders' messages and where their facts went."""
    path = tmp_path / "nested" / "handled.jsonl"
    Ledger(path).mark("gmail", "INBOX:1:2", "", "filed")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_a_torn_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "handled.jsonl"
    Ledger(path).mark("gmail", "INBOX:1:2", "", "filed")
    with open(path, "a") as handle:
        handle.write('{"account": "gm')
    assert Ledger(path).lookup("gmail", "INBOX:1:2", "")["outcome"] == "filed"


def test_entries_carry_no_message_content(tmp_path):
    path = tmp_path / "handled.jsonl"
    Ledger(path).mark("gmail", "INBOX:1:2", "<a@b>", "filed", where="x.md", note="n")
    assert set(json.loads(path.read_text())) == {
        "account", "id", "internet_id", "outcome", "where", "note", "handled"}
