"""Read-only IMAP mailbox, for the providers that will not issue a read-only token.

Gmail's `gmail.readonly` is a Google *restricted* scope: an OAuth token for it
costs either a re-authorisation every seven days or a paid CASA assessment. A
DonDominio mailbox has no read-only credential at all. Both are reached here with
a password that can also send, which was a deliberate trade -- and the reason this
module has no write path of any kind. Send, when it exists, is its own module
behind its own approval prompt.

Two things are load-bearing and easy to undo by accident:

  * **Every fetch uses BODY.PEEK.** A plain RFC822 fetch sets the \\Seen flag, and
    reading somebody's mail must not change their mailbox.
  * **Every SELECT is readonly.** Belt to the same braces.

Ids carry the folder and the UIDVALIDITY as well as the UID, because an IMAP UID
is unique only within one folder and only until the server renumbers. A bare UID
would silently read a different message after a renumber.
"""

from __future__ import annotations

import email
import re
from typing import Callable, Iterable

from .models import Message, MessageSummary, imap_body_text

# Folders that already contain every message, so walking the rest is waste.
# Gmail's own superset; it excludes Trash and Spam, which is Gmail's semantics
# rather than ours.
SUPERSET_FOLDERS = ("[Gmail]/All Mail",)

# A summary asks for three named header fields rather than a byte prefix of the
# message. A prefix looks sufficient and is not: DonDominio prepends a large
# spam-report block, so From, Subject and Date can sit several kilobytes in, and
# the first version of this returned blank summaries for every message in that
# mailbox. Naming the fields is also far less data.
SUMMARY_FIELDS = "(FROM SUBJECT DATE)"

# The snippet needs part 1's MIME header as well as its first bytes, because
# BODY[1] arrives as transferred -- base64 without its Content-Transfer-Encoding
# is gibberish, and a gibberish snippet is worse than none.
SNIPPET_BYTES = 600
SNIPPET_CHARS = 200

SUMMARY_SPEC = (
    f"(UID BODY.PEEK[HEADER.FIELDS {SUMMARY_FIELDS}] "
    f"BODY.PEEK[1.MIME] BODY.PEEK[1]<0.{SNIPPET_BYTES}>)"
)

_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"[^"]*"\s+(?P<name>.+)$')


class StaleMessageId(Exception):
    """The folder was renumbered, so this id no longer means what it meant."""


def encode_id(folder: str, uidvalidity: str | int, uid: str | int) -> str:
    return f"{folder}:{uidvalidity}:{uid}"


def decode_id(message_id: str) -> tuple[str, str, str]:
    """Split from the right, so a folder name containing a colon survives."""
    parts = message_id.rsplit(":", 2)
    if len(parts) != 3 or not all(parts) or not parts[1].isdigit() or not parts[2].isdigit():
        raise ValueError(
            f"{message_id!r} is not an agent-mail message id; expected "
            "folder:uidvalidity:uid, as returned by mail_search or mail_list_recent"
        )
    return parts[0], parts[1], parts[2]


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _folder_names(conn) -> list[str]:
    typ, lines = conn.list()
    names = []
    for line in lines or []:
        if line is None:
            continue
        raw = line if isinstance(line, bytes) else str(line).encode()
        match = _LIST_LINE.match(raw.strip())
        if not match:
            continue
        if b"\\Noselect" in match.group("flags"):
            continue
        names.append(match.group("name").decode().strip().strip('"'))
    return names


def _scope(conn) -> list[str]:
    """One superset folder if the server has one, otherwise every folder.

    The hotmail side reads the whole mailbox -- Graph's /me/messages spans
    folders -- so a message filed out of the inbox stays visible. Matching that
    is the point; an INBOX-only scope would quietly make mail invisible.
    """
    names = _folder_names(conn)
    for superset in SUPERSET_FOLDERS:
        if superset in names:
            return [superset]
    return names or ["INBOX"]


def _uidvalidity(conn, folder: str) -> str:
    typ, data = conn.status(_quote(folder), "(UIDVALIDITY)")
    for item in data or []:
        raw = item if isinstance(item, bytes) else str(item).encode()
        found = re.search(rb"UIDVALIDITY\s+(\d+)", raw)
        if found:
            return found.group(1).decode()
    return "0"


def _literals(data) -> list[tuple[bytes, bytes]]:
    """imaplib hands each literal back as (response-head, payload)."""
    out = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
            head = item[0] if isinstance(item[0], (bytes, bytearray)) else b""
            out.append((bytes(head), bytes(item[1])))
    return out


def _literal(parts, marker: bytes) -> bytes | None:
    for head, payload in parts:
        if marker in head:
            return payload
    return None


def _raw_message(data) -> bytes | None:
    parts = _literals(data)
    return parts[0][1] if parts else None


def _fetch(conn, uid: str, spec: str) -> bytes | None:
    typ, data = conn.uid("FETCH", str(uid), spec)
    return _raw_message(data)


def _snippet(mime_header: bytes | None, body: bytes | None) -> str:
    """Decode part 1 using its own MIME header, so transfer encodings survive.

    A truncated base64 payload can fail to decode; an empty snippet is the right
    answer then, since the fields that identify a message are the headers.
    """
    if not body:
        return ""
    try:
        part = email.message_from_bytes((mime_header or b"") + b"\r\n" + body)
        text = imap_body_text(part) or body.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a snippet must never fail a listing
        text = body.decode("utf-8", "replace")
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_CHARS]


class ImapMailbox:
    """The three read operations, and nothing else.

    There is no send, no delete, no mark-as-read and no draft method, and a test
    asserts on the public surface so it cannot be widened by accident.
    """

    def __init__(self, connect: Callable[[], object], account: str = "gmail"):
        self._connect = connect
        self.account = account

    # -- the three operations ------------------------------------------------

    def list_recent(self, limit: int = 20) -> list[MessageSummary]:
        return self._summaries(("ALL",), limit)

    def search(self, query: str, limit: int = 20) -> list[MessageSummary]:
        return self._summaries(("TEXT", query), limit)

    def read(self, message_id: str) -> Message:
        folder, uidvalidity, uid = decode_id(message_id)
        conn = self._connect()
        try:
            live = _uidvalidity(conn, folder)
            if live != uidvalidity:
                raise StaleMessageId(
                    f"{folder} was renumbered since this id was issued "
                    f"(UIDVALIDITY was {uidvalidity}, is now {live}); search again"
                )
            conn.select(_quote(folder), readonly=True)
            raw = _fetch(conn, uid, "(UID BODY.PEEK[])")
            if raw is None:
                raise ValueError(f"no message {uid} in {folder}")
            return Message.from_imap(email.message_from_bytes(raw), self.account, message_id)
        finally:
            self._close(conn)

    # -- internals -----------------------------------------------------------

    def _summaries(self, criteria: Iterable[str], limit: int) -> list[MessageSummary]:
        conn = self._connect()
        found: list[MessageSummary] = []
        try:
            for folder in _scope(conn):
                found.extend(self._folder_summaries(conn, folder, criteria, limit))
        finally:
            self._close(conn)
        found.sort(key=lambda m: m.received, reverse=True)
        return found[:limit]

    def _folder_summaries(self, conn, folder, criteria, limit) -> list[MessageSummary]:
        conn.select(_quote(folder), readonly=True)
        uidvalidity = _uidvalidity(conn, folder)
        typ, data = conn.uid("SEARCH", None, *criteria)
        uids = (data[0] or b"").split() if data else []
        out = []
        # UIDs ascend with arrival, so the newest are at the end. Fetch only that
        # many: anything below a folder's top `limit` is older than `limit`
        # messages in the same folder, so it cannot reach the merged top `limit`.
        # Fetching the lot first and truncating afterwards is correct and hangs
        # for minutes against Gmail's All Mail, which is how this was found.
        for uid in uids[-max(limit, 1):][::-1]:
            typ, data = conn.uid("FETCH", uid.decode(), SUMMARY_SPEC)
            parts = _literals(data)
            headers = _literal(parts, b"HEADER.FIELDS")
            if headers is None:
                continue
            out.append(MessageSummary.from_imap(
                email.message_from_bytes(headers), self.account,
                encode_id(folder, uidvalidity, uid.decode()),
                snippet=_snippet(_literal(parts, b"BODY[1.MIME]"),
                                 _literal(parts, b"BODY[1]<")),
            ))
        return out

    @staticmethod
    def _close(conn) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - a failed logout must not lose results
            pass
