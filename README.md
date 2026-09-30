# agent-mail

Read-only access to personal mailboxes, over MCP. Outlook/Hotmail through
Microsoft Graph, and Gmail and a DonDominio mailbox through IMAP.

## The promise, and how it is kept

**Nothing here can send, delete or modify mail.** But *what enforces that* now
differs by account, and the difference is the whole security story:

| Account | What stops a write |
|---|---|
| `hotmail` | **the token.** Scopes are `Mail.Read` and `User.Read` — not `Mail.ReadWrite`, not `Mail.Send`. Microsoft would refuse a write |
| `gmail`, `cultuvilla` | **the code.** The credential is a password that could send; there is simply no code path that does |

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
uv run agent-mail
```

First run prints a code and a URL. After that it refreshes silently — Microsoft
refresh tokens do not carry Google's seven-day testing-mode expiry.

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

Fully mocked. **No test may touch the network or a real mailbox** — keep it that
way.

## Setup — the IMAP accounts (`gmail`, `cultuvilla`)

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
| `cultuvilla` | `imap.cultuvilla.es:993` | `cultuvilla.json` | the full address |

Either `password` or `app_password` is accepted as the key, so the file written for
each provider reads in that provider's own words. Write them without the value
touching your shell history:

```bash
python3 ~/githubs/scripts/store_secret.py agent-mail/gmail.json app_password
python3 ~/githubs/scripts/store_secret.py agent-mail/gmail.json username
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
