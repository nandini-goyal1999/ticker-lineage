"""
Temporal overlap logic and the failure-class classifier.

The fixtures are the real SEC sequences, reduced to the minimum number of
snapshots that still reproduce the behaviour. Where the test asserts a number,
that number came out of the live data.
"""

import pytest

from tickerlineage.analyze import classify_transitions, fractured_histories, naive_join_cost
from tickerlineage.build import EdgeSet

QUARTERS = ["2021Q1", "2021Q2", "2021Q3", "2021Q4", "2022Q1", "2022Q2", "2022Q3", "2022Q4"]


def make(rows_per_snapshot):
    """Build an EdgeSet from [[(ticker, cik, title), ...], ...]."""
    es = EdgeSet()
    for i, rows in enumerate(rows_per_snapshot):
        es.ingest(i, QUARTERS[i], [{"ticker": t, "cik": c, "title": n} for t, c, n in rows])
    return es


class TestSegmentBuilding:
    def test_contiguous_ownership_is_one_segment(self):
        es = make([[("AAPL", "320193", "Apple Inc.")]] * 4)
        assert len(es.edges["AAPL"]) == 1
        assert es.edges["AAPL"][0].observations == 4

    def test_cik_change_opens_a_new_segment(self):
        es = make([
            [("S", "101830", "Sprint Corporation")],
            [("S", "101830", "Sprint Corporation")],
            [("S", "1583708", "SentinelOne, Inc.")],
        ])
        segs = es.edges["S"]
        assert [s.cik for s in segs] == ["101830", "1583708"]
        assert segs[0].i1 == 1 and segs[1].i0 == 2

    def test_gap_then_different_cik_is_captured(self):
        es = make([
            [("S", "101830", "Sprint Corporation")],
            [],                                        # ticker released
            [],
            [("S", "1583708", "SentinelOne, Inc.")],
        ])
        segs = es.edges["S"]
        assert len(segs) == 2
        assert segs[1].i0 - segs[0].i1 - 1 == 2       # two-quarter gap

    def test_known_limitation_gap_under_same_cik_is_collapsed(self):
        """
        Documented, not fixed. A ticker that vanishes and returns under the
        SAME CIK is recorded as continuous. Reuse detection is unaffected;
        intra-period gaps are understated. presence_drift() quantifies it.
        """
        es = make([
            [("XYZ", "111", "Acme")],
            [],
            [("XYZ", "111", "Acme")],
        ])
        assert len(es.edges["XYZ"]) == 1               # the collapse
        assert es.presence_drift() == [0, 1, 0]        # and it is measurable


class TestOverlapVsDisjoint:
    """Share classes and renames are the same query read two ways. Getting the
    overlap test backwards silently swaps the two counts."""

    def test_concurrent_tickers_are_share_classes(self):
        es = make([[("GOOG", "1652044", "Alphabet Inc."),
                    ("GOOGL", "1652044", "Alphabet Inc.")]] * 3)
        result = fractured_histories(es)
        assert len(result["concurrent_multi_ticker"]) == 1
        assert result["renamed"] == []

    def test_disjoint_tickers_are_a_rename(self):
        es = make([
            [("BLL", "9389", "Ball Corporation")],
            [("BLL", "9389", "Ball Corporation")],
            [("BALL", "9389", "Ball Corporation")],
        ])
        result = fractured_histories(es)
        assert len(result["renamed"]) == 1
        assert result["concurrent_multi_ticker"] == []
        assert result["renamed"][0]["tickers"] == ["BALL", "BLL"]

    def test_touching_but_not_overlapping_is_disjoint(self):
        # Boundary: segment A ends at i=1, B starts at i=2. Not an overlap.
        es = make([
            [("OLD", "500", "Acme")],
            [("OLD", "500", "Acme")],
            [("NEW", "500", "Acme")],
        ])
        assert len(fractured_histories(es)["renamed"]) == 1


class TestClassification:
    def test_reassignment_needs_a_gap_and_unrelated_names(self):
        es = make([
            [("S", "101830", "Sprint Corporation")],
            [],
            [("S", "1583708", "SentinelOne, Inc.")],
        ])
        t = classify_transitions(es)[0]
        assert t.klass == "reassignment"
        assert t.resolved_by == "unresolved"
        assert t.gap_quarters == 1

    def test_identical_names_are_a_holdco_restructure(self):
        es = make([
            [("BLK", "1364742", "BlackRock, Inc.")],
            [("BLK", "2012383", "BlackRock, Inc.")],
        ])
        t = classify_transitions(es)[0]
        assert t.klass == "holdco_restructure"
        assert t.resolved_by == "deterministic"

    def test_apache_to_apa_is_the_documented_residual(self):
        """
        Apache Corp. -> APA Corp (CIK 6769 -> 1841666) IS a holdco
        restructure. Name matching cannot see it: "APACHE" vs "APA" scores
        66.7, well under the 88 threshold, because it is an abbreviation and
        not a typo.

        The test asserts the honest behaviour rather than the desired one.
        Lowering FUZZY_THRESHOLD until this passes would buy one true positive
        and an unknown number of false ones - and a false positive here merges
        two unrelated companies, which is the exact failure this project
        exists to measure.

        The correct fix is an external deterministic signal (SEC formerNames
        and filing continuity via the submissions API), not a looser threshold.
        """
        es = make([
            [("APA", "6769", "Apache Corp.")],
            [("APA", "1841666", "APA Corp")],
        ])
        t = classify_transitions(es)[0]
        assert t.resolved_by == "unresolved"
        assert t.klass == "ambiguous"
        assert 60 <= t.score < 88

    def test_no_gap_and_unrelated_names_is_ambiguous_not_guessed(self):
        """Refusing to classify is a valid answer. Guessing here is how you
        report a file artifact as a corporate event."""
        es = make([
            [("GOOGL", "1288776", "Google Inc.")],
            [("GOOGL", "1652044", "Alphabet Inc.")],
        ])
        assert classify_transitions(es)[0].klass == "ambiguous"


class TestRegimeArtifacts:
    def test_sharp_row_count_change_is_flagged(self):
        es = EdgeSet()
        for i, n in enumerate([100, 102, 101, 240, 238]):   # +138% at i=3
            es.ingest(i, QUARTERS[i],
                      [{"ticker": f"T{k}", "cik": str(k), "title": "X"} for k in range(n)])
        assert es.regime_artifacts() == [3]

    def test_stable_window_excludes_the_artifact(self):
        es = EdgeSet()
        for i, n in enumerate([100, 102, 101, 240, 238, 239, 241]):
            es.ingest(i, QUARTERS[i],
                      [{"ticker": f"T{k}", "cik": str(k), "title": "X"} for k in range(n)])
        lo, hi = es.stable_window()
        assert (lo, hi) == (3, 6)                       # the longer clean run

    def test_normal_quarterly_drift_is_not_flagged(self):
        es = EdgeSet()
        for i, n in enumerate([1000, 1050, 990, 1020]):
            es.ingest(i, QUARTERS[i],
                      [{"ticker": f"T{k}", "cik": str(k), "title": "X"} for k in range(n)])
        assert es.regime_artifacts() == []


class TestNaiveJoinCost:
    def test_counts_only_post_handover_observations(self):
        es = make([
            [("S", "101830", "Sprint")],
            [("S", "101830", "Sprint")],
            [("S", "1583708", "SentinelOne")],
            [("S", "1583708", "SentinelOne")],
        ])
        cost = naive_join_cost(es)
        assert cost["observations"] == 4
        assert cost["misattributed"] == 2       # the two SentinelOne quarters
        assert cost["pct_misattributed"] == 50.0

    def test_stable_ticker_costs_nothing(self):
        es = make([[("AAPL", "320193", "Apple Inc.")]] * 4)
        assert naive_join_cost(es)["misattributed"] == 0

    def test_clip_restricts_the_window(self):
        es = make([
            [("S", "101830", "Sprint")],
            [("S", "101830", "Sprint")],
            [("S", "1583708", "SentinelOne")],
        ])
        # Clipped to the pre-handover window, there is nothing to misattribute.
        assert naive_join_cost(es.clip(0, 1))["misattributed"] == 0

    def test_empty_edgeset_does_not_divide_by_zero(self):
        assert naive_join_cost(EdgeSet())["pct_misattributed"] == 0.0


class TestIngestionRefusals:
    def test_duplicate_ticker_in_one_snapshot_is_logged_not_silent(self, caplog):
        es = EdgeSet()
        with caplog.at_level("WARNING"):
            es.ingest(0, "2021Q1", [
                {"ticker": "DUP", "cik": "1", "title": "A"},
                {"ticker": "DUP", "cik": "2", "title": "B"},
            ])
        assert "duplicate ticker" in caplog.text
