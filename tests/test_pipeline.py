"""
End-to-end pipeline run against a fake fetcher.

Asserts the output files exist, carry the regime column, and survive
non-ASCII company names - which on Windows used to crash the CSV writer,
because the default file encoding there is cp1252, not UTF-8.
"""

import csv
import json

import pytest

from tickerlineage import pipeline
from tickerlineage.analyze import classify_transitions, run_all
from tickerlineage.build import EdgeSet
from tickerlineage.fetch import Snapshot

SNAPS = [Snapshot(f"20{y}0101000000", f"20{y}Q1", str(y)) for y in range(21, 26)]


def _rows(n, extra=()):
    return [{"ticker": f"T{k}", "cik": str(k), "title": "Filler"} for k in range(n)] + list(extra)


SPRINT = {"ticker": "S", "cik": "101830", "title": "Sprint Corporation"}
SENTINEL = {"ticker": "S", "cik": "1583708", "title": "SentinelOne, Inc."}
UNICODE = {"ticker": "ZZZ", "cik": "999", "title": "Ζήτα Ολυμπος — 株式会社"}

# Snapshot 2 jumps +150% in rows: a regime artifact.
MAPS = [_rows(100, [SPRINT, UNICODE]), _rows(100, [UNICODE]),
        _rows(250, [SENTINEL, UNICODE]), _rows(250, [SENTINEL, UNICODE]),
        _rows(251, [SENTINEL, UNICODE])]


class FakeFetcher:
    def __init__(self, cache, user_agent=None):
        pass

    def list_snapshots(self):
        return SNAPS

    quarterly = staticmethod(lambda snaps: snaps)

    def ticker_map(self, snap):
        return MAPS[SNAPS.index(snap)]


def test_end_to_end_writes_utf8_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "Fetcher", FakeFetcher)
    pipeline.main(["--out", str(tmp_path), "--user-agent", "x x@example.com"])

    findings = json.loads((tmp_path / "findings_generated.json").read_text(encoding="utf-8"))
    assert findings["regime_artifacts"] == ["2023Q1"]

    with (tmp_path / "snapshots.csv").open(encoding="utf-8") as fh:
        snaps = list(csv.DictReader(fh))
    assert [r["regime"] for r in snaps] == ["stable", "stable", "artifact", "stable", "stable"]

    edges = (tmp_path / "identifier_edges.csv").read_text(encoding="utf-8")
    assert UNICODE["title"] in edges

    with (tmp_path / "ticker_transitions.csv").open(encoding="utf-8") as fh:
        (t,) = list(csv.DictReader(fh))
    assert t["ticker"] == "S" and t["klass"] == "file_artifact"


def test_no_snapshots_exits_cleanly(tmp_path, monkeypatch):
    class Empty(FakeFetcher):
        def list_snapshots(self):
            return []
    monkeypatch.setattr(pipeline, "Fetcher", Empty)
    with pytest.raises(SystemExit, match="no snapshots"):
        pipeline.main(["--out", str(tmp_path), "--user-agent", "x x@example.com"])


def test_run_all_refuses_an_empty_edgeset():
    with pytest.raises(ValueError):
        run_all(EdgeSet())


def test_handover_on_artifact_snapshot_is_reclassified():
    es = EdgeSet()
    for i, rows in enumerate(MAPS):
        es.ingest(i, SNAPS[i].quarter, rows)
    (t,) = classify_transitions(es)
    assert t.switch_quarter == "2023Q1"
    assert t.klass == "file_artifact"
    assert t.resolved_by == "unresolved"
