"""
Snapshot sequence -> temporal identifier edges.

An edge is an assertion with a validity range: "ticker T belonged to CIK C
from snapshot i0 through snapshot i1." Ranges, not points. That is the whole
difference between this and a naive mapping table.

Also detects regime artifacts: snapshots where the SOURCE FILE changed
composition rather than the market changing. Those generate false reuse and
false delisting signals, and if you do not flag them you will report them as
findings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# A snapshot-over-snapshot row-count change larger than this fraction is
# treated as the file changing, not the market. Calibrated against the two
# known SEC events (2019Q4 +117%, 2020Q3 -38%); normal quarters move <10%.
REGIME_CHANGE_THRESHOLD = 0.25


@dataclass
class Edge:
    ticker: str
    cik: str
    title: str
    i0: int
    i1: int
    source: str = "sec_company_tickers_via_wayback"
    confidence: float = 1.0

    @property
    def observations(self) -> int:
        return self.i1 - self.i0 + 1


@dataclass
class EdgeSet:
    edges: dict[str, list[Edge]] = field(default_factory=dict)
    file_rows: list[int] = field(default_factory=list)
    quarters: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- ingestion

    def ingest(self, index: int, quarter: str, rows: list[dict]) -> None:
        """
        Fold one snapshot into the edge set.

        Idempotent per (index, ticker, cik): re-running with the same snapshot
        extends the same segment rather than creating a duplicate.
        """
        self.file_rows.append(len(rows))
        self.quarters.append(quarter)
        seen_this_snapshot: set[str] = set()

        for row in rows:
            ticker, cik = row["ticker"], row["cik"]
            if ticker in seen_this_snapshot:
                # Same ticker twice in one file. Rare, and a real data-quality
                # signal - log it rather than letting last-write-wins decide.
                log.warning("duplicate ticker %s within snapshot %s", ticker, quarter)
            seen_this_snapshot.add(ticker)

            segments = self.edges.setdefault(ticker, [])
            last = segments[-1] if segments else None
            if last is not None and last.cik == cik:
                last.i1 = max(last.i1, index)
            else:
                segments.append(Edge(ticker=ticker, cik=cik, title=row.get("title", ""),
                                     i0=index, i1=index))

    # -------------------------------------------------------------- analysis

    def regime_artifacts(self) -> list[int]:
        """
        Snapshot indices where the file's own composition changed sharply.

        This is the check that keeps you honest. Without it the 2019Q4
        expansion alone contributes 132 spurious 'ticker reuse' transitions.
        """
        flagged = []
        for i in range(1, len(self.file_rows)):
            before, after = self.file_rows[i - 1], self.file_rows[i]
            if before == 0:
                continue
            if abs(after - before) / before > REGIME_CHANGE_THRESHOLD:
                flagged.append(i)
                log.warning("regime artifact at %s: %d -> %d rows", self.quarters[i], before, after)
        return flagged

    def stable_window(self) -> tuple[int, int]:
        """Longest contiguous run of snapshots containing no regime artifact."""
        artifacts = set(self.regime_artifacts())
        best = (0, -1)
        start = 0
        for i in range(len(self.file_rows) + 1):
            if i in artifacts or i == len(self.file_rows):
                if i - 1 - start > best[1] - best[0]:
                    best = (start, i - 1)
                start = i
        return best

    def regime_labels(self) -> list[str]:
        """Per snapshot: 'artifact' if the file's composition broke there, else 'stable'."""
        artifacts = set(self.regime_artifacts())
        return ["artifact" if i in artifacts else "stable" for i in range(len(self.file_rows))]

    def presence_drift(self) -> list[int]:
        """
        Per snapshot: (tickers implied present by segments) - (actual file rows).

        Non-zero means the gap-collapsing limitation is biting - a ticker
        vanished and came back under the same CIK, and the segment spans the
        hole. Publishing this number is cheaper than pretending it is zero.
        """
        implied = [0] * len(self.file_rows)
        for segments in self.edges.values():
            for e in segments:
                for i in range(e.i0, e.i1 + 1):
                    implied[i] += 1
        return [implied[i] - self.file_rows[i] for i in range(len(self.file_rows))]

    # ----------------------------------------------------------------- views

    def by_cik(self) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = {}
        for segments in self.edges.values():
            for e in segments:
                out.setdefault(e.cik, []).append(e)
        return out

    def clip(self, lo: int, hi: int) -> "EdgeSet":
        """Restrict every edge to a snapshot window. Used for the stable-regime rerun."""
        clipped = EdgeSet(file_rows=self.file_rows[lo:hi + 1], quarters=self.quarters[lo:hi + 1])
        for ticker, segments in self.edges.items():
            kept = []
            for e in segments:
                a, b = max(e.i0, lo), min(e.i1, hi)
                if a <= b:
                    kept.append(Edge(e.ticker, e.cik, e.title, a, b, e.source, e.confidence))
            if kept:
                clipped.edges[ticker] = kept
        return clipped

    def to_rows(self) -> list[dict]:
        return [
            {"ticker": e.ticker, "cik": e.cik, "title": e.title,
             "i0": e.i0, "i1": e.i1, "source": e.source, "confidence": e.confidence}
            for segments in self.edges.values() for e in segments
        ]
