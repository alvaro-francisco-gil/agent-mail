"""Fetch one attachment of a message and keep it in a private folder.

Separate from GraphMailbox and ImapMailbox so their surfaces stay exactly as tested.
It reads the way they do: the Graph half holds the `Mail.Read` token, and the IMAP
half goes through imap._message_bytes, readonly and with PEEK. A test asserts each
class has one public method, `fetch`.

The file goes to disk rather than back through the tool result, because a PDF as
base64 would fill a conversation and tell the model nothing. The folder is
outside the credential directory, so a saved file can go on a draft.

Anyone can email a file, so its name is hostile until `_safe_name` has been over it.
Endpoints and permission are from Microsoft's documentation, fetched 2026-11-04:
https://learn.microsoft.com/en-us/graph/api/attachment-get
"""

from __future__ import annotations

import email
import hashlib
import os
import shutil
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .graph import ATTACHMENT_FIELDS, BASE, _get_json
from .imap import _message_bytes
from .models import Attachment, imap_attachments

MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024

# Anything deleted can be fetched again from the mailbox, so this costs little.
RETENTION_DAYS = 30

# Most filesystems stop at 255 bytes; this leaves room for a suffix to survive.
MAX_NAME_BYTES = 200
MAX_SUFFIX_CHARS = 16


@dataclass(frozen=True)
class AttachmentFile:
    name: str
    content_type: str
    content: bytes


def _refuse_if_too_large(attachment: Attachment) -> None:
    if attachment.size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"{attachment.name!r} is {attachment.size} bytes; the limit is {MAX_ATTACHMENT_BYTES}"
        )


def _get_bytes(url: str, headers: dict) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


class GraphAttachments:
    def __init__(
        self,
        auth,
        *,
        transport: Callable[[str, dict], dict] = _get_json,
        download: Callable[[str, dict], bytes] = _get_bytes,
    ) -> None:
        self.auth = auth
        self.transport = transport
        self.download = download

    def fetch(self, message_id: str, attachment_id: str) -> AttachmentFile:
        message, attachment_part = (urllib.parse.quote(value, safe="") for value in (message_id, attachment_id))
        url = f"{BASE}/me/messages/{message}/attachments/{attachment_part}"
        headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
        select = urllib.parse.urlencode({"$select": ",".join(ATTACHMENT_FIELDS)}, safe=",")
        attachment = Attachment.from_graph(self.transport(f"{url}?{select}", headers))
        if attachment.link:
            raise ValueError(f"{attachment.name!r} is a cloud link, not a file in the mailbox")
        # Asked before the download, which is the point of a limit here.
        _refuse_if_too_large(attachment)
        return AttachmentFile(attachment.name, attachment.content_type, self.download(f"{url}/$value", headers))


class ImapAttachments:
    def __init__(self, connect: Callable[[], object]) -> None:
        self._connect = connect

    def fetch(self, message_id: str, attachment_id: str) -> AttachmentFile:
        raw = _message_bytes(self._connect, message_id)
        for attachment, content in imap_attachments(email.message_from_bytes(raw)):
            if attachment.id == attachment_id:
                _refuse_if_too_large(attachment)
                return AttachmentFile(attachment.name, attachment.content_type, content)
        raise ValueError(f"no attachment {attachment_id!r} in this message; mail_read lists them")


def attachments_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(cache).expanduser() / "agent-mail" / "attachments"


def _safe_name(name: str) -> str:
    """One plain path component, whatever the sender wrote.

    Directories go, so `../../.bashrc` cannot leave the folder. Control and format
    characters go too: a right-to-left override makes `exe.pdf` of `fdp.exe`.
    """
    text = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    if len(text.encode()) > MAX_NAME_BYTES:
        stem, suffix = os.path.splitext(text)
        suffix = suffix if len(suffix) <= MAX_SUFFIX_CHARS else ""
        room = MAX_NAME_BYTES - len(suffix.encode())
        text = stem.encode()[:room].decode("utf-8", "ignore") + suffix
    return text or "attachment"


def _prune(root: Path) -> None:
    """A failure here must not lose the file being saved."""
    cutoff = time.time() - RETENTION_DAYS * 24 * 60 * 60
    for folder in root.iterdir():
        try:
            if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def save(file: AttachmentFile, root: Path, key: str) -> Path:
    """Under a folder named for `key`, so two attachments called `scan.pdf` stay
    apart and saving one twice leaves one file."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    _prune(root)
    folder = root / hashlib.sha256(key.encode()).hexdigest()[:32]
    folder.mkdir(exist_ok=True, mode=0o700)
    path = folder / _safe_name(file.name)
    # Created 0600 in one step, as the token cache is. The content came from a
    # stranger, so it is never marked executable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(file.content)
    # Rewriting a file leaves its folder's clock alone, and that clock is its age.
    os.utime(folder)
    return path
