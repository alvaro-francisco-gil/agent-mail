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

from dataclasses import dataclass


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
        )


@dataclass(frozen=True)
class Message(MessageSummary):
    """A summary plus the body, as plain text.

    `body_text` is plain text by the time it reaches here. Graph is asked for it
    with `Prefer: outlook.body-content-type="text"`; Gmail's MIME tree is walked
    for a text/plain part. HTML never reaches a caller.
    """

    body_text: str = ""

    @classmethod
    def from_graph(cls, raw: dict, account: str) -> "Message":
        summary = MessageSummary.from_graph(raw, account)
        body = raw.get("body") or {}
        return cls(
            **summary.__dict__,
            body_text=(body.get("content") or "") if isinstance(body, dict) else "",
        )
