"""Question understanding: intent, qualifier slots and temporal anchors.

The parser is deliberately rule-based and documented so that paraphrases of the same question map
to the same slots (consistency) and so every decision can be explained.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .text import (MONTH_YEAR_RE, _MONTHS, money_values, month_end, parse_long_date, percents,
                   stem, term_months, tokens)

# Words that describe *what kind of number* is wanted rather than *which product*.
RATE_WORDS = frozenset("rate apy apr dividend interest yield percent percentage annual".split())
# Modifiers that never identify a product on their own (scoring only, never required for coverage).
SOFT_WORDS = frozenset(
    """standard regular normal typical basic general new existing earn pay paid charge charged
    balance between much offer offered have get got option product dupaco credit union member
    account accounts tier level amount cost price vehicle car fixed year month term long
    repeat again say said once mean meaning info information detail details tell list
    special loan see use find look online kind type available still open close closed today
    purchase highest lowest best top maximum minimum most least cheapest link url website site
    address located location hours hour time going im ive become
    receive received receiving get getting give given share
    intro introductory promotional promo late early mid version advertise advertised advertising
    lock locked stretch stretched financing finance financed brand want looking plan planning
    reach someone anyone today now current currently right lowest ask asking pick choose
    option options whats thing things way ways start starting new open opening apply money
    minimum min send sending take taking using use operate operates run runs maintain maintains
    yr yrs please number numbers count total""".split()
)
# Query-side synonym expansion (domain lexicon). Keys/values are stemmed tokens.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "cd": ("certificate",),
    "certificate": ("certificate", "cd"),
    "mortgage": ("mortgage", "conventional"),
    "car": ("auto",),
    "vehicle": ("auto", "motorcycle"),
    "auto": ("auto", "vehicle"),
    "card": ("visa", "card"),
    "visa": ("visa", "card"),
    "savings": ("saving",),
    "heloc": ("heloc", "equity"),
    "ira": ("ira", "retirement", "roth", "traditional"),
    "retirement": ("ira", "retirement"),
    "checking": ("checking",),
    "hsa": ("hsa", "health"),
    "rv": ("rv", "recreational", "boat"),
    "boat": ("boat", "recreational"),
    "update": ("update", "change", "modify"),
    "change": ("change", "update"),
    "university": ("university", "college", "school"),
    "college": ("college", "university", "school"),
    "flexibility": ("flexibility", "flexible"),
    "phone": ("phone", "call", "telephone"),
    "atm": ("atm", "atms"),
    "suv": ("auto", "vehicle"),
    "truck": ("auto", "vehicle"),
    "motorcycle": ("motorcycle", "auto"),
    "extension": ("extension", "ext"),
    "cd": ("certificate", "cd"),
    "jumbo": ("jumbo",),
    "mailing": ("mailing", "mail", "box"),
}
# When only a generic category is named, the primary product of that category (stated as such on
# the site: "our primary savings account") is preferred.  Soft preference, never a filter.
DEFAULT_PRODUCT_HINTS: dict[str, tuple[str, ...]] = {"saving": ("share",)}

_LIST_RE = re.compile(r"\b(rates|all|list|every|options|which .* offer|what .* offer|what .* have|"
                      r"cards|certificates|accounts)\b")
_SUPER_HIGH_RE = re.compile(r"\b(highest|top|maximum|max|most)\b")
_SUPER_LOW_RE = re.compile(r"\b(lowest|cheapest|least)\b")
_URL_RE = re.compile(r"\b(links?|urls?|website|web ?site|web address|web page|webpage|locator)\b")
_URL_WEAK_RE = re.compile(r"\bonline\b")
_HOURS_RE = re.compile(r"\b(hours|open until|what time|when .* open|opening times)\b")
_COUNT_RE = re.compile(r"\bhow many\b|\bnumber of\b|\bcount of\b|\btotal number\b")
_RATE_RE = re.compile(r"\b(rates?|apys?|aprs?|dividends?|interest|yields?)\b|%")
_YESNO_RE = re.compile(r"^(do|does|is|are|can|could|will|did|has|have|was|were)\b")
_STILL_RE = re.compile(r"\b(still|anymore|any more|currently)\b.*\b(open|available|offered|operating)\b|"
                       r"\b(open|available|offered)\b.*\b(still|anymore)\b")
_LAST_YEAR_RE = re.compile(r"\b(last|previous|past) year\b")
_MODEL_YEAR_RE = re.compile(r"\b((?:19|20)\d\d)\b")
_STREET_WORDS = frozenset("avenue ave street st road rd drive dr boulevard blvd lane ln way branch".split())
_CAP_STOP = frozenset("""I What When Where Which Who Why How Is Are Do Does Can Could Will Would Should
                      The A An Dupaco Dupaco's Visa APY APR CD CDs IRA HSA ATM Shine Monday Tuesday
                      Wednesday Thursday Friday Saturday Sunday Please Tell Give Provide Get My Your""".split())


@dataclass
class Query:
    raw: str
    lower: str
    terms: list[str]                       # content tokens (stemmed, stopwords removed)
    product_terms: list[str]               # terms minus rate words and pure numbers
    required: list[str]                    # product terms that must be covered by evidence
    rate_kinds: set[str] = field(default_factory=set)
    term_months: Optional[int] = None
    amounts: list[float] = field(default_factory=list)
    model_year: Optional[int] = None
    asked_percents: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)   # capitalised names (branches, programs)
    ref_date: Optional[date] = None        # date embedded in the question ("as of May 2026")
    is_rate: bool = False
    is_list: bool = False
    is_url: bool = False
    is_hours: bool = False
    is_count: bool = False
    is_yesno: bool = False
    is_still: bool = False
    is_fee: bool = False
    asks_last_year: bool = False
    superlative: Optional[str] = None      # "high" | "low" | "best" (direction depends on kind)
    negated: list[str] = field(default_factory=list)   # "not the jumbo" -> ["jumbo"]
    modifiers: list[str] = field(default_factory=list)  # words naming the product ("lot" in "lot loan")
    _req_cache: Optional[list[str]] = None


def _stem_set(words) -> frozenset:
    return frozenset(stem(w) for w in words)


RATE_WORDS = _stem_set(RATE_WORDS)
SOFT_WORDS = _stem_set(SOFT_WORDS)
SYNONYMS = {stem(k): tuple(dict.fromkeys(stem(v) for v in vs)) for k, vs in SYNONYMS.items()}
DEFAULT_PRODUCT_HINTS = {stem(k): tuple(stem(v) for v in vs) for k, vs in DEFAULT_PRODUCT_HINTS.items()}
_STREET_WORDS = _stem_set(_STREET_WORDS)


_MONTH_TOKENS = frozenset(stem(m) for m in _MONTHS) - {"mar", "may"} | {"may"}


_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
          "nineteen"]
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_NUMBER_WORD_VALUES = {w: i for i, w in enumerate(_UNITS)} | dict(_TENS)
for _t, _tv in _TENS.items():               # compounds: "twenty-four", "eighty four"
    for _u in range(1, 10):
        _NUMBER_WORD_VALUES[f"{_t}-{_UNITS[_u]}"] = _tv + _u
        _NUMBER_WORD_VALUES[f"{_t} {_UNITS[_u]}"] = _tv + _u
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_NUMBER_WORD_VALUES, key=len, reverse=True)) + r")\b")
_NEGATION_RE = re.compile(r"\b(?:not|non|excluding|except|other than|without)\s+(?:the\s+|a\s+|an\s+|any\s+)?([a-z]+)")
_FEE_Q_RE = re.compile(r"\b(fees?|costs?|penalty|penalties|surcharges?)\b")
_CHARGE_RE = re.compile(r"\b(charges?|charged)\b")
# product head nouns: the words right before them name the product being asked about
_PRODUCT_HEAD_RE = re.compile(
    r"((?:[a-z0-9/'-]+\s+){1,2})(?:loans?|mortgages?|certificates?|cds?|accounts?|cards?|iras?|"
    r"lines? of credit|helocs?|savings|checking|clubs?)\b")


def product_modifiers(low: str) -> list[str]:
    """Words directly naming a product: 'a lot loan' -> ['lot']; 'reverse mortgage' -> ['revers'].
    These must be matched by evidence; otherwise the site does not publish that product's rate."""
    out = []
    for m in _PRODUCT_HEAD_RE.finditer(low):
        for t in tokens(m.group(1)):
            if t.isalpha() and t not in SOFT_WORDS and t not in RATE_WORDS and t not in out:
                out.append(t)
    return out
_PAY_RE = re.compile(r"\b(pays?|earns?|earning|paying)\b")


def numberize(text: str) -> str:
    """'two-year' -> '2-year', 'six month' -> '6 month' (slots are parsed from digits)."""
    return _NUMBER_WORD_RE.sub(lambda m: str(_NUMBER_WORD_VALUES[m.group(1)]), text)


def _embedded_date(text: str) -> Optional[date]:
    d = parse_long_date(text)
    if d:
        return d
    m = MONTH_YEAR_RE.search(text)
    if m:
        return month_end(int(m.group(2)), _MONTHS[m.group(1).lower()])
    return None


def _entities(raw: str) -> list[str]:
    # capitalised words that are not merely sentence-initial ("Number of branches?")
    words = [m.group(0) for m in re.finditer(r"\b[A-Z][a-zA-Z$'-]+\b", raw)
             if not re.search(r"(^|[.?!]\s+)$", raw[:m.start()])]
    out = []
    for i, w in enumerate(words):
        if w in _CAP_STOP or w.lower() in _MONTHS:
            continue
        t = tokens(w)
        if t and t[0] not in _STREET_WORDS and t[0] not in RATE_WORDS:
            out.append(t[0])
    return out


def parse_question(question: str) -> Query:
    raw = (question or "").strip()
    low = raw.lower()
    negated = [stem(w) for w in _NEGATION_RE.findall(low)]
    terms = tokens(numberize(low))
    numeric = {t for t in terms if re.fullmatch(r"\d+(?:\.\d+)?", t)} | _MONTH_TOKENS
    product_terms = [t for t in terms if t not in RATE_WORDS and t not in numeric and t not in negated]
    required = [t for t in dict.fromkeys(product_terms) if t not in SOFT_WORDS]

    kinds = set()
    if re.search(r"\bapys?\b|annual percentage yield|\byield\b", low):
        kinds.add("apy")
    if re.search(r"\baprs?\b|annual percentage rate", low):
        kinds.add("apr")
    if re.search(r"\bdividend", low):
        kinds.add("dividend")

    q = Query(raw=raw, lower=low, terms=terms, product_terms=product_terms, required=required,
              rate_kinds=kinds, negated=negated)
    q.term_months = term_months(numberize(low))
    q.amounts = money_values(raw)
    q.asked_percents = percents(raw)
    ref = _embedded_date(raw)
    q.ref_date = ref
    for m in _MODEL_YEAR_RE.finditer(raw):
        y = int(m.group(1))
        around = low[max(0, m.start() - 25): m.end() + 25]
        if re.search(r"model|vehicle|car|truck|motorcycle|suv|auto", around) and not (ref and ref.year == y and
                                                                             MONTH_YEAR_RE.search(raw)):
            q.model_year = y
    q.entities = _entities(raw)
    q.is_url = bool(_URL_RE.search(low))
    q.is_fee = bool(_FEE_Q_RE.search(low)) or (bool(_CHARGE_RE.search(low)) and not _RATE_RE.search(low))
    q.modifiers = [t for t in product_modifiers(numberize(low)) if t not in negated]
    explicit_rate = bool(re.search(r"\b(apys?|aprs?|dividends?|interest|yields?)\b|%", low))
    # "rate" inside a product name ("1st Rate Checking") does not make a fee question a rate question
    q.is_rate = ((explicit_rate or (bool(_RATE_RE.search(low)) and not q.is_fee)
                  or (bool(_PAY_RE.search(low)) and bool(q.amounts) and not q.is_fee))
                 and not _HOURS_RE.search(low) and not q.is_url)
    if not q.is_url and not q.is_rate and _URL_WEAK_RE.search(low):
        q.is_url = True
    q.is_hours = bool(_HOURS_RE.search(low))
    q.is_count = bool(_COUNT_RE.search(low))
    q.is_yesno = bool(_YESNO_RE.match(low))
    q.is_still = bool(_STILL_RE.search(low))
    q.asks_last_year = bool(_LAST_YEAR_RE.search(low))
    if re.search(r"\bbest\b", low):
        q.superlative = "best"
    elif _SUPER_HIGH_RE.search(low):
        q.superlative = "high"
    elif _SUPER_LOW_RE.search(low):
        q.superlative = "low"
    q.is_list = bool(_LIST_RE.search(low)) and q.superlative is None
    return q


def expand(term: str) -> tuple[str, ...]:
    return SYNONYMS.get(term, (term,))
