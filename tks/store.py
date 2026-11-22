"""Versioned, deduplicated evidence store with per-snapshot views and a BM25 index.

Temporal model
--------------
* Snapshots are ingested oldest -> newest and numbered 0..n-1.
* Each URL keeps a list of *versions*; a page whose markdown hash is unchanged since its previous
  version is not re-parsed (its blocks are simply re-observed).
* A *block* is identified by (canonical url, content signature).  Identical blocks across
  versions are stored once and carry two bitmasks over snapshots:
    - ``observed``: the block was literally present in that crawl;
    - ``active``:   the block is part of the website *as it stood* at that snapshot, i.e. it belongs
                    to the latest version of its page at or before that snapshot.  A page missing
                    from a later crawl is carried forward (crawls are partial; absence is not
                    evidence of deletion), but blocks that disappear from a re-crawled page stop
                    being active (they were superseded).
* ``view(k)`` is therefore a bit test, so temporal filtering costs O(1) per candidate.
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .pages import DATED_CLASSES, authority, canonical_url, classify
from .parse import RawBlock, expiry_date, parse_page
from .text import parse_iso, tokens

BM25_K1 = 1.2
BM25_B = 0.75
BOILERPLATE_PAGE_SHARE = 0.35   # a line on >35% of a crawl's pages is site chrome


@dataclass
class Block:
    bid: int
    url: str                     # original URL as crawled (for citations)
    curl: str                    # canonical URL (identity)
    page_class: str
    authority: float
    page_title: str
    kind: str
    heading: str
    text: str
    links: list[tuple[str, str]]
    caption: str
    headers: tuple[str, ...]
    cells: tuple[str, ...]
    label: str
    values: tuple[tuple[str, str, str], ...]
    eff_date: Optional[date]
    published: Optional[date]
    expires: Optional[date] = None
    observed: int = 0
    active: int = 0
    first_snapshot: int = -1
    last_snapshot: int = -1
    terms: Counter = field(default_factory=Counter)
    length: int = 0


@dataclass
class SnapshotInfo:
    index: int
    snapshot_id: str
    taken_on: date


class KnowledgeStore:
    def __init__(self) -> None:
        self.snapshots: list[SnapshotInfo] = []
        self.blocks: list[Block] = []
        self._block_by_sig: dict[tuple[str, str], int] = {}
        # curl -> list of (snapshot index, page hash, [block ids])
        self.versions: dict[str, list[tuple[int, str, list[int]]]] = defaultdict(list)
        self.page_meta: dict[str, dict] = {}
        self._host_counts: Counter = Counter()
        self._parsed_by_hash: dict[str, tuple] = {}
        # index (built lazily)
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._avgdl = 1.0
        self._indexed = False

    # ------------------------------------------------------------------ ingestion
    def ingest(self, snapshot_id: str, taken_on: str, pages: Iterable) -> None:
        pages = list(pages)
        taken = parse_iso(taken_on)
        if taken is None:
            raise ValueError(f"snapshot {snapshot_id!r} has an invalid taken_on date: {taken_on!r}")
        k = len(self.snapshots)
        if self.snapshots and taken < self.snapshots[-1].taken_on:
            raise ValueError("snapshots must be ingested oldest first")
        self.snapshots.append(SnapshotInfo(k, snapshot_id, taken))
        drop = self._boilerplate_lines(pages)
        seen_in_crawl: set[str] = set()
        for page in pages:
            url = getattr(page, "url", None)
            md = getattr(page, "markdown", None)
            if not url or not isinstance(md, str):
                continue                       # malformed page: skip, never crash ingestion
            curl = canonical_url(url)
            self._host_counts[curl.split("/", 1)[0]] += 1
            if curl in seen_in_crawl:
                continue
            seen_in_crawl.add(curl)
            title = getattr(page, "title", "") or ""
            cls = classify(url, title)
            phash = hashlib.sha1(md.encode("utf-8", "replace")).hexdigest()
            prev = self.versions[curl][-1] if self.versions[curl] else None
            if prev and prev[1] == phash:
                bids = prev[2]
            else:
                bids = self._add_page(url, curl, title, cls, md, phash, drop, k)
            self.versions[curl].append((k, phash, bids))
            for b in bids:
                blk = self.blocks[b]
                blk.observed |= 1 << k
                if blk.first_snapshot < 0:
                    blk.first_snapshot = k
                blk.last_snapshot = k
            self.page_meta[curl] = {"url": url, "title": title, "class": cls,
                                    "fetched_at": getattr(page, "fetched_at", "")}
        # build view k: latest version of every page at or before k (carry forward missing pages)
        for curl, vers in self.versions.items():
            for b in vers[-1][2]:
                self.blocks[b].active |= 1 << k
        self._indexed = False

    @staticmethod
    def _boilerplate_lines(pages: list) -> frozenset[str]:
        counts: Counter = Counter()
        for p in pages:
            md = getattr(p, "markdown", "") or ""
            counts.update({ln.strip() for ln in md.splitlines() if 0 < len(ln.strip()) < 300})
        limit = max(5, BOILERPLATE_PAGE_SHARE * len(pages))
        return frozenset(ln for ln, c in counts.items() if c > limit and not ln.startswith(("|", "#")))

    def _add_page(self, url: str, curl: str, title: str, cls: str, md: str, phash: str,
                  drop: frozenset[str], k: int) -> list[int]:
        parsed = parse_page(md, drop, dated=cls in DATED_CLASSES,
                            inline_facts=cls in ("rates", "product", "info", "landing"))
        bids: list[int] = []
        for rb in parsed.blocks:
            sig = self._signature(rb, parsed.published)
            key = (curl, sig)
            bid = self._block_by_sig.get(key)
            if bid is None:
                bid = len(self.blocks)
                self._block_by_sig[key] = bid
                self.blocks.append(self._make_block(bid, url, curl, cls, title, parsed.h1, rb,
                                                    parsed.published))
            if bid not in bids:
                bids.append(bid)
        return bids

    @staticmethod
    def _signature(rb: RawBlock, published: Optional[date]) -> str:
        raw = "\x1f".join([rb.kind, rb.heading, rb.text, str(rb.eff_date), str(published)])
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _make_block(bid: int, url: str, curl: str, cls: str, title: str, h1: str, rb: RawBlock,
                    published: Optional[date]) -> Block:
        blk = Block(bid=bid, url=url, curl=curl, page_class=cls, authority=authority(cls),
                    page_title=h1 or title, kind=rb.kind, heading=rb.heading, text=rb.text,
                    links=rb.links, caption=rb.caption, headers=rb.headers, cells=rb.cells,
                    label=rb.label, values=rb.values, eff_date=rb.eff_date, published=published,
                    expires=expiry_date(rb.text) if rb.kind == "row" else None)
        # index text: headings/captions repeated once more to weight context terms
        ctx = f"{blk.page_title} {rb.heading} {rb.caption}"
        terms = tokens(f"{ctx} {rb.caption} {rb.text}")
        blk.terms = Counter(terms)
        blk.length = len(terms)
        return blk

    # ------------------------------------------------------------------ site identity
    @property
    def site_root(self) -> str:
        """Registrable domain of the crawled site, e.g. 'dupaco.com' (derived, not configured)."""
        if not self._host_counts:
            return ""
        host = self._host_counts.most_common(1)[0][0]
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    @property
    def org_label(self) -> str:
        """Lower-case organisation token derived from the domain ('dupaco')."""
        return self.site_root.split(".")[0] if self.site_root else ""

    @property
    def org_name(self) -> str:
        label = self.org_label
        return label.replace("-", " ").title() if label else "the credit union"

    # ------------------------------------------------------------------ views
    def view_index(self, as_of: Optional[date]) -> Optional[int]:
        """Latest snapshot taken on/before `as_of` (None -> latest). None if before coverage."""
        if not self.snapshots:
            return None
        if as_of is None:
            return len(self.snapshots) - 1
        best = None
        for s in self.snapshots:
            if s.taken_on <= as_of:
                best = s.index
        return best

    def active_blocks(self, k: int) -> Iterable[Block]:
        bit = 1 << k
        return (b for b in self.blocks if b.active & bit)

    # ------------------------------------------------------------------ BM25
    def build_index(self) -> None:
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        total = 0
        for b in self.blocks:
            total += b.length
            for t, tf in b.terms.items():
                postings[t].append((b.bid, tf))
        self._postings = dict(postings)
        self._avgdl = total / max(1, len(self.blocks))
        self._n = len(self.blocks)
        self._idf = {t: math.log(1 + (self._n - len(p) + 0.5) / (len(p) + 0.5))
                     for t, p in self._postings.items()}
        self._indexed = True

    def ensure_index(self) -> None:
        if not self._indexed:
            self.build_index()

    def idf(self, term: str) -> float:
        self.ensure_index()
        return self._idf.get(term, math.log(1 + (self._n + 0.5) / 0.5))

    def search(self, query_terms: list[str], k: int, limit: int = 50,
               kinds: Optional[set[str]] = None) -> list[tuple[float, Block]]:
        """BM25 over blocks active in view k.  Cost: O(sum of posting lengths of query terms)."""
        self.ensure_index()
        bit = 1 << k
        scores: dict[int, float] = defaultdict(float)
        for t in set(query_terms):
            plist = self._postings.get(t)
            if not plist:
                continue
            idf = self._idf[t]
            for bid, tf in plist:
                b = self.blocks[bid]
                if not (b.active & bit) or (kinds and b.kind not in kinds):
                    continue
                norm = tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * b.length / self._avgdl))
                scores[bid] += idf * norm
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:limit]
        return [(s, self.blocks[bid]) for bid, s in ranked]
