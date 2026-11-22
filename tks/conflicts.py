"""Contradiction detection over the versioned fact store.

A *fact* is a table row identified by (product caption, normalised row label).  Rows with the same
identity on different pages, or on the same page in different snapshots, are competing statements
of one fact.  Different tiers/terms/products have different labels and are therefore never treated
as conflicts ("respect qualifiers").

For every fact with more than one distinct value we emit ONE conflict listing all values seen,
the value the evidence hierarchy selects as current, and the deciding rule.

Cost: one pass over blocks O(B) + sorting each group; computed once and cached by the caller.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .engine import _norm_label, _resolution_key
from .store import Block, KnowledgeStore


@dataclass
class ConflictRecord:
    topic: str
    values: list[str]
    blocks: list[Block]
    resolution: Optional[str]
    reason: str


_PRIMARY_ORDER = ("apy", "apr", "rate", "dividend")


def _primary_value(b: Block) -> Optional[tuple[str, str]]:
    for kind in _PRIMARY_ORDER:
        for kd, _h, v in b.values:
            if kd == kind:
                return kind, v
    return None


def _canon(v: str) -> str:
    def one(x: str) -> str:
        return f"{float(x):.4g}"
    nums = re.findall(r"\d+(?:\.\d+)?", v)
    return "-".join(one(n) for n in nums)


def _reason(win: Block, others: list[Block], latest: int) -> str:
    dated_older = [o for o in others if o.eff_date and win.eff_date and o.eff_date < win.eff_date]
    if dated_older:
        return (f"newest effective date wins: {win.eff_date.isoformat()} on {win.url} supersedes "
                f"{min(o.eff_date for o in dated_older).isoformat()}")
    if any(o.authority < win.authority for o in others):
        return f"higher-authority source ({win.page_class} page {win.url}) over {others[0].page_class} page"
    if win.last_snapshot == latest:
        return "value present in the latest snapshot supersedes earlier snapshots"
    return "most recently observed value"


def find_conflicts(store: KnowledgeStore) -> list[ConflictRecord]:
    if not store.snapshots:
        return []
    latest = len(store.snapshots) - 1
    groups: dict[tuple[str, str, str], list[Block]] = defaultdict(list)
    for b in store.blocks:
        if b.kind != "row" or not b.values or b.page_class in ("junk", "listing"):
            continue
        pv = _primary_value(b)
        if pv is None:
            continue
        caption = _norm_label(b.caption.split(" / ")[-1]) if b.caption else ""
        groups[(caption, _norm_label(b.label), pv[0])].append(b)

    out: list[ConflictRecord] = []
    for (caption, label, kind), rows in groups.items():
        by_val: dict[str, list[Block]] = defaultdict(list)
        for b in rows:
            by_val[_canon(_primary_value(b)[1])].append(b)
        if len(by_val) < 2:
            continue
        current = [b for b in rows if b.active >> latest & 1]
        pool = current or rows
        win = max(pool, key=_resolution_key)
        win_val = _primary_value(win)[1]
        others = [b for b in rows if _canon(_primary_value(b)[1]) != _canon(win_val)]
        values = []
        for v, bs in sorted(by_val.items(), key=lambda kv: min(x.first_snapshot for x in kv[1])):
            values.append(_primary_value(bs[0])[1])
        reps = [win] + [max(bs, key=_resolution_key) for v, bs in by_val.items()
                        if v != _canon(win_val)]
        name = (f"{caption} {label}".strip() if caption and caption not in label else label)
        topic = f"{name} {kind.upper() if kind in ('apy', 'apr') else kind}"
        out.append(ConflictRecord(topic=topic, values=values, blocks=reps, resolution=win_val,
                                  reason=_reason(win, others, latest)))
    out.sort(key=lambda c: c.topic)
    return out
