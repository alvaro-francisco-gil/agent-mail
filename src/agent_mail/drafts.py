"""Put a new message in a mailbox's Drafts folder, and nothing else.

This is the one module that writes to a mailbox, and it is separate from
GraphMailbox and ImapMailbox so their read-only surfaces stay exactly as tested.
A draft reaches nobody until a person opens it and clicks send: there is no send,
no delete and no edit here, and a test asserts on that surface too.

The Graph half has its own token, scoped to `Mail.ReadWrite` alone, used only here,
so the reading tools keep a `Mail.Read` token that Microsoft will refuse a write on.
Endpoint and permission are from Microsoft's documentation, fetched 2026-11-03:
https://learn.microsoft.com/en-us/graph/api/user-post-messages
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable

from .imap import _LIST_LINE, _quote

GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"

# Inline attachments on a Graph request must total under 3 MB. Larger ones need an
# upload session, which nothing here has needed yet.
GRAPH_ATTACHMENT_LIMIT = 3 * 1024 * 1024

# Only for a server that does not flag its Drafts folder with RFC 6154's \Drafts.
FALLBACK_DRAFT_FOLDERS = ("Drafts", "INBOX.Drafts")


@dataclass(frozen=True)
class Draft:
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def build_mime(draft: Draft, sender: str) -> bytes:
    """A complete RFC 5322 message. EmailMessage refuses a newline in a header."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    if draft.bcc:
        message["Bcc"] = ", ".join(draft.bcc)
    message["Subject"] = draft.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content(draft.body)
    for path in draft.attachments:
        maintype, subtype = _content_type(path).split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return message.as_bytes()


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _recipients(addresses: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": address}} for address in addresses]


class GraphDrafter:
    def __init__(self, auth, *, transport: Callable[[str, dict, dict], dict] = _post_json) -> None:
        self.auth = auth
        self.transport = transport

    def create(self, draft: Draft) -> str:
        total = sum(path.stat().st_size for path in draft.attachments)
        if total >= GRAPH_ATTACHMENT_LIMIT:
            raise ValueError(f"attachments total {total} bytes; Graph takes under {GRAPH_ATTACHMENT_LIMIT} inline")
        payload = {
            "subject": draft.subject,
            "body": {"contentType": "Text", "content": draft.body},
            "toRecipients": _recipients(draft.to),
            "ccRecipients": _recipients(draft.cc),
            "bccRecipients": _recipients(draft.bcc),
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": path.name,
                    "contentType": _content_type(path),
                    "contentBytes": base64.b64encode(path.read_bytes()).decode(),
                }
                for path in draft.attachments
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.auth.access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        created = self.transport(GRAPH_MESSAGES, headers, payload)
        if created.get("isDraft") is not True:
            raise RuntimeError("Graph did not report the new message as a draft")
        return "Drafts"


def _drafts_folder(conn) -> str:
    """The folder the server flags \\Drafts; Gmail's name follows the UI language."""
    typ, lines = conn.list()
    names = []
    for line in lines or []:
        match = _LIST_LINE.match(line.strip()) if isinstance(line, bytes) else None
        if not match:
            continue
        name = match.group("name").decode().strip().strip('"')
        if re.search(rb"\\Drafts\b", match.group("flags")):
            return name
        names.append(name)
    for fallback in FALLBACK_DRAFT_FOLDERS:
        if fallback in names:
            return fallback
    raise RuntimeError("this mailbox has no folder flagged or named as Drafts")


class ImapDrafter:
    def __init__(self, connect: Callable[[], object], sender: str) -> None:
        self._connect = connect
        self.sender = sender

    def create(self, draft: Draft) -> str:
        raw = build_mime(draft, self.sender)
        conn = self._connect()
        try:
            folder = _drafts_folder(conn)
            typ, data = conn.append(_quote(folder), r"(\Draft \Seen)", None, raw)
            if typ != "OK":
                raise RuntimeError(f"the server refused the draft: {data!r}")
            return folder
        finally:
            conn.logout()
