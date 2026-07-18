# tests/test_bot.py
from types import SimpleNamespace
from app import bot


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


def test_effective_category_trip_mode():
    from app import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    try:
        assert bot._effective_category(conn, "kopi") == ("Food", False)
        db.set_setting(conn, "trip_mode", "1")
        assert bot._effective_category(conn, "kopi") == ("Travel", True)
        db.set_setting(conn, "trip_mode", "0")
        # keyword-tagged Travel is not via_trip — no "trip mode" suffix
        assert bot._effective_category(conn, "scoot flight") == ("Travel", False)
    finally:
        conn.close()


def test_cat_line_suffix():
    assert bot._cat_line("Travel", True) == "🏷️ Travel · ✈️ trip mode"
    assert bot._cat_line("Food", False) == "🏷️ Food"


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


def test_describe_goal():
    g = {"id": 3, "name": "Japan", "target_cents": 300000, "saved_cents": 75000}
    s = bot._describe_goal(g)
    assert "#3" in s and "Japan" in s
    assert "S$750.00" in s and "S$3,000.00" in s and "25%" in s


def test_describe_goal_escapes_html_special_chars():
    g = {"id": 4, "name": "Save<50% & more", "target_cents": 100000, "saved_cents": 0}
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
