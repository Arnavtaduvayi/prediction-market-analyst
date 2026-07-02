"""Unit tests for the longshot seller's quoting + correlation guards."""
import unittest

import bot_seller


class TestNoQuote(unittest.TestCase):
    def test_improves_no_bid_by_one_tick(self):
        # YES 0.05/0.08 -> NO book 0.92/0.95 -> we rest 0.93
        self.assertAlmostEqual(bot_seller.no_quote(0.05, 0.08), 0.93)

    def test_never_crosses_no_ask(self):
        # YES 0.05/0.06 -> NO book 0.94/0.95 -> join the bid, don't cross
        self.assertAlmostEqual(bot_seller.no_quote(0.05, 0.06), 0.94)


class TestCorrelationKeys(unittest.TestCase):
    def test_event_key_strips_strike(self):
        self.assertEqual(bot_seller.event_key("KXBTCD-26JUL0317-T61999.99"),
                         "KXBTCD-26JUL0317")

    def test_ladder_strikes_share_event(self):
        a = bot_seller.event_key("KXBTCD-26JUL0317-T61999.99")
        b = bot_seller.event_key("KXBTCD-26JUL0317-T62999.99")
        self.assertEqual(a, b)

    def test_series_key(self):
        self.assertEqual(bot_seller.series_key("KXWT20MATCH-26JUL011000ESSWAR-ESS"),
                         "KXWT20MATCH")


class TestBandDiscipline(unittest.TestCase):
    def test_no_limit_cap_blocks_dust_selling(self):
        # yes at 0.01/0.03 -> NO quote would be 0.98 > NO_LIMIT_MAX -> reject zone
        limit = bot_seller.no_quote(0.01, 0.03)
        self.assertGreater(limit, bot_seller.NO_LIMIT_MAX)


if __name__ == "__main__":
    unittest.main()
