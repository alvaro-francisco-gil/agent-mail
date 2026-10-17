# Listing and searching mail by date

**Priority:** medium — today no tool can reach mail older than the newest `limit` matches, which the record repo's mail sweep and every "when did this arrive" question need

**Goal:** An agent can ask for mail from a date window ("March 2025 from the AEAT")
instead of only the newest N, on all five accounts, without a new tool or a new
mailbox method.

## Context

As of `bbecc8e` (2026-10-16):

- The three tools in `src/agent_mail/server.py` take `account`, `limit` and (for
  search) `query`; nothing else. `mail_list_recent` returns the newest `limit`
  messages; `mail_search` the newest (IMAP) or Graph-ordered (hotmail) `limit` matches.
  There is no offset or date, so anything below the top `limit` is unreachable by
  listing, and a common term reaches only recent hits.
- IMAP: `ImapMailbox._folder_summaries` fetches the last `limit` UIDs from
  `UID SEARCH` per folder — the deliberate fix recorded under "Fetching every matching
  UID and truncating afterwards hangs" in the README. Raising `limit` to dig deeper is
  exactly that slow path, so date criteria are the way down, not bigger limits.
- Graph: `GraphMailbox.list_recent` sends `$orderby=receivedDateTime desc`;
  `search` sends `$search`. Microsoft documents KQL `received` as a searchable
  message property and that "A `$search` request returns up to 1,000 results"
  (learn.microsoft.com/graph/search-query-parameter). The same page says message
  search results "are sorted by the date and time the message was sent", not by
  relevance as the `mail_search` description and the comment in `graph.py` claim;
  the README line for the dated tools should state the order the live test observes.
- Demand outside this repo: `professional/docs/plans/ideas/mail-history-sweep.md`
  (4 878 readable messages, 255 `[unknown:]` markers to answer) wants a census of
  sender domain, count and first/last date per domain across whole mailboxes, turns
  each marker into a search, and leaves open "a date floor per phase-2 query" — which
  only a date parameter can express.

## Design

**Two optional parameters, threaded through the existing three methods.** The surface
tests (`{list_recent, search, read}` on both mailboxes) stay exactly as they are: this
adds keyword arguments, not methods.

Illustrative:

```python
def mail_search(query: str, account: str = "hotmail", limit: int = 20,
                since: str | None = None, before: str | None = None) -> list[dict]:
    """since/before: YYYY-MM-DD; since is inclusive, before exclusive."""
```

- **Parsing in one place:** `server.py` parses `YYYY-MM-DD` with `date.fromisoformat`
  and passes `datetime.date` objects down, refusing anything else with a clear error
  (the dates then cannot carry an injection into either protocol).
- **IMAP:** append `SINCE dd-Mon-yyyy` / `BEFORE dd-Mon-yyyy` to the `UID SEARCH`
  criteria in `_summaries`. RFC 3501 defines both on the internal date, ignoring time
  and zone, which matches day granularity. The "last `limit` UIDs per folder" rule
  still holds within the window.
- **Graph, listing:** `$filter=receivedDateTime ge …Z and receivedDateTime lt …Z`
  beside the existing `$orderby=receivedDateTime desc` (the ordered property also in
  the filter).
- **Graph, search:** a KQL `received>=… AND received<…` term inside `$search`.
  Today `search()` wraps the whole query in one pair of double quotes
  (`search=f'"{query}"'`), so the date term has to be composed into that quoted
  string (or the clauses re-quoted), and a `"` in the caller's query must be escaped
  so it cannot close the string early and add KQL terms of its own. It goes in
  `$search` rather than `$filter` because a date `$filter` combined with `$search` on messages is not something to
  assume; the live test settles which form Graph accepts.
- Tool descriptions say the dates are the received date, and keep the `UNTRUSTED` text.

**Alternatives considered:**
- An `offset` / page cursor — on IMAP it means walking UIDs from the top again each
  page across folders; dates are what a person actually asks for.
- A separate `mail_census` tool for sender-domain counts — larger, and a new surface;
  the sweep can be built on dated listings first.

**Testing strategy:** unit tests per layer at the fakes: `server` refuses a malformed
date before building a mailbox; IMAP sends `SINCE`/`BEFORE` in the right format in
every folder and nothing new when neither is given; Graph builds the `$filter` URL for
listing and the KQL term for search. One `live` test per transport in
`tests/test_live.py` lists a one-week window a month back and asserts every row's
`received` falls inside it, quoting nothing.

## Paths

- `src/agent_mail/server.py`
- `src/agent_mail/imap.py`
- `src/agent_mail/graph.py`
- `tests/test_server.py`
- `tests/test_imap.py`
- `tests/test_graph.py`
- `tests/test_live.py`
- `README.md` — the tools' parameters and the received-date semantics

## Approval

Decided 2026-10-17 (Álvaro): yes — add optional `since` / `before` dates to `mail_list_recent` and `mail_search` on every account?

## Out of scope

- Pagination or offsets.
- Sender-domain aggregation or any census tool.
- Searching by sender, recipient or attachment as separate fields.
