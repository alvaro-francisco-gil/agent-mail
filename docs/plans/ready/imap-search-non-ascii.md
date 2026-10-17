# IMAP search takes a Spanish term

**Priority:** medium — known pain the README names itself: "Worth doing: half of Álvaro's mail is in Spanish"

**Goal:** `mail_search("sesión", account=…)` works on the four IMAP accounts instead of
being refused, so a Spanish word can be searched for as it is spelled.

## Context

As of `bbecc8e` (2026-10-16):

- `ImapMailbox.search` in `src/agent_mail/imap.py` refuses any query where
  `query.isascii()` is false, with an error that points to the README. The test
  `test_a_non_ascii_query_is_refused_clearly_rather_than_crashing` in
  `tests/test_imap.py` pins that refusal.
- README, "Search cannot take a non-ASCII term yet", records the experiment of
  2026-10-15: `UID SEARCH CHARSET UTF-8 TEXT "término"` returns `OK` on
  `imap.cultuvilla.es` and `BAD Could not parse command` on `imap.gmail.com`, which
  "wants a literal, not a quoted string". Neither server advertises `UTF8=ACCEPT`.
  The README ends: "So it needs a literal and probably a per-server branch."
- Three of the four IMAP accounts are on `imap.gmail.com` (`IMAP_ACCOUNTS` in
  `src/agent_mail/server.py`, after `bbecc8e feat(imap): read the Cultuvilla and
  Órdago Gmail accounts`), so the refusal covers most IMAP mail.
- Folding the accent away is not a workaround: "sesion" and "sesión" are different
  strings to `SEARCH TEXT`.
- The injection fix (`cc0244d fix(imap): refuse control characters -- a search query
  could inject IMAP commands`) quotes the query with `_quote` and refuses control
  characters through `_reject_control_chars`. The new path must keep that refusal.

## Design

**One path for every server: a literal.** An IMAP literal (`{n}` followed by the raw
bytes) is the RFC 3501 form for any string argument, so a server that accepts the
quoted form should accept the literal too. That avoids the per-server branch the
README expected. If the live test shows DonDominio refusing the literal, the
fallback is a per-host choice in `IMAP_ACCOUNTS`, not a guess.

The stdlib already does the framing: `imaplib.IMAP4._command` appends
` {len}` and sends `self.literal` after the server's continuation. So in `search`,
for a non-ASCII query (illustrative):

```python
_reject_control_chars(query, "the search query")
conn.literal = query.encode("utf-8")
conn.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")   # imaplib adds {n} and sends the bytes
```

ASCII queries keep today's quoted path unchanged, so nothing that works now moves.
Because `_summaries` runs the same criteria in every folder in scope, the literal is
set again before each folder's `UID SEARCH` (imaplib clears it after one command).
That means `criteria` grows from a tuple of strings to something that can carry
"set this literal first"; a small `_search(conn, query)` helper in `imap.py` is enough.

Control characters stay refused even though a literal could carry CR/LF safely: the
refusal is the documented security control, and nobody searches for a newline.

**Alternatives considered:**
- Gmail's `X-GM-RAW` extension — Gmail only, so DonDominio would still need the
  literal; two paths for no gain.
- Keep the quoted form for DonDominio and a literal for Gmail — what the README
  expected, and needless if the literal works on both.

**Testing strategy:** unit tests against the fake connection: a non-ASCII query sets
`literal` to its UTF-8 bytes and sends `CHARSET UTF-8 TEXT` in every folder; an ASCII
query is still quoted; a query with CR/LF is still refused before connecting. The
refusal test above is replaced, not kept. Because a fake shares the code's beliefs,
the rule in `AGENTS.md` applies: add a `live` test in `tests/test_live.py` that
searches each IMAP account for a fixed non-ASCII term and asserts the command
returns `OK` (results may be empty), without quoting any result.

## Paths

- `src/agent_mail/imap.py`
- `tests/test_imap.py`
- `tests/test_live.py`
- `README.md` — the "Search cannot take a non-ASCII term yet" section becomes what
  was found
- `src/agent_mail/server.py` — only if the live test needs a per-host choice

## Approval

Decided 2026-10-17 (Álvaro): yes — send non-ASCII IMAP search terms as a `CHARSET UTF-8` literal, verified by a live test on both servers?

## Out of scope

- Non-ASCII on the Graph side (`hotmail`), which already accepts it.
- Escaping a `"` inside a Graph `$search` term (`GraphMailbox.search` interpolates the
  query into `"…"`). Graph documents a backslash escape for quotes inside a clause for
  directory objects; whether that holds for messages is unverified. Worth its own
  small plan if it bites.
- Accent-insensitive matching.
