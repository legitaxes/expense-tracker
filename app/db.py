from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_year_month() -> str:
    return datetime.now().strftime("%Y-%m")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_cents INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY,
            monthly_limit_cents INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alert_state (
            category TEXT NOT NULL,
            year_month TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            notified_at TEXT NOT NULL,
            PRIMARY KEY (category, year_month, threshold)
        );
        CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);
        CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
        CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_cents INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            frequency TEXT NOT NULL,
            anchor INTEGER,
            next_due TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rec_due ON recurring(active, next_due);
        CREATE TABLE IF NOT EXISTS pending_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_cents INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS goal_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            amount_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS merchant_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stem TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_cost_cents INTEGER NOT NULL,
            market_price_cents INTEGER NOT NULL,
            market_value_cents INTEGER NOT NULL,
            unrealized_pl_cents INTEGER NOT NULL,
            account TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            currency TEXT NOT NULL,
            total_value_cents INTEGER NOT NULL,
            cash_cents INTEGER NOT NULL DEFAULT 0,
            unrealized_pl_cents INTEGER NOT NULL DEFAULT 0,
            sgd_rate REAL,
            UNIQUE(snapshot_date, currency)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pw_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS payslips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT NOT NULL UNIQUE,
            pdf_path TEXT NOT NULL,
            raw_text TEXT NOT NULL DEFAULT '',
            net_cents INTEGER,
            gross_cents INTEGER,
            source_uid INTEGER,
            received_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payslip_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payslip_id INTEGER NOT NULL REFERENCES payslips(id) ON DELETE CASCADE,
            section TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            ordinal INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payslip_items_slip
            ON payslip_items(payslip_id);
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            last4 TEXT,
            match TEXT,
            archived_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_instruments (
            source TEXT NOT NULL,
            token TEXT NOT NULL,
            card_id INTEGER NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            PRIMARY KEY (source, token)
        );
        """
    )
    # Idempotent column adds — runs every boot against the live DB.
    for table in ("transactions", "pending_imports"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "type" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN type TEXT NOT NULL DEFAULT 'expense'")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    if "link_tid" not in cols:
        # Reimbursement rows (negative amount) point at the expense they repay.
        conn.execute("ALTER TABLE transactions ADD COLUMN link_tid INTEGER")
    # Which card paid. NULL is the Unassigned state, which is a normal, visible
    # bucket -- existing history is deliberately left alone rather than guessed at.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    for name, ddl in (("card_id", "INTEGER"), ("instrument", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}")
    # Payslips record how their text was obtained ('pdf' or 'ocr'); OCR figures
    # warrant a second look, so the dashboard badges them.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(payslips)")]
    if "text_source" not in cols:
        conn.execute("ALTER TABLE payslips ADD COLUMN text_source TEXT")
    # Goals gained wishlist fields (deadline, auto-funding, purchase link).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(goals)")]
    for name, ddl in (
        ("target_date", "TEXT"),
        ("monthly_cents", "INTEGER"),
        ("next_due", "TEXT"),
        ("category", "TEXT"),
        ("priority", "INTEGER NOT NULL DEFAULT 2"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
        ("bought_at", "TEXT"),
        ("bought_tid", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {name} {ddl}")
    conn.commit()


def add_transaction(conn, amount_cents, note, category, source, created_at=None,
                    type_="expense", link_tid=None, card_id=None, instrument=None) -> int:
    created_at = created_at or now_iso()
    cur = conn.execute(
        "INSERT INTO transactions (amount_cents, note, category, created_at, source, "
        "type, link_tid, card_id, instrument) VALUES (?,?,?,?,?,?,?,?,?)",
        (amount_cents, note, category, created_at, source, type_, link_tid,
         card_id, instrument),
    )
    conn.commit()
    return cur.lastrowid


def get_transaction(conn, tid):
    return conn.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()


def list_transactions(conn, date_from=None, date_to=None, category=None, limit=None):
    clauses, params = [], []
    if date_from:
        clauses.append("created_at >= ?"); params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?"); params.append(date_to + "T23:59:59")
    if category:
        clauses.append("category = ?"); params.append(category)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = ("SELECT t.*, COALESCE((SELECT SUM(-r.amount_cents) FROM transactions r "
           "WHERE r.link_tid = t.id), 0) AS reimbursed_cents "
           f"FROM transactions t{where} ORDER BY created_at DESC, id DESC")
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return conn.execute(sql, params).fetchall()


_UNSET = object()


def update_transaction(conn, tid, amount_cents=None, note=None, category=None,
                       card_id=_UNSET) -> bool:
    sets, params = [], []
    if amount_cents is not None:
        sets.append("amount_cents=?"); params.append(amount_cents)
    if note is not None:
        sets.append("note=?"); params.append(note)
    if category is not None:
        sets.append("category=?"); params.append(category)
    # A sentinel, not None: clearing the card to Unassigned is a real edit, so
    # None cannot double as "not supplied" the way it does above.
    if card_id is not _UNSET:
        sets.append("card_id=?"); params.append(card_id)
    if not sets:
        return False
    params.append(tid)
    cur = conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return cur.rowcount > 0


def delete_transaction(conn, tid) -> bool:
    # Reimbursements pointing here degrade to unlinked category offsets.
    conn.execute("UPDATE transactions SET link_tid=NULL WHERE link_tid=?", (tid,))
    cur = conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
    conn.commit()
    return cur.rowcount > 0


def recent_expense_candidates(conn, min_cents, days=7, limit=8):
    """Picker candidates for linking a reimbursement to its original expense."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM transactions WHERE type='expense' AND amount_cents >= ? "
        "AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (min_cents, cutoff, limit)).fetchall()


def get_links(conn, tid):
    return conn.execute(
        "SELECT * FROM transactions WHERE link_tid=? ORDER BY created_at, id",
        (tid,)).fetchall()


# Marks a payback's note. Shared with bot.py so the prefix the bot WRITES is
# the prefix unlink STRIPS -- two copies of this literal would drift and leave
# "↩️ ↩️ " notes behind.
REIMB_PREFIX = "↩️ "


def link_as_reimbursement(conn, tid, target_tid) -> bool:
    """Turn an income row into a payback of the expense `target_tid`.

    The web deliberately mirrors the bot's model rather than inventing a second
    one: a reimbursement IS a negative expense carrying link_tid, sitting in the
    ORIGINAL's category. That is what makes the month net down through the
    existing reimbursed_cents sum. Leaving the row as income and merely
    cross-referencing it would push that sum the wrong way.

    Returns False rather than raising for every rejection, so the caller maps
    one predictable failure to one status code.
    """
    if tid == target_tid:
        return False
    row = get_transaction(conn, tid)
    target = get_transaction(conn, target_tid)
    if row is None or target is None:
        return False
    # Only money IN can become a payback, and only a real expense can receive
    # one. Guarding the target's sign is what stops a payback being linked to
    # another payback, which would net twice.
    if row["type"] != "income" or row["amount_cents"] <= 0:
        return False
    if target["type"] != "expense" or target["amount_cents"] <= 0:
        return False
    note = row["note"] or ""
    if not note.startswith(REIMB_PREFIX):
        note = REIMB_PREFIX + note
    conn.execute(
        "UPDATE transactions SET amount_cents=?, category=?, note=?, "
        "type='expense', link_tid=? WHERE id=?",
        (-row["amount_cents"], target["category"], note, target_tid, tid))
    conn.commit()
    return True


def unlink_reimbursement(conn, tid) -> bool:
    """Exact inverse of link_as_reimbursement: back to a plain income row.

    Also reachable for a payback the BOT created from a pending import, which
    never was an income row. Restoring it to income is still the right answer
    there -- the money did arrive, it is simply no longer attributed to a
    spend -- and it keeps one unlink path instead of two.
    """
    row = get_transaction(conn, tid)
    if row is None or row["link_tid"] is None:
        return False
    note = row["note"] or ""
    if note.startswith(REIMB_PREFIX):
        note = note[len(REIMB_PREFIX):]
    conn.execute(
        "UPDATE transactions SET amount_cents=?, category='Income', note=?, "
        "type='income', link_tid=NULL WHERE id=?",
        (abs(row["amount_cents"]), note, tid))
    conn.commit()
    return True


def latest_transaction_id(conn):
    row = conn.execute("SELECT id FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def _card_clause(card_id):
    """SQL fragment + params narrowing a query to one card.

    Returns (fragment, params). The fragment starts with " AND " so callers
    can splice it into an existing WHERE, or is "" for no filter at all.

    card_id is three-state, matching the client (static/app.js:692):
      None -> no filter at all
      0    -> the Unassigned bucket (card_id IS NULL)
      N    -> that card

    Never test card_id for falsiness: 0 and None mean different things, and
    collapsing them silently returns all-cards data under a filter.

    Raises:
      TypeError: card_id is neither None nor an int (bool included, since
        bool is an int subclass).
      ValueError: card_id is a negative int.
    """
    if card_id is None:
        return "", []
    # Strict on purpose: this helper is the ONLY place the sentinel is decoded,
    # so a wrong type reaching it means a caller misunderstood the contract, and
    # the symptom would otherwise be wrong numbers rather than an error. bool is
    # rejected explicitly because it is an int subclass — True would silently
    # filter to card 1.
    if isinstance(card_id, bool) or not isinstance(card_id, int):
        raise TypeError("card_id must be an int or None, got %r" % (card_id,))
    if card_id < 0:
        raise ValueError("card_id must be 0 (unassigned) or a card id, got %d" % card_id)
    if card_id == 0:
        return " AND card_id IS NULL", []
    return " AND card_id = ?", [card_id]


def month_summary(conn, year_month, card_id=None):
    clause, params = _card_clause(card_id)
    rows = conn.execute(
        "SELECT category, SUM(amount_cents) AS total FROM transactions "
        "WHERE substr(created_at,1,7)=? AND type='expense'" + clause +
        " GROUP BY category ORDER BY total DESC",
        [year_month] + params,
    ).fetchall()
    by_category = [{"category": r["category"], "total_cents": r["total"]} for r in rows]
    total = sum(r["total"] for r in rows)
    return {"month": year_month, "total_cents": total, "by_category": by_category}


def month_income(conn, year_month) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM transactions "
        "WHERE substr(created_at,1,7)=? AND type='income'",
        (year_month,),
    ).fetchone()
    return row["total"]


def year_summary(conn, year):
    rows = conn.execute(
        "SELECT substr(created_at,1,7) AS ym, SUM(amount_cents) AS total "
        "FROM transactions WHERE substr(created_at,1,4)=? AND type='expense' "
        "GROUP BY ym", (year,)).fetchall()
    by_ym = {r["ym"]: r["total"] for r in rows}
    cat_rows = conn.execute(
        "SELECT substr(created_at,1,7) AS ym, category, SUM(amount_cents) AS total "
        "FROM transactions WHERE substr(created_at,1,4)=? AND type='expense' "
        "GROUP BY ym, category", (year,)).fetchall()
    by_ym_cat: dict[str, dict[str, int]] = {}
    for r in cat_rows:
        by_ym_cat.setdefault(r["ym"], {})[r["category"]] = r["total"]
    cats = conn.execute(
        "SELECT category, SUM(amount_cents) AS total FROM transactions "
        "WHERE substr(created_at,1,4)=? AND type='expense' "
        "GROUP BY category ORDER BY total DESC", (year,)).fetchall()
    income = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM transactions "
        "WHERE substr(created_at,1,4)=? AND type='income'", (year,)).fetchone()["total"]
    # Months that haven't happened yet aren't charted: the current year stops
    # at this month; past (and future) years keep all 12.
    now = current_year_month()
    last_month = int(now[5:7]) if year == now[:4] else 12
    return {"year": year, "total_cents": sum(by_ym.values()), "income_cents": income,
            "by_month": [{"month": f"{m:02d}",
                          "total_cents": by_ym.get(f"{year}-{m:02d}", 0),
                          "by_category": by_ym_cat.get(f"{year}-{m:02d}", {})}
                         for m in range(1, last_month + 1)],
            "by_category": [{"category": r["category"], "total_cents": r["total"]}
                            for r in cats]}


def cashflow(conn):
    rows = conn.execute(
        "SELECT substr(created_at,1,7) AS period, type, SUM(amount_cents) AS total "
        "FROM transactions GROUP BY period, type ORDER BY period"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["period"],
                           {"period": r["period"], "income_cents": 0, "expense_cents": 0})
        d["income_cents" if r["type"] == "income" else "expense_cents"] = r["total"]
    return list(out.values())


def trend(conn, granularity="month", card_id=None):
    length = 7 if granularity == "month" else 10
    clause, params = _card_clause(card_id)
    rows = conn.execute(
        f"SELECT substr(created_at,1,{length}) AS period, category, "
        "SUM(amount_cents) AS total FROM transactions WHERE type='expense'" + clause +
        " GROUP BY period, category ORDER BY period", params
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["period"], {"period": r["period"], "total_cents": 0,
                                         "by_category": {}})
        d["total_cents"] += r["total"]
        d["by_category"][r["category"]] = r["total"]
    return list(out.values())


def merchant_leaderboard(conn, year_month, limit=15):
    # naive LOWER(note) grouping; add an alias map only if the list reads badly
    # bare `category` = any row in the group (a merchant's rows share one category in practice)
    rows = conn.execute(
        "SELECT LOWER(note) AS merchant, SUM(amount_cents) AS total, COUNT(*) AS n, category "
        "FROM transactions WHERE substr(created_at,1,7)=? AND note<>'' AND type='expense' "
        "GROUP BY merchant ORDER BY total DESC LIMIT ?",
        (year_month, limit),
    ).fetchall()
    return [{"merchant": r["merchant"], "total_cents": r["total"], "count": r["n"],
             "category": r["category"]}
            for r in rows]


def set_budget(conn, category, limit_cents):
    conn.execute(
        "INSERT INTO budgets (category, monthly_limit_cents) VALUES (?,?) "
        "ON CONFLICT(category) DO UPDATE SET monthly_limit_cents=excluded.monthly_limit_cents",
        (category, limit_cents),
    )
    conn.commit()


def delete_budget(conn, category) -> bool:
    cur = conn.execute("DELETE FROM budgets WHERE category=?", (category,))
    conn.commit()
    return cur.rowcount > 0


def get_budgets(conn):
    rows = conn.execute(
        "SELECT category, monthly_limit_cents FROM budgets"
    ).fetchall()
    return {r["category"]: r["monthly_limit_cents"] for r in rows}


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def remember_merchant(conn, stem, category) -> None:
    """Learn (or re-learn) a merchant's category. Re-learning replaces the entry
    and resets its hit count — a corrected entry starts its tally over."""
    ts = now_iso()
    conn.execute(
        "INSERT INTO merchant_memory (stem, category, hits, created_at, updated_at) "
        "VALUES (?,?,0,?,?) ON CONFLICT(stem) DO UPDATE SET "
        "category=excluded.category, hits=0, updated_at=excluded.updated_at",
        (stem, category, ts, ts),
    )
    conn.commit()


def recall_merchant(conn, stem):
    """Return the remembered category for `stem`, or None. Bumps `hits`, which
    orders /merchants and makes an over-broad stem visible as a runaway count."""
    row = conn.execute(
        "SELECT id, category FROM merchant_memory WHERE stem=?", (stem,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE merchant_memory SET hits=hits+1 WHERE id=?", (row["id"],))
    conn.commit()
    return row["category"]


def list_merchants(conn):
    return conn.execute(
        "SELECT * FROM merchant_memory ORDER BY hits DESC, stem").fetchall()


def get_merchant(conn, mid):
    return conn.execute("SELECT * FROM merchant_memory WHERE id=?", (mid,)).fetchone()


def forget_merchant(conn, mid) -> bool:
    cur = conn.execute("DELETE FROM merchant_memory WHERE id=?", (mid,))
    conn.commit()
    return cur.rowcount > 0


# ---- cards ------------------------------------------------------------------

def add_card(conn, name, kind, last4=None, match=None) -> int:
    cur = conn.execute(
        "INSERT INTO cards (name, kind, last4, match, created_at) VALUES (?,?,?,?,?)",
        (name, kind, last4, match, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_card(conn, cid):
    return conn.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()


def list_cards(conn, include_archived=False):
    where = "" if include_archived else " WHERE archived_at IS NULL"
    return conn.execute(f"SELECT * FROM cards{where} ORDER BY id").fetchall()


_CARD_FIELDS = ("name", "kind", "last4", "match")


def update_card(conn, cid, **fields) -> bool:
    sets = [f"{k}=?" for k in fields if k in _CARD_FIELDS]
    if not sets:
        return False
    params = [fields[k] for k in fields if k in _CARD_FIELDS] + [cid]
    cur = conn.execute(f"UPDATE cards SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return cur.rowcount > 0


def archive_card(conn, cid) -> bool:
    """Soft delete. History keeps its attribution; the card stops resolving."""
    cur = conn.execute("UPDATE cards SET archived_at=? WHERE id=? AND archived_at IS NULL",
                       (now_iso(), cid))
    conn.commit()
    return cur.rowcount > 0


def card_tx_count(conn, cid) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE card_id=?",
                        (cid,)).fetchone()["n"]


def delete_card(conn, cid) -> bool:
    """Hard delete, refused while any transaction still points here -- losing the
    name would strand those rows with an id that resolves to nothing."""
    if card_tx_count(conn, cid):
        return False
    conn.execute("DELETE FROM card_instruments WHERE card_id=?", (cid,))
    cur = conn.execute("DELETE FROM cards WHERE id=?", (cid,))
    conn.commit()
    return cur.rowcount > 0


def bind_instrument(conn, source, token, card_id) -> None:
    """Map (source, token) to a card. Rebinding replaces and resets the hit
    count, exactly as remember_merchant does for a corrected merchant."""
    conn.execute(
        "INSERT INTO card_instruments (source, token, card_id, hits, last_seen) "
        "VALUES (?,?,?,0,?) ON CONFLICT(source, token) DO UPDATE SET "
        "card_id=excluded.card_id, hits=0, last_seen=excluded.last_seen",
        (source, token, card_id, now_iso()))
    conn.commit()


def recall_instrument(conn, source, token):
    """card_id for (source, token), or None. Bumps hits so an over-broad
    source default is visible as a runaway count."""
    row = conn.execute(
        "SELECT ci.card_id FROM card_instruments ci JOIN cards c ON c.id = ci.card_id "
        "WHERE ci.source=? AND ci.token=? AND c.archived_at IS NULL",
        (source, token)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE card_instruments SET hits=hits+1, last_seen=? "
                 "WHERE source=? AND token=?", (now_iso(), source, token))
    conn.commit()
    return row["card_id"]


def list_instruments(conn):
    return conn.execute(
        "SELECT ci.*, c.name AS card_name FROM card_instruments ci "
        "JOIN cards c ON c.id = ci.card_id ORDER BY ci.hits DESC, ci.source, ci.token"
    ).fetchall()


def forget_instrument(conn, source, token) -> bool:
    cur = conn.execute("DELETE FROM card_instruments WHERE source=? AND token=?",
                       (source, token))
    conn.commit()
    return cur.rowcount > 0


def month_card_spend(conn, year_month):
    """Per-card expense total for a month. card_id NULL is the Unassigned bucket."""
    return conn.execute(
        "SELECT t.card_id AS card_id, c.name AS name, c.kind AS kind, "
        "COALESCE(SUM(t.amount_cents),0) AS total_cents "
        "FROM transactions t LEFT JOIN cards c ON c.id = t.card_id "
        "WHERE substr(t.created_at,1,7)=? AND t.type='expense' "
        "GROUP BY t.card_id ORDER BY total_cents DESC", (year_month,)).fetchall()


def month_category_spend(conn, year_month, category) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM transactions "
        "WHERE substr(created_at,1,7)=? AND category=? AND type='expense'",
        (year_month, category),
    ).fetchone()
    return row["total"]


def check_and_record_alerts(conn, category, year_month, thresholds):
    """Return thresholds newly crossed this month for category, recording each so it
    fires at most once. Returns [] if the category has no budget."""
    limit = get_budgets(conn).get(category)
    if not limit:
        return []
    spent = month_category_spend(conn, year_month, category)
    pct = spent / limit * 100
    crossed = []
    for t in sorted(thresholds):
        if pct >= t:
            already = conn.execute(
                "SELECT 1 FROM alert_state WHERE category=? AND year_month=? AND threshold=?",
                (category, year_month, t),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO alert_state (category, year_month, threshold, notified_at) "
                    "VALUES (?,?,?,?)",
                    (category, year_month, t, now_iso()),
                )
                crossed.append(t)
    conn.commit()
    return crossed


def add_pending_import(conn, amount_cents, note, created_at, type_="expense") -> int:
    """Store a suggested (unconfirmed) email import so the Telegram Add/Ignore
    buttons can reference it by id — callback_data is too small for the payload."""
    cur = conn.execute(
        "INSERT INTO pending_imports (amount_cents, note, created_at, type) VALUES (?,?,?,?)",
        (amount_cents, note, created_at, type_),
    )
    conn.commit()
    return cur.lastrowid


def get_pending_import(conn, pid):
    return conn.execute("SELECT * FROM pending_imports WHERE id=?", (pid,)).fetchone()


def delete_pending_import(conn, pid) -> bool:
    cur = conn.execute("DELETE FROM pending_imports WHERE id=?", (pid,))
    conn.commit()
    return cur.rowcount > 0


def add_recurring(conn, amount_cents, note, category, frequency, anchor, next_due) -> int:
    """Insert a recurring rule. `frequency` is 'daily'|'weekly'|'monthly'; `anchor` is
    the weekday 0-6 (weekly), day-of-month 1-31 (monthly), or None (daily). `next_due`
    is a 'YYYY-MM-DD' cursor that only ever moves forward."""
    cur = conn.execute(
        "INSERT INTO recurring (amount_cents, note, category, frequency, anchor, "
        "next_due, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
        (amount_cents, note, category, frequency, anchor, next_due, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def get_recurring(conn, rid):
    return conn.execute("SELECT * FROM recurring WHERE id=?", (rid,)).fetchone()


def list_recurring(conn):
    return conn.execute(
        "SELECT * FROM recurring WHERE active=1 ORDER BY next_due, id").fetchall()


def due_recurring(conn, today_iso):
    return conn.execute(
        "SELECT * FROM recurring WHERE active=1 AND next_due <= ? ORDER BY next_due, id",
        (today_iso,),
    ).fetchall()


def set_next_due(conn, rid, next_due) -> None:
    conn.execute("UPDATE recurring SET next_due=? WHERE id=?", (next_due, rid))
    conn.commit()


def deactivate_recurring(conn, rid) -> bool:
    cur = conn.execute("UPDATE recurring SET active=0 WHERE id=? AND active=1", (rid,))
    conn.commit()
    return cur.rowcount > 0


def add_goal(conn, name, target_cents) -> int:
    cur = conn.execute(
        "INSERT INTO goals (name, target_cents, created_at) VALUES (?,?,?)",
        (name, target_cents, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_goal(conn, gid):
    return conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()


def list_goals(conn):
    """Every goal column plus saved_cents. Unbought first, then priority,
    then deadline (nulls last). Pace is added by app.goals — not here."""
    rows = conn.execute(
        "SELECT g.*, COALESCE(SUM(c.amount_cents),0) AS saved "
        "FROM goals g LEFT JOIN goal_contributions c ON c.goal_id=g.id "
        "GROUP BY g.id "
        "ORDER BY g.bought_at IS NOT NULL, g.priority, "
        "         g.target_date IS NULL, g.target_date, g.id").fetchall()
    out = []
    for r in rows:
        g = {k: r[k] for k in r.keys() if k != "saved"}
        g["saved_cents"] = r["saved"]
        out.append(g)
    return out


def find_goal(conn, name):
    """Case-insensitive exact match, else unique prefix; None if absent/ambiguous.
    Prefix matching is plain string startswith — no LIKE wildcards."""
    rows = conn.execute("SELECT * FROM goals").fetchall()
    low = name.lower()
    exact = [r for r in rows if r["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]
    prefixed = [r for r in rows if r["name"].lower().startswith(low)]
    return prefixed[0] if len(prefixed) == 1 else None


def delete_goal(conn, gid) -> bool:
    cur = conn.execute("DELETE FROM goals WHERE id=?", (gid,))
    conn.commit()
    return cur.rowcount > 0


# bought_at/bought_tid are deliberately absent — only mark_goal_bought sets those.
_GOAL_EDITABLE = frozenset({"name", "target_cents", "target_date", "monthly_cents",
                            "next_due", "category", "priority", "note"})


def update_goal(conn, gid, **fields) -> bool:
    sets = {k: v for k, v in fields.items() if k in _GOAL_EDITABLE}
    if not sets:
        return False
    assignments = ", ".join(f"{k}=?" for k in sets)
    cur = conn.execute(f"UPDATE goals SET {assignments} WHERE id=?",
                       (*sets.values(), gid))
    conn.commit()
    return cur.rowcount > 0


def mark_goal_bought(conn, gid, tid) -> bool:
    cur = conn.execute(
        "UPDATE goals SET bought_at=?, bought_tid=? WHERE id=? AND bought_at IS NULL",
        (now_iso(), tid, gid))
    conn.commit()
    return cur.rowcount > 0


def goals_due_funding(conn, today_iso):
    """Unbought goals with a monthly amount whose cursor has arrived."""
    return conn.execute(
        "SELECT g.id, g.name, g.monthly_cents, g.next_due, g.target_cents, "
        "COALESCE(SUM(c.amount_cents),0) AS saved "
        "FROM goals g LEFT JOIN goal_contributions c ON c.goal_id=g.id "
        "WHERE g.monthly_cents IS NOT NULL AND g.bought_at IS NULL "
        "  AND g.next_due IS NOT NULL AND g.next_due<=? "
        "GROUP BY g.id ORDER BY g.id", (today_iso,)).fetchall()


def add_contribution(conn, gid, amount_cents) -> int:
    cur = conn.execute(
        "INSERT INTO goal_contributions (goal_id, amount_cents, created_at) VALUES (?,?,?)",
        (gid, amount_cents, now_iso()))
    conn.commit()
    return cur.lastrowid


def frequent_notes(conn, days=90, limit=6):
    """Most-repeated notes in the window, for quick-add buttons.
    Median in Python — SQLite has no median aggregate."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT note, amount_cents, created_at FROM transactions "
        "WHERE type='expense' AND note<>'' AND created_at>=?", (cutoff,)).fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["note"].lower(),
                              {"amounts": [], "display": r["note"], "latest": ""})
        g["amounts"].append(r["amount_cents"])
        if r["created_at"] > g["latest"]:
            g["latest"], g["display"] = r["created_at"], r["note"]
    ranked = sorted(groups.values(), key=lambda g: len(g["amounts"]), reverse=True)[:limit]
    return [{"note": g["display"],
             "median_cents": sorted(g["amounts"])[len(g["amounts"]) // 2],
             "count": len(g["amounts"])} for g in ranked]


def search_transactions(conn, q, limit=8):
    esc = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return conn.execute(
        "SELECT * FROM transactions WHERE note LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC, id DESC LIMIT ?", (f"%{esc}%", limit)).fetchall()


def record_pace_alert(conn, category, year_month) -> bool:
    """Once-per-category-month marker for the 110%-pace warning. Rides the
    alert_state PK (category, year_month, threshold)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO alert_state (category, year_month, threshold, notified_at) "
        "VALUES (?,?,110,?)", (category, year_month, now_iso()))
    conn.commit()
    return cur.rowcount > 0


# ---- portfolio ------------------------------------------------------------
# Current holdings are replaced wholesale each poll; history is upserted one
# row per (date, currency) so repeated manual refreshes can't distort the chart.

_POSITION_FIELDS = ("symbol", "name", "currency", "quantity", "avg_cost_cents",
                    "market_price_cents", "market_value_cents",
                    "unrealized_pl_cents", "account")


def replace_positions(conn, rows) -> None:
    """Swap the whole holdings table in one transaction.

    Wholesale replacement (not upsert) is deliberate: a sold-out position must
    disappear, and there is no reliable delete signal from the broker API.
    """
    now = now_iso()
    payload = [tuple(r[f] for f in _POSITION_FIELDS) + (now,) for r in rows]
    with conn:
        conn.execute("DELETE FROM portfolio_positions")
        if payload:
            conn.executemany(
                "INSERT INTO portfolio_positions "
                "(symbol, name, currency, quantity, avg_cost_cents, "
                " market_price_cents, market_value_cents, unrealized_pl_cents, "
                " account, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                payload,
            )


def list_positions(conn):
    return conn.execute(
        "SELECT * FROM portfolio_positions ORDER BY market_value_cents DESC"
    ).fetchall()


def upsert_snapshot(conn, snapshot_date, currency, total_value_cents,
                    cash_cents, unrealized_pl_cents, sgd_rate) -> None:
    conn.execute(
        "INSERT INTO portfolio_snapshots "
        "(snapshot_date, currency, total_value_cents, cash_cents, "
        " unrealized_pl_cents, sgd_rate) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(snapshot_date, currency) DO UPDATE SET "
        "  total_value_cents=excluded.total_value_cents, "
        "  cash_cents=excluded.cash_cents, "
        "  unrealized_pl_cents=excluded.unrealized_pl_cents, "
        "  sgd_rate=excluded.sgd_rate",
        (snapshot_date, currency, total_value_cents, cash_cents,
         unrealized_pl_cents, sgd_rate),
    )
    conn.commit()


def snapshot_history(conn, days: int = 90):
    """Oldest-first, so the frontend can plot without re-sorting.

    The cutoff is computed in Python rather than with SQLite's date('now'),
    which is UTC. This app runs on SGT (UTC+8), so every day between midnight
    and 8am local the two disagreed and the window silently ran a day long --
    snapshot_date values are written from local dates. This was the only
    date('now') in the file; everything else already uses datetime.now().
    """
    cutoff = (datetime.now() - timedelta(days=int(days) - 1)).date().isoformat()
    return conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE snapshot_date >= ? "
        "ORDER BY snapshot_date ASC, currency ASC", (cutoff,)).fetchall()


# ---- payslips -----------------------------------------------------------
# Archive of the monthly payslip PDF plus the line items printed on it. These
# never touch `transactions`: salary income is already recorded from the bank
# credit alert, and writing a row here too would double-count every month.


def upsert_payslip(conn, period, pdf_path, raw_text, items,
                   net_cents=None, gross_cents=None, source_uid=None,
                   received_at=None, text_source=None) -> int:
    """Insert or REPLACE one month's payslip and its items. Returns the id.

    Replacing rather than appending is what makes a re-requested month safe:
    payslips are pulled on demand, so asking the provider for July twice must leave one
    July row, not two. The items are rewritten wholesale for the same reason.
    """
    received_at = received_at or now_iso()
    conn.execute(
        "INSERT INTO payslips (period, pdf_path, raw_text, net_cents, "
        " gross_cents, source_uid, received_at, text_source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(period) DO UPDATE SET "
        "  pdf_path=excluded.pdf_path, raw_text=excluded.raw_text, "
        "  net_cents=excluded.net_cents, gross_cents=excluded.gross_cents, "
        "  source_uid=excluded.source_uid, received_at=excluded.received_at, "
        "  text_source=excluded.text_source",
        (period, pdf_path, raw_text, net_cents, gross_cents, source_uid,
         received_at, text_source, now_iso()),
    )
    pid = conn.execute("SELECT id FROM payslips WHERE period=?",
                       (period,)).fetchone()["id"]
    conn.execute("DELETE FROM payslip_items WHERE payslip_id=?", (pid,))
    conn.executemany(
        "INSERT INTO payslip_items (payslip_id, section, label, amount_cents, "
        " ordinal) VALUES (?,?,?,?,?)",
        [(pid, it["section"], it["label"], it["amount_cents"], it["ordinal"])
         for it in items],
    )
    conn.commit()
    return pid


def list_payslips(conn):
    """Newest month first — the dashboard opens on the latest payslip."""
    return conn.execute(
        "SELECT id, period, pdf_path, net_cents, gross_cents, received_at "
        "FROM payslips ORDER BY period DESC").fetchall()


def get_payslip(conn, period):
    return conn.execute("SELECT * FROM payslips WHERE period=?",
                        (period,)).fetchone()


def payslip_items(conn, payslip_id):
    """Document order — the payslip is rendered as it was printed."""
    return conn.execute(
        "SELECT section, label, amount_cents FROM payslip_items "
        "WHERE payslip_id=? ORDER BY ordinal ASC", (payslip_id,)).fetchall()
