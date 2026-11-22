"""Integration tests: synthetic snapshots -> ingest -> retrieval -> resolution -> answer/conflicts."""
import re
import unittest

from tests import _path  # noqa: F401
from tests.fixtures import snapshots
from solution import build
from tks.store import KnowledgeStore

NUM = re.compile(r"\d")


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = build()
        for s in snapshots():
            cls.ks.ingest(s)

    def ask(self, q, as_of=None):
        return self.ks.answer(q, as_of)

    # ---------------------------------------------------------------- temporal
    def test_current_value_uses_latest_snapshot(self):
        a = self.ask("What is the 6-month certificate APY?")
        self.assertIn("3.50%", a.text)
        self.assertNotIn("2.96", a.text)
        self.assertTrue(a.confident)

    def test_as_of_answers_from_the_snapshot_in_force(self):
        a = self.ask("What is the 6-month certificate APY?", as_of="2026-06-30")
        self.assertIn("2.96%", a.text)
        self.assertNotIn("3.50", a.text)

    def test_question_embedded_date_matches_as_of(self):
        a = self.ask("What was the 6-month certificate APY as of May 2026?")
        self.assertIn("2.96%", a.text)

    def test_as_of_before_coverage_abstains(self):
        a = self.ask("What is the 6-month certificate APY?", as_of="2020-01-01")
        self.assertFalse(a.confident)
        self.assertIsNone(NUM.search(a.text))

    def test_expired_promotion_is_not_current(self):
        a = self.ask("What is the six month share certificate APY?")
        self.assertIn("3.50%", a.text)
        self.assertNotIn("2.95", a.text)

    # ---------------------------------------------------------------- stale copies & tiers
    def test_newer_effective_date_beats_stale_calculator(self):
        a = self.ask("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertIn("5.39%", a.text)
        self.assertNotIn("5.34", a.text)
        self.assertTrue(any("/rates" in s.url for s in a.sources))
        self.assertIn("5.34%", a.conflict_note or "")    # disclosed, but not in the answer

    def test_balance_tier_selection(self):
        a = self.ask("What dividend rate do I earn on Share Savings with a balance between $10,000 and $24,999?")
        self.assertIn("0.15%", a.text)
        self.assertNotIn("0.20", a.text)
        self.assertNotIn("0.35", a.text)

    def test_superlative(self):
        a = self.ask("What is the highest Share Savings APY?")
        self.assertIn("0.20%", a.text)
        self.assertIn("25,000", a.text)

    def test_value_check_question(self):
        a = self.ask("Do any savings tiers pay 0.35% APY?")
        self.assertTrue(a.text.startswith("No"))
        self.assertIn("0.20%", a.text)

    def test_list_question_enumerates(self):
        a = self.ask("What are your certificate rates?")
        self.assertIn("3.40%", a.text)
        self.assertIn("3.50%", a.text)

    # ---------------------------------------------------------------- abstention
    def test_unknown_product_abstains_without_digits(self):
        for q in ("What is your rate on a 7/1 adjustable-rate mortgage?",
                  "What interest rate do you charge on a construction loan?",
                  "What are the lobby hours for the Mullan Avenue branch?"):
            a = self.ask(q)
            self.assertFalse(a.confident, q)
            self.assertEqual(set(NUM.findall(a.text)) - set(NUM.findall(q)), set(), q)

    # ---------------------------------------------------------------- narrative evidence
    def test_newest_dated_claim_wins_and_teasers_ignored(self):
        a = self.ask("How many branches does Example CU have?")
        self.assertIn("22", a.text)
        self.assertNotIn("23", a.text)
        self.assertNotIn("99", a.text)

    def test_event_date_vs_reference_date(self):
        a = self.ask("When does the Oak Street branch close?")
        self.assertIn("March 31, 2026", a.text)
        b = self.ask("Is the Oak Street branch still open today (October 2026)?")
        self.assertTrue(b.text.startswith("No"))

    def test_hours(self):
        a = self.ask("What are the lobby hours?")
        self.assertIn("8:30", a.text)
        d = self.ask("What are the drive-up hours?")
        self.assertIn("8 a.m.-6 p.m.", d.text)

    def test_address_prefers_branch_over_namesake(self):
        a = self.ask("What is the address of the Galena branch?")
        self.assertIn("11375 Oak", a.text)

    def test_link_question_returns_corpus_url(self):
        a = self.ask("What link should I use to see certificate rates?")
        self.assertIn("example-cu.test/personal/savings-account", a.text)

    def test_routing_number(self):
        self.assertIn("123456789", self.ask("What is the routing number?").text)

    # ---------------------------------------------------------------- contract properties
    def test_determinism_and_citations(self):
        q = "What is the 6-month certificate APY?"
        a1, a2 = self.ask(q), self.ask(q)
        self.assertEqual(a1.text, a2.text)
        self.assertTrue(a1.sources and a1.sources[0].url.startswith("https://"))
        self.assertTrue(a1.sources[0].snapshot_id)

    def test_conflicts_list_values_and_winner(self):
        cs = self.ks.conflicts()
        six = [c for c in cs if "6 month" in c.topic.lower() and "3.50%" in c.values]
        self.assertTrue(six, [c.topic for c in cs])
        self.assertEqual(six[0].resolution, "3.50%")
        self.assertIn("2.96%", six[0].values)
        self.assertTrue(six[0].reason)
        # different tiers are not conflicts
        self.assertFalse([c for c in cs if "0.15%" in c.values and "0.20%" in c.values])

    def test_bad_inputs(self):
        for q in ("", "   ", "?" * 500):
            a = self.ask(q)
            self.assertFalse(a.confident)
        a = self.ask("What is the 6-month certificate APY?", as_of="not-a-date")
        self.assertIn("3.50%", a.text)


class StoreTests(unittest.TestCase):
    def test_versions_views_and_carry_forward(self):
        st = KnowledgeStore()
        for s in snapshots():
            st.ingest(s.snapshot_id, s.taken_on, s.pages)
        self.assertEqual(st.view_index(None), 1)
        from datetime import date
        self.assertEqual(st.view_index(date(2026, 6, 1)), 0)
        self.assertIsNone(st.view_index(date(2026, 1, 1)))
        contact = [b for b in st.blocks if b.curl.endswith("/contact")]
        self.assertTrue(contact and all(b.active & 0b10 for b in contact))   # carried forward
        old = [b for b in st.blocks if b.curl.endswith("/rates") and "2.96%" in b.text]
        self.assertTrue(old and all(not (b.active & 0b10) for b in old))     # superseded

    def test_ingest_order_is_enforced(self):
        st = KnowledgeStore()
        s1, s2 = snapshots()
        st.ingest(s2.snapshot_id, s2.taken_on, s2.pages)
        with self.assertRaises(ValueError):
            st.ingest(s1.snapshot_id, s1.taken_on, s1.pages)


if __name__ == "__main__":
    unittest.main()
