"""
End-to-end run.

    python -m tickerlineage.pipeline --out data/ --user-agent "Name you@example.com"
    ticker-lineage --out data/ ...          (after `pip install -e .`)

Resumable: every fetch is cached on disk by URL, so an interrupted run picks
up where it stopped and costs nothing to repeat.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

from .analyze import run_all
from .build import EdgeSet
from .fetch import Fetcher


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the temporal identifier graph from SEC data.")
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--cache", type=Path, default=Path(".cache"))
    ap.add_argument("--user-agent", default=None,
                    help="SEC requires 'Name email@example.com'. Defaults to $SEC_USER_AGENT; "
                         "there is no built-in fallback.")
    ap.add_argument("--limit-snapshots", type=int, default=0, help="0 = all quarterly snapshots")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("pipeline")
    args.out.mkdir(parents=True, exist_ok=True)

    f = Fetcher(args.cache, user_agent=args.user_agent)

    log.info("discovering snapshots")
    snaps = f.quarterly(f.list_snapshots())
    if args.limit_snapshots:
        snaps = snaps[: args.limit_snapshots]
    if not snaps:
        raise SystemExit("no snapshots found in the Wayback index - nothing to analyse")
    log.info("%d quarterly snapshots: %s .. %s", len(snaps), snaps[0].quarter, snaps[-1].quarter)

    es = EdgeSet()
    for i, s in enumerate(snaps):
        rows = f.ticker_map(s)
        es.ingest(i, s.quarter, rows)
        log.info("[%2d] %s %s  %d rows", i, s.quarter, s.date, len(rows))

    log.info("analysing")
    results = run_all(es)
    transitions = results.pop("_transitions")

    (args.out / "findings_generated.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    with (args.out / "ticker_transitions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(transitions[0].keys())) if transitions else None
        if w:
            w.writeheader()
            w.writerows(sorted(transitions, key=lambda r: (r["ticker"], r["switch_quarter"])))

    with (args.out / "identifier_edges.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "cik", "title", "i0", "i1", "source", "confidence"])
        w.writeheader()
        w.writerows(sorted(es.to_rows(), key=lambda r: (r["ticker"], r["i0"])))

    labels = es.regime_labels()
    with (args.out / "snapshots.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["i", "quarter", "wayback_date", "file_rows", "regime"])
        for i, s in enumerate(snaps):
            w.writerow([i, s.quarter, s.date, es.file_rows[i], labels[i]])

    h = results["headline"]
    print("\n" + "=" * 66)
    print(f"  stable regime {h['stable_regime']['window']}: "
          f"{h['stable_regime']['pct_misattributed']}% of ticker-quarter observations")
    print(f"  misattributed by a naive ticker join "
          f"({h['stable_regime']['misattributed']:,} of {h['stable_regime']['observations']:,})")
    print(f"  full period   {h['full_period']['window']}: "
          f"{h['full_period']['pct_misattributed']}%  <- inflated, see regime artifacts")
    print(f"  regime artifacts: {results['regime_artifacts']}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    main()
