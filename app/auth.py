"""Authentication primitives: password hashing, sessions, login throttling.

Imports db. db must NEVER import this module (circular import), matching the
rule already in force for goals/portfolio/tiger/fx.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from app import db
from app.config import TRUSTED_PROXIES

# 90 days, absolute. Stamped at login and never extended.
SESSION_SECONDS = 7776000

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}\Z")
MIN_PASSWORD_LEN = 8

# scrypt work factors. 128 * N * r bytes of memory => 16 MB here, which fits
# under OpenSSL's default 32 MB maxmem, so no maxmem override is needed.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, n: int = _N, r: int = _R, p: int = _P) -> str:
    """Return a self-describing hash: scrypt$n$r$p$salt_b64$hash_b64.

    The parameters travel with the hash so they can be raised later without
    invalidating stored passwords.
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        dklen=_DKLEN)
    return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored hash. Never raises on bad input."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                            r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def _validate(username: str, password: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise ValueError(
            "Username must be 3-32 characters: letters, digits, . _ or -")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(
            "Password must be at least %d characters" % MIN_PASSWORD_LEN)


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    _validate(username, password)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, pw_hash, created_at) VALUES (?,?,?)",
            (username, hash_password(password), db.now_iso()),
        )
    except sqlite3.IntegrityError:
        # UNIQUE ... COLLATE NOCASE, so this also catches a case variant.
        raise ValueError("That username is taken")
    conn.commit()
    return int(cur.lastrowid)


def get_user(conn: sqlite3.Connection, username: str):
    return conn.execute(
        "SELECT * FROM users WHERE username=?", (username,)).fetchone()


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int,
                   now: datetime | None = None) -> str:
    """Create a session and return the RAW token (only its hash is stored)."""
    now = now or datetime.now()
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
        "VALUES (?,?,?,?)",
        (_token_hash(token), user_id, now.isoformat(timespec="seconds"),
         (now + timedelta(seconds=SESSION_SECONDS)).isoformat(timespec="seconds")),
    )
    conn.commit()
    return token


def lookup_session(conn: sqlite3.Connection, token: str,
                   now: datetime | None = None) -> int | None:
    """Return the user id for a live session, else None.

    Expiry is enforced here, server-side, so editing the cookie's Max-Age
    buys nothing.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token_hash=?",
        (_token_hash(token),)).fetchone()
    if not row:
        return None
    now = now or datetime.now()
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    return int(row["user_id"])


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
    conn.commit()


def purge_expired(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    now = now or datetime.now()
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                 (now.isoformat(timespec="seconds"),))
    conn.commit()


# --- Login throttle ---------------------------------------------------------
# Sliding failure windows keyed on BOTH the client IP and the username, so
# neither "one IP sprays many usernames" nor "many IPs target one account"
# slips through. In-memory: state is lost on restart, which is acceptable here,
# and correct only because expense-web runs a single uvicorn worker.

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_failed_logins: "defaultdict[str, deque]" = defaultdict(deque)

# Verified against when the username does not exist, so an unknown user costs
# the same wall-clock time as a wrong password. Generated with the current
# parameters so the timing actually matches.
_DUMMY_HASH = hash_password("timing-equalizer")


def ip_key(addr: str) -> str:
    return "ip:%s" % addr


def signup_key(addr: str) -> str:
    """Separate namespace from ip_key().

    Both windows are keyed on the client address, and TRUSTED_PROXIES is empty
    by default, so behind NPM every visitor shares one key. Sharing the
    namespace with login meant a guest fumbling a SIGNUP_CODE five times locked
    the OWNER out of signing in for fifteen minutes. Failed signups tell an
    attacker nothing about passwords, so the two budgets have no reason to be
    the same one.
    """
    return "signup:%s" % addr


def user_key(username: str) -> str:
    return "user:%s" % (username or "").strip().lower()


def login_blocked(*keys: str) -> bool:
    """True if ANY key is over its failure budget. Ages entries out lazily."""
    now = time.monotonic()
    for key in keys:
        attempts = _failed_logins.get(key)
        if not attempts:
            continue
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_FAILURES:
            return True
        # Delete keys with no remaining failures to prevent unbounded growth
        if not attempts:
            del _failed_logins[key]
    return False


def record_login_failure(*keys: str) -> None:
    now = time.monotonic()
    for key in keys:
        _failed_logins[key].append(now)


def reset_throttle() -> None:
    """Test hook — module-level state would otherwise leak between tests."""
    _failed_logins.clear()


def verify_dummy() -> None:
    """Burn a verify so an unknown username is not measurably faster."""
    verify_password("timing-equalizer", _DUMMY_HASH)


def client_ip(request) -> str:
    """The browser's address, not the proxy's.

    X-Forwarded-For is honoured ONLY when the socket peer is in
    TRUSTED_PROXIES; otherwise anyone reaching port 8085 directly could set the
    header and sidestep the IP window entirely.
    """
    peer = getattr(getattr(request, "client", None), "host", "") or "unknown"
    if peer in TRUSTED_PROXIES:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer
