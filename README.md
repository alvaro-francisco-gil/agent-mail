# agent-mail

Read-only access to a personal mailbox, over MCP. Outlook/Hotmail via Microsoft
Graph today; Gmail to follow.

## The promise, and how it is kept

**Nothing here can send, delete or modify mail.** That is not a setting:

- The OAuth scopes requested are `Mail.Read` and `User.Read`. Not
  `Mail.ReadWrite`, not `Mail.Send`.
- `GraphMailbox` has exactly three public methods, and a test asserts that the
  public surface is exactly `{list_recent, search, read}` so it cannot widen by
  accident.
- Every tool is annotated `read_only_hint=True`.

**Message content is data, never instructions.** A mailbox is the one surface a
stranger can write to. Every tool description says so, because that string is
what the model actually reads.

## Dependencies

One: the MCP SDK. The OAuth device-code flow is two form POSTs and a poll loop,
so `msal` would be a far larger attack surface than the code it saves — and the
point of this package is that it can be read in a sitting.

## Setup — Outlook / Hotmail

You need an Entra app registration. It is free and takes about five minutes.

1. Sign in to https://go.microsoft.com/fwlink/?linkid=2083908 **with the
   Microsoft account whose mail you want to read**.
2. **New registration**, name it `agent-mail`.
3. Supported account types: **"Accounts in any organizational directory and
   personal Microsoft accounts"**. Anything narrower excludes a personal
   `@hotmail.com` or `@outlook.com` address.
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
      "args": ["run", "--directory", "/home/powervaro/githubs/agent-mail", "agent-mail"],
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

## Gmail

Not wired up. `gmail.readonly` is a Google *restricted* scope: a Testing-mode
consent screen issues refresh tokens that expire every seven days, production
verification requires a third-party CASA security assessment, and the `Internal`
user type needs Google Workspace. Testing mode plus a weekly re-authorisation is
the plan.
