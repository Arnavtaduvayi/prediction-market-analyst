"""
bot_whaleflow.py — Profitable-whale flow follower (Bot W).

Watches the public Polymarket trade feed and follows BUYS from wallets with
large positive lifetime P&L. Distinct from Bot P (copy): no subscriptions,
no fixed trader roster — any whale clearing the P&L bar counts, and entry
requires CONFIRMATION (two distinct qualifying whales buying the same
outcome inside the window, or one whale swinging very big).

Lineage honesty: naive whale-copy was retired at 48.4% WR vs 53.7% breakeven
over 93 settled. Three things are different here, and the journal exists to
test whether they matter: (1) only wallets with ≥$100k lifetime profit — the
old bot copied *large* wallets, not *profitable* ones; (2) multi-whale
confirmation instead of single-fill triggers; (3) minutes of lag instead of
hours. If this journal converges to the old result anyway, the thesis dies
and the bot gets retired like its ancestor.

The feed is a sample, not a firehose — observations accumulate across cycles
into a rolling window so slow cycles still see confirmation build up.
Qualifying whale SELLS in a market we hold trigger a mirror exit.
"""

import json
import sys
from pathlib import Path

import polylib as pl

JOURNAL_PATH = Path(__file__).resolve().parent / "live_whaleflow_trades.json"
OBS_PATH = pl.DATA / "whaleflow_obs.json"
STRATEGY = "poly_whaleflow"
INITIAL_BANKROLL = 100.0


# ── observation window (pure helpers, tested) ───────────────────────────────

def qualifies(row: dict, cfg: dict) -> bool:
    """A feed row worth remembering: a real-size trade by a proven-profitable
    wallet, in a market with room to be wrong about."""
    try:
        pnl = float(row.get("trader_pnl") or 0)
        usd = float(row.get("size_usd") or 0)
        price = float(row.get("price") or 0)
    except (ValueError, TypeError):
        return False
    return (pnl >= cfg["min_trader_pnl_usd"]
            and usd >= cfg["min_trade_usd"]
            and bool(row.get("market_slug"))
            and cfg["min_price"] <= price <= cfg["max_price"])


def obs_key(row: dict) -> str:
    return "|".join(str(row.get(k) or "") for k in
                    ("user_address", "timestamp", "market_slug", "side", "size_usd"))


def prune_window(obs: dict, window_hours: float) -> dict:
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    out = {}
    for k, row in obs.items():
        ts = pl.parse_feed_ts(row.get("timestamp"))
        if ts and ts.timestamp() > cutoff:
            out[k] = row
    return out


def signals(obs: dict, cfg: dict) -> list[dict]:
    """Aggregate BUY observations per (market, outcome); emit entries that
    clear the confirmation bar."""
    agg: dict[tuple, dict] = {}
    for row in obs.values():
        if (row.get("side") or "").upper() != "BUY":
            continue
        key = (row["market_slug"], row.get("outcome") or "")
        a = agg.setdefault(key, {"whales": set(), "total_usd": 0.0,
                                 "max_usd": 0.0, "last_price": 0.0})
        a["whales"].add(row.get("user_address") or "?")
        usd = float(row.get("size_usd") or 0)
        a["total_usd"] += usd
        if usd >= a["max_usd"]:
            a["max_usd"] = usd
            a["last_price"] = float(row.get("price") or 0)
    out = []
    for (slug, outcome), a in agg.items():
        confirmed = (len(a["whales"]) >= cfg["confirm_whales"]
                     or a["max_usd"] >= cfg["single_whale_usd"])
        if confirmed and outcome:
            out.append({"slug": slug, "outcome": outcome,
                        "whales": len(a["whales"]),
                        "total_usd": round(a["total_usd"], 2),
                        "ref_price": a["last_price"]})
    return out


def exit_signals(obs: dict, held: list[dict], cfg: dict) -> list[dict]:
    """Held positions whose outcome a qualifying whale has SOLD in-window."""
    sold = {(r["market_slug"], r.get("outcome") or "")
            for r in obs.values() if (r.get("side") or "").upper() == "SELL"}
    return [t for t in held if (t["slug"], t["outcome"]) in sold]


# ── I/O ─────────────────────────────────────────────────────────────────────

def _load_obs() -> dict:
    if OBS_PATH.exists():
        return json.loads(OBS_PATH.read_text())
    return {}


def collect(cfg: dict) -> dict:
    obs = prune_window(_load_obs(), cfg["window_hours"])
    rows = (pl.bp_ok("polymarket", "feed", "trades", timeout=45) or {}).get("trades") or []
    fresh = 0
    for row in rows:
        if qualifies(row, cfg):
            k = obs_key(row)
            if k not in obs:
                obs[k] = row
                fresh += 1
    OBS_PATH.write_text(json.dumps(obs, indent=1, default=str))
    print(f"  [whale] window: {len(obs)} obs (+{fresh} new)")
    return obs


def run(cfg_all: dict):
    cfg = cfg_all["whaleflow"]
    if not cfg["enabled"]:
        return
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    obs = collect(cfg)
    held = pl.open_trades(journal)

    # Mirror exits first — the whale who took us in is leaving.
    for t in exit_signals(obs, held, cfg):
        book = pl.fetch_orderbook(t["slug"], t["outcome"])
        bid = (book or {}).get("best_bid") or 0
        if bid <= 0:
            continue
        print(f"  [whale] MIRROR EXIT {t['slug'][:50]} @ ${bid:.3f}")
        pl.execute(cfg_all, ["polymarket", "sell", t["slug"], t["outcome"],
                             f"{t['shares']:.2f}", "--min-price", f"{bid:.3f}",
                             "--yes"],
                   {"bot": "whaleflow", "action": "mirror_sell",
                    "slug": t["slug"], "price": bid})
        proceeds = round(t["shares"] * bid, 4)
        t["pnl"] = round(proceeds - t["cost"], 4)
        t["status"] = "settled"
        t["settled_at"] = pl.now_iso()
        t["exit_reason"] = "MIRROR_SELL"
        journal["bankroll"] += proceeds
        pl.start_cooldown("whale:" + t["slug"])

    held_slugs = {t["slug"] for t in pl.open_trades(journal)}
    entered = 0
    for sig in signals(obs, cfg):
        if len(held_slugs) + entered >= cfg["max_open"]:
            break
        slug = sig["slug"]
        if slug in held_slugs or pl.in_cooldown("whale:" + slug):
            continue
        state = pl.normalize_market(
            (pl.bp_ok("polymarket", "market", slug, timeout=30) or {}).get("market")
            or {})
        hrs = pl.hours_until(state.get("end_date") or "")
        if hrs is None or hrs > cfg["max_hours_to_resolution"] or hrs <= 0:
            continue
        book = pl.fetch_orderbook(slug, sig["outcome"])
        top = pl.best_ask_with_depth(book) if book else None
        if top is None:
            continue
        ask, _ = top
        if ask > sig["ref_price"] + cfg["slippage"] or not \
                (cfg["min_price"] <= ask <= cfg["max_price"]):
            print(f"  [whale] skip {slug[:50]}: moved {sig['ref_price']:.3f}→{ask:.3f}")
            continue

        shares = round(cfg["stake_usd"] / ask, 2)
        print(f"  [whale] FOLLOW {sig['whales']}w ${sig['total_usd']:,.0f} → "
              f"BUY {sig['outcome']} {shares}sh @ ${ask:.3f}  {slug[:45]}")
        res = pl.execute(cfg_all, [
            "polymarket", "buy", slug, sig["outcome"],
            f"{cfg['stake_usd']:.2f}", "--max-price", f"{ask:.3f}", "--yes",
        ], {"bot": "whaleflow", "action": "follow_buy", "slug": slug,
            "outcome": sig["outcome"], "price": ask, **{k: sig[k] for k in
                                                        ("whales", "total_usd")}})
        journal["trades"].append(pl.new_trade(
            slug, state.get("question") or slug, sig["outcome"], shares, ask,
            STRATEGY, end_date=state.get("end_date"),
            whales=sig["whales"], whale_usd=sig["total_usd"],
            executed=bool(res.get("executed"))))
        journal["bankroll"] -= round(shares * ask, 4)
        pl.start_cooldown("whale:" + slug)
        entered += 1

    if entered == 0:
        print("  [whale] no confirmed signals")
    pl.save_journal(journal, JOURNAL_PATH)


if __name__ == "__main__":
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run(cfg_all)
    else:
        print("usage: bot_whaleflow.py [run]")
