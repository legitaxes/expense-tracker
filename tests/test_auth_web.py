# tests/test_auth_web.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, db, web

GOOD = {"username": "jia", "password": "hunter2hunter2"}


def _raw_get(path: str, encoded_raw_path: bytes | None = None) -> int:
    """Call the ASGI app directly with an unnormalized path, bypassing
    httpx's client-side dot-segment normalization (RFC 3986 collapses
    "/icons/../app.js" to "/app.js" before the request is ever sent, which
    would make a TestClient.get("/icons/../app.js") probe pass for the wrong
    reason -- it never reaches our code with the traversal segment intact).

    This mirrors what uvicorn's h11 layer hands the ASGI app for a real
    request: `scope["path"]` is already percent-decoded (that's why a
    percent-encoded traversal like "/icons/%2e%2e/app.js" arrives as the
    identical decoded path our middleware sees -- there is nothing separate
    to simulate for the encoded form beyond documenting that raw_path, which
    our code never reads, differs).
    """
    status = {}
    body = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": encoded_raw_path or path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    asyncio.run(web.app(scope, receive, send))
    return status["code"]


@pytest.fixture
def anon(tmp_path, monkeypatch):
    """A client with no session and an empty user table."""
    dbfile = str(tmp_path / "t.db")
    monkeypatch.setattr(web, "DB_PATH", dbfile)
    monkeypatch.setattr(web, "SIGNUP_CODE", "")
    auth.reset_throttle()
    conn = db.connect(dbfile)
    db.init_schema(conn)

    def override():
        yield conn

    web.app.dependency_overrides[web.get_conn] = override
    with TestClient(web.app) as c:
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


def test_status_reports_open_signup_when_no_users(anon):
    body = anon.get("/api/auth/status").json()
    assert body == {"authed": False, "signup_open": True, "needs_code": False}


def test_status_flags_needs_code_when_one_is_configured(anon, monkeypatch):
    monkeypatch.setattr(web, "SIGNUP_CODE", "letmein")
    assert anon.get("/api/auth/status").json()["needs_code"] is True


def test_signup_creates_the_first_account_and_logs_in(anon):
    assert anon.post("/api/auth/signup", json=GOOD).status_code == 200
    assert anon.get("/api/auth/status").json()["authed"] is True
    assert anon.get("/api/transactions").status_code == 200


def test_signup_closes_after_the_first_account(anon):
    anon.post("/api/auth/signup", json=GOOD)
    second = anon.post("/api/auth/signup",
                       json={"username": "other", "password": "hunter2hunter2"})
    assert second.status_code == 403
    assert anon.get("/api/auth/status").json()["signup_open"] is False


def test_signup_code_reopens_and_is_required(anon, monkeypatch):
    anon.post("/api/auth/signup", json=GOOD)
    monkeypatch.setattr(web, "SIGNUP_CODE", "letmein")
    assert anon.get("/api/auth/status").json()["signup_open"] is True
    bad = anon.post("/api/auth/signup",
                    json={"username": "other", "password": "hunter2hunter2",
                          "code": "wrong"})
    assert bad.status_code == 403
    ok = anon.post("/api/auth/signup",
                   json={"username": "other", "password": "hunter2hunter2",
                         "code": "letmein"})
    assert ok.status_code == 200


def test_signup_code_is_required_on_first_run_too(anon, monkeypatch):
    monkeypatch.setattr(web, "SIGNUP_CODE", "letmein")
    assert anon.post("/api/auth/signup", json=GOOD).status_code == 403


def test_wrong_signup_code_is_distinct_from_signup_being_closed(anon, monkeypatch):
    # Nothing is withheld by conflating them: /api/auth/status already returned
    # needs_code, so the client has rendered a code field. Telling a user who is
    # looking at that field "Signup is closed" reads as a bug.
    monkeypatch.setattr(web, "SIGNUP_CODE", "letmein")
    wrong = anon.post("/api/auth/signup", json=dict(GOOD, code="nope"))
    assert wrong.status_code == 403
    assert "closed" not in wrong.json()["detail"].lower()
    assert "code" in wrong.json()["detail"].lower()


def test_signup_rejects_weak_input(anon):
    r = anon.post("/api/auth/signup", json={"username": "ab", "password": "x"})
    assert r.status_code == 400


def test_login_logout_round_trip(anon):
    anon.post("/api/auth/signup", json=GOOD)
    anon.post("/api/auth/logout")
    assert anon.get("/api/transactions").status_code == 401
    assert anon.post("/api/auth/login", json=GOOD).status_code == 200
    assert anon.get("/api/transactions").status_code == 200


def test_logout_revokes_server_side(anon):
    anon.post("/api/auth/signup", json=GOOD)
    token = anon.cookies.get("session")
    anon.post("/api/auth/logout")
    # Re-presenting the same cookie must fail: the row is gone, not just the
    # client copy.
    anon.cookies.set("session", token)
    assert anon.get("/api/transactions").status_code == 401


def test_wrong_password_and_unknown_user_are_indistinguishable(anon):
    anon.post("/api/auth/signup", json=GOOD)
    anon.post("/api/auth/logout")
    wrong = anon.post("/api/auth/login",
                      json={"username": "jia", "password": "nottherightone"})
    unknown = anon.post("/api/auth/login",
                        json={"username": "ghost", "password": "nottherightone"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_login_locks_out_after_repeated_failures(anon):
    anon.post("/api/auth/signup", json=GOOD)
    anon.post("/api/auth/logout")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        anon.post("/api/auth/login", json={"username": "jia", "password": "bad"})
    blocked = anon.post("/api/auth/login", json=GOOD)
    assert blocked.status_code == 429
    # Distinct from "invalid" -- a lockout dressed as bad credentials trains
    # the user to keep retrying and extend their own lockout.
    assert "locked" in blocked.json()["detail"].lower()


def test_gate_401s_api_and_302s_pages(anon):
    assert anon.get("/api/transactions").status_code == 401
    page = anon.get("/", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/login"


def test_gate_allowlist_is_reachable_anonymously(anon):
    # /login and /auth.js are also allowlisted but their files do not exist
    # until Task 4, which adds its own assertion for them.
    for path in ["/health", "/styles.css", "/sw.js", "/manifest.json"]:
        assert anon.get(path, follow_redirects=False).status_code == 200, path


def test_login_page_and_its_script_serve_anonymously(anon):
    for path in ["/login", "/auth.js"]:
        assert anon.get(path, follow_redirects=False).status_code == 200, path


def test_app_shell_is_not_reachable_anonymously(anon):
    for path in ["/app.js", "/index.html"]:
        assert anon.get(path, follow_redirects=False).status_code == 302, path


def test_signup_page_404s_when_closed(anon):
    anon.post("/api/auth/signup", json=GOOD)
    anon.post("/api/auth/logout")
    assert anon.get("/signup", follow_redirects=False).status_code == 404


def test_icon_prefix_traversal_does_not_bypass_the_gate(anon):
    # anon's TestClient context has already run app startup, so calling the
    # ASGI app directly still hits an initialized app (schema created,
    # dependency override wired). No cookie is attached at this scope level,
    # matching the "no cookie present" probe that found this bug.
    #
    # Assert the gate's own answer (302) rather than merely "not 200": a 404
    # would satisfy != 200 for the wrong reason if app.js were ever renamed or
    # the static mount broke, leaving the test green with the guard deleted.
    assert _raw_get("/icons/../app.js") == 302
    assert _raw_get("/icons/../index.html") == 302


def test_icon_prefix_traversal_percent_encoded_does_not_bypass_the_gate(anon):
    # A real client sending "/icons/%2e%2e/app.js" arrives at this app with
    # scope["path"] already percent-decoded by uvicorn's h11 layer -- i.e.
    # identical to the literal ".." case above. raw_path (unused by our code)
    # is set to the encoded bytes to document that distinction rather than to
    # exercise a different code path.
    assert _raw_get("/icons/../app.js",
                    encoded_raw_path=b"/icons/%2e%2e/app.js") == 302


# Each of these is a DISTINCT decoded scope["path"] that a single raw request
# target produces, not a restatement of the ".." case above.
#
# uvicorn splits the query string off the RAW target before percent-decoding,
# so "%3F" and "%23" survive the split and land inside scope["path"] as literal
# "?" / "#". Starlette then builds request.url by urlsplit()ing a reconstructed
# URL, where those characters TRUNCATE the path -- request.url.path for the
# first case below is just "/icons/", which sails through the allowlist, while
# StaticFiles resolves the untruncated scope["path"] and served app.js (67KB)
# and index.html (the whole dashboard shell) anonymously. Verified live before
# the fix; this is why the gate reads scope["path"].
@pytest.mark.parametrize("path", [
    "/icons/?/../../app.js",       # from GET /icons/%3F/../../app.js
    "/icons/#/../../app.js",       # from GET /icons/%23/../../app.js
    "/icons/?/../../",             # normpath "." + html=True -> index.html
    "/styles.css?/../app.js",      # an EXACT allowlist entry as the launchpad
    "/icons/x\\..\\..\\app.js",    # ntpath-only; must gate on Linux too
    "/icons/./../app.js",
    "/icons//../app.js",
])
def test_noncanonical_paths_never_bypass_the_gate(anon, path):
    assert _raw_get(path) == 302


def test_icon_files_still_serve_anonymously(anon):
    # Prove the traversal fix didn't overcorrect: the login page needs its
    # icons and they must stay reachable with no session.
    assert _raw_get("/icons/icon-32.png") == 200


def test_login_cookie_is_httponly_lax_and_not_secure(anon):
    # The three flags with the nastiest silent failure modes in this feature:
    # - Secure on a plain-HTTP LAN host means the cookie is never sent and
    #   the owner is locked out of their own dashboard.
    # - SameSite=Strict silently breaks following a link in from Telegram.
    # Assert the absence of Secure explicitly -- that's the one that locks
    # the user out, and no test would otherwise catch its accidental return.
    r = anon.post("/api/auth/signup", json=GOOD)
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie


def test_gated_asset_redirect_arrives_as_a_cacheable_200(anon):
    """Why sw.js cannot trust `res.ok` alone.

    A gated GET 302s to /login, and the FOLLOWED response is a 200 whose body
    is login-page HTML -- which `res.ok` cannot distinguish from the asset the
    request asked for. Caching on res.ok would therefore write login HTML under
    the ORIGINAL url's key, so the SW also requires !res.redirected. This pins
    the premise against the real gate; the SW-side guard is asserted in
    tests/test_static_layout.py.
    """
    r = anon.get("/app.js")  # TestClient follows redirects by default
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert [h.status_code for h in r.history] == [302]
    assert "login-form" in r.text


def test_bad_signup_codes_do_not_lock_the_owner_out_of_signing_in(anon, monkeypatch):
    """Signup failures must not spend the LOGIN budget.

    Both windows are keyed on the client IP, and TRUSTED_PROXIES is empty by
    default, so behind NPM every visitor shares one IP key. Sharing the
    namespace therefore meant: set a SIGNUP_CODE to invite someone, they fumble
    it five times, and the owner cannot sign in for fifteen minutes.
    """
    anon.post("/api/auth/signup", json=GOOD)          # owner exists
    monkeypatch.setattr(web, "SIGNUP_CODE", "letmein")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        r = anon.post("/api/auth/signup",
                      json={"username": "guest", "password": "hunter2hunter2",
                            "code": "wrong"})
        assert r.status_code == 403
    # The signup window itself must still close.
    assert anon.post("/api/auth/signup",
                     json={"username": "guest", "password": "hunter2hunter2",
                           "code": "letmein"}).status_code == 429
    # ...but logging in is a different budget and stays available.
    assert anon.post("/api/auth/login", json=GOOD).status_code == 200
