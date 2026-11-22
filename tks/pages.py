"""Page classification and source authority.

Authority is a *prior* used only to break ties after explicit effective dates (see resolve.py).
It encodes a defensible editorial hierarchy: the official rate sheet and product pages are the
system of record; promotional landing pages, calculators and articles frequently embed stale
copies of the same numbers.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

PAGE_CLASSES = {
    # class: authority prior (0..1)
    "rates": 1.00,      # official rate sheets (/rates, /business-rates)
    "product": 0.90,    # product / service pages
    "info": 0.80,       # contact, about, membership, policies
    "landing": 0.45,    # campaign landing pages (*-lp, switch-and-earn, ...)
    "newsroom": 0.40,   # press releases (dated)
    "article": 0.35,    # blog / learn / business resources (dated)
    "calculator": 0.30, # calculators embed illustrative, often stale, numbers
    "document": 0.35,   # hubfs PDFs / spreadsheets converted to markdown
    "listing": 0.05,    # tag / author / category listing pages
    "junk": 0.0,        # tracking-parameter duplicates of other pages
}

DATED_CLASSES = frozenset({"newsroom", "article", "document"})

_LANDING_HINTS = ("-lp", "switch-and-earn", "earn-more", "offer-expired", "homebuying-guidance",
                  "mortgage-refi", "money-makeover", "thankuse", "not-for-sale")


def canonical_url(url: str) -> str:
    """Normalise for identity: drop fragment, trailing slash, scheme and 'www.'."""
    p = urlparse(url if "//" in url else "https://" + url)
    path = p.path.rstrip("/") or "/"
    q = f"?{p.query}" if p.query else ""
    return p.netloc.lower().removeprefix("www.") + path.lower() + q


def classify(url: str, title: str = "") -> str:
    p = urlparse(url)
    path = p.path.lower().rstrip("/")
    if p.query and ("campaignid" in p.query.lower() or "pageurl" in p.query.lower()):
        return "junk"
    if "hslang=" in p.query.lower():
        return "junk"
    if path in ("/rates", "/business-rates"):
        return "rates"
    if "/tag/" in path or "/author/" in path or "category=" in p.query.lower() or path in (
        "/newsroom", "/learn/blog", "/business/resources", "/resources/personal-resources"):
        return "listing"
    if path.startswith("/hubfs") or re.search(r"\.(pdf|xlsx?|csv|docx?)$", path):
        return "document"
    if path.startswith("/calculators") or path.endswith("-calculators") or "calculator" in path:
        return "calculator"
    if path.startswith("/newsroom"):
        return "newsroom"
    if (path.startswith(("/learn", "/business/resources", "/resources", "/dupacodaily"))
            or re.match(r"^/\d{4}/\d{2}/", path)):
        return "article"
    if any(h in path for h in _LANDING_HINTS):
        return "landing"
    if path in ("", "/contact", "/about", "/about/join", "/membership", "/digital-privacy-policy",
                "/privacy-policy") or path.startswith(("/about", "/disclosures", "/careers")):
        return "info"
    return "product"


def authority(page_class: str) -> float:
    return PAGE_CLASSES.get(page_class, 0.5)
