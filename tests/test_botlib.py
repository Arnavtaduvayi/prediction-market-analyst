"""Unit tests for botlib: fees, sizing, trade construction, microstructure."""
import unittest

import botlib


class TestKalshiFee(unittest.TestCase):
    def test_midpoint_one_contract(self):
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds UP to 0.02
        self.assertEqual(botlib.kalshi_fee(0.50, 1), 0.02)

    def test_scales_with_contracts(self):
        # 0.07 * 10 * 0.25 = 0.175 -> 0.18
        self.assertEqual(botlib.kalshi_fee(0.50, 10), 0.18)

    def test_zero_at_extremes(self):
        self.assertEqual(botlib.kalshi_fee(1.0, 5), 0.0)
        self.assertEqual(botlib.kalshi_fee(0.0, 5), 0.0)

    def test_no_contracts(self):
        self.assertEqual(botlib.kalshi_fee(0.5, 0), 0.0)

    def test_always_rounds_up(self):
        # tiny raw fee still costs a full cent
        self.assertEqual(botlib.kalshi_fee(0.99, 1), 0.01)


class TestMakerFee(unittest.TestCase):
    def test_quarter_of_taker_rate(self):
        # 0.0175 * 100 * 0.5 * 0.5 = 0.4375 -> 0.44 (vs taker 1.75)
        self.assertEqual(botlib.kalshi_fee(0.50, 100, rate=botlib.MAKER_FEE_RATE), 0.44)

    def test_still_rounds_up(self):
        # 0.0175 * 1 * 0.93 * 0.07 = 0.00114 -> full cent
        self.assertEqual(botlib.kalshi_fee(0.93, 1, rate=botlib.MAKER_FEE_RATE), 0.01)


class TestRestingOrders(unittest.TestCase):
    def _order(self, side="no", limit=0.93):
        return botlib.new_resting_order("TICK", "t", side, 3, limit, "x",
                                        expire_hours=8)

    def test_construction(self):
        o = self._order()
        self.assertEqual(o["status"], "resting")
        self.assertEqual(o["entry_price"], 0.93)
        self.assertEqual(o["fee"], botlib.kalshi_fee(0.93, 3, rate=botlib.MAKER_FEE_RATE))
        self.assertEqual(o["cost"], round(3 * 0.93 + o["fee"], 2))

    def test_printed_through_requires_strictly_better(self):
        o = self._order(side="no", limit=0.93)
        at_limit = [{"no_price_dollars": "0.93"}]
        through = [{"no_price_dollars": "0.92"}]
        worse = [{"no_price_dollars": "0.95"}]
        self.assertFalse(botlib._printed_through(o, at_limit))   # queue-pessimism
        self.assertTrue(botlib._printed_through(o, through))
        self.assertFalse(botlib._printed_through(o, worse))

    def test_printed_through_uses_side_price(self):
        o = self._order(side="yes", limit=0.62)
        # a YES print at 0.61 is through a YES bid at 0.62
        self.assertTrue(botlib._printed_through(o, [{"yes_price_dollars": "0.61"}]))
        self.assertFalse(botlib._printed_through(o, [{"yes_price_dollars": "0.63"}]))

    def test_bad_prints_ignored(self):
        o = self._order()
        self.assertFalse(botlib._printed_through(o, [{"no_price_dollars": None},
                                                     {"no_price_dollars": "junk"},
                                                     {}]))


class TestKellySize(unittest.TestCase):
    def test_positive_edge_capped(self):
        # f* = (0.6 - 0.4)/1 = 0.2; *0.25 = 0.05; cap 0.05 -> 0.05
        self.assertAlmostEqual(botlib.kelly_size(0.60, 0.50, 0.25, 0.05), 0.05)

    def test_negative_edge_is_zero(self):
        self.assertEqual(botlib.kelly_size(0.40, 0.50, 0.25, 0.05), 0.0)

    def test_invalid_price_is_zero(self):
        self.assertEqual(botlib.kelly_size(0.9, 1.0, 0.25, 0.05), 0.0)
        self.assertEqual(botlib.kelly_size(0.9, 0.0, 0.25, 0.05), 0.0)


class TestNewTrade(unittest.TestCase):
    def test_cost_includes_fee(self):
        t = botlib.new_trade("TICK", "title", "yes", 10, 0.50, "x")
        self.assertEqual(t["fee"], 0.18)
        self.assertEqual(t["cost"], round(10 * 0.50 + 0.18, 2))
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["side"], "yes")

    def test_extra_fields_passthrough(self):
        t = botlib.new_trade("TICK", "title", "no", 1, 0.40, "x",
                             hold_to_settlement=True, arb_id="abc")
        self.assertTrue(t["hold_to_settlement"])
        self.assertEqual(t["arb_id"], "abc")


class TestMicrostructure(unittest.TestCase):
    def _trades(self):
        return [
            {"count_fp": "10", "yes_price_dollars": "0.40", "no_price_dollars": "0.60",
             "taker_outcome_side": "yes"},
            {"count_fp": "30", "yes_price_dollars": "0.60", "no_price_dollars": "0.40",
             "taker_outcome_side": "no"},
        ]

    def test_vwap(self):
        # (10*0.40 + 30*0.60) / 40 = 0.55
        self.assertAlmostEqual(botlib.vwap(self._trades()), 0.55)

    def test_vwap_empty(self):
        self.assertIsNone(botlib.vwap([]))

    def test_flow_imbalance(self):
        fi = botlib.flow_imbalance(self._trades())
        # yes_usd = 10*0.40 = 4 ; no_usd = 30*0.40 = 12 ; share = 4/16 = 0.25
        self.assertAlmostEqual(fi["yes_share"], 0.25)


if __name__ == "__main__":
    unittest.main()
