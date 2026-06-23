"""Unit tests for the weather probability model."""
import unittest

import weather_data
import bot_weather


class TestNormalModel(unittest.TestCase):
    def test_norm_cdf_center(self):
        self.assertAlmostEqual(weather_data.norm_cdf(0.0), 0.5, places=6)

    def test_norm_cdf_tails(self):
        self.assertGreater(weather_data.norm_cdf(4.0), 0.999)
        self.assertLess(weather_data.norm_cdf(-4.0), 0.001)

    def test_disjoint_brackets_sum_to_one(self):
        mu, sigma = 85.0, 3.0
        buckets = [
            (None, 80),   # 80 or below
            (81, 85),
            (86, 90),
            (91, None),   # 91 or above
        ]
        total = sum(weather_data.bracket_probability(lo, hi, mu, sigma)
                    for lo, hi in buckets)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_above_at_forecast_is_near_half(self):
        p = weather_data.bracket_probability(85, None, 85.0, 3.0)  # "85 or above"
        self.assertTrue(0.45 < p < 0.65)

    def test_sigma_grows_with_lead(self):
        self.assertLess(weather_data.sigma_for_lead(0), weather_data.sigma_for_lead(2))


class TestBracketParsing(unittest.TestCase):
    def test_above(self):
        self.assertEqual(bot_weather.parse_bounds("90° or above"), (90, None))

    def test_below(self):
        self.assertEqual(bot_weather.parse_bounds("92° or below"), (None, 92))

    def test_range(self):
        self.assertEqual(bot_weather.parse_bounds("87° to 88°"), (87, 88))

    def test_target_date(self):
        d = bot_weather.parse_target_date("KXHIGHNY-26JUN24-T89")
        self.assertEqual((d.year, d.month, d.day), (2026, 6, 24))


if __name__ == "__main__":
    unittest.main()
