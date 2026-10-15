"""TLS for the mail transports: verify where possible, pin where it is not.

`imaplib.IMAP4_SSL` and `smtplib` are easy to use insecurely, and this package did
until 2026-10-15. Passing no `ssl_context` to `IMAP4_SSL` makes it call
`ssl._create_stdlib_context()`, whose defaults are `check_hostname=False` and
`verify_mode=CERT_NONE` -- no verification at all. Every IMAP connection here was
unverified, so anyone in a network position could have read the mail and taken the
password that also sends.

The two providers need different answers, established by inspection:

* **Gmail** presents a normally-chained certificate, so `imap.gmail.com` and
  `smtp.gmail.com` verify with the stdlib defaults. Nothing special required.
* **DonDominio's IMAP** presents a **self-signed** certificate: CN *"Mail
  Service"*, issuer *Soluciones Corporativas IP, SL*, valid 2019 to **2074**. No
  public CA will ever chain it, on any hostname, so CA verification cannot be made
  to succeed. Pinning its fingerprint is **stronger** than CA verification rather
  than a concession: it accepts exactly one certificate instead of any certificate
  a CA will sign.

  Their **SMTP** on 587 is different and does present a real Sectigo certificate
  for `*.dondominio.com`, so submission verifies normally when addressed as
  `mailsrv1.dondominio.com`. `smtp.cultuvilla.es` fails only because the name is
  not on that certificate.

A fingerprint is a public value, not a secret, so it belongs in the source where a
reviewer can see it change.
"""

from __future__ import annotations

import hashlib
import ssl

# DonDominio's self-signed mail certificate, read from imap.cultuvilla.es:993 and
# mailsrv1.dondominio.com:993 on 2026-10-15 (both present the same one).
# Rotation shows up as CertificateMismatch naming both values, which is the point.
DONDOMINIO_MAIL_SHA256 = (
    "F5:3E:70:CE:0D:69:79:96:71:51:B5:26:A6:CC:43:64:"
    "BB:D7:23:9E:F9:E5:FF:CF:8E:CB:34:BD:26:F7:BF:15"
)


class CertificateMismatch(Exception):
    """The server presented a certificate other than the pinned one."""


def _normalise(fingerprint: str) -> str:
    return fingerprint.replace(":", "").replace(" ", "").lower()


def context_for(pin: str | None) -> ssl.SSLContext:
    """A verifying context, unless a pin takes over the job.

    With a pin, hostname and chain checks are switched off deliberately: they
    cannot pass against a self-signed certificate, and leaving them on would only
    mean the connection failed before the pin could be checked. The pin is then
    the whole of the verification, so `assert_fingerprint` must be called on every
    connection that uses one.
    """
    if pin is None:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def assert_fingerprint(der: bytes | None, pin: str) -> None:
    """Raise unless the certificate is exactly the pinned one.

    A missing certificate raises rather than passing: `getpeercert` can return
    None, and treating that as success would make the pin decorative.
    """
    if not der:
        raise CertificateMismatch(
            "the server presented no certificate, so the pinned fingerprint "
            f"{pin} could not be checked"
        )
    actual = hashlib.sha256(der).hexdigest()
    if actual != _normalise(pin):
        raise CertificateMismatch(
            "the server's certificate does not match the pinned fingerprint.\n"
            f"  pinned: {_normalise(pin)}\n"
            f"  actual: {actual}\n"
            "Either the server rotated its certificate, in which case verify the "
            "new one out of band and update tls.py, or this connection is being "
            "intercepted."
        )
