"""Unit tests for the arbitrage detectors — the math that must never lie."""
import unittest

import bot_arb


def leg(ticker, yes_bid, yes_ask, sub="", bid_size=1000, ask_size=1000):
    return {"ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "bid_size": bid_size, "ask_size": ask_size, "sub_title": sub, "title": ticker}


class TestSumArb(unittest.TestCase):
    def test_underround_NOT_traded(self):
        # Σ yes_ask = 0.90 < 1 looks like a buy-all underround, but we never
        # trade it: "mutually exclusive" ≠ "collectively exhaustive", so buying
        # all YES is not risk-free. With Σ yes_bid = 0.84 < 1 there's no
        # overround either → must return None.
        legs = [leg("A", 0.28, 0.30), leg("B", 0.28, 0.30), leg("C", 0.28, 0.30)]
        self.assertIsNone(bot_arb.detect_sum_arb(legs))

    def test_overround_detected(self):
        legs = [leg("A", 0.40, 0.45), leg("B", 0.40, 0.45), leg("C", 0.40, 0.45)]
        arb = bot_arb.detect_sum_arb(legs)
        self.assertIsNotNone(arb)
        self.assertEqual(arb["kind"], "sum_overround")
        self.assertTrue(all(l["side"] == "no" for l in arb["legs"]))

    def test_no_arb_when_fees_eat_it(self):
        # asks sum to ~1.02 (no underround); bids sum to 0.99 (no overround)
        legs = [leg("A", 0.33, 0.34), leg("B", 0.33, 0.34), leg("C", 0.33, 0.34)]
        self.assertIsNone(bot_arb.detect_sum_arb(legs))

    def test_needs_book_on_every_leg(self):
        legs = [leg("A", 0.28, 0.30, ask_size=0), leg("B", 0.28, 0.30), leg("C", 0.28, 0.30)]
        # underround requires ask_size>0 on ALL legs -> falls through
        self.assertIsNone(bot_arb.detect_sum_arb(legs))


class TestLadderArb(unittest.TestCase):
    def test_monotonicity_violation_detected(self):
        # higher strike (110) bid richer than lower strike (100) ask -> arb
        legs = [leg("X-T100", 0.38, 0.40, "100 or above"),
                leg("X-T110", 0.55, 0.58, "110 or above")]
        arb = bot_arb.detect_ladder_arb(legs)
        self.assertIsNotNone(arb)
        self.assertEqual(arb["kind"], "ladder")
        self.assertEqual(arb["legs"][0]["side"], "yes")   # buy low strike
        self.assertEqual(arb["legs"][1]["side"], "no")    # sell high strike

    def test_valid_ladder_ignored(self):
        # monotone-correct: lower strike pricier than higher strike -> no arb
        legs = [leg("X-T100", 0.60, 0.62, "100 or above"),
                leg("X-T110", 0.30, 0.32, "110 or above")]
        self.assertIsNone(bot_arb.detect_ladder_arb(legs))

    def test_non_above_ladder_skipped(self):
        legs = [leg("X-T100", 0.38, 0.40, "100 or below"),
                leg("X-T110", 0.55, 0.58, "110 or below")]
        self.assertIsNone(bot_arb.detect_ladder_arb(legs))


if __name__ == "__main__":
    unittest.main()
