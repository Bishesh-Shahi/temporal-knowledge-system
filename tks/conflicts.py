"""Contradiction detection over the versioned evidence store.

Two independent detectors, both deterministic and computed once:

1. **Table facts.**  Every table row is assigned a *fact identity*:
       (table family, row label with model-year bands abstracted, occurrence index, value kinds)
   A table family groups editions of the same table across pages and snapshots (rows mostly
   coincide once digits are abstracted, and captions share a product word).  The occurrence index
   aligns rows whose bands shift between editions ("2023-26 Model Year | 60 Months" in one
   edition is the same tier as "2024-27 Model Year | 60 Months" in the next).  Different terms,
   balance tiers, loan amounts or products produce different identities and are therefore never
   reported as conflicts.  A fact with more than one observed value is a conflict; its resolution
   is chosen with the same evidence hierarchy the answer engine uses (newest effective date among
   non-superseded tables in the latest snapshot, then authority, then recency).

2. **Narrative claims.**  Sentences are templated by replacing their numbers with '#'.  A
   template that recurs on several pages with different numbers (boilerplate such as "It has N
   employees and N branch office locations") is a contradiction between dated statements; the
   most recently published statement wins.

Cost: O(B) to group blocks plus O(T * peers) to cluster T table instances; cached by the caller.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .engine import (AnswerEngine, TableInfo, _attributable, _norm_label, _resolution_key,
                     same_table_edition, sentences)
from .query import RATE_WORDS, SOFT_WORDS
from .store import Block, KnowledgeStore
from .text import tokens

MIN_TEMPLATE_PAGES = 5          # boilerplate: a narrative template must recur on >= 5 pages
MAX_TEMPLATE_NUMBERS = 2


@dataclass
class ConflictRecord:
    topic: str
    values: list[str]
    blocks: list[Block]
    resolution: Optional[str]
    reason: str


# ============================================================================ table facts

def _value_string(b: Block) -> str:
    """All rate values of a row in a canonical order, e.g. '7.25% rate / 7.32% APR'."""
    order = {"rate": 0, "dividend": 1, "apr": 2, "apy": 3}
    seen, parts = set(), []
    for kind, _h, v in sorted(b.values, key=lambda x: order.get(x[0], 9)):
        if (kind, v) in seen:
            continue
        seen.add((kind, v))
        parts.append(f"{v.replace('-', ' to ')} {kind.upper() if kind in ('apy', 'apr') else kind}")
    return " / ".join(parts)


def _canon(b: Block) -> tuple:
    return tuple(sorted({f"{float(n):.4g}" for _k, _h, v in b.values for n in re.findall(r"\d+(?:\.\d+)?", v)}))


def _row_key(label: str) -> str:
    """Label with year bands abstracted but terms, tiers and amounts kept."""
    s = _norm_label(label)
    s = re.sub(r"\b(19|20)\d\d\s*-\s*\d{2,4}\b", "<years>", s)
    s = re.sub(r"\b(19|20)\d\d\b(?=\s+model)", "<years>", s)
    return s


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _table_families(store: KnowledgeStore) -> tuple[dict, dict]:
    """Cluster table instances (curl, caption, headers, eff_date, first_snapshot-insensitive)."""
    tables: dict[tuple, list[Block]] = defaultdict(list)
    for b in store.blocks:
        if (b.kind == "row" and b.values and b.label and b.page_class not in ("junk", "listing")
                and _attributable(b)):
            tables[(b.curl, b.caption, b.headers, b.eff_date)].append(b)
    info = {k: TableInfo(k[1], rows) for k, rows in tables.items()}
    by_sig: dict[str, list[tuple]] = defaultdict(list)
    for k, ti in info.items():
        for sg in ti.raw | ti.stripped:
            by_sig[sg].append(k)
    uf = _UnionFind()
    for a in tables:
        uf.find(a)
        for b in {x for sg in info[a].raw | info[a].stripped for x in by_sig[sg]}:
            if b == a:
                continue
            same_page = a[0] == b[0] and a[1] == b[1]
            if not same_page and not same_table_edition(info[a], info[b]):
                continue
            uf.union(a, b)
    return tables, {k: uf.find(k) for k in tables}


def _table_conflicts(store: KnowledgeStore) -> list[ConflictRecord]:
    latest = len(store.snapshots) - 1
    engine = AnswerEngine(store)
    stale = engine._superseded_tables(latest)
    tables, family = _table_families(store)

    facts: dict[tuple, list[Block]] = defaultdict(list)
    for tkey, rows in tables.items():
        # occurrence index among rows with the same key that were on the page *at the same time*
        # (several model-year bands of one edition), so successive editions of a row align
        seen: dict[str, list[Block]] = defaultdict(list)
        for r in sorted(rows, key=lambda b: b.bid):
            rk = _row_key(r.label)
            kinds = tuple(sorted({v[0] for v in r.values}))
            occ = sum(1 for o in seen[rk] if o.observed & r.observed)
            facts[(family[tkey], rk, occ, kinds)].append(r)
            seen[rk].append(r)

    out: list[ConflictRecord] = []
    bit = 1 << latest
    for (_fam, rk, _i, _kinds), rows in facts.items():
        by_val: dict[tuple, list[Block]] = defaultdict(list)
        for b in rows:
            by_val[_canon(b)].append(b)
        if len(by_val) < 2:
            continue
        current = [b for b in rows if b.active & bit
                   and (b.curl, b.caption, b.headers, b.eff_date) not in stale]
        pool = current or [b for b in rows if b.active & bit] or rows
        win = max(pool, key=_resolution_key)
        losers = [b for b in rows if _canon(b) != _canon(win)]
        ordered = sorted(by_val.values(), key=lambda bs: min(x.first_snapshot for x in bs))
        values = [_value_string(max(bs, key=_resolution_key)) for bs in ordered]
        reps = [win] + [max(bs, key=_resolution_key) for v, bs in by_val.items() if v != _canon(win)]
        out.append(ConflictRecord(topic=_topic(win), values=values, blocks=reps,
                                  resolution=_value_string(win), reason=_reason(win, losers, latest)))
    return out


def _topic(b: Block) -> str:
    cap = b.caption.split(" / ")[-1].strip() if b.caption else ""
    label = re.sub(r"\b(\d{1,3})\s*(month|year)s?\b", r"\1-\2", b.label, flags=re.I)
    kinds = sorted({v[0] for v in b.values}, key=lambda k: ("apy", "apr", "rate", "dividend").index(k)
                   if k in ("apy", "apr", "rate", "dividend") else 9)
    kind = kinds[0].upper() if kinds and kinds[0] in ("apy", "apr") else "rate"
    name = label if (not cap or set(tokens(cap)) <= set(tokens(label))) else f"{cap}: {label}"
    return f"{name} {kind}"


def _reason(win: Block, losers: list[Block], latest: int) -> str:
    older = sorted({o.eff_date for o in losers if o.eff_date and win.eff_date and o.eff_date < win.eff_date})
    if older:
        stale_pages = sorted({o.page_class for o in losers if o.active >> latest & 1 and o.curl != win.curl})
        extra = f"; stale copies remain on {', '.join(stale_pages)} pages" if stale_pages else ""
        return (f"newest effective date wins: {win.eff_date.isoformat()} on the {win.page_class} page "
                f"{win.url} supersedes {older[0].isoformat()}{extra}")
    if win.expires is None and any(o.expires for o in losers):
        return "the competing value is an expired promotion"
    if any(o.authority < win.authority for o in losers):
        return f"higher-authority source ({win.page_class} page {win.url}) over lower-authority copies"
    if win.last_snapshot == latest:
        return "the value on the latest snapshot supersedes earlier snapshots"
    return "most recently observed value"


# ============================================================================ narrative claims

_NUM = re.compile(r"(?<![\d.$/%])\b\d{1,3}(?:,\d{3})*\b(?![.%/]\d|%)")
# approximators are wording, not content: "over N" / "nearly N" / "more than N" state the same claim
_HEDGE = re.compile(r"\b(?:more than|over|nearly|almost|about|approximately|around|roughly|some)\s+(?=#)")
_YEARISH = re.compile(r"\b(19|20)\d\d\b")


def _narrative_conflicts(store: KnowledgeStore) -> list[ConflictRecord]:
    latest = len(store.snapshots) - 1
    groups: dict[str, list[tuple[tuple, Block, str]]] = defaultdict(list)
    for b in store.blocks:
        if b.kind != "text" or b.page_class in ("junk", "listing"):
            continue
        for s in sentences(b.text):
            if not (30 <= len(s) <= 220) or _YEARISH.search(s) or "%" in s or "$" in s:
                continue
            nums = _NUM.findall(s)
            if not nums or len(nums) > MAX_TEMPLATE_NUMBERS:
                continue
            tpl = _HEDGE.sub("~", _NUM.sub("#", s.lower()))
            groups[tpl].append((tuple(nums), b, s))
    out: list[ConflictRecord] = []
    for tpl, items in groups.items():
        pages = {b.curl for _n, b, _s in items}
        values = {n for n, _b, _s in items}
        if len(pages) < MIN_TEMPLATE_PAGES or len(values) < 2:
            continue
        if not [t for t in tokens(tpl) if t.isalpha() and t not in SOFT_WORDS]:
            continue
        # winner: the most recently published statement still on the site
        cur = [it for it in items if it[1].active >> latest & 1] or items
        win = max(cur, key=lambda it: (it[1].published or date.min, it[1].authority, it[1].last_snapshot))
        order = sorted(values, key=lambda n: min((it[1].published or date.max) for it in items if it[0] == n))
        reps = [win[1]] + [max((it for it in items if it[0] == n), key=lambda it: it[1].published or date.min)[1]
                           for n in order if n != win[0]]
        when = win[1].published.isoformat() if win[1].published else "undated"
        out.append(ConflictRecord(
            topic=tpl.replace("#", "N").strip(" ."),
            values=[" / ".join(n) for n in order], blocks=reps, resolution=" / ".join(win[0]),
            reason=f"most recently published statement ({when}, {win[1].url}) supersedes older press "
                   f"releases repeating outdated boilerplate"))
    return out


def find_conflicts(store: KnowledgeStore) -> list[ConflictRecord]:
    if not store.snapshots:
        return []
    out = _table_conflicts(store) + _narrative_conflicts(store)
    out.sort(key=lambda c: c.topic)
    return out
