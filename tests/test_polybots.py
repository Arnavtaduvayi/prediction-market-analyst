"""Unit tests for the live Polymarket roster — pure logic only, no network."""

import time
import unittest

import bot_copy
import bot_polyarb
import bot_polyseller
import bot_polytheta
import bot_whaleflow
import polylib as pl


def _stats(last_days_ago=1.0, total_trades=500, is_bot=False):
    return {
        "activity_bounds": {"data": {
            "total_trades": total_trades,
            "last_trade_timestamp": time.time() - last_days_ago * 86400,
        }},
        "behavior_stats": {"data": {"is_likely_bot": is_bot}},
    }


CFG = pl.DEFAULT_CONFIG


class TestCopySelection(unittest.TestCase):
    def test_active_profitable_trader_eligible(self):
        ok, why = bot_copy.eligible({"pnl": 5000, "address": "0xa"}, _stats(), CFG["copy"])
        self.assertTrue(ok, why)

    def test_dormant_whale_rejected(self):
        # The #1 all-time PnL wallet last traded in 2024 — must be rejected.
        ok, why = bot_copy.eligible({"pnl": 22e6, "address": "0xa"},
                                    _stats(last_days_ago=600), CFG["copy"])
        self.assertFalse(ok)
        self.assertIn("stale", why)

    def test_thin_track_record_rejected(self):
        ok, _ = bot_copy.eligible({"pnl": 5000, "address": "0xa"},
                                  _stats(total_trades=12), CFG["copy"])
        self.assertFalse(ok)

    def test_farm_bot_rejected(self):
        ok, why = bot_copy.eligible({"pnl": 5000, "address": "0xa"},
                                    _stats(is_bot=True), CFG["copy"])
        self.assertFalse(ok)
        self.assertIn("bot", why)

    def test_losing_trader_rejected(self):
        ok, _ = bot_copy.eligible({"pnl": -100, "address": "0xa"}, _stats(), CFG["copy"])
        self.assertFalse(ok)

    def test_pick_roster_caps_n(self):
        cands = [{"row": {"address": f"0x{i}", "username": f"t{i}", "pnl": 100},
                  "stats": {}, "ok": True, "reason": "ok"} for i in range(10)]
        roster = bot_copy.pick_roster(cands, CFG["copy"])
        self.assertEqual(len(roster), CFG["copy"]["n_traders"])

    def test_start_args_include_all_risk_caps(self):
        args = bot_copy.start_args("0xabc", CFG["copy"])
        for flag in ("--amount", "--max-trade-size", "--daily-limit",
                     "--mirror-percent-cap", "--budget", "--max-per-market",
                     "--exit-behavior", "--yes"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--exit-behavior") + 1], "mirror_sells")


class TestArbMath(unittest.TestCase):
    def test_multi_outcome_edge(self):
        # 3 outcomes at 30+30+35 = 95¢ for a $1 payout → 5¢ edge.
        self.assertAlmostEqual(bot_polyarb.basket_edge([0.30, 0.30, 0.35], 1.0, 0), 0.05)

    def test_fees_reduce_edge(self):
        edge = bot_polyarb.basket_edge([0.30, 0.30, 0.35], 1.0, 100)  # 1%
        self.assertAlmostEqual(edge, 0.05 - 0.0095)

    def test_negrisk_no_basket_edge(self):
        # 4 mutually exclusive markets, NO asks sum to 2.90, payout ≥ 3.
        self.assertAlmostEqual(
            bot_polyarb.basket_edge([0.75, 0.72, 0.71, 0.72], 3.0, 0), 0.10)

    def test_fair_book_has_no_edge(self):
        self.assertLess(bot_polyarb.basket_edge([0.50, 0.51], 1.0, 0), 0.0)

    def test_sizing_depth_limited(self):
        legs = [{"ask": 0.30, "ask_size": 30}, {"ask": 0.65, "ask_size": 300}]
        # depth cap = 30/3 = 10 shares; stake cap = 25/0.95 ≈ 26 → 10.
        self.assertEqual(bot_polyarb.size_basket(legs, 0.95, 25.0, 3.0), 10.0)

    def test_sizing_stake_limited(self):
        legs = [{"ask": 0.30, "ask_size": 3000}, {"ask": 0.65, "ask_size": 3000}]
        self.assertEqual(bot_polyarb.size_basket(legs, 0.95, 25.0, 3.0), 26.0)

    def test_sizing_zero_when_too_thin(self):
        legs = [{"ask": 0.30, "ask_size": 2}, {"ask": 0.65, "ask_size": 300}]
        self.assertEqual(bot_polyarb.size_basket(legs, 0.95, 25.0, 3.0), 0.0)


def _mkt(yes=0.05, vol=100_000, hrs=24):
    from datetime import datetime, timedelta, timezone
    end = (datetime.now(timezone.utc) + timedelta(hours=hrs)).isoformat()
    return {"slug": "m", "outcomes": [{"name": "Yes", "price": yes},
                                      {"name": "No", "price": 1 - yes}],
            "volume_24h": vol, "end_date": end}


class TestSellerFilters(unittest.TestCase):
    def test_good_longshot_passes(self):
        ok, why = bot_polyseller.check_market(_mkt(), CFG["seller"])
        self.assertTrue(ok, why)

    def test_not_a_longshot_rejected(self):
        ok, _ = bot_polyseller.check_market(_mkt(yes=0.20), CFG["seller"])
        self.assertFalse(ok)

    def test_thin_volume_rejected(self):
        ok, _ = bot_polyseller.check_market(_mkt(vol=500), CFG["seller"])
        self.assertFalse(ok)

    def test_too_far_out_rejected(self):
        ok, _ = bot_polyseller.check_market(_mkt(hrs=500), CFG["seller"])
        self.assertFalse(ok)

    def test_resolving_too_soon_rejected(self):
        ok, _ = bot_polyseller.check_market(_mkt(hrs=1), CFG["seller"])
        self.assertFalse(ok)

    def test_multi_outcome_market_rejected(self):
        m = _mkt()
        m["outcomes"] = [{"name": "T1", "price": 0.5},
                         {"name": "KC", "price": 0.5}]
        ok, why = bot_polyseller.check_market(m, CFG["seller"])
        self.assertFalse(ok)
        self.assertIn("binary", why)

    def test_book_band_and_depth(self):
        cfg = CFG["seller"]
        good = {"asks": [{"price": 0.95, "size": 500}], "spread": 0.01}
        ok, why, ask = bot_polyseller.check_book(good, cfg)
        self.assertTrue(ok, why)
        self.assertEqual(ask, 0.95)
        # 0.98 is above the band — residual can't cover a loss.
        high = {"asks": [{"price": 0.98, "size": 500}], "spread": 0.01}
        self.assertFalse(bot_polyseller.check_book(high, cfg)[0])
        # Wide spread = illiquid book.
        wide = {"asks": [{"price": 0.95, "size": 500}], "spread": 0.05}
        self.assertFalse(bot_polyseller.check_book(wide, cfg)[0])
        # Not enough depth for our stake.
        thin = {"asks": [{"price": 0.95, "size": 3}], "spread": 0.01}
        self.assertFalse(bot_polyseller.check_book(thin, cfg)[0])


class TestThetaLogic(unittest.TestCase):
    CFG = CFG["theta"]

    def test_favorite_side_found_either_way(self):
        m = {"outcomes": [{"name": "Yes", "price": 0.05},
                          {"name": "No", "price": 0.95}]}
        self.assertEqual(bot_polytheta.favorite_outcome(m, self.CFG)["name"], "No")
        m2 = {"outcomes": [{"name": "T1", "price": 0.93},
                           {"name": "KC", "price": 0.07}]}
        self.assertEqual(bot_polytheta.favorite_outcome(m2, self.CFG)["name"], "T1")

    def test_no_favorite_in_band(self):
        m = {"outcomes": [{"name": "Yes", "price": 0.50},
                          {"name": "No", "price": 0.50}]}
        self.assertIsNone(bot_polytheta.favorite_outcome(m, self.CFG))

    def test_maker_bid_improves_but_never_crosses(self):
        # 2-tick spread: improve the bid by one tick.
        self.assertEqual(bot_polytheta.maker_bid(0.940, 0.960, self.CFG), 0.941)
        # 1-tick spread: join the bid, never take the ask.
        self.assertEqual(bot_polytheta.maker_bid(0.950, 0.951, self.CFG), 0.950)
        # Degenerate book: no quote.
        self.assertIsNone(bot_polytheta.maker_bid(0.95, 0.95, self.CFG))
        self.assertIsNone(bot_polytheta.maker_bid(0, 0.95, self.CFG))

    def _order(self):
        return {"logged_at": "2026-07-18T10:00:00+00:00",
                "outcome": "Yes", "entry_price": 0.941}

    def test_fill_requires_print_strictly_through(self):
        later = "2026-07-18T11:00:00+00:00"
        through = [{"outcome": "Yes", "price": 0.940, "timestamp": later}]
        at_limit = [{"outcome": "Yes", "price": 0.941, "timestamp": later}]
        wrong_outcome = [{"outcome": "No", "price": 0.900, "timestamp": later}]
        earlier = [{"outcome": "Yes", "price": 0.900,
                    "timestamp": "2026-07-18T09:00:00+00:00"}]
        self.assertTrue(bot_polytheta.print_fills(self._order(), through))
        self.assertFalse(bot_polytheta.print_fills(self._order(), at_limit))
        self.assertFalse(bot_polytheta.print_fills(self._order(), wrong_outcome))
        self.assertFalse(bot_polytheta.print_fills(self._order(), earlier))


def _feed_row(addr="0xW", ts="2026-07-18 16:00:00 UTC", slug="mkt-a",
              outcome="Yes", side="BUY", price=0.40, usd=1000, pnl=500000):
    return {"user_address": addr, "timestamp": ts, "market_slug": slug,
            "outcome": outcome, "side": side, "price": price,
            "size_usd": usd, "trader_pnl": pnl}


class TestWhaleflowLogic(unittest.TestCase):
    CFG = CFG["whaleflow"]

    def test_qualifies_filters(self):
        self.assertTrue(bot_whaleflow.qualifies(_feed_row(), self.CFG))
        self.assertFalse(bot_whaleflow.qualifies(_feed_row(pnl=50), self.CFG))
        self.assertFalse(bot_whaleflow.qualifies(_feed_row(usd=100), self.CFG))
        self.assertFalse(bot_whaleflow.qualifies(_feed_row(price=0.97), self.CFG))
        self.assertFalse(bot_whaleflow.qualifies(_feed_row(slug=""), self.CFG))

    def test_two_whales_confirm(self):
        obs = {"1": _feed_row(addr="0xA"), "2": _feed_row(addr="0xB")}
        sigs = bot_whaleflow.signals(obs, self.CFG)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["whales"], 2)

    def test_one_small_whale_not_confirmed(self):
        sigs = bot_whaleflow.signals({"1": _feed_row()}, self.CFG)
        self.assertEqual(sigs, [])

    def test_one_big_whale_confirms(self):
        sigs = bot_whaleflow.signals({"1": _feed_row(usd=5000)}, self.CFG)
        self.assertEqual(len(sigs), 1)

    def test_sells_do_not_build_entry_signal(self):
        obs = {"1": _feed_row(addr="0xA", side="SELL"),
               "2": _feed_row(addr="0xB", side="SELL")}
        self.assertEqual(bot_whaleflow.signals(obs, self.CFG), [])

    def test_exit_on_whale_sell_of_held_outcome(self):
        held = [{"slug": "mkt-a", "outcome": "Yes"}]
        obs = {"1": _feed_row(side="SELL")}
        self.assertEqual(bot_whaleflow.exit_signals(obs, held, self.CFG), held)
        self.assertEqual(bot_whaleflow.exit_signals(
            obs, [{"slug": "mkt-b", "outcome": "Yes"}], self.CFG), [])

    def test_window_prunes_old_observations(self):
        obs = {"old": _feed_row(ts="2026-07-17 00:00:00 UTC"),
               "new": _feed_row(ts=pl.now_iso())}
        pruned = bot_whaleflow.prune_window(obs, 6)
        self.assertIn("new", pruned)
        self.assertNotIn("old", pruned)


class TestCopyPaperSim(unittest.TestCase):
    CFG = CFG["copy"]

    def _journal(self):
        return {"strategy": "poly_copy", "bankroll": 100.0, "trades": []}

    def test_buy_opens_paper_position(self):
        j = self._journal()
        self.assertEqual(bot_copy.sim_row(j, _feed_row(), self.CFG), "entry")
        self.assertEqual(len(pl.open_trades(j)), 1)
        self.assertAlmostEqual(j["bankroll"], 95.0, places=1)

    def test_duplicate_buy_ignored(self):
        j = self._journal()
        bot_copy.sim_row(j, _feed_row(), self.CFG)
        self.assertIsNone(bot_copy.sim_row(j, _feed_row(), self.CFG))
        self.assertEqual(len(pl.open_trades(j)), 1)

    def test_mirror_sell_closes_at_traders_price(self):
        j = self._journal()
        bot_copy.sim_row(j, _feed_row(price=0.40), self.CFG)
        self.assertEqual(
            bot_copy.sim_row(j, _feed_row(side="SELL", price=0.60), self.CFG),
            "exit")
        t = j["trades"][0]
        self.assertEqual(t["exit_reason"], "MIRROR_SELL")
        self.assertGreater(t["pnl"], 0)

    def test_sell_without_position_ignored(self):
        j = self._journal()
        self.assertIsNone(
            bot_copy.sim_row(j, _feed_row(side="SELL"), self.CFG))

    def test_global_open_cap_respected(self):
        j = self._journal()
        cfg = dict(self.CFG, max_open_paper=2, max_per_market_usd=1000)
        for i in range(4):
            bot_copy.sim_row(j, _feed_row(slug=f"mkt-{i}"), cfg)
        self.assertEqual(len(pl.open_trades(j)), 2)

    def test_per_market_cap_respected(self):
        j = self._journal()
        bot_copy.sim_row(j, _feed_row(outcome="Yes"), self.CFG)
        # Same market, other outcome: 5+5 ≤ 10 cap → allowed.
        self.assertEqual(
            bot_copy.sim_row(j, _feed_row(outcome="No"), self.CFG), "entry")
        # Third entry would exceed the $10/market cap.
        self.assertIsNone(
            bot_copy.sim_row(j, _feed_row(outcome="Maybe"), self.CFG))


class TestGammaNormalizer(unittest.TestCase):
    RAW = {"slug": "will-x", "question": "Will X?",
           "outcomes": '["Yes","No"]', "outcomePrices": '[0.0035,0.9965]',
           "volume24hr": "5233764.542", "endDate": "2026-06-01T00:00:00Z",
           "events": [{"slug": "family-slug"}]}

    def test_raw_gamma_record_normalized(self):
        m = pl.normalize_market(self.RAW)
        self.assertEqual(m["outcomes"][0], {"name": "Yes", "price": 0.0035})
        self.assertAlmostEqual(m["volume_24h"], 5233764.542)
        self.assertEqual(m["event_slug"], "family-slug")
        self.assertEqual(bot_polyseller.event_key(m), "family-slug")

    def test_already_normalized_passes_through(self):
        m = _mkt()
        self.assertIs(pl.normalize_market(m), m)

    def test_normalized_record_flows_into_filter(self):
        ok, why = bot_polyseller.check_market(
            pl.normalize_market(self.RAW), CFG["seller"])
        # Fails only on the far-out end date — fields all parsed correctly.
        self.assertFalse(ok)
        self.assertIn("resolution", why)

    def test_negrisk_key_from_market_or_event(self):
        raw = dict(self.RAW, negRisk=True)
        self.assertTrue(pl.normalize_market(raw)["enable_neg_risk"])
        self.assertTrue(pl.normalize_market(
            self.RAW, {"enableNegRisk": True})["enable_neg_risk"])
        self.assertFalse(pl.normalize_market(self.RAW)["enable_neg_risk"])

    def test_defaults_keep_market_tradeable(self):
        m = pl.normalize_market(self.RAW)
        self.assertTrue(m["active"] and m["enable_order_book"])
        self.assertFalse(m["closed"])


class TestPolylib(unittest.TestCase):
    def test_cli_json_parse_skips_warning_lines(self):
        text = 'Warning: something\n{"ok": true, "x": 1}'
        self.assertEqual(pl._parse_cli_json(text), {"ok": True, "x": 1})

    def test_cli_json_parse_handles_arrays(self):
        self.assertEqual(pl._parse_cli_json('[1, 2]'), [1, 2])

    def test_trade_record_cost(self):
        t = pl.new_trade("slug", "title", "No", 5.26, 0.95, "s")
        self.assertAlmostEqual(t["cost"], 5.0, places=1)
        self.assertEqual(t["status"], "open")

    def test_auth_expired_token_not_authed(self):
        # Real post-expiry shape: logged_in stays true, token invalid.
        status = {"account": {"logged_in": True, "access_token_valid": False,
                              "access_token_status": "expired_refreshable"}}
        self.assertFalse(pl.auth_ok(status))

    def test_auth_valid_token_authed(self):
        self.assertTrue(pl.auth_ok(
            {"account": {"logged_in": True, "access_token_valid": True}}))
        self.assertTrue(pl.auth_ok({"health": {"token_valid": True}}))

    def test_auth_legacy_shape(self):
        self.assertFalse(pl.auth_ok(
            {"account": {"logged_in": False, "reauth_required": True}}))

    def test_config_merge_preserves_defaults(self):
        merged = pl._merge(pl.DEFAULT_CONFIG, {"seller": {"stake_usd": 9.0}})
        self.assertEqual(merged["seller"]["stake_usd"], 9.0)
        self.assertEqual(merged["seller"]["max_open"],
                         pl.DEFAULT_CONFIG["seller"]["max_open"])
        self.assertFalse(merged["live"])


if __name__ == "__main__":
    unittest.main()
