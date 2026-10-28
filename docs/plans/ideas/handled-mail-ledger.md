# A local ledger of handled mail, so no session goes over the same message twice

**Priority:** medium — every session starts from a blank slate, so mail already filed or acted on gets re-read and re-triaged; the cost grows with every session that reads mail and with the planned mail-history sweep

**Goal:** An agent can record that a message was handled (filed, ignored, acted on, and
where the fact landed), and can ask any list or search to leave handled messages out, on
all five accounts and from any repo's session, without anything in the mailbox changing.

## Context

As of 2026-10-28:

- Nothing records what was handled. Reading never sets `\Seen` (IMAP `SELECT` readonly,
  `BODY.PEEK`) and hotmail's token is `Mail.Read`, so the mailbox cannot carry the state,
  by design. Álvaro noticed sessions going over the same mail more than once.
- Mail is read from several repos' sessions — `professional`, Cultuvilla, Órdago — so a
  log kept in one record repo would be invisible to the others. agent-mail is the one
  path every read goes through, and filtering ("only unhandled") has to happen at fetch
  time, so the ledger belongs here.
- The facts themselves keep living where they live now: a connection "changes how facts
  arrive, never where they live" (`professional` AGENTS.md routing). The ledger only says
  *this message was dealt with, and here is where it went*.

## Shape

- **Storage:** one JSONL file in `_cache_dir()` (`~/.config/agent-mail/handled.jsonl`),
  `0600`, never in the repo — this repo is public and the entries name senders and
  subjects.
- **Entry:** account, message id, date handled, outcome (`filed` / `ignored` /
  `acted`), optional `where` (repo-relative path or URL the fact landed in), optional
  one-line note. Append-only; the latest entry for a message wins.
- **Tools:** `mail_mark_handled(account, message_id, outcome, where="", note="")`, and
  `include_handled: bool = True` on `mail_list_recent` and `mail_search`. A summary
  carries `handled` (outcome and date) when the ledger knows it, so a caller sees the
  state without a second call.
- **Its own module** (`ledger.py`), never a method on `GraphMailbox` or `ImapMailbox`:
  the surface tests stay exactly `{list_recent, search, read}`. `mail_mark_handled` is
  the first tool without `read_only_hint` — it writes local state, never the mailbox —
  and its description and the README must say so plainly, so the promise "nothing here
  can send, delete or modify mail" stays true and reads true.

## Open questions

- **Stable ids.** IMAP ids are `folder:uidvalidity:uid` and change if the server
  renumbers or the message moves folders; Graph ids change on move unless the request
  sends `Prefer: IdType="ImmutableId"`. Key the ledger on the RFC 5322 `Message-ID`
  (Graph `internetMessageId`) as well, and match on either? `[unknown: does every
  account return Message-ID in the summary fetch cheaply?]`
- **Filtering after the limit.** Leaving handled mail out after fetching `limit` rows
  can return fewer than asked; fetch more and trim, or say how many were hidden?
- Does the mail-history sweep in `professional`
  (`docs/plans/ideas/mail-history-sweep.md`) wait for this, so it can run in chunks
  and resume?

## Done when

- A message marked handled in one session is hidden or flagged in the next, from a
  different repo, on IMAP and hotmail alike; tests cover the ledger file's mode, the
  filter, and that the mailbox surface tests are untouched.
- README and AGENTS.md describe the ledger and the one non-read-only tool.
