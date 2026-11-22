"""Markdown page -> evidence blocks.

A page is segmented into two kinds of blocks:

* ``row``  - one table row, carrying its table caption, column headers, label cells, typed rate
             values (rate / dividend / APY / APR) and the "Rates are effective as of" date that
             governs the table.  Rows are the unit of numeric facts.
* ``text`` - a chunk of consecutive paragraphs under one heading (<= ~CHUNK_CHARS), with the
             hyperlinks it contains.  Text chunks are the unit of narrative evidence.

Boilerplate (link-out modals, phone-country pickers, related-post teaser widgets, listing chrome)
is removed *before* segmentation so that teaser cards cannot leak other posts' dates or numbers
into the host page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .text import (EFFECTIVE_RE, LONG_DATE_RE, clean_inline, extract_links, parse_long_date,
                   parse_numeric_date)

CHUNK_CHARS = 520

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_REGIONAL_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]")
_DROP_LINE_RE = re.compile(
    r"^(?:✕|messenger(?: launcher)?|third party plugins|sort by:.*|load more|\d+ min read|"
    r"#+\s*filter|zoom in.*|heads up! this link leads.*|we only do this when it's helpful.*|"
    r"we only do this when it’s helpful.*|\\?#\s*\\?#\s*\\?#)$",
    re.I,
)
_TEASER_HEADINGS = ("recent news", "handpicked for you", "related posts", "related articles",
                    "you may also like", "more news", "latest news")
_STANDALONE_DATE_RE = re.compile(LONG_DATE_RE.pattern + r"\s*$", re.I)


@dataclass
class RawBlock:
    kind: str                          # "row" | "text"
    heading: str                       # heading path, " > " joined
    text: str                          # display/plain text
    links: list[tuple[str, str]] = field(default_factory=list)
    # row-only fields
    caption: str = ""
    headers: tuple[str, ...] = ()
    cells: tuple[str, ...] = ()
    label: str = ""
    values: tuple[tuple[str, str, str], ...] = ()   # (kind, header, "4.20%")
    eff_date: Optional[date] = None

    @property
    def expires(self) -> Optional[date]:
        """End date of a promotion stated in the block ("offer ended 2025-09-30")."""
        return expiry_date(self.text)


@dataclass
class ParsedPage:
    h1: str
    published: Optional[date]
    blocks: list[RawBlock]


def _split_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _value_kind(header: str, cell: str) -> str:
    h = header.lower()
    c = cell.lower()
    if "apy" in h or "yield" in h or "apy" in c:
        return "apy"
    if "apr" in h or "annual percentage rate" in h or "apr" in c:
        return "apr"
    if "dividend" in h:
        return "dividend"
    return "rate"


_NON_LABEL_HEADERS = ("min.", "min ", "minimum", "points", "payment", "example", "remaining",
                      "principal", "interest")


def _is_link_only(raw: str) -> bool:
    s = raw.strip()
    return bool(s) and s.startswith(("[", "![")) and s.endswith(")")


def _strip_boilerplate(lines: list[str], drop_lines: frozenset[str]) -> list[str]:
    out: list[str] = []
    skip_level = 0
    for raw in lines:
        s = raw.strip()
        m = _HEADING_RE.match(s)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and clean_inline(m.group(2)).lower().strip(" :") in _TEASER_HEADINGS:
                skip_level = level
                continue
        if skip_level:
            continue
        if _is_link_only(s) and all(("/tag/" in u or "/author/" in u) for _a, u in extract_links(s) or [("", "")]) \
                and extract_links(s):
            continue          # article taxonomy / byline links are navigation, not content
        if s and (s in drop_lines or _REGIONAL_FLAG_RE.search(s) or _DROP_LINE_RE.match(clean_inline(s))
                  or s.startswith("![](data:")):
            continue
        out.append(raw)
    return out


def find_byline(lines: list[str]) -> Optional[date]:
    """Publish/updated date printed above the H1 of an article ("September 23, 2021")."""
    for raw in lines[:25]:
        s = raw.strip()
        if s.startswith("# "):
            break
        if _STANDALONE_DATE_RE.match(clean_inline(s)):
            return parse_long_date(clean_inline(s))
    return None


def _effective_near(lines: list[str], start: int, end: int, step: int) -> Optional[date]:
    """Scan up to 8 non-empty lines from `start` towards `end` for an effective-date statement,
    stopping at headings and other tables."""
    seen = 0
    i = start
    while (i < end if step > 0 else i > end) and seen < 8:
        s = lines[i].strip()
        if s:
            if _HEADING_RE.match(s) or s.startswith("|"):
                break
            m = EFFECTIVE_RE.search(clean_inline(s))
            if m:
                return parse_numeric_date(m.group(1))
            seen += 1
        i += step
    return None


def parse_page(markdown: str, drop_lines: frozenset[str] = frozenset(),
               dated: bool = False, inline_facts: bool = True) -> ParsedPage:
    lines = markdown.splitlines()
    published = find_byline(lines) if dated else None
    lines = _strip_boilerplate(lines, drop_lines)

    blocks: list[RawBlock] = []
    headings: list[tuple[int, str]] = []
    h1 = ""
    para: list[str] = []          # cleaned lines of the current paragraph
    para_links: list[tuple[str, str]] = []
    chunk: list[str] = []
    chunk_links: list[tuple[str, str]] = []
    captions: list[str] = []      # short plain lines since the last table/heading
    group = ""                    # table-group label ("Certificates") carried to following tables

    def heading_path() -> str:
        return " > ".join(t for _, t in headings)

    def flush_chunk() -> None:
        nonlocal chunk, chunk_links
        if chunk:
            text = " ".join(chunk).strip()
            if text:
                blocks.append(RawBlock("text", heading_path(), text, list(dict.fromkeys(chunk_links))))
        chunk, chunk_links = [], []

    def flush_para() -> None:
        nonlocal para, para_links
        if para:
            text = " ".join(para).strip()
            if chunk and len(" ".join(chunk)) + len(text) > CHUNK_CHARS:
                flush_chunk()
            if text:
                chunk.append(text if text[-1] in ".!?:;" else text + ".")
                chunk_links.extend(para_links)
        para, para_links = [], []

    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        s = raw.strip()
        hm = _HEADING_RE.match(s)
        if hm:
            flush_para()
            flush_chunk()
            level, htext = len(hm.group(1)), clean_inline(hm.group(2))
            if level == 1 and not h1:
                h1 = htext
            headings = [(lv, t) for lv, t in headings if lv < level]
            if htext:
                headings.append((level, htext))
                captions = [htext]
                group = ""
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1].strip()):
            flush_para()
            flush_chunk()
            header = [clean_inline(c) for c in _split_cells(s)]
            j = i + 2
            rows = []
            while j < n and lines[j].strip().startswith("|"):
                rows.append([clean_inline(c) for c in _split_cells(lines[j])])
                j += 1
            eff = _effective_near(lines, j, n, 1) or _effective_near(lines, i - 1, -1, -1)
            # caption = group label + table label.  A table preceded by a single short line
            # ("Regular") inherits the group of the previous table ("Certificates").
            if len(captions) >= 2:
                group = captions[-2]
                caption = f"{group} / {captions[-1]}"
            elif captions:
                caption = (f"{group} / {captions[-1]}" if group and group != captions[-1]
                           else captions[-1])
            else:
                caption = group
            for cells in rows:
                blocks.append(_row_block(heading_path(), caption, header, cells, eff))
            captions = []
            i = j
            continue
        if not s:
            flush_para()
            i += 1
            continue
        text = clean_inline(s.lstrip("-*+ ").strip()) if s[:2] in ("- ", "* ", "+ ") else clean_inline(s)
        links = extract_links(s)
        if text and len(text) <= 60 and text[-1] not in ".:;!?," and not _is_link_only(s):
            captions.append(text)
            captions = captions[-3:]
        if text:
            para.append(text)
            para_links.extend(links)
            inline = (_inline_fact(text, heading_path(), headings[-1][1] if headings else "")
                      if inline_facts else None)
            if inline is not None:
                blocks.append(inline)
        i += 1
    flush_para()
    flush_chunk()
    return ParsedPage(h1=h1, published=published, blocks=blocks)


_INLINE_RE = re.compile(r"^([^:|]{2,80}):\s*(.*\d(?:\.\d+)?\s*%.*)$")
_PAREN_RE = re.compile(r"\([^)]*\)")
_PCT_ONLY_RE = re.compile(r"\d+(?:\.\d+)?\s*%(?:\s*(?:-|to)\s*\d+(?:\.\d+)?\s*%)?")
EXPIRY_RE = re.compile(
    r"\b(?:offer|promotion|promo|special|rate)?\s*(?:ended|expired|ends|expires|valid through|"
    r"available through|good through)\s+(?:on\s+)?"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", re.I)


def parse_any_date(s: str) -> Optional[date]:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s.strip())
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return parse_numeric_date(s) or parse_long_date(s)


def expiry_date(text: str) -> Optional[date]:
    m = EXPIRY_RE.search(text)
    return parse_any_date(m.group(1)) if m else None


def _inline_fact(text: str, heading: str, section: str) -> Optional[RawBlock]:
    """A short 'label: value%' line ("6-month certificate APY: 2.95%") is a fact like a table row."""
    if len(text) > 160:
        return None
    m = _INLINE_RE.match(text)
    if not m:
        return None
    label_part, value_part = m.group(1).strip(), m.group(2)
    vals = _PCT_ONLY_RE.findall(value_part)
    first = _PCT_ONLY_RE.search(value_part)
    if not vals or len(label_part.split()) > 10 or first is None or first.start() > 40:
        return None
    words = _PCT_ONLY_RE.sub(" ", _PAREN_RE.sub(" ", value_part))
    label = re.sub(r"\s+", " ", f"{label_part} {words}").strip(" .;,")
    values = tuple((_value_kind(label, value_part), "", v.replace(" ", "")) for v in vals)
    eff_m = EFFECTIVE_RE.search(text)
    eff = parse_numeric_date(eff_m.group(1)) if eff_m else None
    return RawBlock("row", heading, text, [], section, (), (text,), label, values, eff)


def _row_block(heading: str, caption: str, header: list[str], cells: list[str],
               eff: Optional[date]) -> RawBlock:
    if len(header) < len(cells):
        header = header + [""] * (len(cells) - len(header))
    values, label_parts = [], []
    for h, c in zip(header, cells):
        if not c:
            continue
        if "%" in c and not any(k in h.lower() for k in ("points", "payment", "min")):
            for pct in re.findall(r"\d+(?:\.\d+)?\s*%(?:\s*-\s*\d+(?:\.\d+)?\s*%)?", c):
                values.append((_value_kind(h, c), h, pct.replace(" ", "")))
        elif not any(k in h.lower() for k in _NON_LABEL_HEADERS) and c not in ("-", "—"):
            label_parts.append(c)
    if not values:
        # fee schedules and similar tables state dollar amounts instead of rates
        for h, c in zip(header, cells):
            if c and "$" in c and not any(k in h.lower() for k in ("min", "payment", "example", "balance")):
                for amt in re.findall(r"\$\s?\d[\d,]*(?:\.\d+)?", c):
                    values.append(("amount", h, amt.replace(" ", "")))
        if values and label_parts and any(v[2] in label_parts[0] for v in values):
            values = []
    label = " | ".join(label_parts)
    shown = "; ".join(f"{h}: {c}" if h else c for h, c in zip(header, cells) if c)
    text = f"{caption}: {shown}" if caption else shown
    return RawBlock("row", heading, text, [], caption, tuple(header), tuple(cells), label,
                    tuple(values), eff)
