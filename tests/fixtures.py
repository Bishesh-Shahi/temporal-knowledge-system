"""Synthetic website snapshots used by the tests.

The fixture deliberately reproduces the failure modes of the real task with invented content
(no private snapshot text): a rate that changes between crawls, a stale calculator copy with an
older effective date, tiered tables, an expired promotion, dated press releases that disagree on a
count, a future-dated closure, branch hours, and a link-only question.
"""
from __future__ import annotations

from types import SimpleNamespace

SITE = "https://www.example-cu.test"


def page(path: str, title: str, md: str, fetched_at: str = "") -> SimpleNamespace:
    return SimpleNamespace(url=SITE + path, title=title, markdown=md, fetched_at=fetched_at)


def rates_page(six_month: str, eff: str, auto60: str, auto_eff: str, band: str) -> str:
    return f"""# Rates

Certificates

Regular Certificates

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 12 Month | 3.02% | 3.05%1 | $1,000.00 |
| 6 Month | {six_month} | {six_month}1 | $1,000.00 |

Rates are effective as of {eff} and are subject to change without notice.

Share Savings

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY)1 |
| --- | --- | --- | --- |
| Share Savings | $25,000+ | 0.30% | 0.30% |
| Share Savings | $10,000 - $24,999.99 | 0.25% | 0.25% |
| Share Savings | $0 - $9,999.99 | 0.05% | 0.05% |

Rates are effective as of 06/15/2026 and are subject to change without notice.

Money Market

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY)1 |
| --- | --- | --- | --- |
| Investor's Choice Money Market | $25,000 - $99,999.99 | 0.45% | 0.45% |

Rates are effective as of 06/02/2026 and are subject to change without notice.

Auto Loans

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| {band} Model Year | 60 Months | {auto60} APR1 |
| {band} Model Year | 84 Months | 5.07% APR |

Rates are effective as of {auto_eff}. All loans are subject to approval.
"""


CALCULATOR = """# Vehicle Affordability Calculator

Auto Loan Rates

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2023-26 Model Year | 60 Months | 4.88% APR |
| 2023-26 Model Year | 84 Months | 5.02% APR |

**Rates are effective as of 1/1/2026. All loans are subject to approval.**
"""

PROMO = """# Summer Promo

Summer special: 6-month certificate APY 1.75% (offer ended 2026-04-30)
"""


def news(n: int, date_line: str, body: str) -> str:
    return f"""2 min read

[Jane Writer]({SITE}/newsroom/author/jane)
{date_line}

# News item {n}

[Branch Announcements]({SITE}/newsroom/tag/branch-announcements)

{body}

## Handpicked for You

Sep 17, 2026

Example CU has 99 branch office locations in a teaser card that must be ignored.
"""


CONTACT = """# Contact Us

Have a question? Call us at 800-555-0100.

**Routing Number:** 123456789

### Example Credit Union

Example Branch

500 Oak Ln., Springfield, IL, 62701

### Springfield Motors

937 Springfield Square Dr, Springfield, IL, 62701
"""

SAVINGS = """# Savings

### Term-Share Certificates

Lock in a higher dividend rate for a few months or a few years.

See [today's rates](https://www.example-cu.test/personal/savings-account#rates)
"""


def snapshots() -> list[SimpleNamespace]:
    s1 = SimpleNamespace(snapshot_id="01_2026-05-12_baseline", taken_on="2026-05-12", pages=[
        page("/rates", "Rates", rates_page("2.11%", "03/03/2026", "4.81%", "5/1/2026", "2023-26")),
        page("/calculators/vehicle-affordability", "Vehicle Affordability", CALCULATOR),
        page("/contact", "Contact", CONTACT),
        page("/personal/savings-account", "Savings", SAVINGS),
        page("/newsroom/old-release", "Old release", news(1, "March 17, 2026",
             "Example CU is a credit union. It has 600 employees and 17 branch office locations.")),
        page("/newsroom/branch-closure", "Closure", news(2, "June 20, 2025",
             "The Oak Street branch will close March 31, 2026. Members can use the Main Street branch.")),
        page("/newsroom/learning-lab", "Lab", news(3, "September 23, 2021",
             "Branch hours are:\n\n**Lobby**\n\nMonday-Thursday: 8:30 a.m.-5 p.m.\n\nFriday: 8:30 a.m.-6 p.m.\n\n"
             "Saturday: 8:30 a.m.-12:30 p.m.\n\n**Drive-up**\n\nMonday-Friday: 8 a.m.-6 p.m.")),
    ])
    s2 = SimpleNamespace(snapshot_id="02_2026-09-28_latest", taken_on="2026-09-28", pages=[
        page("/rates", "Rates", rates_page("2.65%", "09/01/2026", "4.93%", "08/31/2026", "2024-27")),
        page("/calculators/vehicle-affordability", "Vehicle Affordability", CALCULATOR),
        page("/promo", "Promo", PROMO),
        page("/newsroom/new-release", "New release", news(4, "May 05, 2026",
             "Example CU is a credit union. It has 700 employees and 16 branch office locations.")),
    ])
    return [s1, s2]
