"""Imported transactions are attributed to the card that paid, and the card can
be corrected from Telegram."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import bot, cards, db, trust_email
from tests.test_trust_email import LOCAL   # verbatim real Trust card body

NOW = datetime(2026, 7, 10, 20, 0, 0)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


class FakeBot:
    """Just enough Bot for _handle_import: it only ever sends messages."""

    def __init__(self):
        self.messages = []

    async def send_message(self, **kw):
        self.messages.append(kw)


@pytest.fixture
def fake_bot(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    return FakeBot()


def _import(fake, conn, body=LOCAL, sender=None):
    asyncio.run(bot._handle_import(fake, conn, 1,
                                   sender or trust_email.TRUST_SENDER,
                                   "Yay! Transaction successful", body, NOW))
    return db.list_transactions(conn)


def test_a_bound_source_attributes_from_the_first_import(conn, fake_bot):
    # The zero-correction path: bind the sender when the card is created and
    # every later import from it is attributed with no further work.
    cid = db.add_card(conn, "Trust Link", "debit")
    db.bind_instrument(conn, "trust-email", "", cid)
    [row] = _import(fake_bot, conn)
    assert row["card_id"] == cid
    assert row["instrument"] == "Trust Link card"


def test_a_learned_instrument_attributes_a_second_card_on_one_sender(conn, fake_bot):
    # The case the source default cannot cover: two cards, one sender.
    debit = db.add_card(conn, "Trust Debit", "debit")
    credit = db.add_card(conn, "Trust Credit", "credit")
    db.bind_instrument(conn, "trust-email", "", debit)
    db.bind_instrument(conn, "trust-email", "trust link card", credit)
    [row] = _import(fake_bot, conn)
    assert row["card_id"] == credit


def test_an_unknown_instrument_imports_unassigned(conn, fake_bot):
    [row] = _import(fake_bot, conn)
    assert row["card_id"] is None
    # The raw instrument is still recorded, so one tap can bind it later.
    assert row["instrument"] == "Trust Link card"


# --- the picker -------------------------------------------------------------

def test_every_transaction_message_offers_the_card_picker(conn):
    data = [b.callback_data for row in bot._tx_buttons(7).inline_keyboard for b in row]
    assert "card:7" in data


def test_card_names_are_escaped_in_the_picker(conn):
    # A card named "M&S" would 400 an HTML-parsed message and self-lock the
    # picker -- the same bug already hit merchant stems.
    db.add_card(conn, "M&S Card", "credit")
    labels = [b.text for row in bot._card_picker_rows(conn, 7) for b in row]
    assert any("&amp;" in t for t in labels)


def test_picker_lists_only_active_cards_plus_unassigned(conn):
    a = db.add_card(conn, "Active", "debit")
    gone = db.add_card(conn, "Gone", "debit")
    db.archive_card(conn, gone)
    data = [b.callback_data for row in bot._card_picker_rows(conn, 7) for b in row]
    assert f"cs:7:{a}" in data
    assert f"cs:7:{gone}" not in data
    assert "cs:7:0" in data          # the Unassigned entry


def test_picker_carries_the_transaction_id(conn):
    db.add_card(conn, "Trust Link", "debit")
    data = [b.callback_data for row in bot._card_picker_rows(conn, 42) for b in row]
    assert all(d.startswith("cs:42:") for d in data)


# --- the callbacks ----------------------------------------------------------

class _NoClose:
    """The callbacks close the connection they open; the fixture owns this one."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


class FakeQuery:
    """Just enough CallbackQuery: the card callbacks answer, then edit."""

    def __init__(self, data):
        self.data = data
        self.answers, self.texts, self.markups = [], [], []

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.texts.append(text)
        self.markups.append(kw.get("reply_markup"))

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markups.append(reply_markup)


def _press(conn, monkeypatch, handler, data):
    """Tap an inline button: run `handler` against a fake callback query."""
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    monkeypatch.setattr(bot, "_conn", lambda: _NoClose(conn))
    q = FakeQuery(data)
    update = SimpleNamespace(callback_query=q,
                             effective_chat=SimpleNamespace(id=555),
                             effective_message=None)
    asyncio.run(handler(update, None))
    return q


def test_the_card_button_opens_a_picker_for_that_transaction(conn, monkeypatch):
    cid = db.add_card(conn, "Trust Link", "debit")
    q = _press(conn, monkeypatch, bot.cb_card, "card:7")
    data = [b.callback_data for row in q.markups[0].inline_keyboard for b in row]
    assert f"cs:7:{cid}" in data


def test_the_card_button_with_no_cards_explains_instead_of_an_empty_picker(conn, monkeypatch):
    q = _press(conn, monkeypatch, bot.cb_card, "card:7")
    assert q.markups == []               # the message is left alone
    assert "No cards" in (q.answers[0] or "")


def test_setcard_sets_the_card_and_learns_the_instrument(conn, monkeypatch):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="Trust Link card")
    q = _press(conn, monkeypatch, bot.cb_setcard, f"cs:{tid}:{cid}")
    assert db.get_transaction(conn, tid)["card_id"] == cid
    # Learning is the proof this went through cards.assign_card rather than a
    # bare update_transaction: the next Trust charge on the same instrument
    # lands on this card by itself.
    assert cards.effective_card(conn, "trust-email", "Trust Link card")[0] == cid
    assert "Trust Link" in q.texts[0]


def test_setcard_zero_unassigns_and_forgets_the_binding(conn, monkeypatch):
    cid = db.add_card(conn, "Trust Link", "debit")
    db.bind_instrument(conn, "trust-email", "trust link card", cid)
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             card_id=cid, instrument="Trust Link card")
    _press(conn, monkeypatch, bot.cb_setcard, f"cs:{tid}:0")
    assert db.get_transaction(conn, tid)["card_id"] is None
    # Correcting TO Unassigned forgets, or a wrong binding would be unremovable.
    assert cards.effective_card(conn, "trust-email", "Trust Link card")[0] is None


def test_setcard_on_a_vanished_transaction_says_so(conn, monkeypatch):
    cid = db.add_card(conn, "Trust Link", "debit")
    q = _press(conn, monkeypatch, bot.cb_setcard, f"cs:999:{cid}")
    assert "gone" in q.texts[0].lower()


# --- /cards -----------------------------------------------------------------

def test_cards_lines_show_this_months_spend_and_a_zero_for_an_unused_card(conn):
    hot = db.add_card(conn, "Trust Link", "debit")
    db.add_card(conn, "Amex", "credit")
    db.add_transaction(conn, 1234, "kopi", "Food", "telegram",
                       created_at="2026-07-10T12:00:00", card_id=hot)
    text = "\n".join(bot._cards_lines(conn, "2026-07"))
    assert "Trust Link" in text and "S$12.34" in text
    # month_card_spend only returns cards that were spent on; a card sitting at
    # zero is exactly what the user checks when attribution looks broken.
    assert "Amex" in text and "S$0.00" in text


def test_cards_lines_escape_a_card_name(conn):
    db.add_card(conn, "M&S Card", "credit")
    assert "M&amp;S" in "\n".join(bot._cards_lines(conn, "2026-07"))


def test_cards_lines_name_the_bound_sender_without_bumping_its_hits(conn):
    cid = db.add_card(conn, "Blue", "debit")
    db.bind_instrument(conn, "trust-email", "", cid)
    text = "\n".join(bot._cards_lines(conn, "2026-07"))
    assert "Trust" in text
    # db.recall_instrument would bump this, and a runaway hit count is the
    # signal that an over-broad default is running away -- reading it away
    # would destroy the signal.
    assert db.list_instruments(conn)[0]["hits"] == 0


def test_cards_lines_include_the_unassigned_bucket(conn):
    db.add_card(conn, "Trust Link", "debit")
    db.add_transaction(conn, 4500, "cash lunch", "Food", "telegram",
                       created_at="2026-07-11T12:00:00")
    text = "\n".join(bot._cards_lines(conn, "2026-07"))
    assert "Unassigned" in text and "S$45.00" in text


def test_cards_lines_keep_an_archived_card_that_still_spent_this_month(conn):
    gone = db.add_card(conn, "Old Visa", "credit")
    db.add_transaction(conn, 999, "petrol", "Transport", "telegram",
                       created_at="2026-07-01T09:00:00", card_id=gone)
    db.archive_card(conn, gone)
    # Dropping it would leave the listed totals not adding up to the month.
    text = "\n".join(bot._cards_lines(conn, "2026-07"))
    assert "Old Visa" in text and "S$9.99" in text


def test_cards_lines_with_no_cards_say_how_to_make_one(conn):
    assert "No cards yet" in "\n".join(bot._cards_lines(conn, "2026-07"))


def test_help_and_command_menu_advertise_cards(conn):
    assert "/cards" in bot.HELP
    assert any(name == "cards" for name, _desc in bot.BOT_COMMANDS)


def test_assigning_an_archived_card_is_refused(conn):
    """The guard lives in cards.assign_card, not in the caller. web.py had it
    and the bot did not, so a picker message opened before a card was archived
    could bind an instrument recall_instrument can never return."""
    from app import cards as cards_mod
    cid = db.add_card(conn, "Gone", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="gone card")
    db.archive_card(conn, cid)
    assert cards_mod.assign_card(conn, tid, cid) is False
    assert db.get_transaction(conn, tid)["card_id"] is None
    assert cards_mod.effective_card(conn, "trust-email", "gone card") == (None, None)
