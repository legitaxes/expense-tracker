from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
import csv
import io
import re

from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel

from app import db
from app.config import DB_PATH, CATEGORIES
from app.money import dollars_to_cents
from app.categories import guess_category

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    conn.close()
    yield


app = FastAPI(title="Expense Tracker", lifespan=lifespan)


def get_conn():
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


class TxIn(BaseModel):
    amount: float
    note: str = ""
    category: str
    created_at: str | None = None
    type: str = "expense"


class TxPatch(BaseModel):
    amount: float | None = None
    note: str | None = None
    category: str | None = None


class BudgetIn(BaseModel):
    amount: float


class GoalIn(BaseModel):
    name: str
    target: float


class ContribIn(BaseModel):
    amount: float


def _tx(row):
    keys = row.keys()
    return {"id": row["id"], "amount_cents": row["amount_cents"], "note": row["note"],
            "category": row["category"], "created_at": row["created_at"],
            "source": row["source"], "type": row["type"],
            "link_tid": row["link_tid"],
            "reimbursed_cents": row["reimbursed_cents"] if "reimbursed_cents" in keys else 0}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/categories")
def categories():
    return {"categories": CATEGORIES}


@app.get("/api/transactions")
def get_transactions(date_from: str | None = None, date_to: str | None = None,
                     category: str | None = None, limit: int | None = None,
                     conn: sqlite3.Connection = Depends(get_conn)):
    return [_tx(r) for r in db.list_transactions(conn, date_from, date_to, category, limit)]


@app.post("/api/transactions")
def create_transaction(tx: TxIn, conn: sqlite3.Connection = Depends(get_conn)):
    if tx.type not in ("expense", "income"):
        raise HTTPException(400, "type must be expense or income")
    category = "Income" if tx.type == "income" else tx.category
    if tx.type == "expense" and category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    tid = db.add_transaction(conn, dollars_to_cents(tx.amount), tx.note, category,
                             "dashboard", tx.created_at, type_=tx.type)
    return _tx(db.get_transaction(conn, tid))


@app.get("/api/transactions/{tid}/links")
def transaction_links(tid: int, conn: sqlite3.Connection = Depends(get_conn)):
    """The original transaction plus every reimbursement linked to it."""
    row = db.get_transaction(conn, tid)
    if row is None:
        raise HTTPException(404, "not found")
    links = [_tx(r) for r in db.get_links(conn, tid)]
    net = row["amount_cents"] + sum(l["amount_cents"] for l in links)
    return {"original": _tx(row), "links": links, "net_cents": net}


@app.patch("/api/transactions/{tid}")
def patch_transaction(tid: int, patch: TxPatch, conn: sqlite3.Connection = Depends(get_conn)):
    row = db.get_transaction(conn, tid)
    if row is None:
        raise HTTPException(404, "not found")
    if patch.category is not None:
        valid = ["Income"] if row["type"] == "income" else CATEGORIES
        if patch.category not in valid:
            raise HTTPException(400, "unknown category")
    amount_cents = dollars_to_cents(patch.amount) if patch.amount is not None else None
    if amount_cents is not None and row["amount_cents"] < 0:
        amount_cents = -abs(amount_cents)  # reimbursement rows keep their sign
    if not db.update_transaction(conn, tid, amount_cents=amount_cents,
                                 note=patch.note, category=patch.category):
        raise HTTPException(404, "not found or nothing to update")
    return _tx(db.get_transaction(conn, tid))


@app.delete("/api/transactions/{tid}")
def remove_transaction(tid: int, conn: sqlite3.Connection = Depends(get_conn)):
    if not db.delete_transaction(conn, tid):
        raise HTTPException(404, "not found")
    return {"deleted": tid}


@app.get("/api/summary")
def summary(month: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    return db.month_summary(conn, month or db.current_year_month())


@app.get("/api/trend")
def trend(granularity: str = "month", conn: sqlite3.Connection = Depends(get_conn)):
    if granularity not in ("month", "day"):
        raise HTTPException(400, "granularity must be month or day")
    return {"granularity": granularity, "points": db.trend(conn, granularity)}


@app.get("/api/merchants")
def merchants(month: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    ym = month or db.current_year_month()
    return {"month": ym, "merchants": db.merchant_leaderboard(conn, ym)}


@app.get("/api/cashflow")
def get_cashflow(conn: sqlite3.Connection = Depends(get_conn)):
    return {"points": db.cashflow(conn)}


@app.get("/api/budgets")
def budgets(month: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    ym = month or db.current_year_month()
    return [{"category": cat, "limit_cents": limit,
             "spent_cents": db.month_category_spend(conn, ym, cat)}
            for cat, limit in db.get_budgets(conn).items()]


@app.put("/api/budgets/{category}")
def put_budget(category: str, body: BudgetIn, conn: sqlite3.Connection = Depends(get_conn)):
    if category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    cents = dollars_to_cents(body.amount)
    db.set_budget(conn, category, cents)
    return {"category": category, "limit_cents": cents}


def _goal(conn, gid):
    g = next((x for x in db.list_goals(conn) if x["id"] == gid), None)
    if g is None:
        raise HTTPException(404, "goal not found")
    return g


@app.get("/api/goals")
def goals(conn: sqlite3.Connection = Depends(get_conn)):
    return {"goals": db.list_goals(conn)}


@app.post("/api/goals")
def create_goal(body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    gid = db.add_goal(conn, body.name.strip(), dollars_to_cents(body.target))
    return _goal(conn, gid)


@app.delete("/api/goals/{gid}")
def remove_goal(gid: int, conn: sqlite3.Connection = Depends(get_conn)):
    if not db.delete_goal(conn, gid):
        raise HTTPException(404, "goal not found")
    return {"deleted": gid}


@app.post("/api/goals/{gid}/contribute")
def contribute(gid: int, body: ContribIn, conn: sqlite3.Connection = Depends(get_conn)):
    if db.get_goal(conn, gid) is None:
        raise HTTPException(404, "goal not found")
    db.add_contribution(conn, gid, dollars_to_cents(body.amount))
    return _goal(conn, gid)


@app.get("/api/pending")
def pending(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM pending_imports ORDER BY id").fetchall()
    return {"pending": [{"id": r["id"], "amount_cents": r["amount_cents"],
                         "note": r["note"], "created_at": r["created_at"],
                         "type": r["type"]} for r in rows]}


@app.post("/api/pending/{pid}/accept")
def pending_accept(pid: int, conn: sqlite3.Connection = Depends(get_conn)):
    # Mirrors the bot's cb_import_accept — both sides race gracefully (404 = already handled).
    row = db.get_pending_import(conn, pid)
    if row is None:
        raise HTTPException(404, "already handled")
    if row["type"] == "income":
        tid = db.add_transaction(conn, row["amount_cents"], row["note"], "Income",
                                 "suggest-email", created_at=row["created_at"],
                                 type_="income")
    else:
        tid = db.add_transaction(conn, row["amount_cents"], row["note"],
                                 guess_category(row["note"]), "suggest-email",
                                 created_at=row["created_at"])
    db.delete_pending_import(conn, pid)
    return _tx(db.get_transaction(conn, tid))


@app.post("/api/pending/{pid}/ignore")
def pending_ignore(pid: int, conn: sqlite3.Connection = Depends(get_conn)):
    if not db.delete_pending_import(conn, pid):
        raise HTTPException(404, "already handled")
    return {"ignored": pid}


@app.get("/api/year/{year}")
def year_view(year: str, conn: sqlite3.Connection = Depends(get_conn)):
    if not re.fullmatch(r"\d{4}", year):
        raise HTTPException(400, "year must be YYYY")
    return db.year_summary(conn, year)


@app.get("/api/export.csv")
def export_csv(month: str = "all", conn: sqlite3.Connection = Depends(get_conn)):
    if month == "all":
        rows = db.list_transactions(conn)
    else:
        rows = db.list_transactions(conn, date_from=f"{month}-01T00:00:00",
                                    date_to=f"{month}-31")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "type", "category", "note", "amount_sgd", "source"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["type"], r["category"], r["note"],
                    f"{r['amount_cents'] / 100:.2f}", r["source"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=expenses-{month}.csv"})


@app.delete("/api/budgets/{category}")
def remove_budget(category: str, conn: sqlite3.Connection = Depends(get_conn)):
    if not db.delete_budget(conn, category):
        raise HTTPException(404, "no budget for that category")
    return {"deleted": category}


# Static dashboard — mounted last so /api/* and /health resolve first.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
