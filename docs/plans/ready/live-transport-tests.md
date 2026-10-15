# Tests that touch the real servers, read-only

**Priority:** high — four shipped defects had one cause, and this is it
**Gate:** none
**Next:** a `live` pytest marker, excluded by default, with one connect-and-verify
test per account

## Goal

A small set of tests that talk to the actual mail servers, so a wrong belief about
the real world fails in a test run instead of in conversation weeks later.

## Why

Four defects reached production in two sessions, all found by hand. Each was a
wrong belief about reality rather than a logic error, and **the fake shared every
one of those beliefs**, because it was built from the same assumptions as the code:

| Assumed | Actually |
|---|---|
| Headers sit at the front of a message | DonDominio prepends kilobytes of spam-report headers |
| A folder holds dozens of messages | Gmail's All Mail holds 2 444 |
| A search term is inert text | A newline in it becomes a second IMAP command |
| `IMAP4_SSL` verifies certificates | Only if handed a context; otherwise not at all |

The last two were security defects. Details and evidence for all four are in
[README.md](../../../README.md) — this plan does not restate them.

**A fake cannot report that its author misunderstood the protocol.** That is the
gap, and it is not closed by writing more unit tests.

## Approach

Keep the fake for logic; add a thin live layer for reality. Tests marked `live`,
deselected by default so the suite stays offline and fast, run with
`pytest -m live` and skipped when a credential file is absent.

**Read-only, and never destructive.** Connect, assert the certificate, log in,
select a folder read-only, fetch one message with `PEEK`, log out. Nothing is
sent, marked, moved or deleted.

What each account should assert:

- The TLS posture actually in force — verified for Gmail, the pinned fingerprint
  for DonDominio, and **that a wrong pin is refused before login**.
- A listing returns messages with a non-empty sender, subject and date. This alone
  would have caught the blank-summary bug.
- A listing of 20 completes well inside a few seconds. This alone would have caught
  the hang.
- An id from a listing can be read back.

## Tasks

- [ ] `live` marker in `pyproject.toml`, deselected by default; document the
      command in the README
- [ ] Skip cleanly when `~/.config/agent-mail/<account>.json` is missing, so the
      suite still passes on a machine with no credentials
- [ ] One live test per IMAP account: TLS, login, listing shape, timing, read-back
- [ ] A live test that a deliberately wrong pin raises before `login`
- [ ] Decide whether `hotmail` gets one too — it needs a device-code token, which
      may not survive unattended runs

## Then, and not before

[send-path.md](send-path.md) waits on this. Sending is the first thing here that
cannot be undone, and it would sit on the same transport layer that produced all
four defects. Álvaro does not need it — he can send mail himself — so the order is
deliberate rather than cautious.

## Out of scope

- CI. These need credentials, so they are a local command for now; putting them in
  CI means putting mail credentials in CI.
- Writing anything to a mailbox, ever, including a draft.
