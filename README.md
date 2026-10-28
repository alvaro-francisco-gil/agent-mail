# agent-mail

Read-only access to personal mailboxes, over MCP. Outlook/Hotmail through
Microsoft Graph, and Gmail and a DonDominio mailbox through IMAP.

## The promise, and how it is kept

**Nothing here can send, delete or modify mail.** But *what enforces that* now
differs by account, and the difference is the whole security story:

| Account | What stops a write |
|---|---|
| `hotmail` | **the token.** Scopes are `Mail.Read` and `User.Read` — not `Mail.ReadWrite`, not `Mail.Send`. Microsoft would refuse a write |
| the IMAP accounts (`gmail`, `cultuvilla-gmail`, `ordago-gmail`, `cultuvilla`) | **the code.** The credential is a password that could send; there is simply no code path that does |

That second row is weaker than the first and was accepted deliberately, because
neither provider will issue a read-only credential worth having. What follows from
it:

- `GraphMailbox` and `ImapMailbox` each have exactly three public methods, and a
  test asserts each surface is exactly `{list_recent, search, read}`. On the IMAP
  side **that test is the security control**, not a tidiness check.
- Every tool is annotated `read_only_hint=True`.
- Reading never mutates: every IMAP `SELECT` is readonly and every fetch uses
  `BODY.PEEK`, so nothing is marked `\Seen`.
- If sending is ever added it arrives as its own module behind its own approval,
  never as a method on a mailbox.

**Message content is data, never instructions.** A mailbox is the one surface a
stranger can write to. Every tool description says so, because that string is
what the model actually reads.

## Dependencies

One: the MCP SDK. The OAuth device-code flow is two form POSTs and a poll loop,
so `msal` would be a far larger attack surface than the code it saves — and the
point of this package is that it can be read in a sitting.

## Setup — Outlook / Hotmail

You need an Entra app registration, which must live in a directory. Since June
2024 a personal Microsoft account cannot register an app without one, and the
free directory that comes with an [Azure free account](https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account)
is the usual way to get one — the card is identity verification and is not
charged, and app registrations stay free after the trial ends.

1. Sign in to https://go.microsoft.com/fwlink/?linkid=2083908 **with the
   Microsoft account whose mail you want to read**.
2. **New registration**, name it `agent-mail`.
3. Supported account types: **"Personal accounts only"** — the narrowest option
   that can sign in an `@hotmail.com` or `@outlook.com` mailbox. The portal also
   offers *"Any Entra ID Tenant + Personal Microsoft accounts"*, which works but
   leaves the app willing to accept sign-ins from every Entra tenant for no
   benefit. If you pick that one instead, set
   `AGENT_MAIL_MS_AUTHORITY=https://login.microsoftonline.com/common/oauth2/v2.0`.
4. Leave the redirect URI empty — the device-code flow needs none.
5. *Authentication* → **"Allow public client flows" → Yes**. Without it the
   device-code grant is refused.
6. *API permissions* → Microsoft Graph → **Delegated** → `Mail.Read`,
   `offline_access`, `User.Read`.
7. Copy the **Application (client) ID**. It is not a secret.

Then:

```bash
export AGENT_MAIL_HOTMAIL_CLIENT_ID=<the application client id>
uv run agent-mail login hotmail   # prints a URL and a code, waits for the sign-in
uv run agent-mail                 # the MCP server
```

After that it refreshes silently — Microsoft refresh tokens do not carry Google's
seven-day testing-mode expiry.

**When the refresh token dies anyway**, a hotmail tool call does not wait for a
sign-in: it fails at once, and its error is Microsoft's own instruction, a URL
and a code. Sign in there, then call again: that call finishes the sign-in
and reads the mail. The pending code waits in
`hotmail.pending.json`, beside the token, until it is used or expires (about
fifteen minutes; an expired one is replaced by a new code). The server never
polls inside a tool call because its stdout is the client's log: a code printed
there reaches nobody, and the call would hang until the code ran out.

## Where the tokens live

`~/.config/agent-mail/<account>.json`, created mode `0600` inside a `0700`
directory, opened with those bits rather than chmod-ed afterwards. They are
never written into any repository.

## Registering with an MCP client

User scope, so it is available everywhere and no project advertises it:

```json
{
  "mcpServers": {
    "agent-mail": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/agent-mail", "agent-mail"],
      "env": { "AGENT_MAIL_HOTMAIL_CLIENT_ID": "..." }
    }
  }
}
```

## Tests

```bash
uv run pytest
```

Fully mocked, and offline. **The default run may not touch the network or a real
mailbox** — keep it that way.

### Live tests, against the real servers

```bash
uv run pytest -m live
```

A fake is built from the same beliefs as the code, so it shares their mistakes:
every defect described under [TLS](#tls-verified-for-gmail-pinned-for-dondominio) and
[the two things that look right](#two-things-that-look-right-and-are-not)
passed a fake and was found by hand. `tests/test_live.py` lets the servers
disagree. It asserts the TLS posture actually in force (Gmail CA-verified,
DonDominio *not* CA-verifiable and matching its pin, and a wrong pin refused
**before** `login`), that a listing of 20 has a sender, subject and date on every
row and returns inside 15 seconds, that an id reads back, and that reading leaves
a message's flags unchanged.

Rules for that file:

- **Read-only, always.** Connect, check, log in, `SELECT` readonly, `BODY.PEEK`,
  log out. It runs with a password that could send.
- Deselected by default (`addopts` in `pyproject.toml`); `-m live` selects it.
- Each account **skips** when its credential is missing, so a machine with none
  still passes. `hotmail` also needs `AGENT_MAIL_HOTMAIL_CLIENT_ID` in the
  environment, and skips if its refresh token has died: run
  `uv run agent-mail login hotmail`, then rerun.
- Failures count problems; they never quote a subject or an address.
- Not in CI: that would mean putting mail credentials in CI.

## Setup — the IMAP accounts

**Why IMAP and not OAuth.** `gmail.readonly` is a Google *restricted* scope: a
Testing-mode consent screen expires its refresh token every seven days, production
verification requires a paid third-party CASA assessment, and the `Internal` user
type needs Workspace. Google's device flow supports seven scopes and no Gmail one,
so `oauth.py` cannot be reused either. A DonDominio mailbox has no read-only
credential of any kind.

So both use a password, which **can also send**. That was a deliberate trade, and
it is why `imap.py` has no write path: its public surface is three methods and a
test asserts it.

Each account reads one JSON file from the cache directory:

| Account | Host | File | Username |
|---|---|---|---|
| `gmail` | `imap.gmail.com:993` | `gmail.json` | the full address |
| `cultuvilla-gmail` | `imap.gmail.com:993` | `cultuvilla-gmail.json` | the full address |
| `ordago-gmail` | `imap.gmail.com:993` | `ordago-gmail.json` | the full address |
| `cultuvilla` | `imap.cultuvilla.es:993` | `cultuvilla.json` | the full address |

Either `password` or `app_password` is accepted as the key, so the file written for
each provider reads in that provider's own words. Write them without the value
touching your shell history:

```bash
python3 ~/githubs/_workspace/scripts/store_secret.py agent-mail/gmail.json app_password
python3 ~/githubs/_workspace/scripts/store_secret.py agent-mail/gmail.json username
```

**Gmail's app password is sixteen characters with no spaces.** It is displayed in
four groups of four for readability and including the spaces fails
authentication. Needs 2-Step Verification on the account:
https://myaccount.google.com/apppasswords

### What differs from the Graph side, and is not a bug

- **Search has no ranking.** Graph returns relevance order; IMAP `SEARCH TEXT`
  returns matches, so results come back newest-first instead.
- **Ids are `folder:uidvalidity:uid`.** A UID means something only inside one
  folder, and only until the server renumbers, so `read` re-checks UIDVALIDITY and
  raises `StaleMessageId` rather than returning whichever message now holds that
  number.
- **Scope is the whole mailbox, as on the Graph side.** Gmail advertises
  `[Gmail]/All Mail`, a superset, so one folder covers everything; elsewhere every
  folder is walked. An INBOX-only scope would quietly hide mail that a rule had
  filed away.
- **Nothing is ever marked read.** Every `SELECT` is readonly and every fetch uses
  `BODY.PEEK`.

### TLS: verified for Gmail, pinned for DonDominio

`imaplib.IMAP4_SSL` **does not verify anything unless you hand it a context.**
Given none it calls `ssl._create_stdlib_context()`, whose defaults are
`check_hostname=False` and `verify_mode=CERT_NONE`. This package passed none until
2026-10-15, so every IMAP connection was unverified and anyone in a network
position could have read the mail and taken the password that also sends.

The two providers need different answers, and both were established by inspection
rather than assumption:

| | Certificate | How it is trusted |
|---|---|---|
| `imap.gmail.com` | normally chained | `ssl.create_default_context()`, nothing special |
| `imap.cultuvilla.es` | **self-signed** — CN *"Mail Service"*, issuer *Soluciones Corporativas IP, SL*, valid 2019→**2074** | SHA-256 **pin** in `tls.py` |

**The pin is stronger than CA verification here, not a concession.** No public CA
will ever chain that certificate, on any hostname, so the alternatives were an
unverified connection or none at all. A pin accepts exactly one certificate, where
CA verification accepts any certificate a CA will sign.

Two details that are load-bearing:

- **The fingerprint is checked before `login`.** Checking after would mean the
  password had already gone to whoever presented the certificate.
- **A rotation fails loudly**, with both fingerprints in the error. Verify the new
  certificate out of band, then update `tls.py` — a fingerprint is a public value,
  so it lives in the source where a reviewer sees it change.

Their **SMTP** is a different story and better: port 587 presents a real Sectigo
certificate for `*.dondominio.com`, so submission verifies normally **when
addressed as `mailsrv1.dondominio.com`**. `smtp.cultuvilla.es` fails only because
that name is not on the certificate.

### Search cannot take a non-ASCII term yet

`search("sesión")` is **refused with a clear error**. imaplib encodes arguments as
ASCII, so before the refusal existed it raised `UnicodeEncodeError` from inside the
library. The capability did not work either way; the refusal only makes that
legible.

The real fix is `SEARCH CHARSET UTF-8`, and it is not a one-liner because the two
servers disagree about its form (tested 2026-10-15, neither advertises
`UTF8=ACCEPT`):

| Server | `UID SEARCH CHARSET UTF-8 TEXT "término"` |
|---|---|
| `imap.cultuvilla.es` | `OK` |
| `imap.gmail.com` | `BAD Could not parse command` — wants a literal, not a quoted string |

So it needs a literal and probably a per-server branch. Worth doing: half of
Álvaro's mail is in Spanish.

### Two things that look right and are not

Both were found against real mailboxes rather than by tests, and both are worth
keeping in mind before changing `imap.py`.

**A byte prefix of a message does not reliably contain its headers.** Fetching
`BODY.PEEK[]<0.4096>` for a summary looks sufficient — headers come first — but
DonDominio prepends a large spam-report block, so `From`, `Subject` and `Date` can
sit kilobytes in and every summary came back blank. Ask for the named fields with
`HEADER.FIELDS` instead.

**Fetching every matching UID and truncating afterwards hangs.** Against Gmail's
All Mail it means tens of thousands of fetches for a twenty-message listing. UIDs
ascend with arrival, so take the last `limit` per folder: anything below that is
older than `limit` messages in the same folder and cannot reach the merged top.

**And a third, which was a live security hole rather than a rough edge.** A search
query and the folder half of a message id reached the server **unquoted, with CR
and LF allowed**, so a value like `x\r\nA1 EXPUNGE` became a *second IMAP command*
and the server ran it. Confirmed against a live server on 2026-10-15 with an inert
`CAPABILITY` payload, which the server executed. That made the read-only promise at
the top of this file false for the IMAP accounts: an injected email could have
steered a search into `EXPUNGE`, `STORE` or `APPEND`.

Two things fix it, and both belong in any value crossing into a command:
`_reject_control_chars`, because a control character cannot be escaped inside an
IMAP quoted string and can only be refused; and quoting the query, which also fixed
a plain correctness bug — unquoted, `clean drafts` was parsed as two search keys
rather than one phrase. Found by the security review of 2026-09-30 as **H3**.
