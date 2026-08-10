from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import date
from decimal import InvalidOperation
from pathlib import Path
import sqlite3
import csv
import io
import posixpath
import re

from fastapi import FastAPI, HTTPException, Depends, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, RedirectResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from app import cards as cards_mod
from app import db, goals as goals_mod, payslip, payslip_archive, portfolio
from app import tiger, trust_email
from app import auth
from app.config import (DB_PATH, CATEGORIES, DEFAULT_CATEGORY, SIGNUP_CODE,
                        PAYSLIP_PDF_PASSWORD, PAYSLIP_SENDER, PAYSLIP_DIR,
                        GMAIL_USER, GMAIL_APP_PASSWORD)
from app.money import dollars_to_cents
from app.categories import effective_category, learn_from_correction

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
    card_id: int | None = None


CARD_KINDS = ("credit", "debit", "wallet", "cash")


class CardIn(BaseModel):
    name: str
    kind: str
    last4: str | None = None
    match: str | None = None
    bind_source: str | None = None


class CardPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    last4: str | None = None
    match: str | None = None
    # Rebinding a sender is the point of owning a second card from the same
    # issuer ("Trust mail is the debit card today, the credit card next
    # month"). Without it here the only way to move one is hand-edited SQLite.
    # Note `match` still cannot be null-cleared -- None means "not supplied" --
    # but sending "" works, because normalise_instrument("") is falsy and the
    # rule goes inert. A per-field sentinel would be the alternative.
    bind_source: str | None = None


class LinkIn(BaseModel):
    target_tid: int


class BudgetIn(BaseModel):
    amount: float


class GoalIn(BaseModel):
    name: str
    target: float
    target_date: str | None = None
    monthly: float | None = None
    category: str | None = None
    priority: int = 2
    note: str = ""


class GoalPatch(BaseModel):
    # None = leave unchanged. Empty string = clear to NULL.
    name: str | None = None
    target: float | None = None
    target_date: str | None = None
    monthly: float | str | None = None
    category: str | None = None
    priority: int | None = None
    note: str | None = None


class BoughtIn(BaseModel):
    price: float | None = None


class ContribIn(BaseModel):
    amount: float


class SignupIn(BaseModel):
    username: str
    password: str
    code: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


def _tx(row):
    keys = row.keys()
    return {"id": row["id"], "amount_cents": row["amount_cents"], "note": row["note"],
            "category": row["category"], "created_at": row["created_at"],
            "source": row["source"], "type": row["type"],
            "link_tid": row["link_tid"],
            "card_id": row["card_id"] if "card_id" in keys else None,
            "reimbursed_cents": row["reimbursed_cents"] if "reimbursed_cents" in keys else 0}


@app.get("/health")
def health():
    return {"status": "ok"}


COOKIE = "session"

# Paths reachable without a session. /sw.js is here on purpose: the browser
# re-fetches it to check for updates, and a 302-to-login there would fail the
# service worker MIME check, strand the old worker, and stop the cache-version
# bump from ever landing.
_PUBLIC_EXACT = {
    "/health", "/login", "/signup", "/styles.css", "/auth.js", "/sw.js",
    "/manifest.json", "/api/auth/status", "/api/auth/login", "/api/auth/signup",
}
# images/ is an empty directory today -- deliberately not allowlisted, so it
# grants access to nothing and doesn't widen the anonymous surface for free.
#
# /vendor/ IS needed: styles.css is public and its @font-face rules point at
# /vendor/nunito-*.woff2, which the login page's <h1> actually renders in. Gating
# them meant the browser followed the 302, got login HTML where a font should be,
# logged "OTS parsing error: invalid sfntVersion" and fell back to Segoe UI --
# and, because sw.js caches anything with res.ok and a FOLLOWED 302 is ok, wrote
# that HTML into the cache under the font's URL. Only reproducible in prod:
# static/vendor/ is gitignored and created by the Dockerfile at build time, so a
# dev checkout 404s instead. The directory holds third-party fonts and Chart.js
# only -- no private data -- and it inherits the non-canonical-path rejection
# below like every other prefix.
_PUBLIC_PREFIX = ("/icons/", "/vendor/")


def _signup_open(conn) -> bool:
    return bool(SIGNUP_CODE) or auth.user_count(conn) == 0


def _is_public(path: str) -> bool:
    # This must judge the EXACT string StaticFiles will resolve, because the
    # mount at "/" re-derives the file from scope["path"] via
    # os.path.normpath(). Anything non-canonical is therefore a traversal
    # attempt riding an allowlist entry -- both the prefix match
    # ("/icons/../app.js") and the exact entries ("/styles.css/../app.js").
    # No legitimate path in this app is non-canonical, so reject rather than
    # normalize-then-check: normalizing would re-introduce the same class of
    # gap the moment the two normalizers disagreed.
    #
    # The backslash clause matters only on Windows (dev), where ntpath treats
    # "\" as a separator and would collapse "/icons/x\..\..\app.js" to
    # "app.js"; on Linux (prod, python:3.12-slim) posixpath leaves it a
    # literal filename char. Rejecting it keeps both platforms in agreement,
    # so a test cannot pass on Linux CI for the wrong reason.
    if "\\" in path or path != posixpath.normpath(path):
        return False
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX)


@app.middleware("http")
async def require_session(request: Request, call_next):
    # scope["path"] deliberately, NOT request.url.path. Starlette builds
    # request.url by urlsplit()ing a reconstructed URL, so a percent-decoded
    # "?" or "#" inside the path TRUNCATES it -- uvicorn splits the query off
    # the raw target before unquoting, so "/icons/%3F/../../app.js" arrives as
    # scope["path"] == "/icons/?/../../app.js" while request.url.path is just
    # "/icons/". Gating on the truncated string let the mount resolve and
    # serve app.js and index.html anonymously. The two layers must read the
    # same string. (get_route_path(scope) == scope["path"] here because this
    # app sets no root_path.)
    path = request.scope["path"]
    if _is_public(path):
        return await call_next(request)
    conn = db.connect(DB_PATH)
    try:
        uid = auth.lookup_session(conn, request.cookies.get(COOKIE, ""))
    finally:
        conn.close()
    if uid is None:
        # An API caller needs JSON it can branch on. A 302 answering a fetch()
        # would be followed transparently and the login page parsed as JSON.
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    # Phase 1 ignores this; Phase 2 (per-account data) consumes it.
    request.state.user_id = uid
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request, conn=Depends(get_conn)):
    uid = auth.lookup_session(conn, request.cookies.get(COOKIE, ""))
    # needs_code drives whether the signup form shows its code field. It is
    # simply "is a code configured" — when one is, it is required for every
    # signup, first run included.
    return {"authed": uid is not None, "signup_open": _signup_open(conn),
            "needs_code": bool(SIGNUP_CODE)}


def _issue(conn, user_id: int) -> JSONResponse:
    token = auth.create_session(conn, user_id)
    resp = JSONResponse({"ok": True})
    # No Secure: this is served over plain HTTP on a LAN, and a Secure cookie would never be
    # sent. Lax, not Strict, so following a link in from Telegram keeps the
    # session.
    resp.set_cookie(COOKIE, token, max_age=auth.SESSION_SECONDS,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/auth/signup")
def auth_signup(body: SignupIn, request: Request, conn=Depends(get_conn)):
    # signup_key, NOT ip_key: a wrong signup code must not spend the login
    # budget. See auth.signup_key for why they were ever the same.
    ip = auth.signup_key(auth.client_ip(request))
    if auth.login_blocked(ip):
        raise HTTPException(429, "Too many attempts — locked, try again later")
    if not _signup_open(conn):
        raise HTTPException(403, "Signup is closed")
    if SIGNUP_CODE and body.code != SIGNUP_CODE:
        auth.record_login_failure(ip)
        # Distinct from "Signup is closed" on purpose. There is nothing left to
        # withhold here: /api/auth/status already reported needs_code, so the
        # client has rendered the code field and knows a code exists. Reusing
        # the closed message told a user staring at a code box that signup was
        # closed, which reads as a bug.
        raise HTTPException(403, "That signup code is not valid")
    try:
        uid = auth.create_user(conn, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _issue(conn, uid)


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, conn=Depends(get_conn)):
    ip = auth.ip_key(auth.client_ip(request))
    user = auth.user_key(body.username)
    # Checked BEFORE any credential work: a blocked request must not reach the
    # verifier.
    if auth.login_blocked(ip, user):
        raise HTTPException(429, "Too many attempts — locked, try again later")
    row = auth.get_user(conn, body.username)
    if row is None:
        auth.verify_dummy()
        auth.record_login_failure(ip, user)
        raise HTTPException(401, "Invalid username or password")
    if not auth.verify_password(body.password, row["pw_hash"]):
        auth.record_login_failure(ip, user)
        raise HTTPException(401, "Invalid username or password")
    auth.purge_expired(conn)
    return _issue(conn, int(row["id"]))


@app.post("/api/auth/logout")
def auth_logout(request: Request, conn=Depends(get_conn)):
    auth.delete_session(conn, request.cookies.get(COOKIE, ""))
    resp = JSONResponse({"ok": True}, status_code=200)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/login")
def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/signup")
def signup_page(conn=Depends(get_conn)):
    if not _signup_open(conn):
        raise HTTPException(404, "Not found")
    return FileResponse(str(STATIC_DIR / "signup.html"))


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


@app.get("/api/transactions/{tid}/reimburse-candidates")
def reimburse_candidates(tid: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Expenses this incoming transfer could be paying back.

    Reuses the bot's picker query verbatim (>= the transfer amount, last 7
    days, 8 rows) so the two surfaces cannot start disagreeing about what is a
    plausible match.
    """
    row = db.get_transaction(conn, tid)
    if row is None:
        raise HTTPException(404, "not found")
    if row["type"] != "income":
        raise HTTPException(400, "only incoming money can be linked to a spend")
    cands = db.recent_expense_candidates(conn, row["amount_cents"])
    return {"candidates": [_tx(r) for r in cands]}


@app.post("/api/transactions/{tid}/link")
def link_transaction(tid: int, body: LinkIn,
                     conn: sqlite3.Connection = Depends(get_conn)):
    """Convert an incoming transfer into a payback of body.target_tid."""
    if not db.link_as_reimbursement(conn, tid, body.target_tid):
        # db returns one boolean for every rejection (missing row, wrong type,
        # wrong sign, self-link) precisely so this maps to one status.
        raise HTTPException(400, "cannot link these two transactions")
    return _tx(db.get_transaction(conn, tid))


@app.post("/api/transactions/{tid}/unlink")
def unlink_transaction(tid: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Undo a link: the payback becomes a plain income row again."""
    if not db.unlink_reimbursement(conn, tid):
        raise HTTPException(400, "not a linked transaction")
    return _tx(db.get_transaction(conn, tid))


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
    # 0 means "clear to Unassigned" -- JSON has no distinct sentinel, and null
    # already means "field not supplied" for every other field on this model.
    card_id = patch.card_id
    if card_id == 0:
        card_id = None
    elif card_id is not None:
        card = db.get_card(conn, card_id)
        if card is None:
            raise HTTPException(400, "unknown card")
        # An archived card would accept the assignment and bind an instrument
        # that recall_instrument can never return (it joins on archived_at IS
        # NULL) -- silently inert learning. Reject instead of teaching nothing.
        if card["archived_at"]:
            raise HTTPException(400, "card is archived")
    # A card-only edit sends no other field, so update_transaction would report
    # "nothing to update" -- apply the card separately and treat either one
    # succeeding as success.
    changed = db.update_transaction(conn, tid, amount_cents=amount_cents,
                                    note=patch.note, category=patch.category)
    if patch.card_id is not None:
        # assign_card owns the pre-update comparison and the learning call, so
        # the bot picker and this route cannot drift apart. web.py must never
        # import bot.py, which is exactly why that logic lives in cards.py.
        changed = cards_mod.assign_card(conn, tid, card_id) or changed
    if not changed:
        raise HTTPException(404, "not found or nothing to update")
    updated = db.get_transaction(conn, tid)
    # Only an actual category change should re-teach. The web edit sheet always
    # sends `category` (even on an amount/note-only edit), so comparing against
    # the pre-update row is required — `patch.category is not None` alone is
    # true on every save and would silently overwrite a learned correction.
    if patch.category is not None and patch.category != row["category"]:
        learn_from_correction(conn, updated, patch.category)
    return _tx(updated)


@app.delete("/api/transactions/{tid}")
def remove_transaction(tid: int, conn: sqlite3.Connection = Depends(get_conn)):
    if not db.delete_transaction(conn, tid):
        raise HTTPException(404, "not found")
    return {"deleted": tid}


@app.get("/api/summary")
def summary(month: str | None = None,
            card_id: int | None = Query(None, ge=0),
            conn: sqlite3.Connection = Depends(get_conn)):
    # card_id absent -> None -> no filter; card_id=0 -> the Unassigned bucket.
    # ge=0 keeps a negative id from reaching db._card_clause's ValueError as a
    # 500; FastAPI answers 422 at the boundary instead.
    return db.month_summary(conn, month or db.current_year_month(), card_id)


@app.get("/api/trend")
def trend(granularity: str = "month",
          card_id: int | None = Query(None, ge=0),
          conn: sqlite3.Connection = Depends(get_conn)):
    if granularity not in ("month", "day"):
        raise HTTPException(400, "granularity must be month or day")
    return {"granularity": granularity,
            "points": db.trend(conn, granularity, card_id)}


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
    g = next((x for x in goals_mod.list_with_pace(conn) if x["id"] == gid), None)
    if g is None:
        raise HTTPException(404, "goal not found")
    return g


def _validated(category, priority, target_date, target=None, monthly=None):
    """Shared field validation. 400 on bad input, never a silent coercion."""
    if category is not None and category != "" and category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    if priority is not None and priority not in (1, 2, 3):
        raise HTTPException(400, "priority must be 1, 2 or 3")
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(400, "target_date must be YYYY-MM-DD")
    if target is not None and target < 0:
        raise HTTPException(400, "target must not be negative")
    # Only catches the float form here — GoalPatch's monthly is float | str,
    # and a negative string ("-5") is validated after dollars_to_cents in
    # patch_goal, where the sign is unambiguous.
    if monthly is not None and isinstance(monthly, (int, float)) and monthly < 0:
        raise HTTPException(400, "monthly must not be negative")


@app.get("/api/goals")
def list_goals_route(conn: sqlite3.Connection = Depends(get_conn)):
    return {"goals": goals_mod.list_with_pace(conn)}


@app.post("/api/goals")
def create_goal(body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    _validated(body.category, body.priority, body.target_date, body.target, body.monthly)
    gid = db.add_goal(conn, body.name.strip(), dollars_to_cents(body.target))
    monthly = dollars_to_cents(body.monthly) if body.monthly else None
    # Store the canonical form — date.fromisoformat also accepts "20270301",
    # week dates, and datetime strings; writing the raw text would mis-sort
    # ORDER BY target_date and wouldn't populate the web <input type="date">.
    target_date = (date.fromisoformat(body.target_date).isoformat()
                   if body.target_date else None)
    db.update_goal(conn, gid,
                   target_date=target_date,
                   monthly_cents=monthly,
                   next_due=goals_mod.first_of_next_month(date.today()) if monthly else None,
                   category=body.category or None,
                   priority=body.priority,
                   note=body.note)
    return _goal(conn, gid)


@app.patch("/api/goals/{gid}")
def patch_goal(gid: int, body: GoalPatch, conn: sqlite3.Connection = Depends(get_conn)):
    if db.get_goal(conn, gid) is None:
        raise HTTPException(404, "goal not found")
    _validated(body.category, body.priority, body.target_date, body.target, body.monthly)
    fields = {}
    if body.name is not None and body.name.strip():
        fields["name"] = body.name.strip()
    if body.target is not None:
        fields["target_cents"] = dollars_to_cents(body.target)
    if body.target_date is not None:
        # Store the canonical form — same reasoning as create_goal: raw text
        # accepted by date.fromisoformat mis-sorts ORDER BY target_date and
        # wouldn't populate the web <input type="date">.
        fields["target_date"] = (date.fromisoformat(body.target_date).isoformat()
                                 if body.target_date else None)
    if body.category is not None:
        fields["category"] = body.category or None
    if body.priority is not None:
        fields["priority"] = body.priority
    if body.note is not None:
        fields["note"] = body.note
    if body.monthly is not None:
        if body.monthly in ("", 0):
            monthly = None
        else:
            # `monthly` is typed `float | str` so "" can reach here as a literal
            # clear-signal; under Pydantic v2 smart-union matching, ANY string
            # (not just "") matches the str branch verbatim, so a non-numeric
            # string like "abc" reaches dollars_to_cents() uncaught here unless
            # we validate first — the 400 has to come from us, not a coercion.
            try:
                monthly = dollars_to_cents(body.monthly)
            except InvalidOperation:
                raise HTTPException(400, "monthly must be a number")
            if monthly < 0:
                # Catches the string form (e.g. "-5") that _validated's isinstance
                # check on the raw float|str union can't see.
                raise HTTPException(400, "monthly must not be negative")
        fields["monthly_cents"] = monthly
        # Cursor follows the monthly amount: starts when set, clears when unset.
        fields["next_due"] = goals_mod.first_of_next_month(date.today()) if monthly else None
    if fields:
        db.update_goal(conn, gid, **fields)
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


@app.post("/api/goals/{gid}/bought")
def buy_goal(gid: int, body: BoughtIn, conn: sqlite3.Connection = Depends(get_conn)):
    g = db.get_goal(conn, gid)
    if g is None:
        raise HTTPException(404, "goal not found")
    if g["bought_at"]:
        raise HTTPException(400, "already bought")
    cents = dollars_to_cents(body.price) if body.price is not None else g["target_cents"]
    # The goal's own category wins outright — an explicit choice beats trip mode,
    # so this deliberately does not call effective_category().
    tid = db.add_transaction(conn, cents, g["name"],
                             g["category"] or DEFAULT_CATEGORY, "goal")
    db.mark_goal_bought(conn, gid, tid)
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
        category, _origin = effective_category(conn, row["note"])
        tid = db.add_transaction(conn, row["amount_cents"], row["note"],
                                 category, "suggest-email",
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


# ---- portfolio ------------------------------------------------------------
# GET returns 200 with configured:false rather than an error status — the
# frontend uses it to decide whether to render the tab at all, and a 4xx would
# be indistinguishable from a real outage.

@app.get("/api/portfolio")
def get_portfolio(conn: sqlite3.Connection = Depends(get_conn)):
    if not tiger.configured():
        return {"configured": False}
    return {"configured": True, **portfolio.current(conn)}


@app.get("/api/portfolio/history")
def get_portfolio_history(days: int = 90,
                          conn: sqlite3.Connection = Depends(get_conn)):
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days must be between 1 and 3650")
    if not tiger.configured():
        return {"configured": False, "points": []}
    return portfolio.history(conn, days)


# Plain `def`, not `async def`: tigeropen is a blocking SDK, and FastAPI
# dispatches sync routes to a threadpool. Marking this async would freeze the
# whole dashboard for every user until the broker responded.
@app.post("/api/portfolio/refresh")
def refresh_portfolio(conn: sqlite3.Connection = Depends(get_conn)):
    if not tiger.configured():
        raise HTTPException(503, "Tiger API is not configured")
    try:
        return {"configured": True, **portfolio.refresh(conn)}
    except tiger.NotConfigured as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(503, f"{type(exc).__name__}: {exc}")


# ---- payslips -------------------------------------------------------------
# Same configured:false convention as portfolio above. No payslip ever becomes
# a transaction (salary income comes from the bank credit alert; a row here
# would double-count the month).
#
# The two import routes below are plain `def`, not `async def`, on purpose:
# rendering + OCR is seconds of blocking CPU, and FastAPI runs sync routes in a
# threadpool. As `async def` they would stall every other request for the
# duration — the same reasoning as /api/portfolio/refresh.

@app.get("/api/payslips")
def get_payslips(conn: sqlite3.Connection = Depends(get_conn)):
    if not PAYSLIP_PDF_PASSWORD:
        return {"configured": False, "payslips": []}
    rows = db.list_payslips(conn)
    return {"configured": True, "payslips": [
        {"period": r["period"], "net_cents": r["net_cents"],
         "gross_cents": r["gross_cents"], "received_at": r["received_at"]}
        for r in rows]}


@app.get("/api/payslips/{period}")
def get_payslip(period: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = db.get_payslip(conn, period)
    if row is None:
        raise HTTPException(404, "no payslip for that period")
    items = [{"section": i["section"], "label": i["label"],
              "amount_cents": i["amount_cents"]}
             for i in db.payslip_items(conn, row["id"])]
    return {
        "period": row["period"],
        "net_cents": row["net_cents"], "gross_cents": row["gross_cents"],
        "received_at": row["received_at"],
        "parsed": bool(row["raw_text"]),
        "raw_text": row["raw_text"],
        "text_source": row["text_source"],
        # Derived on read, never stored — same reasoning as goal pace. The
        # payslip states its own totals, so this identity holding is the main
        # evidence every digit was read correctly, and it must reflect whatever
        # the items currently say.
        "reconciles": payslip.reconciles(row["net_cents"], row["gross_cents"],
                                         items),
        "items": items,
    }


@app.get("/api/payslips/{period}/pdf")
def get_payslip_pdf(period: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The archived PDF itself.

    Deliberately served from /api/* rather than the static mount: the auth
    middleware answers /api/* with 401 JSON, so an expired session fails
    cleanly instead of handing back a login page with a .pdf content type.
    """
    row = db.get_payslip(conn, period)
    if row is None:
        raise HTTPException(404, "no payslip for that period")
    path = Path(row["pdf_path"])
    if not path.is_file():
        raise HTTPException(404, "archived file is missing")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"payslip-{row['period']}.pdf")


def _require_payslips_configured():
    if not PAYSLIP_PDF_PASSWORD:
        raise HTTPException(400, "Payslip archiving is off — set "
                                 "PAYSLIP_PDF_PASSWORD and recreate the stack.")


@app.post("/api/payslips/import")
def import_payslips_from_email(limit: int = 3,
                               conn: sqlite3.Connection = Depends(get_conn)):
    """Search the mailbox for the payroll sender and archive what it finds.

    The web equivalent of /payslip. Searches by sender rather than reading the
    bot's UID cursor: the cursor is go-forward, so a payslip older than it can
    never be reached, and rewinding it would re-import every transaction email
    in between. This never writes the cursor.
    """
    _require_payslips_configured()
    if not (GMAIL_APP_PASSWORD and PAYSLIP_SENDER):
        raise HTTPException(400, "Email access isn't configured — set "
                                 "GMAIL_USER and GMAIL_APP_PASSWORD.")
    limit = max(1, min(20, limit))
    try:
        _, emails = trust_email.fetch_from_sender(
            trust_email.IMAP_HOST, GMAIL_USER, GMAIL_APP_PASSWORD,
            PAYSLIP_SENDER, limit)
    except Exception as exc:
        raise HTTPException(503, f"{type(exc).__name__}: {exc}")
    results = []
    for uid, _sender, _subject, _body, msg_dt, attachments in emails:
        pdf = next((d for fn, d in attachments if fn.lower().endswith(".pdf")),
                   None)
        if pdf is None:
            continue  # payroll correspondence with no payslip attached
        results += payslip_archive.archive_pdf(
            conn, pdf, PAYSLIP_PDF_PASSWORD, PAYSLIP_DIR,
            msg_dt.strftime("%Y-%m"), uid,
            msg_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    return {"scanned": len(emails), "imported": _import_summary(results)}


MAX_PAYSLIP_UPLOAD = 20 * 1024 * 1024


@app.post("/api/payslips/upload")
async def upload_payslip(request: Request,
                         conn: sqlite3.Connection = Depends(get_conn)):
    """Archive a payslip PDF handed over directly.

    For months that never arrived by email, or one already saved to disk. The
    PDF is the raw request body rather than a multipart form: it is a single
    file with no other fields, and multipart would mean adding python-multipart
    for nothing. `fetch(url, {method: "POST", body: file})` sends exactly this.

    The password still comes from the environment — never accepted over the
    wire, so a decrypted upload works and an encrypted one needs the same
    PAYSLIP_PDF_PASSWORD the email path uses.
    """
    _require_payslips_configured()
    # Streamed with a cap rather than request.body(): an unbounded read would
    # hold whatever was sent entirely in memory.
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_PAYSLIP_UPLOAD:
            raise HTTPException(413, "PDF too large (20MB limit).")
    if not bytes(data).startswith(b"%PDF"):
        # The parser would fail anyway, but a clear message here beats a 500
        # from deep inside pypdf.
        raise HTTPException(400, "That doesn't look like a PDF.")
    # Async only to read the body; the render + OCR still goes to a thread or
    # it would stall every other request for seconds.
    results = await run_in_threadpool(
        payslip_archive.archive_pdf, conn, bytes(data), PAYSLIP_PDF_PASSWORD,
        PAYSLIP_DIR, date.today().strftime("%Y-%m"))
    return {"scanned": 1, "imported": _import_summary(results)}


def _import_summary(results: list[dict]) -> list[dict]:
    """Per-month outcome for the dashboard. Line items are left out — the Pay
    tab re-fetches the month it lands on, so shipping them twice is waste."""
    return [{"period": r["period"], "parsed": r["parsed"],
             "items": len(r["items"]), "net_cents": r["net_cents"],
             "text_source": r["text_source"], "reconciles": r["reconciles"],
             "warnings": r["warnings"]}
            for r in results]


# ---- cards ------------------------------------------------------------------

def _card(row, tx_count=0):
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "last4": row["last4"], "match": row["match"],
            "archived": row["archived_at"] is not None, "tx_count": tx_count}


# Derived from the parser map rather than a retyped list, so a future parser
# added to trust_email.SOURCES is bindable with no change here or in the JS.
def _known_sources() -> set[str]:
    return {src for _label, src in trust_email.SOURCES.values()}


@app.get("/api/cards")
def get_cards(conn: sqlite3.Connection = Depends(get_conn)):
    rows = db.list_cards(conn)
    active = {r["id"] for r in rows}
    # Which card each sender currently defaults to. Read from list_instruments
    # rather than db.recall_instrument because that one BUMPS the hit count,
    # and the dashboard re-fetches this route on every refresh -- the count is
    # the signal that an over-broad default is running away, so polling it
    # would destroy the signal. Filtering on the active ids matches
    # recall_instrument's archived join: a binding to an archived card never
    # resolves, so it must not be reported as in force either.
    bound = {r["source"]: r["card_id"] for r in db.list_instruments(conn)
             if r["token"] == "" and r["card_id"] in active}
    sources = sorted(({"value": src, "label": label, "card_id": bound.get(src)}
                      for label, src in trust_email.SOURCES.values()),
                     key=lambda s: s["label"])
    return {"cards": [_card(r, db.card_tx_count(conn, r["id"])) for r in rows],
            "sources": sources}


@app.post("/api/cards")
def create_card(body: CardIn, conn: sqlite3.Connection = Depends(get_conn)):
    if body.kind not in CARD_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(CARD_KINDS)}")
    if body.bind_source is not None and body.bind_source not in _known_sources():
        raise HTTPException(400, "unknown sender")
    cid = db.add_card(conn, body.name.strip(), body.kind, body.last4, body.match)
    if body.bind_source:
        db.bind_instrument(conn, body.bind_source, "", cid)
    return _card(db.get_card(conn, cid))


@app.get("/api/cards/breakdown")
def cards_breakdown(month: str | None = None,
                    conn: sqlite3.Connection = Depends(get_conn)):
    ym = month or db.current_year_month()
    rows = db.month_card_spend(conn, ym)
    return {"month": ym,
            "buckets": [{"card_id": r["card_id"], "name": r["name"],
                         "kind": r["kind"], "total_cents": r["total_cents"]}
                        for r in rows]}


@app.patch("/api/cards/{cid}")
def edit_card(cid: int, patch: CardPatch, conn: sqlite3.Connection = Depends(get_conn)):
    if db.get_card(conn, cid) is None:
        raise HTTPException(404, "not found")
    if patch.kind is not None and patch.kind not in CARD_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(CARD_KINDS)}")
    # Before the update, not after: a 400 that has already renamed the card
    # reports failure for a change that happened.
    if patch.bind_source is not None and patch.bind_source not in _known_sources():
        raise HTTPException(400, "unknown sender")
    # bind_source is not a column. `is not None` (never truthiness) is what
    # lets match="" through to clear a rule.
    fields = {k: v for k, v in patch.model_dump().items()
              if v is not None and k != "bind_source"}
    if fields:
        db.update_card(conn, cid, **fields)
    # The same source-level default create_card writes. bind_instrument
    # upserts, so this REPLACES the previous card's claim on that sender --
    # which is exactly what "this issuer's mail is my other card now" means.
    if patch.bind_source:
        db.bind_instrument(conn, patch.bind_source, "", cid)
    return _card(db.get_card(conn, cid))


@app.delete("/api/cards/{cid}")
def remove_card(cid: int, conn: sqlite3.Connection = Depends(get_conn)):
    if db.get_card(conn, cid) is None:
        raise HTTPException(404, "not found")
    # Archive rather than delete while history still points here: losing the
    # name would strand those rows pointing at an id that resolves to nothing.
    if db.delete_card(conn, cid):
        return {"deleted": cid, "archived": False}
    db.archive_card(conn, cid)
    return {"deleted": cid, "archived": True}


# Static dashboard — mounted last so /api/* and /health resolve first.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
