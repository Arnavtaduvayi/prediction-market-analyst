"""Unit tests for cross-venue pair math — no network."""
import unittest

import bot_xvenue as x


class TestDateParsing(unittest.TestCase):
    def test_valid_tokens(self):
        self.assertEqual(x.parse_kalshi_date("26JUL"), (2026, 7))
        self.assertEqual(x.parse_kalshi_date("27JAN"), (2027, 1))

    def test_invalid_tokens(self):
        self.assertIsNone(x.parse_kalshi_date("JULY"))
        self.assertIsNone(x.parse_kalshi_date("26XYZ"))
        self.assertIsNone(x.parse_kalshi_date(""))


class TestPolyFee(unittest.TestCase):
    def test_us_taker_formula(self):
        # 0.06 * 100 * 0.5 * 0.5 = 1.50 (the documented cap case)
        self.assertEqual(x.poly_taker_fee(0.50, 100), 1.50)

    def test_extremes_free(self):
        self.assertEqual(x.poly_taker_fee(1.0, 10), 0.0)
        self.assertEqual(x.poly_taker_fee(0.0, 10), 0.0)


class TestQuotes(unittest.TestCase):
    def _event(self):
        return {"markets": [
            {"groupItemTitle": "No change", "bestBid": 0.88, "bestAsk": 0.89},
            {"groupItemTitle": "Dead side", "bestBid": None, "bestAsk": 0.001},
        ]}

    def test_poly_quotes_skip_one_sided_books(self):
        q = x.poly_market_quotes(self._event())
        self.assertIn("No change", q)
        self.assertNotIn("Dead side", q)
        self.assertAlmostEqual(q["No change"]["mid"], 0.885)


class TestHardArb(unittest.TestCase):
    def _row(self, k_bid, k_ask, p_bid, p_ask):
        return {
            "kalshi": {"ticker": "KXFEDDECISION-26JUL-H0", "title": "t",
                       "yes_bid_dollars": str(k_bid), "yes_ask_dollars": str(k_ask),
                       "close_time": "2099-01-01T00:00:00Z"},
            "poly": {"bid": p_bid, "ask": p_ask,
                     "mid": (p_bid + p_ask) / 2, "spread": p_ask - p_bid},
            "poly_vol24h": 1e6, "poly_liquidity": 1e6,
            "poly_event": "slug", "outcome": "No change", "pair_id": "fed",
        }

    def test_locks_when_combined_below_dollar(self):
        # YES kalshi @ 0.55 + NO poly @ (1-0.62)=0.38 = 0.93 + fees -> locked
        data = {"bankroll": 75.0, "trades": []}
        row = self._row(0.50, 0.55, 0.62, 0.63)
        self.assertTrue(x.try_hard_arb(row, data))
        t = data["trades"][0]
        self.assertTrue(t["arb_pair"])
        self.assertGreater(t["locked_profit"], 0)
        self.assertLess(data["bankroll"], 75.0)

    def test_no_arb_when_prices_agree(self):
        data = {"bankroll": 75.0, "trades": []}
        row = self._row(0.87, 0.88, 0.88, 0.89)
        self.assertFalse(x.try_hard_arb(row, data))
        self.assertEqual(data["trades"], [])


class TestFairValue(unittest.TestCase):
    def _row(self, k_bid, k_ask, p_mid, vol=1e6, liq=1e6, spread=0.01):
        return {
            "kalshi": {"ticker": "KXFEDDECISION-26OCT-H0", "title": "t",
                       "yes_bid_dollars": str(k_bid), "yes_ask_dollars": str(k_ask),
                       "close_time": "2099-01-01T00:00:00Z"},
            "poly": {"bid": p_mid - spread / 2, "ask": p_mid + spread / 2,
                     "mid": p_mid, "spread": spread},
            "poly_vol24h": vol, "poly_liquidity": liq,
            "poly_event": "slug", "outcome": "No change", "pair_id": "fed",
        }

    def test_quotes_inside_fair_value_on_divergence(self):
        data = {"bankroll": 75.0, "trades": []}
        row = self._row(0.56, 0.65, 0.655)   # kalshi mid 0.605, div +0.05
        self.assertTrue(x.try_fair_value(row, data, set()))
        o = data["trades"][0]
        self.assertEqual(o["status"], "resting")
        self.assertEqual(o["side"], "yes")
        # never above fair - margin, never at/over the ask
        self.assertLessEqual(o["entry_price"], round(0.655 - x.FAIR_MARGIN, 2))
        self.assertLess(o["entry_price"], 0.65)
        self.assertEqual(o["target_yes_mid"], 0.655)

    def test_small_divergence_no_trade(self):
        data = {"bankroll": 75.0, "trades": []}
        row = self._row(0.87, 0.88, 0.885)   # div 0.01
        self.assertFalse(x.try_fair_value(row, data, set()))

    def test_thin_poly_is_not_an_anchor(self):
        data = {"bankroll": 75.0, "trades": []}
        row = self._row(0.56, 0.65, 0.655, vol=100, liq=1000)
        self.assertFalse(x.try_fair_value(row, data, set()))

    def test_rich_kalshi_quotes_no_side(self):
        data = {"bankroll": 75.0, "trades": []}
        # kalshi mid 0.70 vs poly 0.60 -> buy NO below NO-fair 0.40
        row = self._row(0.65, 0.75, 0.60)
        self.assertTrue(x.try_fair_value(row, data, set()))
        o = data["trades"][0]
        self.assertEqual(o["side"], "no")
        self.assertLessEqual(o["entry_price"], round(0.40 - x.FAIR_MARGIN, 2))


if __name__ == "__main__":
    unittest.main()
