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
    conn.commit()


def add_transaction(conn, amount_cents, note, category, source, created_at=None,
                    type_="expense", link_tid=None) -> int:
    created_at = created_at or now_iso()
    cur = conn.execute(
        "INSERT INTO transactions (amount_cents, note, category, created_at, source, "
        "type, link_tid) VALUES (?,?,?,?,?,?,?)",
        (amount_cents, note, category, created_at, source, type_, link_tid),
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


def update_transaction(conn, tid, amount_cents=None, note=None, category=None) -> bool:
    sets, params = [], []
    if amount_cents is not None:
        sets.append("amount_cents=?"); params.append(amount_cents)
    if note is not None:
        sets.append("note=?"); params.append(note)
    if category is not None:
        sets.append("category=?"); params.append(category)
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


def latest_transaction_id(conn):
    row = conn.execute("SELECT id FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def month_summary(conn, year_month):
    rows = conn.execute(
        "SELECT category, SUM(amount_cents) AS total FROM transactions "
        "WHERE substr(created_at,1,7)=? AND type='expense' GROUP BY category ORDER BY total DESC",
        (year_month,),
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


def trend(conn, granularity="month"):
    length = 7 if granularity == "month" else 10
    rows = conn.execute(
        f"SELECT substr(created_at,1,{length}) AS period, category, "
        "SUM(amount_cents) AS total FROM transactions WHERE type='expense' "
        "GROUP BY period, category ORDER BY period"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["period"], {"period": r["period"], "total_cents": 0,
                                         "by_category": {}})
        d["total_cents"] += r["total"]
        d["by_category"][r["category"]] = r["total"]
    return list(out.values())


def merchant_leaderboard(conn, year_month, limit=15):
    # ponytail: naive LOWER(note) grouping; add an alias map only if the list reads badly
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


def add_recurring(conn, amount_cents, note, category, frequency, anchor, next_due,
                  created_at=None) -> int:
    """Insert a recurring rule. `frequency` is 'daily'|'weekly'|'monthly'; `anchor` is
    the weekday 0-6 (weekly), day-of-month 1-31 (monthly), or None (daily). `next_due`
    is a 'YYYY-MM-DD' cursor that only ever moves forward."""
    created_at = created_at or now_iso()
    cur = conn.execute(
        "INSERT INTO recurring (amount_cents, note, category, frequency, anchor, "
        "next_due, active, created_at) VALUES (?,?,?,?,?,?,1,?)",
        (amount_cents, note, category, frequency, anchor, next_due, created_at),
    )
    conn.commit()
    return cur.lastrowid


def get_recurring(conn, rid):
    return conn.execute("SELECT * FROM recurring WHERE id=?", (rid,)).fetchone()


def list_recurring(conn, active_only=True):
    sql = "SELECT * FROM recurring"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY next_due, id"
    return conn.execute(sql).fetchall()


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
    rows = conn.execute(
        "SELECT g.id, g.name, g.target_cents, "
        "COALESCE(SUM(c.amount_cents),0) AS saved "
        "FROM goals g LEFT JOIN goal_contributions c ON c.goal_id=g.id "
        "GROUP BY g.id ORDER BY g.id").fetchall()
    return [{"id": r["id"], "name": r["name"], "target_cents": r["target_cents"],
             "saved_cents": r["saved"]} for r in rows]


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
