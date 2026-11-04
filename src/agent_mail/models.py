"""The one message shape both providers normalise to.

Outlook and Gmail agree on nothing. Graph returns `receivedDateTime` and a
nested `from.emailAddress.address`; Gmail returns a flat list of RFC 5322
headers and a base64url MIME tree. Normalising at the provider boundary is what
keeps `server.py` free of provider branches, and keeps that difference from
leaking into every caller.

Every field defaults to empty rather than raising. A mailbox is full of
malformed and unusual messages -- drafts with no sender, calendar responses,
delivery failures -- and a tool call that dies on one of them is worse than one
that returns a blank subject.
"""

from __future__ import annotations

import email.header
import email.utils
import html
import mimetypes
import re
from dataclasses import dataclass
from datetime import timezone
from email.message import Message as EmailMessage


def _header_text(value) -> str:
    """Decode an RFC 2047 header.

    Non-ASCII headers travel encoded, and Graph hands them over already decoded.
    Without this, a Spanish subject reaches a caller as
    `=?utf-8?b?c2VzacOzbg==?=`, which is what the real mailbox returned before
    this existed. A charset Python cannot resolve returns the raw text rather than
    raising, on the same principle as the rest of this module.
    """
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)


def _imap_sender(msg: EmailMessage) -> tuple[str, str]:
    """RFC 5322 puts name and address in one field; Graph hands them over split."""
    name, address = email.utils.parseaddr(_header_text(msg.get("From")))
    return name, address


def _imap_received(msg: EmailMessage) -> str:
    """Normalise to the UTC ISO shape Graph returns, so results from two
    providers sort against each other correctly."""
    raw = msg.get("Date")
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        return parsed.isoformat() + "Z"
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def _strip_html(markup: str) -> str:
    """Enough to make HTML readable. Script and style bodies go first, or their
    contents survive tag removal and read as content."""
    text = _DROP.sub(" ", markup)
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()


def _decode(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", "replace")


def imap_body_text(msg: EmailMessage) -> str:
    """The plain-text body, preferring a text/plain part and falling back to
    reduced HTML. Attachments are skipped; HTML never reaches a caller."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return _decode(part).strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                return _strip_html(_decode(part))
        return ""
    body = _decode(msg)
    return _strip_html(body) if msg.get_content_type() == "text/html" else body.strip()


@dataclass(frozen=True)
class Attachment:
    """What a message says it carries; the bytes come from attachments.py."""

    id: str
    name: str
    content_type: str
    size: int
    # Part of the message's layout, usually a logo. Some mailers mark real files
    # inline too, so it is listed all the same.
    inline: bool = False
    # A cloud link rather than a file: the bytes are not in the mailbox.
    link: bool = False

    @classmethod
    def from_graph(cls, raw: dict) -> "Attachment":
        kind = raw.get("@odata.type") or ""
        name = raw.get("name") or ""
        content_type = raw.get("contentType") or ""
        # An attached message or event. Graph names it after its subject and gives
        # no type, and its raw form is what a saved file holds.
        if kind.endswith("itemAttachment"):
            content_type = content_type or "message/rfc822"
            name = name if name.lower().endswith(".eml") else f"{name}.eml"
        return cls(
            id=raw.get("id") or "",
            name=name,
            content_type=content_type or "application/octet-stream",
            size=raw.get("size") or 0,
            inline=bool(raw.get("isInline")),
            link=kind.endswith("referenceAttachment"),
        )


def _imap_parts(msg: EmailMessage):
    """Every leaf of the MIME tree. An attached message counts as one leaf, where
    walk() would descend and list its attachments as this message's own."""
    if msg.get_content_type() != "message/rfc822" and msg.is_multipart():
        for part in msg.get_payload():
            yield from _imap_parts(part)
    else:
        yield msg


def _is_imap_attachment(part: EmailMessage) -> bool:
    """A named part, one marked as an attachment, or a nameless part of a type that
    is never body text. The types are listed because a bounce's
    message/delivery-status is not a file."""
    return bool(part.get_filename() or part.get_content_disposition() == "attachment") or (
        part.get_content_type() == "message/rfc822"
        or part.get_content_maintype() in ("application", "image", "audio", "video")
    )


def _imap_part_bytes(part: EmailMessage) -> bytes:
    if part.get_content_type() == "message/rfc822" and part.is_multipart():
        return part.get_payload(0).as_bytes()
    return part.get_payload(decode=True) or b""


def imap_attachments(msg: EmailMessage) -> list[tuple[Attachment, bytes]]:
    """Each attachment with its bytes. An id is the position in the message, which
    cannot change: a stored message is immutable."""
    found = []
    for part in _imap_parts(msg):
        if not _is_imap_attachment(part):
            continue
        number = str(len(found) + 1)
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        content = _imap_part_bytes(part)
        found.append((
            Attachment(
                id=number,
                name=_header_text(part.get_filename())
                or f"attachment-{number}{mimetypes.guess_extension(content_type) or ''}",
                content_type=content_type,
                size=len(content),
                inline=disposition == "inline" or (disposition is None and bool(part.get("Content-ID"))),
            ),
            content,
        ))
    return found


def _graph_sender(raw: dict) -> dict:
    """Graph sends `from` as an object, as an explicit null, or not at all."""
    sender = raw.get("from") or raw.get("sender") or {}
    return (sender.get("emailAddress") or {}) if isinstance(sender, dict) else {}


@dataclass(frozen=True)
class MessageSummary:
    """Enough to decide whether a message is worth opening."""

    id: str
    account: str
    sender_name: str
    sender_address: str
    subject: str
    received: str
    snippet: str
    # The RFC 5322 Message-ID. Unlike `id`, it survives a move between folders,
    # which is why the handled-mail ledger matches on it as well.
    internet_id: str = ""

    @classmethod
    def from_graph(cls, raw: dict, account: str) -> "MessageSummary":
        address = _graph_sender(raw)
        return cls(
            id=raw.get("id") or "",
            account=account,
            sender_name=address.get("name") or "",
            sender_address=address.get("address") or "",
            subject=raw.get("subject") or "",
            received=raw.get("receivedDateTime") or "",
            snippet=raw.get("bodyPreview") or "",
            internet_id=raw.get("internetMessageId") or "",
        )

    @classmethod
    def from_imap(cls, msg: EmailMessage, account: str, message_id: str,
                  snippet: str = "") -> "MessageSummary":
        name, address = _imap_sender(msg)
        return cls(
            id=message_id,
            account=account,
            sender_name=name,
            sender_address=address,
            subject=_header_text(msg.get("Subject")),
            received=_imap_received(msg),
            snippet=snippet,
            internet_id=str(msg.get("Message-ID") or "").strip(),
        )


@dataclass(frozen=True)
class Message(MessageSummary):
    """A summary plus the body, as plain text.

    `body_text` is plain text by the time it reaches here. Graph is asked for it
    with `Prefer: outlook.body-content-type="text"`; Gmail's MIME tree is walked
    for a text/plain part. HTML never reaches a caller.
    """

    body_text: str = ""
    attachments: tuple[Attachment, ...] = ()

    @classmethod
    def from_graph(cls, raw: dict, account: str, attachments: tuple[Attachment, ...] = ()) -> "Message":
        summary = MessageSummary.from_graph(raw, account)
        body = raw.get("body") or {}
        return cls(
            **summary.__dict__,
            body_text=(body.get("content") or "") if isinstance(body, dict) else "",
            attachments=attachments,
        )

    @classmethod
    def from_imap(cls, msg: EmailMessage, account: str, message_id: str) -> "Message":
        summary = MessageSummary.from_imap(msg, account, message_id)
        return cls(
            **summary.__dict__,
            body_text=imap_body_text(msg),
            attachments=tuple(attachment for attachment, _ in imap_attachments(msg)),
        )
