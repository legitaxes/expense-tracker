# tests/test_auth.py
from datetime import datetime, timedelta

import pytest

from app import auth, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


def test_hash_round_trip():
    stored = auth.hash_password("hunter2hunter2")
    assert auth.verify_password("hunter2hunter2", stored)
    assert not auth.verify_password("wrong", stored)


def test_hash_is_salted():
    # Same password twice must not produce the same string, or the hashes leak
    # which accounts share a password.
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_reads_stored_parameters_not_current_constants():
    # The work factor must be changeable later without invalidating old hashes,
    # so verification parses n/r/p out of the stored string.
    weak = auth.hash_password("pw", n=2 ** 10)
    assert weak.split("$")[1] == str(2 ** 10)
    assert auth.verify_password("pw", weak)


def test_verify_rejects_garbage_without_raising():
    assert not auth.verify_password("pw", "not-a-hash")
    assert not auth.verify_password("pw", "")


def test_create_user_and_lookup(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    row = auth.get_user(conn, "jia")
    assert row["id"] == uid
    assert auth.user_count(conn) == 1


def test_username_is_case_insensitive_for_uniqueness(conn):
    auth.create_user(conn, "Jia", "hunter2hunter2")
    with pytest.raises(ValueError):
        auth.create_user(conn, "jia", "hunter2hunter2")
    assert auth.get_user(conn, "JIA") is not None


def test_create_user_validates_input(conn):
    with pytest.raises(ValueError):
        auth.create_user(conn, "ab", "hunter2hunter2")          # too short
    with pytest.raises(ValueError):
        auth.create_user(conn, "has space", "hunter2hunter2")   # bad chars
    with pytest.raises(ValueError):
        auth.create_user(conn, "jia", "short")                  # password < 8


def test_session_round_trip(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    token = auth.create_session(conn, uid)
    assert auth.lookup_session(conn, token) == uid


def test_session_token_is_not_stored_verbatim(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    token = auth.create_session(conn, uid)
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
    assert token not in stored


def test_session_expires_absolutely(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    now = datetime(2026, 1, 1, 12, 0, 0)
    token = auth.create_session(conn, uid, now=now)
    assert auth.lookup_session(conn, token, now=now + timedelta(days=89)) == uid
    assert auth.lookup_session(conn, token, now=now + timedelta(days=91)) is None


def test_lookup_rejects_unknown_token(conn):
    assert auth.lookup_session(conn, "nope") is None


def test_delete_session_revokes(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    token = auth.create_session(conn, uid)
    auth.delete_session(conn, token)
    assert auth.lookup_session(conn, token) is None


def test_purge_expired_removes_only_stale_rows(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    now = datetime(2026, 1, 1)
    old = auth.create_session(conn, uid, now=now - timedelta(days=100))
    fresh = auth.create_session(conn, uid, now=now)
    auth.purge_expired(conn, now=now)
    assert auth.lookup_session(conn, old, now=now) is None
    assert auth.lookup_session(conn, fresh, now=now) == uid


def test_create_user_rejects_newline_in_username(conn):
    # Newlines would be a header/log injection vector if stored in usernames.
    # Test both trailing and embedded newlines.
    with pytest.raises(ValueError):
        auth.create_user(conn, "abcxyz\n", "hunter2hunter2")   # trailing newline
    with pytest.raises(ValueError):
        auth.create_user(conn, "ab\ncd", "hunter2hunter2")     # embedded newline


def test_session_expires_at_boundary(conn):
    uid = auth.create_user(conn, "jia", "hunter2hunter2")
    now = datetime(2026, 1, 1, 12, 0, 0)
    token = auth.create_session(conn, uid, now=now)
    # At the exact expiry moment, the session is expired (uses <=)
    expires_at = now + timedelta(seconds=auth.SESSION_SECONDS)
    assert auth.lookup_session(conn, token, now=expires_at) is None


def test_throttle_blocks_after_max_failures():
    auth.reset_throttle()
    key = auth.ip_key("10.0.0.9")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert not auth.login_blocked(key)
        auth.record_login_failure(key)
    assert auth.login_blocked(key)


def test_throttle_keys_are_independent():
    auth.reset_throttle()
    ip = auth.ip_key("10.0.0.9")
    user = auth.user_key("jia")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(ip)
    assert auth.login_blocked(ip)
    assert not auth.login_blocked(user)


def test_user_key_is_case_insensitive():
    assert auth.user_key("Jia") == auth.user_key("jia") == auth.user_key(" JIA ")


def test_throttle_window_expires(monkeypatch):
    auth.reset_throttle()
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
    key = auth.ip_key("10.0.0.9")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(key)
    assert auth.login_blocked(key)
    clock["t"] += auth.LOGIN_WINDOW_SECONDS + 1
    assert not auth.login_blocked(key)


def test_dummy_hash_uses_the_same_parameters_as_real_hashes():
    # If the dummy is cheaper than a real verify, an unknown username answers
    # faster and the username list leaks by timing.
    real = auth.hash_password("whatever")
    real_n, real_r, real_p = real.split("$")[1:4]
    dummy_n, dummy_r, dummy_p = auth._DUMMY_HASH.split("$")[1:4]
    assert (real_n, real_r, real_p) == (dummy_n, dummy_r, dummy_p)


class _Req:
    def __init__(self, peer, headers=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers or {}


def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXIES", ())
    req = _Req("192.168.1.200", {"x-forwarded-for": "1.2.3.4"})
    assert auth.client_ip(req) == "192.168.1.200"


def test_client_ip_honors_forwarded_header_from_a_trusted_peer(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXIES", ("192.168.1.200",))
    req = _Req("192.168.1.200", {"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
    assert auth.client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_when_peer_is_untrusted(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXIES", ("192.168.1.200",))
    req = _Req("10.0.0.99", {"x-forwarded-for": "1.2.3.4"})
    assert auth.client_ip(req) == "10.0.0.99"


def test_throttle_cleanup(monkeypatch):
    # Calling login_blocked on a never-failed key must not create an entry
    auth.reset_throttle()
    key = auth.ip_key("10.0.0.9")
    auth.login_blocked(key)
    assert key not in auth._failed_logins

    # A key whose failures have aged out must be evicted from _failed_logins
    auth.reset_throttle()
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["t"])
    key = auth.ip_key("10.0.0.9")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(key)
    assert key in auth._failed_logins
    clock["t"] += auth.LOGIN_WINDOW_SECONDS + 1
    auth.login_blocked(key)
    assert key not in auth._failed_logins


def test_login_blocked_with_multiple_keys_ors_results():
    # Test that login_blocked returns True when ANY key is over budget
    auth.reset_throttle()
    ip = auth.ip_key("10.0.0.9")
    user = auth.user_key("jia")

    # When only IP is over budget, should return True
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(ip)
    assert auth.login_blocked(ip, user)

    # Reset and test when only username is over budget
    auth.reset_throttle()
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(user)
    assert auth.login_blocked(ip, user)


def test_record_login_failure_with_multiple_keys():
    # Verify that record_login_failure records on both keys independently
    auth.reset_throttle()
    ip = auth.ip_key("10.0.0.9")
    user = auth.user_key("jia")

    # Record LOGIN_MAX_FAILURES on both keys together
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_failure(ip, user)

    # Both keys should independently be blocked
    assert auth.login_blocked(ip)
    assert auth.login_blocked(user)


def test_client_ip_handles_missing_client():
    # When request.client is None, client_ip should return "unknown" safely
    req = type("Req", (), {"client": None, "headers": {}})()
    assert auth.client_ip(req) == "unknown"
