import pytest
from app import db, fx


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def test_sgd_is_identity_without_any_fetch(conn):
    def boom(_):
        raise AssertionError("must not call the network for SGD")

    assert fx.sgd_rate(conn, "SGD", fetcher=boom) == 1.0


def test_fetches_and_caches(conn):
    calls = []

    def fetcher(cur):
        calls.append(cur)
        return 1.34

    assert fx.sgd_rate(conn, "USD", fetcher=fetcher, now=1000.0) == 1.34
    # Second call inside the TTL must be served from cache.
    assert fx.sgd_rate(conn, "USD", fetcher=fetcher, now=1100.0) == 1.34
    assert calls == ["USD"]


def test_refetches_after_ttl(conn):
    rates = iter([1.34, 1.40])

    def fetcher(_):
        return next(rates)

    assert fx.sgd_rate(conn, "USD", fetcher=fetcher, now=1000.0) == 1.34
    later = 1000.0 + fx.CACHE_TTL_SECONDS + 1
    assert fx.sgd_rate(conn, "USD", fetcher=fetcher, now=later) == 1.40


def test_falls_back_to_stale_cache_on_failure(conn):
    def ok(_):
        return 1.34

    def fail(_):
        raise OSError("network down")

    fx.sgd_rate(conn, "USD", fetcher=ok, now=1000.0)
    later = 1000.0 + fx.CACHE_TTL_SECONDS + 1
    # Stale-but-real beats None: the toggle stays usable during an outage.
    assert fx.sgd_rate(conn, "USD", fetcher=fail, now=later) == 1.34


def test_returns_none_when_no_cache_and_fetch_fails(conn):
    def fail(_):
        raise OSError("network down")

    assert fx.sgd_rate(conn, "USD", fetcher=fail, now=1000.0) is None


def test_currency_is_case_insensitive(conn):
    assert fx.sgd_rate(conn, "sgd", fetcher=lambda _: 9.9) == 1.0


def test_http_fetch_sends_a_non_default_user_agent(monkeypatch):
    """frankfurter 403s the default urllib UA (Python-urllib/3.x) — verified
    directly against the live endpoint. _http_fetch must send an honest,
    descriptive UA instead. No live network call: capture the Request object
    urlopen receives."""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"rates": {"SGD": 1.34}}'

    def fake_urlopen(req, timeout=None):
        captured["request"] = req
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen)

    rate = fx._http_fetch("USD")

    assert rate == 1.34
    assert captured["timeout"] == fx._TIMEOUT
    req = captured["request"]
    ua = req.get_header("User-agent")  # urllib title-cases header keys
    assert ua is not None
    assert ua != ""
    assert "python-urllib" not in ua.lower()
