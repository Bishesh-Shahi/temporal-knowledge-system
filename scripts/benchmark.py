"""Measure ingest time, peak memory, index size and answer latency.

    python scripts/benchmark.py --snapshots <kit>/snapshots --questions <kit>/eval/public_eval.jsonl

Prints a JSON report.  Uses only the standard library (time.perf_counter, tracemalloc).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.kit import load_snapshots  # noqa: E402
from solution import build  # noqa: E402


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=Path, required=True)
    ap.add_argument("--questions", type=Path, nargs="+", required=True)
    ap.add_argument("--memory", action="store_true", help="trace peak memory (slows ingest ~2-3x)")
    args = ap.parse_args()

    snaps = load_snapshots(args.snapshots)
    if args.memory:
        tracemalloc.start()
    ks = build()
    t0 = time.perf_counter()
    per_snapshot = []
    for s in snaps:
        t = time.perf_counter()
        ks.ingest(s)
        per_snapshot.append({"snapshot": s.snapshot_id, "pages": len(s.pages),
                             "seconds": round(time.perf_counter() - t, 2)})
    ingest_s = time.perf_counter() - t0
    peak_mb = tracemalloc.get_traced_memory()[1] / 1e6 if args.memory else None
    if args.memory:
        tracemalloc.stop()

    items = [json.loads(line) for f in args.questions for line in f.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    lat = []
    for it in items:
        t = time.perf_counter()
        ks.answer(it["question"], it.get("as_of"))
        lat.append(time.perf_counter() - t)
    t = time.perf_counter()
    conflicts = ks.conflicts()
    conflicts_s = time.perf_counter() - t

    st = ks.store
    report = {
        "snapshots": per_snapshot,
        "ingest_seconds_total": round(ingest_s, 2),
        "peak_memory_mb_during_ingest": None if peak_mb is None else round(peak_mb, 1),
        "unique_blocks": len(st.blocks),
        "table_row_blocks": sum(b.kind == "row" for b in st.blocks),
        "index_terms": len(st._postings),
        "questions": len(items),
        "answer_latency_ms": {
            "mean": round(1000 * statistics.mean(lat), 2),
            "p50": round(1000 * pct(lat, 0.50), 2),
            "p95": round(1000 * pct(lat, 0.95), 2),
            "max": round(1000 * max(lat), 2),
        },
        "conflicts": len(conflicts),
        "conflicts_seconds": round(conflicts_s, 2),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
