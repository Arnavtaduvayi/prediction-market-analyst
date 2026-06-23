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
