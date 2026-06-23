"""Unit tests for the new exit_monitor triggers (target/stop) — no network."""
import unittest

import exit_monitor


def state(yes_bid, yes_ask):
    mid = (yes_bid + yes_ask) / 2
    return {"yes_mid": mid, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "status": "active", "settlement": None, "volume_24h": 0}


class TestTargetStop(unittest.TestCase):
    def test_yes_target_hit(self):
        trade = {"side": "yes", "target_yes_mid": 0.60, "logged_at": ""}
        reason, _ = exit_monitor.check_exit_triggers(trade, state(0.61, 0.63))
        self.assertEqual(reason, "TARGET_HIT")

    def test_yes_stop_loss(self):
        trade = {"side": "yes", "stop_yes_mid": 0.40, "logged_at": ""}
        reason, price = exit_monitor.check_exit_triggers(trade, state(0.34, 0.36))
        self.assertEqual(reason, "STOP_LOSS")
        self.assertEqual(price, 0.34)  # sell YES to the bid

    def test_no_stop_loss(self):
        # short via NO; price rising hurts us
        trade = {"side": "no", "stop_yes_mid": 0.60, "logged_at": ""}
        reason, price = exit_monitor.check_exit_triggers(trade, state(0.64, 0.66))
        self.assertEqual(reason, "STOP_LOSS")
        self.assertAlmostEqual(price, 1.0 - 0.66)  # NO bid = 1 - YES ask

    def test_no_target_hit(self):
        trade = {"side": "no", "target_yes_mid": 0.40, "logged_at": ""}
        reason, _ = exit_monitor.check_exit_triggers(trade, state(0.37, 0.39))
        self.assertEqual(reason, "TARGET_HIT")

    def test_hold_to_settlement_skips_all_triggers(self):
        trade = {"side": "yes", "stop_yes_mid": 0.99, "hold_to_settlement": True,
                 "logged_at": ""}
        reason, _ = exit_monitor.check_exit_triggers(trade, state(0.10, 0.12))
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
