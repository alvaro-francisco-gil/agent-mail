"""Read-only Outlook mailbox over Microsoft Graph v1.0.

Endpoint, permission and query-parameter behaviour are from Microsoft's
documentation, fetched 2026-09-22:
https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0

Note the permission: `Mail.Read`, not `Mail.ReadBasic`. ReadBasic is the
least-privileged option and Microsoft recommends it, but it excludes message
bodies, which is most of what this server is for. It is still read-only:
`Mail.Send` appears nowhere in this project, and `Mail.ReadWrite` only on the
separate token drafts.py uses.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from typing import Callable, Iterable

from .models import Attachment, Message, MessageSummary

BASE = "https://graph.microsoft.com/v1.0"

# Graph's documented ceiling for $top. Asking for more is an error, not a
# larger page.
MAX_PAGE = 1000

# An explicit $select keeps pages small. The docs warn that returning hundreds
# of messages with full payloads can trigger a gateway timeout (HTTP 504).
SUMMARY_FIELDS = ("id", "subject", "from", "receivedDateTime", "bodyPreview", "internetMessageId")
FULL_FIELDS = (*SUMMARY_FIELDS, "body", "toRecipients", "ccRecipients", "hasAttachments")
# Named so that a listing never carries the contentBytes of every file.
ATTACHMENT_FIELDS = ("id", "name", "contentType", "size", "isInline")

# Without this, Graph returns bodies as HTML. Plain text is what a model should
# be reading, and it is a request header rather than a query parameter.
PREFER_TEXT = 'outlook.body-content-type="text"'


def _get_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _kql(query: str, since: date | None, before: date | None) -> str:
    """The $search value: one double-quoted KQL string.

    The caller's `"` and `\\` are escaped so the query cannot close the string
    early and add terms of its own. With a window, the query is parenthesised so
    an OR inside it cannot swallow the dates: unparenthesised, `a OR b AND
    received>=...` ignored the window when tried live on 2026-11-20. The dates go
    in KQL because Graph answers 400 SearchWithFilter to a $filter beside
    $search (tried live, 2026-11-20).
    """
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    terms = []
    if since:
        terms.append(f"received>={since.isoformat()}")
    if before:
        terms.append(f"received<{before.isoformat()}")
    if not terms:
        return f'"{escaped}"'
    return '"' + " AND ".join([f"({escaped})", *terms]) + '"'


class GraphMailbox:
    """The three read operations, and nothing else.

    There is no send, no delete, no mark-as-read and no draft method. That
    absence is the security property of this package; a test asserts on the
    public surface so it cannot be widened by accident.
    """

    def __init__(
        self,
        auth,
        account: str = "hotmail",
        *,
        transport: Callable[[str, dict], dict] = _get_json,
    ) -> None:
        self.auth = auth
        self.account = account
        self.transport = transport

    def list_recent(self, limit: int = 20, *, since: date | None = None,
                    before: date | None = None) -> list[MessageSummary]:
        # Graph wants an $orderby property in the $filter too, and first; the
        # window is on that same property, so both hold.
        clauses = []
        if since:
            clauses.append(f"receivedDateTime ge {since.isoformat()}T00:00:00Z")
        if before:
            clauses.append(f"receivedDateTime lt {before.isoformat()}T00:00:00Z")
        url = self._url(
            "/me/messages",
            select=SUMMARY_FIELDS,
            top=limit,
            orderby="receivedDateTime desc",
            filter=" and ".join(clauses) or None,
        )
        return [MessageSummary.from_graph(raw, self.account) for raw in self._pages(url, limit)]

    def search(self, query: str, limit: int = 20, *, since: date | None = None,
               before: date | None = None) -> list[MessageSummary]:
        # $orderby cannot be combined with $search on messages; Graph returns
        # matches newest first on its own (seen live, 2026-11-20).
        url = self._url("/me/messages", select=SUMMARY_FIELDS, top=limit,
                        search=_kql(query, since, before))
        return [MessageSummary.from_graph(raw, self.account) for raw in self._pages(url, limit)]

    def read(self, message_id: str) -> Message:
        url = self._url(f"/me/messages/{urllib.parse.quote(message_id)}", select=FULL_FIELDS)
        raw = self.transport(url, self._headers(prefer_text=True))
        attachments: tuple[Attachment, ...] = ()
        # Graph answers false when a message holds only inline parts, so a
        # signature logo costs no second request and does not get listed.
        if raw.get("hasAttachments"):
            url = self._url(f"/me/messages/{urllib.parse.quote(message_id)}/attachments", select=ATTACHMENT_FIELDS)
            attachments = tuple(Attachment.from_graph(item) for item in self._pages(url, MAX_PAGE))
        return Message.from_graph(raw, self.account, attachments)

    def _url(
        self,
        path: str,
        *,
        select: Iterable[str] | None = None,
        top: int | None = None,
        orderby: str | None = None,
        search: str | None = None,
        filter: str | None = None,
    ) -> str:
        params: dict[str, str] = {}
        if select:
            params["$select"] = ",".join(select)
        if top is not None:
            params["$top"] = str(max(1, min(top, MAX_PAGE)))
        if orderby:
            params["$orderby"] = orderby
        if search:
            params["$search"] = search
        if filter:
            params["$filter"] = filter
        query = urllib.parse.urlencode(params, safe='",: ')
        return f"{BASE}{path}" + (f"?{query}" if query else "")

    def _headers(self, *, prefer_text: bool = False) -> dict:
        headers = {
            "Authorization": f"Bearer {self.auth.access_token()}",
            "Accept": "application/json",
        }
        if prefer_text:
            headers["Prefer"] = PREFER_TEXT
        return headers

    def _pages(self, url: str, limit: int) -> list[dict]:
        """Walk @odata.nextLink until the limit is met.

        The nextLink is used exactly as returned. Graph's own docs are explicit
        that you must not extract $skip from it and drive paging yourself: it
        counts every item walked past, so it can already exceed the page size in
        the first response.
        """
        collected: list[dict] = []
        while url and len(collected) < limit:
            page = self.transport(url, self._headers(prefer_text=True))
            collected.extend(page.get("value") or [])
            url = page.get("@odata.nextLink") or ""
        return collected[:limit]
