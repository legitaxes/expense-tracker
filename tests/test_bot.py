# tests/test_bot.py
import asyncio
from types import SimpleNamespace

import pytest

from app import bot
from app import db as db_module


def test_is_authorized(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 999)
    allowed = SimpleNamespace(effective_chat=SimpleNamespace(id=999))
    denied = SimpleNamespace(effective_chat=SimpleNamespace(id=111))
    none = SimpleNamespace(effective_chat=None)
    assert bot.is_authorized(allowed) is True
    assert bot.is_authorized(denied) is False
    assert bot.is_authorized(none) is False


def test_match_category_case_insensitive():
    assert bot._match_category("food") == "Food"
    assert bot._match_category("TRANSPORT") == "Transport"
    assert bot._match_category("nonsense") is None


def test_looks_like_amount():
    assert bot._looks_like_amount("12.50") is True
    assert bot._looks_like_amount("S$12") is True
    assert bot._looks_like_amount("lunch") is False


def test_cat_line_renders_origin():
    # Keyword decisions get no suffix; automatic ones announce themselves.
    assert bot._cat_line("Food", "keyword") == "🏷️ Food"
    assert bot._cat_line("Travel", "trip") == "🏷️ Travel · ✈️ trip mode"
    assert bot._cat_line("Groceries", "memory") == "🏷️ Groceries · 🧠 remembered"


def test_describe_rule_formats():
    daily = {"id": 1, "amount_cents": 500, "note": "coffee", "category": "Food",
             "frequency": "daily", "anchor": None, "next_due": "2026-06-02"}
    weekly = {"id": 2, "amount_cents": 1000, "note": "groceries", "category": "Groceries",
              "frequency": "weekly", "anchor": 0, "next_due": "2026-06-08"}
    monthly = {"id": 3, "amount_cents": 120000, "note": "rent", "category": "Bills",
               "frequency": "monthly", "anchor": 1, "next_due": "2026-07-01"}
    assert "#1" in bot._describe_rule(daily) and "daily" in bot._describe_rule(daily)
    assert "Mon" in bot._describe_rule(weekly)
    assert "day 1" in bot._describe_rule(monthly) and "#3" in bot._describe_rule(monthly)


def test_summary_lines_with_income():
    s = {"month": "2026-07", "total_cents": 150000,
         "by_category": [{"category": "Food", "total_cents": 150000}]}
    lines = bot._summary_lines(s, income=500000)
    text = "\n".join(lines)
    assert "S$1,500.00" in text and "Food" in text
    assert "Income" in text and "S$5,000.00" in text
    assert "Net" in text and "S$3,500.00" in text


def test_summary_lines_without_income():
    s = {"month": "2026-07", "total_cents": 1000,
         "by_category": [{"category": "Food", "total_cents": 1000}]}
    assert not any("Income" in ln for ln in bot._summary_lines(s, income=0))


_BASE_GOAL_FIELDS = {"category": None, "priority": 2, "note": "", "bought_at": None,
                     "target_date": None, "monthly_cents": None,
                     "pace": {"status": "none", "need_per_month": None,
                              "months_left": None, "shortfall_per_month": 0}}


def test_describe_goal():
    g = {"id": 3, "name": "Japan", "target_cents": 300000, "saved_cents": 75000,
         **_BASE_GOAL_FIELDS}
    s = bot._describe_goal(g)
    assert "#3" in s and "Japan" in s
    assert "S$750.00" in s and "S$3,000.00" in s and "25%" in s


def test_describe_goal_escapes_html_special_chars():
    g = {"id": 4, "name": "Save<50% & more", "target_cents": 100000, "saved_cents": 0,
         **_BASE_GOAL_FIELDS}
    s = bot._describe_goal(g)
    name_line = s.splitlines()[0]
    assert "&lt;" in name_line
    assert "&amp;" in name_line
    assert "<50%" not in name_line


def test_edit_prompt_roundtrip():
    row = {"id": 142, "amount_cents": 1230, "note": "TRUST CARD BUS/MRT"}
    prompt = bot._edit_prompt(row)
    assert "#142" in prompt and "S$12.30" in prompt
    assert bot._parse_edit_prompt_tid(prompt) == 142


def test_parse_edit_prompt_tid_rejects_other_text():
    assert bot._parse_edit_prompt_tid("kopi 2.80") is None
    assert bot._parse_edit_prompt_tid("") is None
    assert bot._parse_edit_prompt_tid(None) is None


def test_digest_lines_all_sources_with_markers():
    rows = [
        {"amount_cents": 1230, "note": "GRAB* SG", "category": "Transport",
         "type": "expense", "source": "trust-email"},
        {"amount_cents": 450, "note": "kopi", "category": "Food",
         "type": "expense", "source": "telegram"},
        {"amount_cents": 12000, "note": "refund", "category": "Income",
         "type": "income", "source": "suggest-email"},
    ]
    lines = bot._digest_lines(rows)
    assert "3 txns" in lines[0] and "S$16.80" in lines[0]
    assert "✉" in lines[1] and "✉" not in lines[2] and "\U0001F4B0 +" in lines[3]
    assert bot._digest_lines([]) is None


def test_reimb_confirm_net_and_warning():
    orig = {"amount_cents": 12000, "note": "dinner", "category": "Food"}
    msg = bot._reimb_confirm(4000, orig, [{"amount_cents": -4000}])
    assert "S$40.00" in msg and "net S$80.00" in msg and "⚠" not in msg
    over = bot._reimb_confirm(15000, orig, [{"amount_cents": -15000}])
    assert "⚠" in over


def test_merchants_text_lists_by_position():
    rows = [{"stem": "ntuc fp", "category": "Groceries", "hits": 12},
            {"stem": "shopee", "category": "Shopping", "hits": 4}]
    text = bot._merchants_text(rows)
    assert "1. ntuc fp → Groceries · 12×" in text
    assert "2. shopee → Shopping · 4×" in text


def test_merchants_text_empty_state_explains_how_to_teach():
    text = bot._merchants_text([])
    assert "fix a category" in text.lower()


def test_merchants_text_escapes_ampersand_in_stem():
    # Stems come from bank merchant strings (e.g. "H&M SINGAPORE SG" -> "h&m")
    # and get interpolated into an HTML-parsed message; an unescaped "&" makes
    # Telegram reject the whole /merchants listing with 400 Bad Request.
    rows = [{"stem": "h&m", "category": "Shopping", "hits": 3}]
    text = bot._merchants_text(rows)
    assert "h&amp;m" in text
    assert "h&m" not in text


def test_merchants_text_escapes_angle_brackets_in_stem():
    # Stems come from bank merchant strings (e.g. "TOYS<R>US SG") and get
    # interpolated into an HTML-parsed message; an unescaped "<r>" reads as an
    # unknown tag and makes Telegram reject the whole /merchants listing.
    rows = [{"stem": "toys<r>us", "category": "Shopping", "hits": 1}]
    text = bot._merchants_text(rows)
    assert "toys&lt;r&gt;us" in text


def test_goal_prompt_marker_is_not_claimed_by_the_note_regex():
    """The two ForceReply prompt families must never collide."""
    prompt = bot._goal_prompt(4, "due")
    assert bot._parse_edit_prompt_tid(prompt) is None
    assert bot._parse_goal_prompt(prompt) == (4, "due")


def test_note_prompt_is_not_claimed_by_the_goal_regex():
    row = {"id": 7, "amount_cents": 250, "note": "kopi"}
    assert bot._parse_goal_prompt(bot._edit_prompt(row)) is None


def test_parse_goal_prompt_handles_every_field():
    for field in ("due", "monthly", "note", "price"):
        assert bot._parse_goal_prompt(bot._goal_prompt(12, field)) == (12, field)


def test_parse_goal_prompt_returns_none_for_unrelated_text():
    assert bot._parse_goal_prompt("just a normal message") is None
    assert bot._parse_goal_prompt(None) is None


def test_describe_goal_escapes_html_in_name_and_note():
    g = {"id": 1, "name": "H&M TV", "target_cents": 1000, "saved_cents": 0,
         **_BASE_GOAL_FIELDS, "note": "<b>x</b>"}
    out = bot._describe_goal(g)
    assert "H&amp;M" in out and "&lt;b&gt;" in out
    assert "<b>x</b>" not in out


def test_describe_goal_shows_behind_shortfall():
    g = {"id": 1, "name": "Japan", "target_cents": 300000, "saved_cents": 100000,
         "category": "Travel", "priority": 1, "note": "", "bought_at": None,
         "target_date": "2026-08-01", "monthly_cents": 60000,
         "pace": {"status": "behind", "need_per_month": 100000,
                  "months_left": 2, "shortfall_per_month": 40000}}
    out = bot._describe_goal(g)
    assert "2026-08-01" in out
    assert "needs S$1,000.00/mo" in out          # format_cents(100000)
    assert "saving S$600.00/mo" in out           # format_cents(60000)
    assert "behind S$400.00/mo" in out           # format_cents(40000)


# --- _apply_goal_reply + on_edit_reply dispatch -----------------------------
# _apply_goal_reply is a coroutine with no awaits, so asyncio.run() exercises
# it synchronously against a real (temp-file) sqlite DB -- no pytest-asyncio
# needed. bot._conn() reads the module-level DB_PATH each call, so pointing
# that at a temp file lets every _conn() call inside the function under test
# share the same on-disk DB.

@pytest.fixture
def goal_db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    conn = db_module.connect(path)
    db_module.init_schema(conn)
    conn.close()
    monkeypatch.setattr(bot, "DB_PATH", path)
    return path


def _mk_goal(db_path, name="Camera", target_cents=100000, **fields):
    conn = db_module.connect(db_path)
    gid = db_module.add_goal(conn, name, target_cents)
    if fields:
        db_module.update_goal(conn, gid, **fields)
    conn.close()
    return gid


def _read_goal(db_path, gid):
    conn = db_module.connect(db_path)
    row = db_module.get_goal(conn, gid)
    conn.close()
    return row


def test_apply_goal_reply_bought_dash_uses_target_amount(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=150000, category="Shopping")
    err, g = asyncio.run(bot._apply_goal_reply(gid, "price", "-"))
    assert err == ""
    assert g["bought_at"] is not None
    row = _read_goal(goal_db, gid)
    conn = db_module.connect(goal_db)
    tx = db_module.get_transaction(conn, row["bought_tid"])
    conn.close()
    assert tx["amount_cents"] == 150000


def test_apply_goal_reply_bought_defaults_category_when_goal_has_none(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000)  # no category set
    asyncio.run(bot._apply_goal_reply(gid, "price", "-"))
    row = _read_goal(goal_db, gid)
    conn = db_module.connect(goal_db)
    tx = db_module.get_transaction(conn, row["bought_tid"])
    conn.close()
    assert tx["category"] == bot.DEFAULT_CATEGORY


def test_apply_goal_reply_bought_uses_goals_own_category_when_set(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000, category="Shopping")
    asyncio.run(bot._apply_goal_reply(gid, "price", "-"))
    row = _read_goal(goal_db, gid)
    conn = db_module.connect(goal_db)
    tx = db_module.get_transaction(conn, row["bought_tid"])
    conn.close()
    assert tx["category"] == "Shopping"


def test_apply_goal_reply_bought_twice_is_refused(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000)
    asyncio.run(bot._apply_goal_reply(gid, "price", "-"))
    err, g = asyncio.run(bot._apply_goal_reply(gid, "price", "-"))
    assert g is None
    assert "already" in err.lower()


def test_apply_goal_reply_due_malformed_date_returns_message_not_raise(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000)
    err, g = asyncio.run(bot._apply_goal_reply(gid, "due", "not-a-date"))
    assert g is None
    assert "YYYY-MM-DD" in err
    assert _read_goal(goal_db, gid)["target_date"] is None


def test_apply_goal_reply_due_dash_clears_target_date(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000, target_date="2027-01-01")
    err, g = asyncio.run(bot._apply_goal_reply(gid, "due", "-"))
    assert err == ""
    assert g["target_date"] is None


def test_apply_goal_reply_due_normalizes_non_canonical_isoformat(goal_db):
    # date.fromisoformat also accepts "20270301" (basic format, no dashes) --
    # the stored value must still come out as YYYY-MM-DD or it mis-sorts and
    # won't populate the web <input type="date">.
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000)
    err, g = asyncio.run(bot._apply_goal_reply(gid, "due", "20270301"))
    assert err == ""
    assert g["target_date"] == "2027-03-01"


def test_apply_goal_reply_monthly_dash_clears_monthly_and_next_due(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000,
                   monthly_cents=20000, next_due="2026-08-01")
    err, g = asyncio.run(bot._apply_goal_reply(gid, "monthly", "-"))
    assert err == ""
    assert g["monthly_cents"] is None
    assert _read_goal(goal_db, gid)["next_due"] is None


def test_describe_goal_shows_monthly_even_without_deadline(goal_db):
    # status "none" (no target_date) previously dropped monthly_cents
    # entirely -- arming auto-save on a no-deadline goal was invisible.
    g = {"id": 5, "name": "Emergency Fund", "target_cents": 500000, "saved_cents": 0,
         "category": None, "priority": 2, "note": "", "bought_at": None,
         "target_date": None, "monthly_cents": 20000,
         "pace": {"status": "none", "need_per_month": None,
                  "months_left": None, "shortfall_per_month": 0}}
    out = bot._describe_goal(g)
    assert "saving S$200.00/mo" in out


def test_send_goal_nudge_excludes_bought_goals(goal_db, monkeypatch):
    # Buying writes a transaction, not a contribution, so saved_cents stays
    # below target_cents forever -- bought_at must be checked explicitly or
    # a bought goal keeps getting offered leftover money on the 1st.
    conn = db_module.connect(goal_db)
    db_module.add_goal(conn, "Open", 100000)
    bought_gid = db_module.add_goal(conn, "Bought", 100000)
    tid = db_module.add_transaction(conn, 1, "x", "Other", "goal")
    db_module.mark_goal_bought(conn, bought_gid, tid)
    conn.close()

    monkeypatch.setattr(bot.db, "month_income", lambda c, ym: 100000)
    monkeypatch.setattr(bot.db, "month_summary", lambda c, ym: {"total_cents": 0})

    sent = {}

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent["markup"] = kw.get("reply_markup")

    app = SimpleNamespace(bot=FakeBot())
    asyncio.run(bot.send_goal_nudge(app, "2026-06"))
    labels = [btn.text for row in sent["markup"].inline_keyboard for btn in row]
    assert any("Open" in label for label in labels)
    assert not any("Bought" in label for label in labels)


def test_apply_goal_reply_note_dash_clears_note(goal_db):
    gid = _mk_goal(goal_db, name="Camera", target_cents=100000, note="birthday gift")
    err, g = asyncio.run(bot._apply_goal_reply(gid, "note", "-"))
    assert err == ""
    assert g["note"] == ""


def test_apply_goal_reply_missing_goal_reports_gone(goal_db):
    err, g = asyncio.run(bot._apply_goal_reply(9999, "note", "hello"))
    assert g is None
    assert "gone" in err.lower()


# --- on_edit_reply dispatch order + on_text fall-through --------------------
# The prompt message IS the state (no ConversationHandler, no per-user dict),
# so on_edit_reply's job is purely dispatch: goal prompt -> _apply_goal_reply,
# note prompt -> the existing note-edit body, anything else -> on_text(). A
# regression here would silently break plain-text adds like "kopi 2.80".

def _fake_message(text, reply_to_message=None):
    replies = []

    async def reply_text(txt, **kwargs):
        replies.append((txt, kwargs))

    msg = SimpleNamespace(text=text, reply_to_message=reply_to_message,
                          reply_text=reply_text)
    msg.replies = replies
    return msg


def _bot_message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(is_bot=True))


def _human_message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(is_bot=False))


def _fake_update(message, chat_id):
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))


def test_on_edit_reply_falls_through_to_on_text_when_no_reply(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    called = {"hit": False}

    async def fake_on_text(update, ctx):
        called["hit"] = True

    monkeypatch.setattr(bot, "on_text", fake_on_text)
    msg = _fake_message("kopi 2.80", reply_to_message=None)
    asyncio.run(bot.on_edit_reply(_fake_update(msg, 555), None))
    assert called["hit"] is True


def test_on_edit_reply_falls_through_to_on_text_for_unrelated_bot_reply(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    called = {"hit": False}

    async def fake_on_text(update, ctx):
        called["hit"] = True

    monkeypatch.setattr(bot, "on_text", fake_on_text)
    replied = _bot_message("just chatting, not a prompt")
    msg = _fake_message("kopi 2.80", reply_to_message=replied)
    asyncio.run(bot.on_edit_reply(_fake_update(msg, 555), None))
    assert called["hit"] is True


def test_on_edit_reply_falls_through_to_on_text_when_replying_to_a_human(monkeypatch):
    # Replying to a non-bot message must never be read as a stateful prompt,
    # even if its text happens to look like one.
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    called = {"hit": False}

    async def fake_on_text(update, ctx):
        called["hit"] = True

    monkeypatch.setattr(bot, "on_text", fake_on_text)
    replied = _human_message(bot._goal_prompt(3, "note"))
    msg = _fake_message("kopi 2.80", reply_to_message=replied)
    asyncio.run(bot.on_edit_reply(_fake_update(msg, 555), None))
    assert called["hit"] is True


def test_on_edit_reply_dispatches_goal_prompt_without_falling_through(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    called = {"on_text": False, "apply_args": None}

    async def fake_on_text(update, ctx):
        called["on_text"] = True

    async def fake_apply(gid, field, text):
        called["apply_args"] = (gid, field, text)
        g = {"id": gid, "name": "Camera", "target_cents": 100000, "saved_cents": 0,
             "category": None, "priority": 2, "note": "", "bought_at": None,
             "target_date": None, "monthly_cents": None,
             "pace": {"status": "none", "need_per_month": None,
                      "months_left": None, "shortfall_per_month": 0}}
        return "", g

    monkeypatch.setattr(bot, "on_text", fake_on_text)
    monkeypatch.setattr(bot, "_apply_goal_reply", fake_apply)
    replied = _bot_message(bot._goal_prompt(9, "note"))
    msg = _fake_message("new note text", reply_to_message=replied)
    asyncio.run(bot.on_edit_reply(_fake_update(msg, 555), None))

    assert called["apply_args"] == (9, "note", "new note text")
    assert called["on_text"] is False
    assert msg.replies  # a reply was sent back to the user


def test_on_edit_reply_dispatches_note_prompt_without_falling_through(monkeypatch, goal_db):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_ID", 555)
    called = {"on_text": False, "apply": False}

    async def fake_on_text(update, ctx):
        called["on_text"] = True

    async def fake_apply(gid, field, text):
        called["apply"] = True
        return "", None

    monkeypatch.setattr(bot, "on_text", fake_on_text)
    monkeypatch.setattr(bot, "_apply_goal_reply", fake_apply)

    conn = db_module.connect(goal_db)
    tid = db_module.add_transaction(conn, 280, "kopi", "Food", "telegram")
    row = db_module.get_transaction(conn, tid)
    conn.close()

    replied = _bot_message(bot._edit_prompt(row))
    msg = _fake_message("kopi with friend", reply_to_message=replied)
    asyncio.run(bot.on_edit_reply(_fake_update(msg, 555), None))

    assert called["on_text"] is False
    assert called["apply"] is False
    conn = db_module.connect(goal_db)
    updated = db_module.get_transaction(conn, tid)
    conn.close()
    assert updated["note"] == "kopi with friend"
