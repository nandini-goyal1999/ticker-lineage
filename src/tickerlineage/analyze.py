"""
The four failure classes, and the naive-join diff.

The deliverable is not the graph. It is a set of measured claims about what a
ticker-keyed join costs you, each one reproducible from the edge set.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rapidfuzz import fuzz

from .build import EdgeSet
from .normalize import normalize, prefix_related, same_normalized

FUZZY_THRESHOLD = 88          # token_set_ratio; below this we do not assert a match


@dataclass
class Transition:
    ticker: str
    from_cik: str
    from_title: str
    to_cik: str
    to_title: str
    last_quarter_at_from: str
    switch_quarter: str
    gap_quarters: int
    klass: str                # reassignment | holdco_restructure | ambiguous
    resolved_by: str          # deterministic | normalized | fuzzy | unresolved
    score: float


def classify_transitions(es: EdgeSet) -> list[Transition]:
    """
    Walk each ticker's segments and classify every CIK handover.

    Three layers, in this order:
      1. deterministic  - normalised names are identical
      2. normalized     - one is a whole-token prefix of the other
      3. fuzzy          - RapidFuzz token_set_ratio above threshold

    A handover the layers cannot relate, with a gap in coverage, is a genuine
    reassignment: the ticker was released and picked up by someone else.
    """
    artifacts = set(es.regime_artifacts())
    out: list[Transition] = []

    for ticker, segments in es.edges.items():
        for prev, cur in zip(segments, segments[1:]):
            if prev.cik == cur.cik:
                continue
            gap = cur.i0 - prev.i1 - 1

            if same_normalized(prev.title, cur.title):
                klass, by, score = "holdco_restructure", "deterministic", 100.0
            elif prefix_related(prev.title, cur.title):
                klass, by, score = "holdco_restructure", "normalized", 100.0
            else:
                score = float(fuzz.token_set_ratio(normalize(prev.title), normalize(cur.title)))
                if score >= FUZZY_THRESHOLD:
                    klass, by = "holdco_restructure", "fuzzy"
                elif gap >= 1:
                    klass, by = "reassignment", "unresolved"
                else:
                    # No gap and no name relation. Could be a same-quarter
                    # handover or a file artifact. Do not guess.
                    klass, by = "ambiguous", "unresolved"

            out.append(Transition(
                ticker=ticker,
                from_cik=prev.cik, from_title=prev.title,
                to_cik=cur.cik, to_title=cur.title,
                last_quarter_at_from=es.quarters[prev.i1],
                switch_quarter=es.quarters[cur.i0],
                gap_quarters=gap,
                # A handover landing on a regime-artifact snapshot is the
                # file changing, not the market. Checked by index, not by
                # quarter label, so duplicate labels cannot misfile it.
                klass="file_artifact" if cur.i0 in artifacts else klass,
                resolved_by=by, score=score,
            ))
    return out


def naive_join_cost(es: EdgeSet) -> dict:
    """
    THE headline number.

    Build the same ticker-quarter panel two ways. A naive ticker-keyed join
    attributes every observation of a ticker to whichever CIK held it first.
    A temporal CIK join attributes each observation to the CIK that actually
    held it at that time. Count the disagreements.
    """
    total = wrong = reuse_tickers = 0
    for segments in es.edges.values():
        total += sum(e.observations for e in segments)
        if len({e.cik for e in segments}) > 1:
            reuse_tickers += 1
            first = segments[0].cik
            wrong += sum(e.observations for e in segments if e.cik != first)
    return {
        "observations": total,
        "misattributed": wrong,
        "pct_misattributed": round(100 * wrong / total, 3) if total else 0.0,
        "reuse_tickers": reuse_tickers,
        "window": f"{es.quarters[0]}..{es.quarters[-1]}" if es.quarters else "",
    }


def fractured_histories(es: EdgeSet) -> dict:
    """
    Class 2 and 3, which are the same query read two ways.

    A CIK holding several tickers whose spans OVERLAP has share classes or
    multiple listings. A CIK holding several tickers whose spans are DISJOINT
    renamed - and a ticker-keyed history breaks at every rename.
    """
    renamed, multi = [], []
    for cik, edges in es.by_cik().items():
        tickers = {e.ticker for e in edges}
        if len(tickers) < 2:
            continue
        overlap = any(
            a.ticker != b.ticker and a.i0 <= b.i1 and b.i0 <= a.i1
            for i, a in enumerate(edges) for b in edges[i + 1:]
        )
        (multi if overlap else renamed).append(
            {"cik": cik, "title": edges[0].title,
             "tickers": sorted(tickers),
             "spans": sorted((e.ticker, es.quarters[e.i0], es.quarters[e.i1]) for e in edges)}
        )
    return {"renamed": renamed, "concurrent_multi_ticker": multi}


def survivorship(es: EdgeSet) -> dict:
    """
    Class 4, reported with a health warning.

    A CIK absent from the final snapshot looks dead. But the source file has
    changed composition twice, so absence is confounded. This returns the
    count AND the confound, and refuses to present a bare number.
    """
    by_cik = es.by_cik()
    last_idx = len(es.quarters) - 1
    gone = [cik for cik, edges in by_cik.items() if max(e.i1 for e in edges) < last_idx]
    return {
        "ciks_total": len(by_cik),
        "ciks_absent_from_final_snapshot": len(gone),
        "confounded_by": [es.quarters[i] for i in es.regime_artifacts()],
        "usable": not es.regime_artifacts(),
        "note": (
            "Not a delisting count. Absence from the ticker file is confounded by "
            "the file's own composition changes. Validate against per-CIK filing "
            "activity before quoting this."
        ),
    }


def run_all(es: EdgeSet) -> dict:
    if not es.quarters:
        # Refuse rather than return a dict of zeros that reads like a finding.
        raise ValueError("run_all needs at least one ingested snapshot")
    lo, hi = es.stable_window()
    stable = es.clip(lo, hi)
    transitions = classify_transitions(es)
    frac = fractured_histories(es)

    counts: dict[str, int] = {}
    for t in transitions:
        counts[t.klass] = counts.get(t.klass, 0) + 1
    resolved: dict[str, int] = {}
    for t in transitions:
        resolved[t.resolved_by] = resolved.get(t.resolved_by, 0) + 1

    return {
        "headline": {
            "stable_regime": naive_join_cost(stable),
            "full_period": naive_join_cost(es),
        },
        "regime_artifacts": [es.quarters[i] for i in es.regime_artifacts()],
        "stable_window": f"{es.quarters[lo]}..{es.quarters[hi]}",
        "transitions": {"total": len(transitions), "by_class": counts, "by_resolver": resolved},
        "fractured_by_rename": len(frac["renamed"]),
        "concurrent_multi_ticker": len(frac["concurrent_multi_ticker"]),
        "survivorship": survivorship(es),
        "presence_drift": es.presence_drift(),
        "_transitions": [asdict(t) for t in transitions],
    }
