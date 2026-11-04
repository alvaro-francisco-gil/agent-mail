import email
import os
import time
from email.message import EmailMessage

import pytest

from agent_mail import server
from agent_mail.attachments import (MAX_ATTACHMENT_BYTES, AttachmentFile, GraphAttachments, ImapAttachments,
                                    _safe_name, attachments_dir, save)
from agent_mail.graph import GraphMailbox
from agent_mail.imap import ImapMailbox, encode_id
from agent_mail.models import Message, imap_attachments


def with_files():
    inner = EmailMessage()
    inner["Subject"] = "forwarded"
    inner.set_content("inner body")
    inner.add_attachment(b"inner file", maintype="application", subtype="pdf", filename="inner.pdf")

    m = EmailMessage()
    m["From"] = "Ana <a@example.com>"
    m["Subject"] = "Invoice"
    m.set_content("see attached")
    m.add_attachment(b"%PDF-1", maintype="application", subtype="pdf",
                     filename="=?utf-8?b?RmFjdHVyYSByZXZpc2nDs24ucGRm?=")
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", disposition="inline", cid="<logo@x>")
    m.add_attachment(inner)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, raw):
        self.raw = raw
        self.commands = []

    def status(self, folder, what):
        return "OK", [f'{folder} (UIDVALIDITY 7)'.encode()]

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", readonly))
        return "OK", [b"1"]

    def uid(self, command, uid, spec):
        self.commands.append((command, spec))
        return "OK", [(b"1 (UID 12 BODY[] {1}", self.raw), b")"]

    def logout(self):
        self.commands.append(("LOGOUT",))


class FakeAuth:
    def access_token(self):
        return "token"


MESSAGE_ID = encode_id("INBOX", 7, 12)


# --- what a message lists -------------------------------------------------------


def test_imap_lists_each_attachment_with_a_readable_name():
    listed = [a for a, _ in imap_attachments(email.message_from_bytes(with_files()))]
    assert [(a.id, a.name, a.content_type) for a in listed] == [
        ("1", "Factura revisión.pdf", "application/pdf"),
        ("2", "attachment-2.png", "image/png"),
        ("3", "attachment-3.eml", "message/rfc822"),
    ]
    assert listed[0].size == len(b"%PDF-1")
    assert [a.inline for a in listed] == [False, True, False]


def test_an_attached_message_does_not_lend_its_own_attachments():
    names = [a.name for a, _ in imap_attachments(email.message_from_bytes(with_files()))]
    assert "inner.pdf" not in names


def test_an_attached_message_comes_back_whole():
    found = imap_attachments(email.message_from_bytes(with_files()))
    assert b"inner body" in found[2][1]


def test_body_parts_are_not_attachments():
    m = EmailMessage()
    m.set_content("text")
    m.add_alternative("<p>html</p>", subtype="html")
    assert imap_attachments(email.message_from_bytes(m.as_bytes())) == []


def test_imap_read_lists_attachments():
    message = ImapMailbox(lambda: FakeIMAP(with_files())).read(MESSAGE_ID)
    assert isinstance(message, Message)
    assert message.body_text == "see attached"
    assert len(message.attachments) == 3


def test_graph_read_asks_for_attachments_only_when_there_are_some():
    urls = []

    def transport(url, headers):
        urls.append(url)
        if "/attachments" in url:
            return {"value": [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "x.pdf",
                               "contentType": "application/pdf", "size": 6, "isInline": False}]}
        return {"id": "abc", "hasAttachments": "full" in url}

    box = GraphMailbox(FakeAuth(), transport=transport)
    assert box.read("empty").attachments == ()
    assert len(urls) == 1
    assert [a.name for a in box.read("full").attachments] == ["x.pdf"]
    assert "contentBytes" not in urls[-1]


def test_graph_marks_links_and_names_attached_messages():
    def transport(url, headers):
        if "/attachments" in url:
            return {"value": [
                {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "r", "name": "Doc", "size": 1},
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "i", "name": "Fwd", "size": 2},
            ]}
        return {"id": "abc", "hasAttachments": True}

    link, item = GraphMailbox(FakeAuth(), transport=transport).read("abc").attachments
    assert link.link is True
    assert (item.link, item.name, item.content_type) == (False, "Fwd.eml", "message/rfc822")


# --- fetching: reading must not change the mailbox --------------------------------


def test_imap_fetch_returns_the_decoded_bytes():
    file = ImapAttachments(lambda: FakeIMAP(with_files())).fetch(MESSAGE_ID, "1")
    assert (file.name, file.content) == ("Factura revisión.pdf", b"%PDF-1")


def test_imap_fetch_opens_readonly_with_peek_and_logs_out():
    conn = FakeIMAP(with_files())
    ImapAttachments(lambda: conn).fetch(MESSAGE_ID, "1")
    assert ("SELECT", True) in conn.commands
    assert ("FETCH", "(UID BODY.PEEK[])") in conn.commands
    assert conn.commands[-1] == ("LOGOUT",)


def test_imap_fetch_of_an_unknown_attachment_says_where_to_look():
    with pytest.raises(ValueError, match="mail_read"):
        ImapAttachments(lambda: FakeIMAP(with_files())).fetch(MESSAGE_ID, "9")


def test_imap_refuses_an_attachment_over_the_limit():
    m = EmailMessage()
    m.set_content("x")
    m.add_attachment(b"0" * (MAX_ATTACHMENT_BYTES + 1), maintype="application", subtype="zip", filename="big.zip")
    with pytest.raises(ValueError, match="limit"):
        ImapAttachments(lambda: FakeIMAP(m.as_bytes())).fetch(MESSAGE_ID, "1")


def graph_attachments(meta, downloads=None):
    def download(url, headers):
        downloads.append((url, headers))
        return b"%PDF-1"

    return GraphAttachments(FakeAuth(), transport=lambda url, headers: meta, download=download)


def test_graph_fetch_downloads_the_raw_value():
    downloads = []
    file = graph_attachments({"name": "x.pdf", "contentType": "application/pdf", "size": 6}, downloads).fetch("m", "a")
    assert file.content == b"%PDF-1"
    url, headers = downloads[0]
    assert url.endswith("/me/messages/m/attachments/a/$value")
    assert headers["Authorization"] == "Bearer token"


def test_graph_refuses_too_large_before_downloading():
    downloads = []
    with pytest.raises(ValueError, match="limit"):
        graph_attachments({"name": "x", "size": MAX_ATTACHMENT_BYTES + 1}, downloads).fetch("m", "a")
    assert downloads == []


def test_graph_refuses_a_link():
    downloads = []
    with pytest.raises(ValueError, match="cloud link"):
        graph_attachments({"@odata.type": "#microsoft.graph.referenceAttachment", "name": "Doc"}, downloads).fetch("m", "a")
    assert downloads == []


def test_graph_ids_cannot_reach_another_endpoint():
    downloads = []
    graph_attachments({"name": "x", "size": 1}, downloads).fetch("m", "../../sendMail")
    assert "/attachments/..%2F..%2FsendMail/$value" in downloads[0][0]


def test_the_surfaces_only_fetch():
    """As with the mailboxes and the drafters, the absence is the control."""
    for cls in (GraphAttachments, ImapAttachments):
        assert {n for n in vars(cls) if not n.startswith("_")} == {"fetch"}, cls


# --- the file on disk -------------------------------------------------------------


@pytest.mark.parametrize("hostile, safe", [
    ("../../.bashrc", "bashrc"),
    ("..\\..\\evil.exe", "evil.exe"),
    ("/etc/passwd", "passwd"),
    ("..", "attachment"),
    ("", "attachment"),
    ("a\r\nb.pdf", "ab.pdf"),
    ("cv‮fdp.exe", "cvfdp.exe"),
    ("Factura revisión.pdf", "Factura revisión.pdf"),
])
def test_a_hostile_name_stays_one_plain_component(hostile, safe):
    assert _safe_name(hostile) == safe


def test_a_long_name_is_cut_and_keeps_its_suffix():
    name = _safe_name("ñ" * 300 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode()) <= 200


def test_a_saved_file_is_private_and_inside_the_root(tmp_path):
    root = tmp_path / "attachments"
    path = save(AttachmentFile("../../evil.pdf", "application/pdf", b"x"), root, "k")
    assert path.read_bytes() == b"x"
    assert path.resolve().is_relative_to(root.resolve())
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(root).st_mode & 0o777 == 0o700


def test_saving_twice_leaves_one_file_and_two_keys_stay_apart(tmp_path):
    file = AttachmentFile("scan.pdf", "application/pdf", b"x")
    first = save(file, tmp_path, "a")
    assert save(file, tmp_path, "a") == first
    assert save(file, tmp_path, "b") != first


def test_a_symlink_planted_at_the_name_is_not_followed(tmp_path):
    file = AttachmentFile("scan.pdf", "application/pdf", b"x")
    path = save(file, tmp_path, "a")
    victim = tmp_path / "victim"
    victim.write_bytes(b"safe")
    path.unlink()
    path.symlink_to(victim)
    with pytest.raises(OSError):
        save(file, tmp_path, "a")
    assert victim.read_bytes() == b"safe"


def test_old_folders_go_and_recent_ones_stay(tmp_path):
    file = AttachmentFile("scan.pdf", "application/pdf", b"x")
    old, recent = save(file, tmp_path, "old"), save(file, tmp_path, "recent")
    long_ago = time.time() - 31 * 24 * 60 * 60
    os.utime(old.parent, (long_ago, long_ago))
    save(file, tmp_path, "new")
    assert not old.exists()
    assert recent.exists()


def test_saving_again_resets_a_files_age(tmp_path):
    file = AttachmentFile("scan.pdf", "application/pdf", b"x")
    path = save(file, tmp_path, "a")
    nearly = time.time() - 29 * 24 * 60 * 60
    os.utime(path.parent, (nearly, nearly))
    save(file, tmp_path, "a")
    assert time.time() - path.parent.stat().st_mtime < 60


# --- the tool ---------------------------------------------------------------------


@pytest.fixture
def mailbox_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_attachment_reader",
                        lambda account: ImapAttachments(lambda: FakeIMAP(with_files())))


def test_the_tool_saves_and_says_where(mailbox_files, tmp_path):
    result = server.mail_save_attachment("gmail", MESSAGE_ID, "1")
    assert result["name"] == "Factura revisión.pdf"
    assert result["size"] == 6
    assert result["path"].startswith(str(tmp_path / "cache" / "agent-mail" / "attachments"))
    assert open(result["path"], "rb").read() == b"%PDF-1"


def test_a_saved_file_can_go_on_a_draft(mailbox_files):
    """Outside the credential directory, which mail_draft refuses."""
    result = server.mail_save_attachment("gmail", MESSAGE_ID, "1")
    assert str(server._attachment(result["path"])) == result["path"]


def test_the_folder_defaults_to_the_users_cache(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert str(attachments_dir()).endswith(".cache/agent-mail/attachments")


def test_hotmail_attachments_use_the_read_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAIL_HOTMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_MAIL_CACHE_DIR", str(tmp_path))
    server._mailboxes.clear()
    try:
        reader = server._attachment_reader("hotmail")
    finally:
        server._mailboxes.clear()
    assert reader.auth.cache_path == tmp_path / "hotmail.json"
    assert not any("ReadWrite" in scope or "Send" in scope for scope in reader.auth.scopes)


def test_an_unknown_account_names_the_valid_ones():
    with pytest.raises(ValueError, match="valid accounts"):
        server._attachment_reader("nobody")


def test_the_tool_is_not_annotated_read_only():
    """It writes a file, and the annotation is what a client shows before approving."""
    assert server.ATTACHMENT_SAVE.read_only_hint is False
    assert server.ATTACHMENT_SAVE.destructive_hint is False
