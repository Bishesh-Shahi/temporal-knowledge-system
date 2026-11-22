import unittest
from datetime import date

from tests import _path  # noqa: F401
from tks.parse import expiry_date, find_byline, parse_page
from tks.text import (clean_inline, money_range, parse_long_date, parse_numeric_date, stem,
                      term_months, tokens, year_range)


class TextTests(unittest.TestCase):
    def test_clean_inline_strips_markdown_and_footnotes(self):
        self.assertEqual(clean_inline("| 4.20%1 |"), "| 4.20% |")
        self.assertEqual(clean_inline("5.19% APR1"), "5.19% APR")
        self.assertEqual(clean_inline("1Rates are effective"), "Rates are effective")
        self.assertEqual(clean_inline("see [today's rates](https://x.test/a#rates)"), "see today's rates")
        self.assertEqual(clean_inline("15.50%-21.00% APR[1](https://x.test#fine)."), "15.50%-21.00% APR.")
        self.assertEqual(clean_inline(r"text\\r\\nmore \*bold\*"), "text more bold")
        self.assertEqual(clean_inline(r"Payment Example \*"), "Payment Example *")
        self.assertEqual(clean_inline("![img](https://x.test/i.png)Hello"), "Hello")

    def test_stemming_is_symmetric(self):
        self.assertEqual(stem("recognition"), stem("recognized"))
        self.assertEqual(stem("branches"), stem("branch"))
        self.assertEqual(stem("flexibility"), stem("flexible"))
        self.assertEqual(tokens("Six-month certificates"), ["6", "month", "certificat"])

    def test_ranges_and_terms(self):
        self.assertEqual(money_range("$10,000 - $24,999.99"), (10000.0, 24999.99))
        self.assertEqual(money_range("$25,000+")[0], 25000.0)
        self.assertEqual(money_range("30-Year Fixed $300,000 or less"), (0.0, 300000.0))
        self.assertEqual(year_range("2024-27 Model Year"), (2024, 2027))
        self.assertEqual(year_range("ATV Model Year 2026-23"), (2023, 2026))
        self.assertEqual(year_range("2016 Model Year (and older)"), (0, 2016))
        self.assertEqual(term_months("60 Months"), 60)
        self.assertEqual(term_months("30-Year Fixed"), 360)
        self.assertIsNone(term_months("Share Savings"))

    def test_dates(self):
        self.assertEqual(parse_numeric_date("08/13/25"), date(2025, 8, 13))
        self.assertEqual(parse_numeric_date("5/1/2026"), date(2026, 5, 1))
        self.assertIsNone(parse_numeric_date("13/45/2026"))
        self.assertEqual(parse_long_date("Sept. 23, 2021"), date(2021, 9, 23))
        self.assertEqual(expiry_date("special 2.95% (offer ended 2025-09-30)"), date(2025, 9, 30))


class ParseTests(unittest.TestCase):
    MD = """# Rates

Certificates

Special Certificates

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 17 Month Fixed Jumbo Certificate Special | 4.15% | 4.20%1 | $100,000.00 |
| 17 Month Fixed Certificate Special | 4.06% | 4.10%2 | $1,000.00 |

Rates are effective as of 09/01/2026 and are subject to change without notice.

[Open my certificate](https://join.test/)

Home Loans

| Program | Rate | Points | APR |
| --- | --- | --- | --- |
| Purchase: 30-Year Fixed Conventional $300,000 or less | 7.25% | 0.00% | 7.32% |

Rates are effective as of 09/25/2026.

## Recent News

Teaser: 99 branch office locations.
"""

    def test_table_rows_carry_caption_values_and_effective_date(self):
        rows = [b for b in parse_page(self.MD).blocks if b.kind == "row"]
        self.assertEqual(len(rows), 3)
        jumbo, std, mortgage = rows
        self.assertEqual(std.label, "17 Month Fixed Certificate Special")
        self.assertIn("Special Certificates", std.caption)
        self.assertEqual(std.values, (("dividend", "Dividend Rate", "4.06%"),
                                      ("apy", "Annual Percentage Yield (APY)", "4.10%")))
        self.assertEqual(std.eff_date, date(2026, 9, 1))
        self.assertEqual(mortgage.eff_date, date(2026, 9, 25))
        # the Points column is not a rate
        self.assertEqual([v[2] for v in mortgage.values], ["7.25%", "7.32%"])
        # link-only lines never become captions
        self.assertNotIn("Open my certificate", mortgage.caption)

    def test_teaser_widgets_are_removed(self):
        text = " ".join(b.text for b in parse_page(self.MD).blocks)
        self.assertNotIn("99 branch", text)

    def test_inline_facts_and_byline(self):
        md = "Jan 1 line\n\nMarch 3, 2024\n\n# Post\n\n6-month certificate APY: 2.95%\n"
        page = parse_page(md, dated=True)
        self.assertEqual(page.published, date(2024, 3, 3))
        rows = [b for b in page.blocks if b.kind == "row"]
        self.assertEqual(rows[0].label, "6-month certificate APY")
        self.assertEqual(rows[0].values[0][0], "apy")
        self.assertIsNone(find_byline(["# Title", "March 3, 2024"]))

    def test_malformed_input_does_not_crash(self):
        for md in ("", "|", "| a |\n| --- |", "# \n\n|x|y|\n|---|---|\n|1%|", "\x00\x01"):
            parse_page(md)


if __name__ == "__main__":
    unittest.main()
