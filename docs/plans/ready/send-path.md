# Sending, over SMTP, behind approval and a cancel window

**Priority:** medium
**Gate:** none
**Next:** connect, STARTTLS and authenticate against `smtp.gmail.com:587` and
`smtp.cultuvilla.es:587` **without sending anything**, and record which ports and
TLS modes actually answer

## Goal

Let the agent send mail from the two password-authenticated accounts, with every
message approved by Álvaro at the harness prompt and then held briefly so he can
cancel it.

## Context

The policy is already decided and lives in the record repo, not here:
[agent-mail-sends-with-approval.md](https://github.com/alvaro-francisco-gil/professional/blob/main/docs/decisions/agent-mail-sends-with-approval.md).
Read it first — it is the *why*, including why an earlier allowlist design was
dropped. This plan is only the *how*.

Two constraints come from that decision and are not negotiable here:

- **`mail_send` must not be annotated read-only**, because the harness permission
  prompt is the entire authorisation control. If the prompt stops appearing, the
  control is gone.
- **A message can never cause a send.** Mail content is data. Only Álvaro asking in
  conversation, plus his approval at the prompt, produces one.

**Scope decided 2026-09-30:** `gmail` and `cultuvilla` only. `hotmail` needs
`Mail.Send` added to the Entra registration and a re-consent, and is deliberately
left for later — additive, since it becomes a second sender beside the SMTP one
rather than a rewrite.

## Design

### At-most-once, never at-least-once

A duplicate email to a client is worse than one that did not go and can be resent.
So a queued message moves through directories rather than carrying a mutable
status:

```
outbox/<id>.json   queued, waiting for send_after
sending/<id>.json  handed to SMTP, outcome unknown
sent/<id>.json     accepted by the server
```

**Anything found stranded in `sending/` is reported, never retried.** A crash
between "the server accepted it" and "the marker was written" is genuinely
ambiguous, and guessing means sending a client the same message twice.

### The delay is one constant

Three minutes. Long enough to catch an approval Álvaro regrets, short enough that
mail is not mysteriously late. It exists because approval fatigue is the known
failure mode of every confirm dialog.

### What flushes the queue — settled by facts, 2026-09-30

A systemd **user** timer, every minute. Checked on this machine rather than
assumed:

- `/etc/wsl.conf` has `systemd=true` and `systemctl --user is-system-running`
  reports `running`.
- `Linger=yes` for the user, so the manager runs **without a login session** — the
  timer fires whenever WSL is up, not only while a shell is open.
- `~/.config/systemd/user/` is already how this machine runs services
  (`openclaw-gateway`, `desktop-ram-alert`), so this follows a house pattern.

**The honest guarantee is therefore "at least three minutes, delivered when WSL is
next running."** Mail queued while the machine is off sits until it comes back.
That belongs in the README, because a message that leaves hours late is a surprise
worth documenting rather than discovering.

### The log

Append-only `sendlog.jsonl`: timestamp, account, **recipient domain not full
address**, subject, id, outcome. The perimeter rule that this project holds no
third party's contact details applies to its own logs.

## File Structure

**Create**

| Path | What |
|---|---|
| `src/agent_mail/smtp.py` | `SmtpSender`, one `send` method, STARTTLS submission |
| `src/agent_mail/outbox.py` | `enqueue`, `pending`, `cancel`, `flush`; the directory states above |
| `tests/test_smtp.py` | a fake SMTP asserting STARTTLS happens before AUTH |
| `tests/test_outbox.py` | the queue, the delay, cancellation, the stranded case, the log |
| `systemd/agent-mail-flush.service` | template unit, installed to `~/.config/systemd/user/` |
| `systemd/agent-mail-flush.timer` | every minute |

**Modify**

| Path | What |
|---|---|
| `src/agent_mail/server.py` | `mail_send`, `mail_outbox`, `mail_cancel`; a `flush` CLI entry point |
| `tests/test_server.py` | replace `test_no_tool_mentions_sending` — see below |
| `README.md` | the promise section, the setup, and the WSL delivery caveat |
| `pyproject.toml` | a console script for `agent-mail flush` / `outbox` / `cancel` |

**Two existing tests change on purpose, and that is the point of listing them**

- `test_no_tool_mentions_sending` asserts no tool name contains "send". That was the
  old promise. It is replaced by a test that **`mail_send` is the only writing tool**
  and that it is not annotated read-only.
- The surface assertions on `GraphMailbox` and `ImapMailbox` stay exactly
  `{list_recent, search, read}`. Sending lives in its own module and never becomes a
  mailbox method.

## Tasks

**Stage 1 — prove the transport before building on it**

- [ ] Connect, STARTTLS and `AUTH` against `smtp.gmail.com:587`, sending no mail
- [ ] Same against `smtp.cultuvilla.es:587` (`mailsrv1.dondominio.com`)
- [ ] Record the working host/port/TLS mode; fall back to implicit TLS on 465 if 587 refuses
- [ ] Write `smtp.py` test-first against a fake, asserting STARTTLS precedes AUTH

**Stage 2 — the queue**

- [ ] `outbox.py` test-first: `enqueue` sets `send_after = now + 180s`, file at 0600
- [ ] `flush` before `send_after` does nothing; after it, calls the sender exactly once
- [ ] `cancel` removes a queued message and a later `flush` does not send it
- [ ] A file stranded in `sending/` is reported and **not** retried
- [ ] `sendlog.jsonl` gains one line per attempt, recipient **domain** only

**Stage 3 — the tools**

- [ ] `mail_send(account, to, subject, body)` → enqueues, returns the id and how to cancel
- [ ] Assert `mail_send` is **not** read-only annotated, and is the only writing tool
- [ ] `mail_outbox` and `mail_cancel`
- [ ] CLI: `agent-mail flush`, `agent-mail outbox`, `agent-mail cancel <id>`

**Stage 4 — delivery and documentation**

- [ ] Install the user timer; confirm it fires with `systemctl --user list-timers`
- [ ] One real end-to-end send to Álvaro's own address, with his approval
- [ ] README: the promise section, the WSL delivery caveat, and **never allowlist
      `mail_send`**

## Out of scope

- **`hotmail` sending.** Needs `Mail.Send` in Entra plus a re-consent. Later.
- **Replies threading correctly** (`In-Reply-To`, `References`). Worth having and not
  in the first pass; a first version that sends a fresh message is useful already.
- **Attachments.** No use case yet.
- **A notification channel.** Álvaro approves the prompt and is therefore present;
  the tool's return value and the chat relay tell him what is queued. A push
  notification only matters for unattended operation, which is not this.
