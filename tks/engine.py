"""Answering: route a parsed question to an evidence strategy, resolve, and compose.

Evidence hierarchy (applied identically by answers and by conflicts()):
  1. explicit "Rates are effective as of" date of the table   (newest wins)
  2. source authority of the page class                        (rate sheet > product > ... > calculator)
  3. observation recency                                       (last snapshot the row was seen in)
Narrative claims (counts, ordinals, events) are ordered by the article's own publish date.

Answer text is composed ONLY from the selected evidence values, never from losing candidates:
mentioning a superseded value is exactly the failure the task is about.  Losing values are reported
in `conflict_note` instead.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .query import (DEFAULT_PRODUCT_HINTS, RATE_WORDS, SOFT_WORDS, Query, expand, parse_question)
from .store import Block, KnowledgeStore
from .text import (LONG_DATE_RE, all_long_dates, fmt_long, money_range, term_months, tokens,
                   year_range)

REFUSAL_TEXT = ("I don't know. That information isn't listed on the website, so I can't "
                "confirm it. Please contact the credit union directly for help with this question.")
OUT_OF_RANGE_TEXT = ("I don't know. My copies of the website don't go back that far, so I "
                     "can't confirm how it stood then. Please contact the credit union directly.")

MIN_TEXT_COVERAGE = 0.5        # share of (idf-weighted) question terms an evidence block must contain
LIST_KEEP_RATIO = 0.75         # list questions keep row groups scoring >= ratio * best
TIE_EPS = 1e-6


@dataclass
class Evidence:
    text: str
    blocks: list[Block]
    note: Optional[str] = None
    confident: bool = True


# ============================================================================ helpers

def _norm_label(label: str) -> str:
    s = label.lower()
    s = re.sub(r"(\d),(\d{3})", r"\1\2", s)
    s = re.sub(r"\.00\b", "", s)
    s = re.sub(r"[$*:()]", " ", s)
    s = re.sub(r"\s*[-–]\s*", "-", s)
    s = re.sub(r"\bmonths?\b", "month", s)
    return re.sub(r"\s+", " ", s).strip(" |")


def _label_shape(label: str) -> str:
    """Row label with every number abstracted: '2023-26 Model Year | 60 Months' -> '# model year | # month'."""
    s = _norm_label(label)
    s = re.sub(r"\d[\d,.]*", "#", s)
    return re.sub(r"#(?:\s*-\s*#)+", "#", s)


def _resolution_key(b: Block) -> tuple:
    return (b.eff_date or date.min, b.authority, b.last_snapshot, -b.bid)


def _fmt_value(v: str) -> str:
    return v.replace("-", " to ")


_KIND_LABEL = {"apy": "APY", "apr": "APR", "dividend": "dividend rate", "rate": "rate"}


def _values_text(b: Block, kinds: set[str]) -> str:
    vals = [v for v in b.values if v[0] in kinds] if kinds else []
    if not vals:
        vals = list(b.values)
    parts, seen = [], set()
    for kind, _h, val in vals:
        key = (kind, val)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{_fmt_value(val)} {_KIND_LABEL.get(kind, kind)}")
    return ", ".join(parts)


def _row_name(b: Block) -> str:
    cap = b.caption.split(" / ")[-1].strip() if b.caption else ""
    label = b.label
    if cap and not set(tokens(cap)) <= set(tokens(label)):
        return f"{cap} ({label})" if label else cap
    return label or cap


_ABBREV = re.compile(r"(?:\b(?:ln|st|rd|dr|ave|blvd|inc|co|corp|jr|sr|mr|mrs|ms|no|vs|etc|e\.g|i\.e|"
                     r"a\.m|p\.m|u\.s|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)|\b[A-Z])\.$", re.I)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'$(])", text)
    out: list[str] = []
    for p in parts:
        if out and _ABBREV.search(out[-1]):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [s.strip() for s in out if s.strip()]


# ============================================================================ engine

class AnswerEngine:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self._superseded_cache: dict[int, set] = {}
        self._link_pop_cache: dict[int, Counter] = {}

    # ---------------------------------------------------------------- entry point
    def answer(self, question: str, as_of: Optional[str] = None) -> tuple[Evidence, Optional[int]]:
        q = parse_question(question)
        if not q.terms:
            return Evidence(REFUSAL_TEXT, [], confident=False), None
        # the organisation's own name never identifies a product or an entity
        org_terms = set(tokens(self.store.org_name)) | {self.store.org_label}
        q.product_terms = [t for t in q.product_terms if t not in org_terms]
        q.required = [t for t in q.required if t not in org_terms]
        q.entities = [t for t in q.entities if t not in org_terms]
        if self.store.org_name.lower() in q.lower and self.store.org_label not in q.lower:
            q.lower = q.lower.replace(self.store.org_name.lower(), self.store.org_label)
        as_of_d = _parse_date(as_of)
        ref = as_of_d
        if ref is None and q.ref_date is not None:
            ref = q.ref_date
        k = self.store.view_index(ref)
        if k is None:
            return Evidence(OUT_OF_RANGE_TEXT, [], confident=False), None
        if q.is_still or ((q.is_yesno or q.lower.startswith("when"))
                          and re.search(r"\b(open|close|closed|closing|end|ends|expire|expires)\b", q.lower)):
            ev = self._still_open(q, k, ref)
            if ev:
                return ev, k
        for route in self._routes(q):
            ev = route(q, k)
            if ev is not None:
                return ev, k
        return Evidence(REFUSAL_TEXT, [], confident=False), k

    def _routes(self, q: Query):
        if q.is_url:
            return [self._url_answer, self._text_answer]
        if q.is_hours:
            return [self._hours_answer]
        if q.is_count:
            return [self._count_answer, self._text_answer]
        if q.is_rate:
            return [self._rate_answer]
        return [self._text_answer]

    # ================================================================ rates (table rows)
    def _row_context(self, b: Block) -> set[str]:
        title = "" if b.caption else b.page_title
        return set(tokens(f"{title} {b.heading} {b.caption} {b.label} {' '.join(b.headers)}"))

    def _superseded_tables(self, k: int) -> set:
        """Tables (page, caption, headers, eff_date) that are stale copies of a table family whose
        newer edition (later effective date) exists on another page in the same view."""
        if k in self._superseded_cache:
            return self._superseded_cache[k]
        tables: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.store.active_blocks(k):
            if b.kind == "row" and b.values:
                tables[(b.curl, b.caption, b.headers, b.eff_date)].append(b)
        # Two tables on different pages are editions of the same table when (a) most of their row
        # labels coincide once digits are abstracted (tier bands like "2023-26" vs "2024-27" shift
        # between editions) and (b) their captions name a common product word.
        sigs, caps = {}, {}
        for key, rows in tables.items():
            sigs[key] = {_label_shape(r.label) for r in rows}
            caps[key] = set(tokens(" ".join(key[1].split(" / ")[-2:]))) - RATE_WORDS - SOFT_WORDS
        by_sig: dict[str, list[tuple]] = defaultdict(list)
        for key, ss in sigs.items():
            for sg in ss:
                by_sig[sg].append(key)
        stale: set = set()
        for a in tables:
            peers = {b for sg in sigs[a] for b in by_sig[sg] if b[0] != a[0]}
            for b in peers:
                sa, sb = sigs[a], sigs[b]
                if len(sa & sb) / min(len(sa), len(sb)) < 0.5:
                    continue
                if caps[a] and caps[b] and not (caps[a] & caps[b]):
                    continue
                da, db = a[3], b[3]
                auth_a, auth_b = tables[a][0].authority, tables[b][0].authority
                if db and ((da is None and auth_b >= auth_a) or (da is not None and db > da)):
                    stale.add(a)
        self._superseded_cache[k] = stale
        return stale

    def _score_row(self, q: Query, b: Block, ctx: set[str]) -> Optional[float]:
        idf = self.store.idf
        for t in q.required:
            if not any(e in ctx for e in expand(t)):
                return None
        label = b.label
        score = 0.0
        # slot constraints (hard filters) and bonuses
        if q.term_months is not None:
            rt = term_months(label)
            if rt is not None:
                if rt != q.term_months:
                    return None
                score += 3.0
        if q.amounts:
            rr = money_range(label)
            if rr is not None:
                if not all(rr[0] - 0.5 <= a <= rr[1] + 0.995 for a in q.amounts):
                    return None
                width = rr[1] - rr[0]
                score += 3.0 + (1.0 / (1.0 + math.log10(1 + width)) if math.isfinite(width) else 0.0)
        if q.model_year is not None:
            yr = year_range(label)
            if yr is not None:
                if not (yr[0] <= q.model_year <= yr[1]):
                    return None
                score += 3.0
        qexp: set[str] = set()
        for t in q.product_terms:
            ex = expand(t)
            qexp.update(ex)
            if any(e in ctx for e in ex):
                score += idf(t) * (0.1 if t in SOFT_WORDS else 1.0)
        hints = {h for t in q.product_terms for h in DEFAULT_PRODUCT_HINTS.get(t, ())}
        label_toks = {t for t in tokens(label) if t.isalpha()} - SOFT_WORDS - RATE_WORDS
        for h in hints:
            if h in label_toks:
                score += 0.5 * idf(h)
        if not q.is_list:
            extra = label_toks - qexp - hints
            score -= 0.5 * sum(idf(t) for t in extra)
        return score

    def _rate_answer(self, q: Query, k: int) -> Optional[Evidence]:
        stale = self._superseded_tables(k)
        view_date = self.store.snapshots[k].taken_on
        wants_fee = bool(_FEE_RE.search(q.lower))
        scored: list[tuple[float, Block]] = []
        for b in self.store.active_blocks(k):
            if b.kind != "row" or not b.values or not (b.label or b.caption):
                continue
            if b.expires is not None and b.expires < view_date:
                continue              # an offer that had already ended is not a current rate
            if not wants_fee and _FEE_RE.search(f"{b.caption} {b.label}"):
                continue              # fee/payment percentages are not interest rates
            if not _attributable(b):
                continue              # a bare tier row whose product cannot be identified
            s = self._score_row(q, b, self._row_context(b))
            if s is not None:
                scored.append((s, b))
        if not scored:
            return Evidence(REFUSAL_TEXT, [], confident=False)
        live = [(s, b) for s, b in scored if (b.curl, b.caption, b.headers, b.eff_date) not in stale]
        dropped = [b for s, b in scored if (b.curl, b.caption, b.headers, b.eff_date) in stale]
        groups: dict[str, list[tuple[float, Block]]] = defaultdict(list)
        for s, b in live:
            groups[_norm_label(b.label) + "§" + _norm_label(b.caption.split(" / ")[-1])].append((s, b))
        gscore = {g: max(s for s, _ in rows) for g, rows in groups.items()}
        best = max(gscore.values())
        if q.is_list:
            floor = best - abs(best) * (1 - LIST_KEEP_RATIO) - TIE_EPS
            chosen = [g for g in groups if gscore[g] >= floor]
        else:
            chosen = [g for g in groups if gscore[g] >= best - TIE_EPS]
        winners: list[Block] = []
        notes = []
        for g in chosen:
            rows = [b for _, b in groups[g]]
            win = max(rows, key=_resolution_key)
            winners.append(win)
            losers = {(_values_text(b, q.rate_kinds), b.eff_date) for b in rows
                      if _values_text(b, q.rate_kinds) != _values_text(win, q.rate_kinds)}
            if losers:
                notes.append(f"{_row_name(win)}: other copies show "
                             + "; ".join(f"{v} (effective {d})" for v, d in sorted(losers, key=str))
                             + f"; used the value effective {win.eff_date} from the higher-priority source.")
        stale_vals = sorted({_values_text(b, q.rate_kinds) for b in dropped} -
                            {_values_text(w, q.rate_kinds) for w in winners})
        if stale_vals:
            notes.append("Older copies on other pages (superseded by a later effective date) show: "
                         + "; ".join(stale_vals) + ".")
        winners = self._order_rows(winners, q)
        if q.superlative and winners:
            winners = [self._extreme(winners, q)]
        if q.asked_percents and q.is_yesno:
            return self._percent_check(q, winners, notes)
        text = self._compose_rows(winners, q)
        support = self._supporting_rows(winners, live)
        return Evidence(text, support, note=" ".join(notes) or None)

    def _order_rows(self, rows: list[Block], q: Query) -> list[Block]:
        seen, out = set(), []
        for b in sorted(rows, key=lambda b: (b.curl, b.bid)):
            key = (_row_name(b), _values_text(b, set()))
            if key not in seen:
                seen.add(key)
                out.append(b)
        # a row whose name is a strict subset of another row's name with identical values is the
        # same tier shown with less context ("$100,000+" vs "1st Rate Checking | $100,000+")
        # (a personal row is never folded into a business row: they are different products)
        named = [(b, set(tokens(_row_name(b))), _values_text(b, set())) for b in out]
        out = [b for b, nb, vb in named
               if not any(vb == vc and nb < nc and (_is_business(b) or not _is_business(c))
                          for c, nc, vc in named if c is not b)]
        if "business" not in q.lower:
            # the member did not ask about business accounts: a business row that only repeats a
            # personal row's values adds nothing; business products with their own rates are kept
            personal = {(_strip_segment(_row_name(b)), _values_text(b, set()))
                        for b in out if not _is_business(b)}
            out = [b for b in out if not _is_business(b)
                   or (_strip_segment(_row_name(b)), _values_text(b, set())) not in personal]
        # personal before business, then the site's own table order (block id within a page)
        return sorted(out, key=lambda b: (_is_business(b), b.bid))

    def _extreme(self, rows: list[Block], q: Query) -> Block:
        def val(b: Block) -> float:
            nums = [float(re.findall(r"\d+(?:\.\d+)?", v)[-1]) for kd, _h, v in b.values
                    if not q.rate_kinds or kd in q.rate_kinds] or [0.0]
            return max(nums)
        return (max if q.superlative == "high" else min)(rows, key=lambda b: (val(b), -b.bid))

    def _percent_check(self, q: Query, rows: list[Block], notes: list[str]) -> Evidence:
        asked = {float(p.rstrip("%")) for p in q.asked_percents}
        hits = [b for b in rows if any(float(re.findall(r"\d+(?:\.\d+)?", v)[-1]) in asked
                                      for _k, _h, v in b.values)]
        if hits:
            return Evidence("Yes. " + self._compose_rows(hits, q), hits, note=" ".join(notes) or None)
        top = self._extreme(rows, Query(**{**q.__dict__, "superlative": "high"}))
        text = (f"No. The highest is {_values_text(top, q.rate_kinds)} for {_row_name(top)}.")
        return Evidence(text, [top], note=" ".join(notes) or None)

    def _compose_rows(self, rows: list[Block], q: Query) -> str:
        if not rows:
            return REFUSAL_TEXT
        parts = [f"{_row_name(b)}: {_values_text(b, q.rate_kinds)}" for b in rows]
        effs = sorted({b.eff_date for b in rows if b.eff_date})
        tail = f" (rates effective as of {effs[-1].strftime('%m/%d/%Y')})" if len(effs) == 1 else ""
        if len(parts) == 1:
            return f"{parts[0]}{tail}."
        return "; ".join(parts) + f"{tail}."

    def _supporting_rows(self, winners: list[Block], live: list[tuple[float, Block]]) -> list[Block]:
        out = list(winners)
        sig = {(_norm_label(w.label), _values_text(w, set())) for w in winners}
        for _s, b in live:
            if (_norm_label(b.label), _values_text(b, set())) in sig and b not in out:
                out.append(b)
        return out

    # ================================================================ narrative text
    def _coverage(self, q: Query, terms) -> float:
        idf = self.store.idf
        want = [t for t in dict.fromkeys(q.product_terms)]
        if not want:
            return 0.0
        # generic words ("link", "use", "still", "offer") barely count: coverage measures whether
        # the evidence talks about the *thing* asked about
        w = {t: idf(t) * (0.1 if t in SOFT_WORDS else 1.0) for t in want}
        tot = sum(w.values())
        got = sum(w[t] for t in want if any(e in terms for e in expand(t)))
        return got / tot if tot else 0.0

    def _entities_ok(self, q: Query, b: Block) -> bool:
        if not q.entities:
            return True
        terms = b.terms
        return all(e in terms for e in q.entities)

    def _ranked_text(self, q: Query, k: int, limit: int = 60) -> list[tuple[float, float, Block]]:
        qterms = [e for t in q.product_terms for e in expand(t)] or q.terms
        hits = self.store.search(qterms, k, limit=limit)
        if not hits:
            return []
        top = hits[0][0]
        ranked = []
        for s, b in hits:
            if b.page_class in ("junk", "listing"):
                continue
            cov = self._coverage(q, b.terms)
            ent = self._entities_ok(q, b)
            final = (s / top) * (0.6 + 0.4 * b.authority) + 0.8 * cov + (0.0 if ent else -2.0)
            ranked.append((final, cov, b))
        ranked.sort(key=lambda x: (-x[0], x[2].bid))
        return ranked

    def _pick_recent(self, ranked: list[tuple[float, float, Block]]) -> tuple[float, float, Block]:
        """Among near-equal candidates, a dated article supersedes an older dated article."""
        best = ranked[0]
        if best[2].published is None:
            return best
        close = [r for r in ranked[:12] if r[0] >= best[0] - 0.35 and r[1] >= best[1] - 1e-9
                 and r[2].published is not None]
        return max(close, key=lambda r: (r[2].published, r[0]))

    def _best_passage(self, q: Query, blocks: list[Block]) -> tuple[str, Block]:
        """Best 1-2 sentence window across the given blocks (all from the chosen page)."""
        best = None
        definitional = bool(re.match(r"(what|who) (is|are)\b|tell me about|describe|explain", q.lower))
        for rank, b in enumerate(blocks):
            text, key = self._best_sentences(q, b, with_key=True)
            if definitional:
                # a definition is where the page first introduces the subject: on equal relevance,
                # prefer the earliest passage of the page
                key = (key[0], -b.bid) + key[1:]
            else:
                key = (key[0] - 0.05 * rank, 0) + key[1:]
            if best is None or key > best[0]:
                best = (key, text, b)
        return best[1], best[2]

    def _best_sentences(self, q: Query, b: Block, max_chars: int = 420, with_key: bool = False):
        sents = sentences(b.text)
        idf = self.store.idf
        if len(sents) <= 1 or len(b.text) <= 220:
            qset = {e for t in q.product_terms for e in expand(t)} | set(q.terms)
            st = set(tokens(b.text))
            mass = sum(idf(t) for t in qset if t in st)
            return (b.text, (mass, 0, 0)) if with_key else b.text
        qset = {e for t in q.product_terms for e in expand(t)} | set(q.terms)
        org = self.store.org_label
        about_org = bool(org) and org in q.lower
        want_date = q.lower.startswith("when")
        definitional = bool(re.match(r"(what|who) (is|are)\b|tell me about|describe|explain", q.lower))
        sent_toks = [set(tokens(s)) for s in sents]
        best_key, best_win = None, (0, 1)
        # score windows of 1-2 consecutive sentences: distinct query-term mass, then specificity
        for i in range(len(sents)):
            for w in (1, 2):
                j = min(len(sents), i + w)
                if j - i < w or len(" ".join(sents[i:j])) > max_chars and w > 1:
                    continue
                toks = set().union(*sent_toks[i:j])
                mass = sum(idf(t) * (0.1 if t in SOFT_WORDS else 1.0) for t in qset if t in toks)
                bonus = 0.0
                window = " ".join(sents[i:j])
                if about_org and org in window.lower():
                    bonus += 1.0
                if want_date and LONG_DATE_RE.search(window):
                    bonus += 2.0
                width_pref = (w if definitional else -w)
                key = (round(mass + bonus, 6), width_pref, -i)
                if best_key is None or key > best_key:
                    best_key, best_win = key, (i, j)
        i, j = best_win
        out = sents[i:j]
        if len(" ".join(out)) < 80 and j < len(sents):
            out.append(sents[j])
        text = " ".join(out)
        return (text, best_key) if with_key else text

    def _text_answer(self, q: Query, k: int) -> Optional[Evidence]:
        ranked = self._ranked_text(q, k)
        if not ranked:
            return Evidence(REFUSAL_TEXT, [], confident=False)
        best = self._pick_recent(ranked)
        if best[1] < MIN_TEXT_COVERAGE or not self._entities_ok(q, best[2]):
            return Evidence(REFUSAL_TEXT, [], confident=False)
        same_page = [best[2]] + [r[2] for r in ranked[:15]
                                 if r[2].curl == best[2].curl and r[2] is not best[2]
                                 and r[1] >= MIN_TEXT_COVERAGE * 0.5][:4]
        text, b = self._best_passage(q, same_page)
        if q.is_url:
            url = self._best_link([b], q, k)
            if url:
                text = f"{text} Link: {url}"
        return Evidence(text, [b])

    # ================================================================ events ("still open?")
    _CLOSE_RE = re.compile(r"\b(will close|closed|close on|closing|will be closed|ends?|ended|"
                           r"expire[sd]?|discontinue[sd]?)\b", re.I)

    def _still_open(self, q: Query, k: int, ref: Optional[date]) -> Optional[Evidence]:
        ranked = self._ranked_text(q, k)
        if not ranked or ranked[0][1] < MIN_TEXT_COVERAGE:
            return None
        ref_date = ref or self.store.snapshots[k].taken_on
        for _f, cov, b in ranked[:8]:
            if cov < MIN_TEXT_COVERAGE or not self._entities_ok(q, b):
                continue
            for s in sentences(b.text):
                if not self._CLOSE_RE.search(s):
                    continue
                for d, _m in all_long_dates(s):
                    if q.is_still or "still" in q.lower or "today" in q.lower:
                        if d <= ref_date:
                            return Evidence(f"No. {s} That date ({fmt_long(d)}) has passed.", [b])
                        return Evidence(f"Yes, for now. {s}", [b])
                    return Evidence(s, [b])
        return None

    # ================================================================ hours
    _DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)[a-z]*\.?"
    _SCHED_RE = re.compile(
        rf"({_DAY}(?:\s*(?:-|to|through|&|and)\s*{_DAY})?)\s*:\s*"
        r"((?:\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)?\s*(?:-|to)\s*(?:\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)|noon))"
        r"|closed|atm only|by appointment)", re.I)

    def _hours_answer(self, q: Query, k: int) -> Optional[Evidence]:
        want_drive = bool(re.search(r"drive|drive-up|drive-thru|drive through", q.lower))
        section = "drive" if want_drive else "lobby"
        schedules: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.store.active_blocks(k):
            if b.kind != "text" or b.page_class in ("junk", "listing"):
                continue
            low = b.text.lower()
            if "lobby" not in low and "drive" not in low:
                continue
            if q.entities and not self._entities_ok(q, b) and not self._page_mentions(b, q):
                continue
            sched = self._extract_schedule(b.text, section)
            if sched:
                schedules[sched].append(b)
        if not schedules:
            return Evidence(REFUSAL_TEXT, [], confident=False)
        ranked = sorted(schedules.items(),
                        key=lambda kv: (-len({b.curl for b in kv[1]}),
                                        -max((b.published or date.min).toordinal() for b in kv[1])))
        variants = len(ranked)
        label = "Drive-up" if want_drive else "Lobby"
        main = "; ".join(ranked[0][0])
        if q.entities:
            text = f"{label} hours: {_end(main)}"
            return Evidence(text, ranked[0][1][:2])
        asks_variation = bool(re.search(r"\b(same|all|every|differ|vary|different)\b", q.lower))
        if variants == 1:
            return Evidence(f"{label} hours: {_end(main)}", ranked[0][1][:2])
        others = ["; ".join(s) for s, _ in ranked[1:]] if asks_variation else [
            "; ".join(s) for s, _ in ranked[1:2]]
        lead = "No. " if asks_variation and q.is_yesno else ""
        text = (f"{lead}{label} hours vary by branch. The most common schedule is {_end(main)} "
                + ("Other branches: " + _end(" | ".join(others)) + " " if others else "")
                + "Ask which branch you plan to visit for its exact hours.")
        support = [b for _s, bs in ranked[: 1 + len(others)] for b in bs[:1]]
        return Evidence(text, support)

    def _page_mentions(self, b: Block, q: Query) -> bool:
        return all(e in set(tokens(b.page_title)) for e in q.entities)

    def _extract_schedule(self, text: str, section: str) -> Optional[tuple]:
        low = text.lower()
        if section == "lobby":
            start = low.find("lobby")
            if start < 0:
                return None
            end_m = re.search(r"drive", low[start + 5:])
            seg = text[start: start + 5 + end_m.start()] if end_m else text[start:]
        else:
            m = re.search(r"drive[- ]?(?:up|thru|through)", low)
            if not m:
                return None
            seg = text[m.start():]
            nxt = re.search(r"\b(lobby|atm|video teller)\b", seg[10:].lower())
            seg = seg[: 10 + nxt.start()] if nxt else seg
        items = []
        for m in self._SCHED_RE.finditer(seg):
            items.append(f"{_tidy_day(m.group(1).strip())} {m.group(2).strip()}")
        return tuple(items) if len(items) >= 1 else None

    # ================================================================ counts
    def _count_answer(self, q: Query, k: int) -> Optional[Evidence]:
        m = re.search(r"how many ([a-z' -]+?)(?: does| do| did| are| is| were| was| have| has|\?|$)", q.lower)
        if not m:
            return None
        noun_toks = [t for t in tokens(m.group(1)) if t not in SOFT_WORDS]
        if not noun_toks:
            return None
        noun = noun_toks[0]
        context = [t for t in q.product_terms if t != noun]
        org = self.store.org_label
        claims = []   # (published, count, sentence, block)
        # a count claim: a number followed, within 4 words, by the counted noun ("22 branch offices",
        # "294 Dupaco employees").  Years are never counts.
        num_re = re.compile(r"(?<![\d.$/])\b(\d{1,3}(?:,\d{3})*|\d+)\b(?![.%/]\d)((?:\s+[A-Za-z$'-]+){1,4})")
        for b in self.store.active_blocks(k):
            if b.kind != "text" or noun not in b.terms or b.page_class in ("junk", "listing"):
                continue
            cq = Query(**{**q.__dict__, "product_terms": context}) if context else None
            if cq and self._coverage(cq, b.terms) < 0.5:
                continue
            for s in sentences(b.text):
                stoks = set(tokens(s))
                if cq and self._coverage(cq, stoks) < 0.5:
                    continue
                if any(e not in stoks for e in q.entities):
                    continue
                if org and org in q.lower and org not in b.text.lower():
                    continue          # the claim must be about the credit union itself
                for mm in num_re.finditer(s):
                    words = tokens(mm.group(2))[:2]      # the counted noun heads the phrase
                    if noun in words and not re.match(r"(19|20)\d\d$", mm.group(1)):
                        claims.append((b.published, int(mm.group(1).replace(",", "")), s, b))
                        break
        if not claims:
            return None
        if q.asks_last_year:
            return self._year_series(claims, q)
        dated = [c for c in claims if c[0] is not None]
        pool = dated or claims
        newest = max(pool, key=lambda c: (c[0] or date.min, c[3].authority, -c[3].bid))
        when = f" (as stated in its most recent update, {fmt_long(newest[0])})" if newest[0] else ""
        phrase = self._count_phrase(newest[2], newest[1], noun)
        values = Counter(c[1] for c in claims)
        note = None
        if len(values) > 1:
            note = ("Different pages state different counts: "
                    + ", ".join(f"{v} ({n} page(s))" for v, n in sorted(values.items()))
                    + f"; used the most recently dated statement ({newest[1]}).")
        return Evidence(f"{self.store.org_name} has {phrase}{when}.", [newest[3]], note=note)

    @staticmethod
    def _count_phrase(sentence: str, count: int, noun: str) -> str:
        m = re.search(rf"\b{count:,}\b\s+((?:[A-Za-z'-]+\s+){{0,3}}?[A-Za-z]*{noun}[a-z]*(?:\s+[a-z]+){{0,2}})",
                      sentence, re.I) or re.search(rf"\b{count}\b\s+((?:[A-Za-z'-]+\s+){{0,3}}?[A-Za-z]*{noun}[a-z]*)",
                                                    sentence, re.I)
        phrase = m.group(1).strip() if m else noun
        phrase = re.sub(r"\s+(in|at|and|with|of|throughout|across)$", "", phrase)
        return f"{count} {phrase}"

    def _year_series(self, claims, q: Query) -> Evidence:
        series = {}
        for pub, n, s, b in claims:
            ym = re.search(r"\bin ((?:19|20)\d\d)\b", s)
            year = int(ym.group(1)) if ym else (pub.year - 1 if pub else None)
            if year is None:
                continue
            if year not in series or (pub or date.min) > (series[year][0] or date.min):
                series[year] = (pub, n, s, b)
        if not series:
            return Evidence(REFUSAL_TEXT, [], confident=False)
        parts = [f"{v[1]} in {y}" for y, v in sorted(series.items())]
        latest_year = max(series)
        text = ("Dupaco's published figures: " + "; ".join(parts)
                + f". The most recent year reported is {latest_year}.")
        return Evidence(text, [v[3] for _y, v in sorted(series.items())])

    # ================================================================ URLs
    def _link_popularity(self, k: int) -> Counter:
        if k not in self._link_pop_cache:
            c: Counter = Counter()
            for b in self.store.active_blocks(k):
                for _a, u in b.links:
                    c[_url_base(u)] += 1
            self._link_pop_cache[k] = c
        return self._link_pop_cache[k]

    def _best_link(self, blocks: list[Block], q: Query, k: int) -> Optional[str]:
        pop = self._link_popularity(k)
        root = self.store.site_root
        qset = {e for t in q.product_terms for e in expand(t)}
        best, best_s = None, 0.0
        for rank, b in enumerate(blocks):
            for anchor, url in b.links:
                host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower()
                if not url.startswith("http") or not (root and host.endswith(root)):
                    continue
                at = set(tokens(anchor + " " + _url_words(url)))
                s = len(at & qset) + 0.1 * math.log1p(pop[_url_base(url)]) - 0.05 * rank
                if s > best_s:
                    best, best_s = url, s
        return _clean_url(best) if best else None

    def _url_answer(self, q: Query, k: int) -> Optional[Evidence]:
        ranked = self._ranked_text(q, k, limit=80)
        if not ranked:
            return None
        top = [r[2] for r in ranked[:10] if r[1] >= 0.5]
        if not top:
            return None
        url = self._best_link(top, q, k)
        if not url:
            return None
        src = next((b for b in top if any(_clean_url(u) == url for _a, u in b.links)), top[0])
        # cite the page the link points to when we hold a copy of it (it is the authority for its
        # own content); otherwise cite the page where the link was found
        target = self._page_block(_url_base(url), k)
        return Evidence(f"You can use {url}", [target] if target else [src])

    def _page_block(self, base_url: str, k: int) -> Optional[Block]:
        from .pages import canonical_url
        curl = canonical_url(base_url)
        vers = self.store.versions.get(curl)
        if not vers:
            return None
        live = [v for v in vers if v[0] <= k]
        if not live or not live[-1][2]:
            return None
        return self.store.blocks[live[-1][2][0]]


# ============================================================================ small utils

def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _url_base(u: str) -> str:
    return re.sub(r"[#?].*$", "", u).rstrip("/").lower().replace("http://", "https://")


def _url_words(u: str) -> str:
    return re.sub(r"[^a-z]+", " ", re.sub(r"^https?://", "", u.lower()))


def _clean_url(u: str) -> str:
    u = u.replace("http://", "https://")
    u = re.sub(r"\?hsLang=[a-z]+", "", u)
    if "#" in u and re.search(r"#.*\d{5,}", u):
        u = u.split("#", 1)[0]
    return u


def _tidy_day(day: str) -> str:
    return re.sub(r"\s*-\s*", "-", day)


def _end(s: str) -> str:
    """Terminate a sentence fragment with exactly one period."""
    s = s.rstrip()
    return s if s.endswith((".", "!", "?")) else s + "."


def _is_business(b: Block) -> bool:
    return "business" in f"{b.caption} {b.label} {b.heading}".lower() or "/business" in b.curl


def _strip_segment(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\bbusiness\b", "", name.lower())).strip()


_FEE_RE = re.compile(r"\b(fees?|charges?|penalty|penalties|minimum payment|payments?)\b", re.I)


def _attributable(b: Block) -> bool:
    """A row is usable evidence only if its product can be named from its label, caption or
    heading (e.g. '$100,000+' alone under an unlabeled table is not attributable)."""
    words = [t for t in tokens(f"{b.caption} {b.label} {b.heading.split(' > ')[-1] if b.heading else ''}")
             if t.isalpha() and t not in SOFT_WORDS and t not in RATE_WORDS]
    return bool(words)
