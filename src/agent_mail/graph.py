"""Read-only Outlook mailbox over Microsoft Graph v1.0.

Endpoint, permission and query-parameter behaviour are from Microsoft's
documentation, fetched 2026-09-22:
https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0

Note the permission: `Mail.Read`, not `Mail.ReadBasic`. ReadBasic is the
least-privileged option and Microsoft recommends it, but it excludes message
bodies, which is most of what this server is for. It is still read-only --
`Mail.ReadWrite` and `Mail.Send` appear nowhere in this project.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable, Iterable

from .models import Message, MessageSummary

BASE = "https://graph.microsoft.com/v1.0"

# Graph's documented ceiling for $top. Asking for more is an error, not a
# larger page.
MAX_PAGE = 1000

# An explicit $select keeps pages small. The docs warn that returning hundreds
# of messages with full payloads can trigger a gateway timeout (HTTP 504).
SUMMARY_FIELDS = ("id", "subject", "from", "receivedDateTime", "bodyPreview")
FULL_FIELDS = (*SUMMARY_FIELDS, "body", "toRecipients", "ccRecipients")

# Without this, Graph returns bodies as HTML. Plain text is what a model should
# be reading, and it is a request header rather than a query parameter.
PREFER_TEXT = 'outlook.body-content-type="text"'


def _get_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


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

    def list_recent(self, limit: int = 20) -> list[MessageSummary]:
        url = self._url(
            "/me/messages",
            select=SUMMARY_FIELDS,
            top=limit,
            orderby="receivedDateTime desc",
        )
        return [MessageSummary.from_graph(raw, self.account) for raw in self._pages(url, limit)]

    def search(self, query: str, limit: int = 20) -> list[MessageSummary]:
        # $orderby is deliberately not applied here: results come back in
        # Graph's own relevance order for the search term.
        url = self._url("/me/messages", select=SUMMARY_FIELDS, top=limit, search=f'"{query}"')
        return [MessageSummary.from_graph(raw, self.account) for raw in self._pages(url, limit)]

    def read(self, message_id: str) -> Message:
        url = self._url(f"/me/messages/{urllib.parse.quote(message_id)}", select=FULL_FIELDS)
        raw = self.transport(url, self._headers(prefer_text=True))
        return Message.from_graph(raw, self.account)

    def _url(
        self,
        path: str,
        *,
        select: Iterable[str] | None = None,
        top: int | None = None,
        orderby: str | None = None,
        search: str | None = None,
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
