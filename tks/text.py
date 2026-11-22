"""Text utilities: markdown cleanup, tokenisation, number/date/range parsing.

Everything here is pure and deterministic; it is shared by ingestion (documents) and
query parsing (questions) so both sides are normalised identically.
"""
from __future__ import annotations

import html
import re
from datetime import date
from typing import Iterable, Optional

# --------------------------------------------------------------------------- markdown cleanup

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_ESCAPED_CRLF_RE = re.compile(r"(?:\\{1,2}r\\{1,2}n)+")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!$|>~])")
_BOLD_RE = re.compile(r"(\*\*|__)")
_ITALIC_RE = re.compile(r"(?<![\w*])[*_]([^*_\n]+)[*_](?![\w*])")
# footnote digits glued to a value/unit: "4.20%1", "5.19% APR1", "(APY)1"
_GLUED_FOOTNOTE_RE = re.compile(r"(%|\bAPR|\bAPY|\))\d\b")
# footnote digit glued to the start of a sentence: "1Rates are effective..."
_LEADING_FOOTNOTE_RE = re.compile(r"^_*\d_*(?=[A-Z][a-z])")
_WS_RE = re.compile(r"[ \t   ]+")


def extract_links(line: str) -> list[tuple[str, str]]:
    """(anchor text, url) pairs in a markdown line, images excluded."""
    line = _IMG_RE.sub("", line)
    return [(clean_inline(t), u) for t, u in _LINK_RE.findall(line) if u]


def clean_inline(text: str) -> str:
    """Turn one markdown line into plain text suitable for display and indexing."""
    s = _ESCAPED_CRLF_RE.sub(" ", text)
    s = html.unescape(s)
    s = s.replace("<br>", "; ").replace("<br/>", "; ").replace("<br />", "; ")
    s = _IMG_RE.sub("", s)

    def _link(m: re.Match) -> str:
        anchor = m.group(1)
        return "" if anchor.strip().isdigit() else anchor   # "[1](#fine-print)" footnote refs

    s = _LINK_RE.sub(_link, s)
    s = _MD_ESCAPE_RE.sub(r"\1", s)
    s = s.replace("\\", " ")
    s = _BOLD_RE.sub("", s)
    s = _ITALIC_RE.sub(r"\1", s)
    s = _GLUED_FOOTNOTE_RE.sub(r"\1", s)
    s = _LEADING_FOOTNOTE_RE.sub("", s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("‑", "-")
    return _WS_RE.sub(" ", s).strip()


# --------------------------------------------------------------------------- tokenisation

STOPWORDS = frozenset(
    """a an and are as at be been being but by can could did do does doing for from had has have
    having how i if in into is it its me my of on or our ours please should so than that the their
    them then there these they this those to too us was we were what whats when where which while who
    whom why will with would you your yours tell give get know like want need provide show let current
    currently right now today dupaco dupacos s about any all also just much many there's i'm i'd
    im id ive going not over under non without via per""".split()
)

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
    "eighteen": "18", "twenty": "20", "thirty": "30", "forty": "40", "sixty": "60",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


_SUFFIXES = ("ibility", "ability", "ations", "ation", "itions", "ition", "izing", "ized", "izes", "ize", "ings", "ing",
             "ions", "ion", "edly", "ed", "ies", "es", "s", "e")
_STEM_EXCEPTIONS = {"news": "news", "business": "business", "yes": "yes", "visa": "visa",
                    "hours": "hour", "plus": "plus", "bonus": "bonus", "status": "status",
                    "foundation": "foundation", "foundations": "foundation"}


def stem(tok: str) -> str:
    """Small deterministic suffix stripper (Porter-lite) applied identically to documents and
    questions: recognition/recognized -> recogn, branches -> branch, savings/saving -> sav."""
    if tok in _STEM_EXCEPTIONS:
        return _STEM_EXCEPTIONS[tok]
    if len(tok) <= 3 or not tok.isalpha():
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith(("ss", "us", "is")):
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[: -len(suf)]
            return base + ("ibl" if suf == "ibility" else "abl" if suf == "ability" else "")
    return tok


def tokens(text: str, keep_stopwords: bool = False) -> list[str]:
    out = []
    for t in _TOKEN_RE.findall(text.lower().replace("'", "")):
        t = _NUMBER_WORDS.get(t, t)
        if not keep_stopwords and t in STOPWORDS:
            continue
        out.append(stem(t))
    return out


# --------------------------------------------------------------------------- numbers & ranges

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k|K|million)?")


def percents(text: str) -> list[str]:
    return [m.group(0).replace(" ", "") for m in _PCT_RE.finditer(text)]


def money_values(text: str) -> list[float]:
    vals = []
    for m in _MONEY_RE.finditer(text):
        v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        if m.group(3):
            v *= 1_000_000 if m.group(3).lower() == "million" else 1_000
        vals.append(v)
    return vals


_RANGE_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:-|to)\s*\$\s?([\d,]+(?:\.\d+)?)"     # $10,000 - $24,999.99
    r"|\$\s?([\d,]+(?:\.\d+)?)\s*(\+|or more|and up|or less|and over)",   # $25,000+ / $300,000 or less
    re.I,
)


def money_range(text: str) -> Optional[tuple[float, float]]:
    """Balance / loan-amount tier expressed in a label, as a closed interval."""
    m = _RANGE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        lo, hi = (float(m.group(i).replace(",", "")) for i in (1, 2))
        return (lo, hi)
    v = float(m.group(3).replace(",", ""))
    return (0.0, v) if "less" in m.group(4).lower() else (v, float("inf"))


_YEAR_RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s*-\s*(\d{2,4})\b")
_OLDER_RE = re.compile(r"\b((?:19|20)\d\d)\s+model year\s*\(and older\)", re.I)


def year_range(text: str) -> Optional[tuple[int, int]]:
    """'2024-27 Model Year' -> (2024, 2027); '2016 Model Year (and older)' -> (0, 2016)."""
    m = _OLDER_RE.search(text)
    if m:
        return (0, int(m.group(1)))
    m = _YEAR_RANGE_RE.search(text)
    if not m:
        return None
    lo, hi = int(m.group(1)), m.group(2)
    hi_i = int(hi) if len(hi) == 4 else (lo // 100) * 100 + int(hi)
    if hi_i < lo:          # "2026-23" (descending)
        lo, hi_i = hi_i, lo
    return (lo, hi_i)


_TERM_RE = re.compile(r"\b(\d{1,3})\s*-?\s*(month|mo|year|yr)s?\b", re.I)


def term_months(text: str) -> Optional[int]:
    m = _TERM_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    return n * 12 if m.group(2).lower() in ("year", "yr") else n


# --------------------------------------------------------------------------- dates

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
         ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
         ("october", "oct"), ("november", "nov"), ("december", "dec")]
    )
    for m in names
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
LONG_DATE_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)
MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.I)
EFFECTIVE_RE = re.compile(r"effective\s+(?:as\s+of\s+)?(\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_numeric_date(s: str) -> Optional[date]:
    m = NUMERIC_DATE_RE.search(s)
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    yi = int(y) + 2000 if len(y) == 2 else int(y)
    return _safe_date(yi, mo, d)


def parse_long_date(s: str) -> Optional[date]:
    m = LONG_DATE_RE.search(s)
    if not m:
        return None
    return _safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))


def all_long_dates(s: str) -> Iterable[tuple[date, re.Match]]:
    for m in LONG_DATE_RE.finditer(s):
        d = _safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        if d:
            yield d, m


def month_end(y: int, m: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def fmt_long(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"
