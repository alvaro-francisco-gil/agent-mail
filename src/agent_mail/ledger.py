"""Which messages have been dealt with, kept on this machine.

The mailboxes cannot carry this, by design: reading never sets \\Seen and the
hotmail token cannot write. So every session used to start blank and go over
mail an earlier session had already filed. This is that memory, and nothing
else: one JSONL line per decision, appended, the latest line for a message
winning.

It lives beside the tokens in the cache directory, not in the repo. The repo is
public, and an entry says which message was handled and where its fact went.
No subject, sender or body is ever written here.

A message is matched on its provider id or its Message-ID header, within one
account. Provider ids change when a message moves folder; Message-ID does not,
but the same one can sit in two mailboxes, which is why the account is part of
the key.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES = ("filed", "ignored", "acted")


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def mark(self, account: str, message_id: str, internet_id: str, outcome: str,
             *, where: str = "", note: str = "") -> dict:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of: {', '.join(OUTCOMES)}")
        entry = {
            "account": account,
            "id": message_id,
            "internet_id": internet_id,
            "outcome": outcome,
            "where": where,
            "note": note,
            "handled": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        # Created 0600 in one step, as the token cache is.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def lookup(self, account: str, message_id: str, internet_id: str) -> dict | None:
        found = None
        for entry in self._entries():
            if entry.get("account") != account:
                continue
            if (message_id and entry.get("id") == message_id) or (
                internet_id and entry.get("internet_id") == internet_id
            ):
                found = entry
        return found

    def _entries(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                yield json.loads(line)
            except ValueError:
                # A write cut short by a crash leaves half a line. Losing that one
                # entry is better than losing the ledger.
                continue
