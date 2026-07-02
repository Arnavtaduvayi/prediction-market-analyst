"""Unit tests for the longshot basket band filter."""
import unittest

import bot_longshot


class TestBand(unittest.TestCase):
    def test_in_band(self):
        for mid in (0.05, 0.10, 0.15):
            self.assertTrue(bot_longshot.in_band(mid))

    def test_out_of_band(self):
        for mid in (0.04, 0.16, 0.20, 0.50, 0.95):
            self.assertFalse(bot_longshot.in_band(mid))


if __name__ == "__main__":
    unittest.main()
