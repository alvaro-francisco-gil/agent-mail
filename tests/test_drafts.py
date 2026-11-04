import email
from email import policy

import pytest

from agent_mail import server
from agent_mail.drafts import Draft, GraphDrafter, ImapDrafter, build_mime


class FakeAuth:
    def access_token(self):
        return "tok"


class FakeConn:
    def __init__(self, folders=None, append_status="OK"):
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Borradores"',
        ]
        self.append_status = append_status
        self.appended = []
        self.logged_out = False

    def list(self):
        return "OK", self.folders

    def append(self, folder, flags, date, raw):
        self.appended.append((folder, flags, raw))
        return self.append_status, [b"done"]

    def logout(self):
        self.logged_out = True


def test_the_surfaces_only_create():
    for cls in (GraphDrafter, ImapDrafter):
        public = {n for n in vars(cls) if not n.startswith("_")}
        assert public == {"create"}, cls


def test_mime_carries_recipients_and_the_attachment(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1")
    raw = build_mime(Draft(["a@x.com"], "Hola ñ", "Body ñ", cc=["b@x.com"], attachments=[pdf]), "me@x.com")
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert (parsed["From"], parsed["To"], parsed["Cc"], parsed["Subject"]) == ("me@x.com", "a@x.com", "b@x.com", "Hola ñ")
    assert parsed.get_body().get_content().strip() == "Body ñ"
    attachment = next(parsed.iter_attachments())
    assert (attachment.get_filename(), attachment.get_content()) == ("invoice.pdf", b"%PDF-1")


def test_mime_carries_bcc_and_leaves_the_visible_recipients_alone():
    raw = build_mime(Draft(["me@x.com"], "s", "b", bcc=["a@x.com", "b@x.com"]), "me@x.com")
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert (parsed["To"], parsed["Cc"], parsed["Bcc"]) == ("me@x.com", None, "a@x.com, b@x.com")


def test_a_newline_in_a_header_is_refused():
    with pytest.raises(ValueError):
        build_mime(Draft(["a@x.com"], "x\r\nBcc: evil@x.com", "b"), "me@x.com")


def test_imap_appends_to_the_flagged_folder_whatever_its_name():
    conn = FakeConn()
    assert ImapDrafter(lambda: conn, "me@x.com").create(Draft(["a@x.com"], "s", "b")) == "[Gmail]/Borradores"
    folder, flags, _ = conn.appended[0]
    assert folder == '"[Gmail]/Borradores"'
    assert "\\Draft" in flags
    assert conn.logged_out


def test_imap_falls_back_to_a_folder_named_drafts():
    conn = FakeConn([b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Drafts"'])
    assert ImapDrafter(lambda: conn, "me@x.com").create(Draft(["a@x.com"], "s", "b")) == "INBOX.Drafts"


def test_imap_with_no_drafts_folder_fails_rather_than_guessing():
    conn = FakeConn([b'(\\HasNoChildren) "/" "INBOX"'])
    with pytest.raises(RuntimeError, match="no folder"):
        ImapDrafter(lambda: conn, "me@x.com").create(Draft(["a@x.com"], "s", "b"))
    assert conn.appended == []


def test_imap_refusal_surfaces():
    conn = FakeConn(append_status="NO")
    with pytest.raises(RuntimeError, match="refused"):
        ImapDrafter(lambda: conn, "me@x.com").create(Draft(["a@x.com"], "s", "b"))
    assert conn.logged_out


def test_graph_posts_a_draft_with_its_attachment(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1")
    seen = {}

    def transport(url, headers, payload):
        seen.update(url=url, headers=headers, payload=payload)
        return {"isDraft": True}

    GraphDrafter(FakeAuth(), transport=transport).create(Draft(["a@x.com"], "s", "b", cc=["c@x.com"], attachments=[pdf]))
    assert seen["url"].endswith("/me/messages")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["payload"]["toRecipients"] == [{"emailAddress": {"address": "a@x.com"}}]
    assert seen["payload"]["bccRecipients"] == []
    assert seen["payload"]["attachments"][0]["contentBytes"] == "JVBERi0x"


def test_graph_refuses_attachments_over_its_inline_limit(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (3 * 1024 * 1024))
    with pytest.raises(ValueError, match="inline"):
        GraphDrafter(FakeAuth(), transport=lambda *a: {}).create(Draft(["a@x.com"], "s", "b", attachments=[big]))


def test_graph_that_does_not_make_a_draft_is_an_error():
    with pytest.raises(RuntimeError, match="draft"):
        GraphDrafter(FakeAuth(), transport=lambda *a: {"isDraft": False}).create(Draft(["a@x.com"], "s", "b"))


@pytest.mark.parametrize("account", ["hotmail", "kaf-hotmail"])
def test_reads_and_drafts_never_share_a_graph_token(tmp_path, monkeypatch, account):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", "cid")
    read = server._hotmail_auth(account, interactive=False)
    draft = server._hotmail_auth(account, interactive=False, drafts=True)
    assert read.cache_path != draft.cache_path
    assert not any("ReadWrite" in scope for scope in read.scopes)
    assert any("ReadWrite" in scope for scope in draft.scopes)
    assert not any("Send" in scope for scope in draft.scopes)


def test_an_unknown_account_cannot_draft():
    server._drafters.clear()
    with pytest.raises(ValueError, match="cannot hold drafts"):
        server._drafter("nobody")


def test_credentials_cannot_be_attached(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    secret = tmp_path / "gmail.json"
    secret.write_text("{}")
    with pytest.raises(ValueError, match="credential"):
        server._attachment(str(secret))


def test_a_relative_or_missing_attachment_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path / "cache"))
    for bad in ("relative.pdf", str(tmp_path / "missing.pdf")):
        with pytest.raises(ValueError, match="absolute"):
            server._attachment(bad)


def test_drafting_is_not_annotated_read_only():
    assert server.DRAFT_WRITE.read_only_hint is False
