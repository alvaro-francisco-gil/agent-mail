"""TLS verification for the IMAP transports.

Found 2026-10-15 while starting the send path: `imaplib.IMAP4_SSL` builds its
context with `ssl._create_stdlib_context()` when none is passed, and that context
has `check_hostname=False` and `verify_mode=CERT_NONE`. So every IMAP connection
this package made was unverified, and a network attacker in position could have
read the mail and taken the password.

gmail can be verified normally. DonDominio cannot: its IMAP service presents a
self-signed certificate (CN "Mail Service", issued by DonDominio, valid to 2074),
which no public CA will ever chain. Pinning its fingerprint is stronger than CA
verification there, not weaker -- it accepts exactly one certificate.
"""
import hashlib
import ssl

import pytest

from agent_mail.tls import CertificateMismatch, assert_fingerprint, context_for

DER = b"a pretend certificate"
FINGERPRINT = hashlib.sha256(DER).hexdigest()


def test_an_unpinned_host_gets_a_verifying_context():
    ctx = context_for(None)
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED


def test_a_pinned_host_gets_a_context_that_defers_to_the_pin():
    """CA verification cannot succeed against a self-signed certificate, so the
    pin replaces it rather than adding to it."""
    ctx = context_for(FINGERPRINT)
    assert ctx.check_hostname is False
    assert ctx.verify_mode is ssl.CERT_NONE


def test_a_matching_certificate_is_accepted():
    assert_fingerprint(DER, FINGERPRINT)


def test_a_colon_separated_fingerprint_is_accepted_as_written_by_openssl():
    spaced = ":".join(FINGERPRINT[i:i + 2] for i in range(0, len(FINGERPRINT), 2)).upper()
    assert_fingerprint(DER, spaced)


def test_a_different_certificate_is_refused():
    with pytest.raises(CertificateMismatch, match="does not match"):
        assert_fingerprint(b"someone else's certificate", FINGERPRINT)


def test_a_missing_certificate_is_refused_rather_than_passing_silently():
    """getpeercert can return None; treating that as a pass would make the pin
    decorative."""
    with pytest.raises(CertificateMismatch):
        assert_fingerprint(None, FINGERPRINT)


def test_the_error_names_both_fingerprints_so_a_rotation_is_diagnosable():
    with pytest.raises(CertificateMismatch) as e:
        assert_fingerprint(b"other", FINGERPRINT)
    message = str(e.value)
    assert FINGERPRINT[:16] in message.replace(":", "").lower()
    assert hashlib.sha256(b"other").hexdigest()[:16] in message.replace(":", "").lower()
