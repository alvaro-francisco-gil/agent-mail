"""Tests against the real servers, read-only. Deselected by default.

    uv run pytest -m live

Every other test in this suite runs against a fake, and the fake was built from the
same beliefs as the code. Four defects got past it that way -- headers that were
not at the front, a folder of thousands, a search term that became a command, a
TLS context that verified nothing -- because **a fake cannot report that its author
misunderstood the protocol.** These tests exist to let reality disagree.

What they may do: connect, check the certificate, log in, SELECT readonly, fetch
with BODY.PEEK, log out. Nothing is sent, marked, moved or deleted, and nothing
here may ever do so -- they run with the same password that could.

Each account skips cleanly when its credential is absent, so `pytest -m live` on a
machine with no credentials passes rather than fails.

Assertion messages count problems instead of quoting mail: a failing run should
not print somebody's subject line into a terminal or a CI log.
"""
from __future__ import annotations

import imaplib
import os
import socket
import ssl
import threading
import time

import pytest

from agent_mail import server
from agent_mail.oauth import DeviceCodeAuth, SignInRequired
from agent_mail.graph import GraphMailbox
from agent_mail.tls import DONDOMINIO_MAIL_SHA256, CertificateMismatch, context_for

pytestmark = pytest.mark.live

LIMIT = 20

# Listings took 2-7 s on 2026-10-15, Gmail the slower. The hang this guards
# against took minutes, so the margin is for a slow network rather than for slow
# code. Reads get no budget: one Gmail read-back measured 2 s, then 42 s, with
# no change in code and no relation to message size.
LISTING_BUDGET_SECONDS = 15

# Same length as the real pin, and not it.
WRONG_PIN = ":".join(["00"] * 32)


def _credential_or_skip(account: str) -> tuple[str, str]:
    host, filename, pin = server.IMAP_ACCOUNTS[account]
    if not (server._cache_dir() / filename).exists():
        pytest.skip(f"no {filename} in {server._cache_dir()}")
    return host, pin


@pytest.fixture(scope="module", params=sorted(server.IMAP_ACCOUNTS))
def imap_account(request):
    host, pin = _credential_or_skip(request.param)
    return request.param, host, pin


@pytest.fixture(scope="module")
def imap_listing(imap_account):
    """One listing per account, shared, so the run makes as few logins as it can."""
    account, host, pin = imap_account
    mailbox = server._imap_mailbox(account, host, server.IMAP_ACCOUNTS[account][1], pin)
    summaries, elapsed = _within_budget(lambda: mailbox.list_recent(limit=LIMIT))
    return mailbox, summaries, elapsed


def _within_budget(call):
    """Run `call`, failing rather than waiting if it outlives the budget.

    The defect this guards against was a hang, and a timing assertion made after
    the call returns never fires on a hang. A daemon thread can be abandoned, so
    the run fails at the budget and the process still exits.
    """
    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test's thread
            result["error"] = exc

    started = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(LISTING_BUDGET_SECONDS)
    elapsed = time.monotonic() - started
    if worker.is_alive():
        pytest.fail(f"listing {LIMIT} still running after {LISTING_BUDGET_SECONDS}s")
    if "error" in result:
        raise result["error"]
    return result["value"], elapsed


# -- TLS: the posture actually in force -----------------------------------------


def _handshake(host: str, context: ssl.SSLContext) -> ssl.SSLSocket:
    raw = socket.create_connection((host, server.IMAP_PORT), timeout=15)
    return context.wrap_socket(raw, server_hostname=host)


def test_gmail_verifies_against_the_system_cas():
    """A verified connection is the only kind whose getpeercert() is populated,
    so a non-empty dict is proof the chain and the hostname were both checked."""
    with _handshake("imap.gmail.com", context_for(None)) as sock:
        assert sock.context.verify_mode is ssl.CERT_REQUIRED
        assert sock.context.check_hostname is True
        assert sock.getpeercert(), "handshake completed but nothing was verified"


def test_dondominio_cannot_be_ca_verified_which_is_why_it_is_pinned():
    """If this ever passes, DonDominio has a chained certificate and the pin in
    tls.py should be replaced by ordinary verification."""
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake("imap.cultuvilla.es", context_for(None)).close()


def test_dondominio_presents_the_pinned_certificate():
    from agent_mail.tls import assert_fingerprint

    with _handshake("imap.cultuvilla.es", context_for(DONDOMINIO_MAIL_SHA256)) as sock:
        assert_fingerprint(sock.getpeercert(True), DONDOMINIO_MAIL_SHA256)


def test_a_wrong_pin_is_refused_before_the_password_is_sent(monkeypatch):
    """The ordering is the security property: a check after login would mean the
    password had already gone to whoever presented the certificate."""
    host, pin = _credential_or_skip("cultuvilla")
    logins = []
    monkeypatch.setattr(imaplib.IMAP4_SSL, "login", lambda self, *a: logins.append(a))
    mailbox = server._imap_mailbox("cultuvilla", host, "cultuvilla.json", WRONG_PIN)
    with pytest.raises(CertificateMismatch):
        mailbox.list_recent(limit=1)
    assert logins == [], "login was attempted against an unpinned certificate"


# -- IMAP: what a listing and a read really return -------------------------------


def test_a_listing_returns_messages(imap_listing):
    _, summaries, _ = imap_listing
    assert summaries, "the listing came back empty"
    assert len(summaries) <= LIMIT


def test_every_summary_has_a_sender_a_subject_and_a_date(imap_listing):
    """The blank-summary bug: DonDominio's spam-report block pushed the headers
    past a byte prefix, and every field came back empty."""
    _, summaries, _ = imap_listing
    for field in ("sender_address", "subject", "received"):
        blank = sum(1 for m in summaries if not getattr(m, field))
        assert blank == 0, f"{blank} of {len(summaries)} summaries have no {field}"


def test_a_listing_is_newest_first(imap_listing):
    _, summaries, _ = imap_listing
    received = [m.received for m in summaries]
    assert received == sorted(received, reverse=True)


def test_a_listing_completes_in_seconds(imap_listing):
    """The hang: fetching every UID in Gmail's All Mail and truncating after."""
    _, _, elapsed = imap_listing
    assert elapsed < LISTING_BUDGET_SECONDS, f"listing {LIMIT} took {elapsed:.1f}s"


def test_an_id_from_a_listing_reads_back(imap_listing):
    mailbox, summaries, _ = imap_listing
    if not summaries:
        pytest.skip("nothing listed to read back")
    first = summaries[0]
    message = mailbox.read(first.id)
    assert message.id == first.id
    assert message.subject == first.subject


def test_reading_does_not_mark_a_message_seen(imap_listing, imap_account):
    """BODY.PEEK and a readonly SELECT, checked where it matters: the flags on
    the server after a read."""
    mailbox, summaries, _ = imap_listing
    account, host, pin = imap_account
    if not summaries:
        pytest.skip("nothing listed to read")
    folder, _, uid = summaries[0].id.rsplit(":", 2)

    def flags():
        conn = mailbox._connect()
        try:
            conn.select(f'"{folder}"', readonly=True)
            typ, data = conn.uid("FETCH", uid, "(FLAGS)")
            return b" ".join(d for d in data if isinstance(d, bytes))
        finally:
            conn.logout()

    before = flags()
    mailbox.read(summaries[0].id)
    assert flags() == before


# -- hotmail ------------------------------------------------------------------


def test_hotmail_lists_and_reads_back():
    """Runs only on a cached token that still refreshes.

    The server's own non-interactive auth is used, so a dead refresh token raises
    SignInRequired instead of polling, and the test skips: run
    `uv run agent-mail login hotmail`, then rerun.
    """
    client_id = os.environ.get("AGENT_MAIL_HOTMAIL_CLIENT_ID")
    cache = server._cache_dir() / "hotmail.json"
    if not client_id or not cache.exists():
        pytest.skip("needs AGENT_MAIL_HOTMAIL_CLIENT_ID and a cached hotmail token")

    auth = DeviceCodeAuth(client_id, server.GRAPH_SCOPES, cache, interactive=False)
    mailbox = GraphMailbox(auth, account="hotmail")

    try:
        summaries, elapsed = _within_budget(lambda: mailbox.list_recent(limit=LIMIT))
    except SignInRequired:
        pytest.skip("the hotmail refresh token no longer works; run `uv run agent-mail login hotmail`")

    assert summaries, "the listing came back empty"
    blank = sum(1 for m in summaries if not (m.sender_address and m.received))
    assert blank == 0, f"{blank} of {len(summaries)} summaries lack a sender or date"
    assert elapsed < LISTING_BUDGET_SECONDS, f"listing {LIMIT} took {elapsed:.1f}s"
    assert mailbox.read(summaries[0].id).id == summaries[0].id
