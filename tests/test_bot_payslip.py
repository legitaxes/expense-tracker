"""The /payslip backfill command and the sender search behind it.

The scheduled poll only ever moves forward from its UID cursor, so a payslip
that arrived before the feature existed can never be picked up by it. This
command reaches those, without rewinding the cursor past every transaction
email in between.
"""
import asyncio
from datetime import datetime
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app import bot, trust_email

SENDER = "payroll@example.com"
_DT = datetime(2026, 7, 31, 9, 0, 0)


def _raw(sender=SENDER, subject="Payslip Request 12345678", pdf=b"%PDF-1.4 x",
         date="Fri, 31 Jul 2026 09:00:00 +0800"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content("<p>see attached</p>", subtype="html")
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf",
                           filename="Payslip 12345678.pdf")
    return msg.as_bytes()


class FakeIMAP:
    """Enough IMAP to drive fetch_from_sender. Records the commands issued so
    the search key can be asserted on."""

    def __init__(self, messages):
        self.messages = messages  # {uid: raw bytes}
        self.commands = []
        self.logged_out = False

    def select(self, box, readonly=False):
        self.commands.append(("select", box, readonly))
        return "OK", [b"1 EXISTS UIDVALIDITY 42 UIDNEXT 99"]

    def uid(self, cmd, *args):
        self.commands.append((cmd, *args))
        if cmd == "search":
            return "OK", [" ".join(str(u) for u in self.messages).encode()]
        uid = int(args[0])
        return "OK", [(b"1 (RFC822 {n}", self.messages[uid])]

    def logout(self):
        self.logged_out = True


@pytest.fixture
def imap(monkeypatch):
    box = {}

    def install(messages):
        box.update(messages)
        fake = FakeIMAP(box)
        monkeypatch.setattr(trust_email, "_login", lambda *a: fake)
        return fake

    return install


# --- fetch_from_sender -----------------------------------------------------

def test_rejects_an_address_that_could_inject_imap_keys(imap):
    # The address is interpolated into the SEARCH command. A quote or a space
    # would close the string and be parsed as further search keys, so it is
    # refused before a connection is even opened.
    for bad in ('a@b.com" OR ALL "', "a@b.com ALL", 'a"b@c.com', "not-an-email"):
        with pytest.raises(ValueError):
            trust_email.fetch_from_sender("h", "u", "p", bad)


def test_searches_by_sender_and_returns_oldest_first(imap):
    fake = imap({7: _raw(), 3: _raw(), 5: _raw()})
    validity, out = trust_email.fetch_from_sender("h", "u", "p", SENDER)
    assert ("search", None, "FROM", f'"{SENDER}"') in fake.commands
    assert validity == 42
    # Oldest first: archiving in arrival order means a later month's payslip
    # overwrites an earlier one's row, not the other way round.
    assert [uid for uid, *_ in out] == [3, 5, 7]


def test_keeps_only_the_newest_n_and_never_writes_to_the_mailbox(imap):
    fake = imap({1: _raw(), 2: _raw(), 3: _raw(), 4: _raw()})
    _, out = trust_email.fetch_from_sender("h", "u", "p", SENDER, limit=2)
    assert [uid for uid, *_ in out] == [3, 4]
    assert ("select", "INBOX", True) in fake.commands  # readonly
    assert fake.logged_out is True


def test_decodes_the_pdf_attachment(imap):
    imap({1: _raw(pdf=b"%PDF-1.4 payslip bytes")})
    _, [(uid, sender, subject, body, msg_dt, attachments)] = \
        trust_email.fetch_from_sender("h", "u", "p", SENDER)
    assert sender == SENDER
    assert attachments[0][1] == b"%PDF-1.4 payslip bytes"
    assert msg_dt.strftime("%Y-%m") == "2026-07"


# --- /payslip --------------------------------------------------------------

def _update(args=None):
    replies = []

    async def reply_text(txt, **kw):
        replies.append(txt)

    upd = SimpleNamespace(
        message=SimpleNamespace(reply_text=reply_text),
        effective_message=SimpleNamespace(reply_text=reply_text),
        effective_chat=SimpleNamespace(id=555),
        get_bot=lambda: object())
    upd.replies = replies
    return upd, SimpleNamespace(args=args or [])


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    monkeypatch.setattr(bot, "GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(bot, "GMAIL_USER", "me@gmail.com")
    monkeypatch.setattr(bot, "PAYSLIP_PDF_PASSWORD", "Pay01011999")
    monkeypatch.setattr(bot, "PAYSLIP_SENDER", SENDER)
    monkeypatch.setattr(bot, "DB_PATH", str(tmp_path / "t.db"))
    from app import db
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    conn.close()


def test_refuses_when_the_payslip_password_is_unset(configured, monkeypatch):
    # Same gating as the poll: an empty password means no payslip is ever
    # fetched, let alone opened. Must not reach out to the mailbox at all.
    monkeypatch.setattr(bot, "PAYSLIP_PDF_PASSWORD", "")
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: pytest.fail("must not touch the mailbox"))
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert "PAYSLIP_PDF_PASSWORD" in upd.replies[0]


def test_refuses_when_email_access_is_unset(configured, monkeypatch):
    monkeypatch.setattr(bot, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: pytest.fail("must not touch the mailbox"))
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert "GMAIL_APP_PASSWORD" in upd.replies[0]


def _capture_imports(monkeypatch):
    seen = []

    async def fake_handle(bot_, conn, uid, sender, subject, body, msg_dt,
                          attachments=()):
        seen.append((uid, sender, attachments))

    monkeypatch.setattr(bot, "_handle_import", fake_handle)
    return seen


def test_routes_each_attached_pdf_through_the_normal_import_path(
        configured, monkeypatch):
    # Routed through _handle_import rather than _archive_payslip directly, so
    # the backfill exercises the same classification the scheduled poll does.
    seen = _capture_imports(monkeypatch)
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: (42, [
                            (3, SENDER, "s", "", _DT, [("a.pdf", b"%PDF a")]),
                            (5, SENDER, "s", "", _DT, [("b.pdf", b"%PDF b")]),
                        ]))
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert [uid for uid, *_ in seen] == [3, 5]


def test_skips_payroll_mail_with_no_attachment_and_says_so(
        configured, monkeypatch):
    seen = _capture_imports(monkeypatch)
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: (42, [(3, SENDER, "s", "", _DT, [])]))
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert seen == []
    assert "No PDF attachments" in upd.replies[-1]


def test_never_advances_the_email_cursor(configured, monkeypatch, tmp_path):
    # A backfill that moved the cursor would skip every transaction email
    # between here and the payslip; one that rewound it would re-import them.
    cursor = tmp_path / "cursor"
    cursor.write_text("42 1000")
    monkeypatch.setattr(bot, "EMAIL_CURSOR_PATH", str(cursor))
    _capture_imports(monkeypatch)
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: (42, [
                            (3, SENDER, "s", "", _DT, [("a.pdf", b"%PDF a")])]))
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert cursor.read_text() == "42 1000"


def test_scan_depth_is_an_argument_and_is_clamped(configured, monkeypatch):
    calls = []
    _capture_imports(monkeypatch)

    def fake_fetch(host, user, pw, sender, limit):
        calls.append(limit)
        return 42, []

    monkeypatch.setattr(bot.trust_email, "fetch_from_sender", fake_fetch)
    for args, expected in ([], 3), (["10"], 10), (["999"], 20), (["0"], 1):
        upd, ctx = _update(args)
        asyncio.run(bot.cmd_payslip(upd, ctx))
    assert calls == [3, 10, 20, 1]


def test_a_non_numeric_argument_gets_usage_not_a_traceback(
        configured, monkeypatch):
    monkeypatch.setattr(bot.trust_email, "fetch_from_sender",
                        lambda *a, **k: pytest.fail("must not touch the mailbox"))
    upd, ctx = _update(["lots"])
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert "Usage" in upd.replies[0]


def test_a_mailbox_failure_reports_instead_of_crashing(configured, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(bot.trust_email, "fetch_from_sender", boom)
    upd, ctx = _update()
    asyncio.run(bot.cmd_payslip(upd, ctx))
    assert "OSError" in upd.replies[-1]
