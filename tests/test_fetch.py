"""
The acquisition layer, tested without the network.

A fake session stands in for requests so the retry policy can be asserted
exactly: which status codes are retried, which fail immediately, and that
nothing is cached until a response actually parses.
"""

import pytest

from tickerlineage import fetch
from tickerlineage.fetch import Fetcher, PermanentFetchError, Snapshot


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout):
        self.calls += 1
        return self.responses.pop(0)


@pytest.fixture
def fetcher(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)   # no real backoff
    return Fetcher(tmp_path, user_agent="Test Suite test@example.com")


def _use(fetcher, *responses):
    fetcher.session = FakeSession(responses)
    return fetcher.session


class TestUserAgent:
    def test_missing_user_agent_fails_at_construction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch, "SEC_UA", "")
        with pytest.raises(ValueError, match="User-Agent"):
            Fetcher(tmp_path, user_agent="   ")


class TestRetryPolicy:
    def test_404_is_not_retried(self, fetcher):
        session = _use(fetcher, FakeResponse(404), FakeResponse(200, {"ok": 1}))
        with pytest.raises(PermanentFetchError):
            fetcher.submissions("320193")
        assert session.calls == 1

    def test_429_is_retried_then_succeeds(self, fetcher):
        session = _use(fetcher, FakeResponse(429), FakeResponse(200, {"ok": 1}))
        assert fetcher.submissions("320193") == {"ok": 1}
        assert session.calls == 2

    def test_5xx_exhausts_retries(self, fetcher):
        _use(fetcher, *[FakeResponse(503)] * 4)
        with pytest.raises(RuntimeError, match="exhausted retries"):
            fetcher.submissions("320193")


class TestCache:
    def test_second_read_comes_from_disk(self, fetcher):
        session = _use(fetcher, FakeResponse(200, {"name": "Société Générale"}))
        first = fetcher.submissions("1")
        second = fetcher.submissions("1")
        assert first == second == {"name": "Société Générale"}
        assert session.calls == 1

    def test_failed_fetch_leaves_no_cache_entry(self, fetcher, tmp_path):
        _use(fetcher, *[FakeResponse(500)] * 4)
        with pytest.raises(RuntimeError):
            fetcher.submissions("1")
        assert list(tmp_path.glob("*.json")) == []


class TestTickerMapShapes:
    SNAP = Snapshot(timestamp="20210701000000", quarter="2021Q3", digest="x")

    def test_dict_of_rows(self, fetcher):
        _use(fetcher, FakeResponse(200, {"0": {"ticker": "aapl", "cik_str": 320193, "title": "Apple Inc."}}))
        assert fetcher.ticker_map(self.SNAP) == [{"ticker": "AAPL", "cik": "320193", "title": "Apple Inc."}]

    def test_zero_rows_refuses(self, fetcher):
        _use(fetcher, FakeResponse(200, {"0": {"ticker": "", "cik_str": ""}}))
        with pytest.raises(ValueError, match="zero rows"):
            fetcher.ticker_map(self.SNAP)

    def test_quarter_bucketing_keeps_first_snapshot(self):
        snaps = [Snapshot("20210815000000", "2021Q3", "b"),
                 Snapshot("20210701000000", "2021Q3", "a"),
                 Snapshot("20211002000000", "2021Q4", "c")]
        assert [s.digest for s in Fetcher.quarterly(sorted(snaps, key=lambda s: s.timestamp))] == ["a", "c"]
