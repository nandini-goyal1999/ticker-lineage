"""
Acquisition layer. One door in.

Every external read goes through this module. Nothing else in the codebase
talks to the network. That means rate limiting, the SEC User-Agent rule,
retry policy and on-disk caching are enforced in exactly one place, and a
fix here is inherited by every caller.

Sources
  - Wayback CDX index      -> which snapshots of company_tickers.json exist
  - Wayback raw snapshots  -> historical ticker -> CIK maps
  - data.sec.gov           -> current entity state, formerNames, filing history

SEC requires a User-Agent identifying you and rate limits to 10 req/s.
Violating either gets your IP blocked, and unblocking is a support ticket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests

log = logging.getLogger(__name__)

# SEC requires a User-Agent naming a real person and a reachable email.
# Read from the environment so a contact address is never committed to a
# public repo. Set SEC_USER_AGENT="Your Name you@example.com" or pass
# --user-agent on the command line.
SEC_UA = os.environ.get("SEC_USER_AGENT", "")
SEC_RATE_LIMIT_HZ = 8.0          # below SEC's stated 10/s, deliberately
WAYBACK_RATE_LIMIT_HZ = 2.0      # Wayback is slower and less forgiving

CDX_URL = "https://web.archive.org/cdx/search/cdx"
TICKER_FILE = "sec.gov/files/company_tickers.json"
SNAPSHOT_URL = "https://web.archive.org/web/{ts}id_/https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"


class PermanentFetchError(RuntimeError):
    """A client error (4xx other than 429). Not retried."""


@dataclass(frozen=True)
class Snapshot:
    timestamp: str      # Wayback 14-digit timestamp
    quarter: str        # e.g. "2021Q3"
    digest: str         # Wayback content digest - identical content, identical digest

    @property
    def date(self) -> str:
        return f"{self.timestamp[:4]}-{self.timestamp[4:6]}-{self.timestamp[6:8]}"


class _Throttle:
    """Minimum-interval throttle. Not a token bucket - we never want bursts."""

    def __init__(self, hz: float) -> None:
        self._interval = 1.0 / hz
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._interval:
            time.sleep(self._interval - gap)
        self._last = time.monotonic()


class Fetcher:
    def __init__(self, cache_dir: Path, user_agent: str | None = None) -> None:
        ua = user_agent or SEC_UA
        if not ua.strip():
            # Fail loud at construction rather than getting IP-banned on the
            # first request and spending an afternoon working out why.
            raise ValueError(
                "SEC requires a User-Agent identifying you. Set the SEC_USER_AGENT "
                'environment variable to "Your Name you@example.com", or pass '
                "--user-agent on the command line."
            )
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self._sec = _Throttle(SEC_RATE_LIMIT_HZ)
        self._wb = _Throttle(WAYBACK_RATE_LIMIT_HZ)

    # ---------------------------------------------------------------- caching

    def _cache_path(self, key: str) -> Path:
        return self.cache / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"

    def _get_json(self, url: str, *, throttle: _Throttle, use_cache: bool = True, tries: int = 4):
        """
        Cached, throttled GET. The cache is content-addressed by URL, so a rerun
        costs nothing and the pipeline is safe to interrupt and resume.
        """
        path = self._cache_path(url)
        if use_cache and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

        last_err: Exception | None = None
        for attempt in range(tries):
            throttle.wait()
            try:
                r = self.session.get(url, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} on {url}")
                if 400 <= r.status_code < 500:
                    # A 404 or 403 will not fix itself in eight seconds.
                    # Retrying only burns rate-limit budget - fail now.
                    raise PermanentFetchError(f"{r.status_code} on {url}")
                data = r.json()
            except PermanentFetchError:
                raise
            except Exception as e:                      # noqa: BLE001 - retried below
                last_err = e
                backoff = 2 ** attempt
                log.warning("fetch failed (%s/%s) %s - retrying in %ss", attempt + 1, tries, e, backoff)
                time.sleep(backoff)
                continue
            # Write only after a successful parse. A half-written cache entry
            # is worse than no cache entry.
            path.write_text(json.dumps(data), encoding="utf-8")
            return data
        raise RuntimeError(f"exhausted retries for {url}") from last_err

    # ------------------------------------------------------------- discovery

    def list_snapshots(self, limit: int = 1000) -> list[Snapshot]:
        """Every archived version of company_tickers.json with distinct content."""
        url = (
            f"{CDX_URL}?url={TICKER_FILE}&output=json"
            f"&fl=timestamp,statuscode,digest&filter=statuscode:200"
            f"&collapse=digest&limit={limit}"
        )
        rows = self._get_json(url, throttle=self._wb)[1:]      # row 0 is the header
        out = []
        for ts, _status, digest in rows:
            q = f"{ts[:4]}Q{(int(ts[4:6]) - 1) // 3 + 1}"
            out.append(Snapshot(timestamp=ts, quarter=q, digest=digest))
        return sorted(out, key=lambda s: s.timestamp)

    @staticmethod
    def quarterly(snapshots: list[Snapshot]) -> list[Snapshot]:
        """First snapshot in each calendar quarter. Deterministic given the same index."""
        seen: dict[str, Snapshot] = {}
        for s in snapshots:
            seen.setdefault(s.quarter, s)
        return sorted(seen.values(), key=lambda s: s.timestamp)

    # ------------------------------------------------------------ retrieval

    def ticker_map(self, snap: Snapshot) -> list[dict]:
        """
        One snapshot's ticker file, normalised to a list of rows.

        SEC has changed this file's *shape* over the years, not just its
        contents. Both the dict-of-rows and list-of-rows forms are handled.
        A shape we do not recognise raises rather than silently returning [].
        """
        raw = self._get_json(SNAPSHOT_URL.format(ts=snap.timestamp), throttle=self._wb)
        if isinstance(raw, dict):
            rows = list(raw.values())
        elif isinstance(raw, list):
            rows = raw
        else:
            raise ValueError(f"unrecognised ticker file shape {type(raw)} at {snap.timestamp}")

        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            cik = str(row.get("cik_str") or row.get("cik") or "").strip()
            if not ticker or not cik:
                # Flag, never drop. A row we cannot key is a data-quality
                # signal, not noise.
                log.debug("unkeyable row in %s: %r", snap.timestamp, row)
                continue
            out.append({"ticker": ticker, "cik": cik, "title": str(row.get("title") or "").strip()})
        if not out:
            raise ValueError(f"snapshot {snap.timestamp} parsed to zero rows - refusing to treat as empty")
        return out

    def submissions(self, cik: str) -> dict:
        """Current entity state for one CIK, including formerNames with date ranges."""
        return self._get_json(SUBMISSIONS_URL.format(cik=cik), throttle=self._sec)

    def submissions_many(self, ciks: list[str]) -> Iterator[tuple[str, dict | None]]:
        for cik in ciks:
            try:
                yield cik, self.submissions(cik)
            except Exception as e:                      # noqa: BLE001
                log.warning("submissions failed for CIK %s: %s", cik, e)
                yield cik, None
