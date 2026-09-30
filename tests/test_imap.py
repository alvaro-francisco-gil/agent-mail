"""IMAP transport tests.

The fake records the commands issued. Asserting on what we ask the server
matters more than on a response we already control -- the same reasoning as
tests/test_graph.py, and the reason PEEK-versus-RFC822 is testable at all.
"""
import email
from email.message import EmailMessage

import pytest

from agent_mail.imap import ImapMailbox, StaleMessageId
from agent_mail.models import Message, MessageSummary

ALL_MAIL = "[Gmail]/All Mail"


def rfc822(subject="Hello", sender="Brendan <b@example.com>",
           date="Wed, 29 Jul 2026 20:21:40 +0200", text="body text", html=None):
    m = EmailMessage()
    if sender is not None:
        m["From"] = sender
    if subject is not None:
        m["Subject"] = subject
    if date is not None:
        m["Date"] = date
    if text is not None and html is not None:
        m.set_content(text)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(text or "")
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, folders=("INBOX",), messages=None, uidvalidity=1000):
        self.folders = list(folders)
        # {folder: {uid: raw_bytes}}
        self.messages = messages if messages is not None else {"INBOX": {12: rfc822()}}
        self.uidvalidity = uidvalidity
        self.commands = []
        self.selected = None
        self.logged_out = False

    def list(self):
        self.commands.append(("LIST",))
        return "OK", [f'(\\HasNoChildren) "/" "{f}"'.encode() for f in self.folders]

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        self.selected = folder.strip('"')
        return "OK", [str(len(self.messages.get(self.selected, {}))).encode()]

    def status(self, folder, what):
        self.commands.append(("STATUS", folder, what))
        name = folder.strip('"')
        return "OK", [f'"{name}" (UIDVALIDITY {self.uidvalidity})'.encode()]

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        if command.upper() == "SEARCH":
            uids = sorted(self.messages.get(self.selected, {}))
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if command.upper() == "FETCH":
            uid, spec = int(args[0]), args[1]
            raw = self.messages.get(self.selected, {}).get(uid)
            if raw is None:
                return "OK", [None]
            parts = []
            if "HEADER.FIELDS" in spec:
                # Return only the named fields, as a server does -- wherever they
                # sit in the header block.
                msg = email.message_from_bytes(raw)
                wanted = ("From", "Subject", "Date")
                block = "".join(f"{k}: {msg[k]}\r\n" for k in wanted if msg.get(k))
                head = f"1 (UID {uid} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(block)}}}"
                parts.append((head.encode(), block.encode()))
            msg = email.message_from_bytes(raw)
            part1 = msg.get_payload(0) if msg.is_multipart() else msg
            if "BODY.PEEK[1.MIME]" in spec:
                mime = "".join(f"{k}: {v}\r\n" for k, v in part1.items())
                parts.append((f" BODY[1.MIME] {{{len(mime)}}}".encode(), mime.encode()))
            if "BODY.PEEK[1]<" in spec:
                # As transferred, and truncated, exactly as a server sends it.
                payload = part1.get_payload(decode=False)
                raw_payload = payload.encode() if isinstance(payload, str) else b""
                cap = int(spec.split("BODY.PEEK[1]<0.")[1].split(">")[0])
                raw_payload = raw_payload[:cap]
                parts.append((f" BODY[1]<0> {{{len(raw_payload)}}}".encode(), raw_payload))
            if not parts:
                parts.append((f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
            return "OK", [*parts, b")"]
        raise AssertionError(f"unexpected UID command {command}")

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    @property
    def issued(self):
        return [" ".join(str(p) for p in c) for c in self.commands]


def mailbox(conn, account="gmail"):
    return ImapMailbox(lambda: conn, account=account)


# --- what we ask the server -------------------------------------------------

def test_select_is_readonly_so_nothing_is_marked_seen():
    c = FakeIMAP()
    mailbox(c).list_recent()
    assert any(cmd[0] == "SELECT" and cmd[2] is True for cmd in c.commands)


def test_bodies_are_peeked_never_fetched_as_rfc822():
    """RFC822 sets the \\Seen flag. Reading his mail must not change his mailbox."""
    c = FakeIMAP()
    mailbox(c).list_recent()
    fetches = [i for i in c.issued if "FETCH" in i]
    assert fetches
    assert all("BODY.PEEK" in f for f in fetches)
    assert not any("RFC822" in f for f in fetches)


def test_a_summary_fetch_asks_for_named_headers_not_a_byte_prefix():
    """A byte prefix is not enough: DonDominio prepends a large spam-report block,
    so From/Subject/Date can sit past any fixed cap. Found against the real
    mailbox on 2026-09-30, after the first version returned blank summaries."""
    c = FakeIMAP()
    mailbox(c).list_recent()
    fetches = [i for i in c.issued if "FETCH" in i and "<0." not in i.split("BODY[1]")[0]]
    assert any("HEADER.FIELDS" in i for i in c.issued)


def test_the_snippet_fetch_is_capped_rather_than_pulling_whole_bodies():
    c = FakeIMAP()
    mailbox(c).list_recent()
    assert any("<0." in i for i in c.issued if "FETCH" in i)


def test_headers_are_found_even_behind_a_huge_header_block():
    junk = "".join(f"X-Spam-Report-{i}: {'x' * 80}\r\n" for i in range(80))
    raw = junk.encode() + rfc822(subject="findme", sender="Berto <berto@example.com>")
    c = FakeIMAP(messages={"INBOX": {1: raw}})
    [m] = mailbox(c).list_recent()
    assert m.subject == "findme"
    assert m.sender_address == "berto@example.com"


def test_search_passes_the_query_as_a_text_term():
    c = FakeIMAP()
    mailbox(c).search("insurance")
    assert any("TEXT" in i and "insurance" in i for i in c.issued)


def test_the_connection_is_closed_after_use():
    c = FakeIMAP()
    mailbox(c).list_recent()
    assert c.logged_out


# --- folder scope -----------------------------------------------------------

def test_gmails_all_mail_is_used_alone_because_it_is_a_superset():
    c = FakeIMAP(folders=("INBOX", "Sent", ALL_MAIL), messages={ALL_MAIL: {1: rfc822()}})
    mailbox(c).list_recent()
    selected = [cmd[1].strip('"') for cmd in c.commands if cmd[0] == "SELECT"]
    assert selected == [ALL_MAIL]


def test_without_a_superset_folder_every_folder_is_walked():
    c = FakeIMAP(
        folders=("INBOX", "Sent", "Drafts"),
        messages={"INBOX": {1: rfc822()}, "Sent": {2: rfc822()}, "Drafts": {}},
    )
    mailbox(c, account="cultuvilla").list_recent()
    selected = [cmd[1].strip('"') for cmd in c.commands if cmd[0] == "SELECT"]
    assert set(selected) == {"INBOX", "Sent", "Drafts"}


def test_results_from_several_folders_come_back_newest_first():
    c = FakeIMAP(
        folders=("INBOX", "Sent"),
        messages={
            "INBOX": {1: rfc822(subject="older", date="Wed, 1 Jul 2026 10:00:00 +0000")},
            "Sent": {2: rfc822(subject="newer", date="Fri, 4 Sep 2026 10:00:00 +0000")},
        },
    )
    assert [m.subject for m in mailbox(c, account="cultuvilla").list_recent()] == ["newer", "older"]


def test_only_the_newest_uids_are_fetched_rather_than_the_whole_folder():
    """The first version fetched every matching UID and truncated afterwards,
    which hung for two minutes against Gmail's All Mail. IMAP UIDs ascend with
    arrival, so the highest are the newest: anything below a folder's top `limit`
    is older than `limit` messages in that same folder and cannot make the global
    newest `limit`."""
    c = FakeIMAP(messages={"INBOX": {u: rfc822() for u in range(1, 51)}})
    mailbox(c).list_recent(limit=3)
    assert len([i for i in c.issued if "FETCH" in i]) == 3


def test_the_newest_are_the_ones_kept_when_a_folder_is_capped():
    c = FakeIMAP(messages={"INBOX": {
        1: rfc822(subject="old", date="Wed, 1 Jul 2026 10:00:00 +0000"),
        2: rfc822(subject="mid", date="Fri, 1 Aug 2026 10:00:00 +0000"),
        3: rfc822(subject="new", date="Mon, 1 Sep 2026 10:00:00 +0000"),
    }})
    assert [m.subject for m in mailbox(c).list_recent(limit=2)] == ["new", "mid"]


def test_the_limit_is_honoured_across_folders():
    c = FakeIMAP(
        folders=("INBOX", "Sent"),
        messages={"INBOX": {1: rfc822(), 2: rfc822()}, "Sent": {3: rfc822()}},
    )
    assert len(mailbox(c, account="cultuvilla").list_recent(limit=2)) == 2


# --- ids --------------------------------------------------------------------

def test_an_id_carries_folder_uidvalidity_and_uid():
    c = FakeIMAP(messages={"INBOX": {12: rfc822()}})
    [m] = mailbox(c).list_recent()
    assert m.id == "INBOX:1000:12"


def test_a_folder_with_a_space_survives_the_round_trip():
    c = FakeIMAP(folders=(ALL_MAIL,), messages={ALL_MAIL: {7: rfc822()}})
    [m] = mailbox(c).list_recent()
    assert mailbox(FakeIMAP(folders=(ALL_MAIL,), messages={ALL_MAIL: {7: rfc822()}})).read(m.id)


def test_reading_a_stale_id_is_refused_rather_than_returning_another_message():
    """UIDs are reassigned when UIDVALIDITY changes, so a stale id points at
    whatever now holds that number."""
    c = FakeIMAP(messages={"INBOX": {12: rfc822()}}, uidvalidity=9999)
    with pytest.raises(StaleMessageId, match="1000"):
        mailbox(c).read("INBOX:1000:12")


def test_a_malformed_id_is_refused_clearly():
    with pytest.raises(ValueError, match="not an agent-mail message id"):
        mailbox(FakeIMAP()).read("abc")


# --- normalisation ----------------------------------------------------------

def test_read_returns_a_full_message_with_the_plain_text_part():
    c = FakeIMAP(messages={"INBOX": {12: rfc822(text="the real body", html="<p>ignore</p>")}})
    m = mailbox(c).read("INBOX:1000:12")
    assert isinstance(m, Message)
    assert "the real body" in m.body_text
    assert "<p>" not in m.body_text


def test_an_html_only_message_is_reduced_to_text():
    c = FakeIMAP(messages={"INBOX": {12: rfc822(text=None, html="<p>Hola <b>Álvaro</b></p>")}})
    m = mailbox(c).read("INBOX:1000:12")
    assert "Hola" in m.body_text and "Álvaro" in m.body_text
    assert "<p>" not in m.body_text


def test_the_sender_is_split_into_name_and_address():
    c = FakeIMAP(messages={"INBOX": {12: rfc822(sender="Brendan <b@example.com>")}})
    [m] = mailbox(c).list_recent()
    assert (m.sender_name, m.sender_address) == ("Brendan", "b@example.com")


def test_the_date_is_normalised_to_the_iso_shape_graph_returns():
    c = FakeIMAP(messages={"INBOX": {12: rfc822(date="Wed, 29 Jul 2026 20:21:40 +0200")}})
    [m] = mailbox(c).list_recent()
    assert m.received.startswith("2026-07-29T")


def test_a_message_missing_its_headers_yields_blanks_rather_than_raising():
    """models.py's rule: a mailbox is full of malformed mail and a tool call that
    dies on one of them is worse than one returning a blank subject."""
    c = FakeIMAP(messages={"INBOX": {12: rfc822(sender=None, subject=None, date=None)}})
    [m] = mailbox(c).list_recent()
    assert (m.sender_address, m.subject, m.received) == ("", "", "")


def test_list_recent_returns_summaries_not_full_messages():
    c = FakeIMAP()
    assert [type(m) for m in mailbox(c).list_recent()] == [MessageSummary]


def test_the_account_name_is_carried_onto_every_result():
    c = FakeIMAP()
    [m] = mailbox(c, account="cultuvilla").list_recent()
    assert m.account == "cultuvilla"


# --- the security property --------------------------------------------------

def test_no_write_methods_exist():
    """Read-only is the absence of a code path. Send arrives as its own module,
    behind its own approval, not as a method here."""
    surface = {name for name in dir(ImapMailbox) if not name.startswith("_")}
    assert surface == {"list_recent", "search", "read"}
