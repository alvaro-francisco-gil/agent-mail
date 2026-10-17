# Hotmail sign-in that a person can see, and that never hangs a tool call

**Priority:** medium — latent: the first hotmail call after the refresh token dies blocks for up to fifteen minutes and shows the sign-in code to no one
**Gate:** decision:when hotmail needs a new sign-in, should the tool call return the URL and code as its error (and finish sign-in on the next call) instead of waiting for it?
**Next:** in `tests/test_oauth.py`, a failing test that a missing token raises
`SignInRequired` carrying the URL and code without entering the polling loop

**Goal:** A dead hotmail refresh token produces an immediate tool error carrying the
sign-in URL and code, instead of a tool call that waits up to fifteen minutes for a
code printed where nobody reads it.

## Context

As of `bbecc8e` (2026-10-16):

- `server._mailbox("hotmail")` in `src/agent_mail/server.py` builds
  `DeviceCodeAuth(client_id, GRAPH_SCOPES, _cache_dir() / "hotmail.json")` with no
  `prompt`, so the default `prompt=print` in `src/agent_mail/oauth.py` applies.
- `DeviceCodeAuth.access_token` falls back to `_device_flow` when there is no
  refresh token, and `_refresh` falls back to it when the refresh is refused ("An
  expired or revoked refresh token lands here"). `_device_flow` then polls the token
  endpoint until `expires_in` runs out (default 900 s), inside the tool call.
- The server runs on stdio (`main` → `mcp.run(transport="stdio")`). With the locked
  `mcp` 2.2.0 (`uv.lock`), `stdio_server` points fd 1 at stderr while serving ("fd 1
  at stderr, so handlers and children … stray output misses the wire",
  `mcp/server/stdio.py`), so `print` lands in the server's stderr: the MCP client's
  log, not the conversation. The person who must type the code never sees it.
- The repo already knows the hang: `test_hotmail_lists_and_reads_back` in
  `tests/test_live.py` passes a prompt that skips, because otherwise "the device-code
  fallback would otherwise poll for fifteen minutes" (README, Live tests). Its advice,
  "re-authorise with the server, then rerun", has no command behind it: the only
  entry point is `agent-mail = "agent_mail.server:main"` in `pyproject.toml`.
- `README.md` says "First run prints a code and a URL", which is true in a terminal
  and not under an MCP client.
- Today's cache at `~/.config/agent-mail/hotmail.json` still holds a refresh token,
  so nothing is broken yet; this is the failure waiting at the end of that token's life.

## Design

**The device flow becomes two non-blocking halves, and the code travels in the error.**

- `DeviceCodeAuth` gains a non-interactive mode used by the server (illustrative):
  when it needs a sign-in it starts the device flow, saves the pending
  `device_code` and its expiry beside the token cache (same `0600` file rules), and
  raises `SignInRequired(message)` carrying Microsoft's own `message` (URL + code).
  The tool call returns that text as its error at once.
- The next `access_token()` call with a pending device code polls the token endpoint
  **once**: success saves tokens and clears the pending entry; `authorization_pending`
  raises `SignInRequired` again with the same code; an expired code starts over.
- The blocking loop stays for a terminal: `agent-mail login hotmail` (a second
  console script, or an argument to `main`) runs today's `_device_flow` with
  `print`, so a person at a shell can sign in without an MCP client, and the live
  test's "re-authorise" advice points at a real command.

The error text is safe to show the model: it is Microsoft's sign-in instruction, not
mail content, and the tool stays read-only.

**Alternatives considered:**
- Only a `login` command, and the server raising "run agent-mail login" — simpler, but
  needs a shell on the machine; returning the code in the error also works from a
  remote session.
- Print to stderr explicitly — already where it goes; the client still hides it.
- MCP elicitation to show the code — depends on client support the repo cannot assume.

**Testing strategy:** in `tests/test_oauth.py`, against the existing fake `_post_form`:
a missing token raises `SignInRequired` with the URL and code and no polling loop; the
next call with `authorization_pending` raises again without sleeping; success saves
tokens and removes the pending entry; an expired pending code starts a new flow; the
pending file is created `0600`. In `tests/test_server.py`: `_mailbox("hotmail")` builds
the non-interactive auth. The live test's skipping prompt can then go.

## Paths

- `src/agent_mail/oauth.py`
- `src/agent_mail/server.py`
- `pyproject.toml` — the `login` entry point
- `tests/test_oauth.py`
- `tests/test_server.py`
- `tests/test_live.py`
- `README.md` — "First run prints a code and a URL" and the Live tests advice

## Approval

**Needs your yes** — the `Gate` question; recommended answer: yes, because the
current failure is silent and long, and the change keeps the read-only scopes and the
one-dependency rule.

## Out of scope

- The IMAP accounts, whose passwords do not expire.
- Changing scopes, the authority or the token cache format beyond the pending entry.
- Raising the `mcp>=1.2` floor in `pyproject.toml` (reported separately: the code
  imports `mcp.server.mcpserver`, a 2.x name).
