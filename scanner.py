"""
scanner.py — Market Scoring (Agent 1)

For each active Kalshi market, score on three factors and kill anything
failing the thresholds. From the LunarResearcher methodology — 93% of
markets die at this stage. That's the point.

Filters:
  - gap        : price vs Polymarket-implied probability ≥ 7%
  - depth      : both bid and ask sizes worth ≥ $500
  - hours_left : 4 ≤ hours_to_resolution ≤ 168 (4h to 7d)
  - volume     : 24h volume ≥ $10,000 (slippage tax otherwise)

Categories filtered OUT:
  - Sports markets (data priced in faster than we can react — proven 52% WR)

Output: queue.json — list of candidate Kalshi markets with scoring metadata
"""

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
TARGETS_FILE = Path(__file__).parent / "data" / "targets.json"

# Scanner thresholds — calibrated for ~3-5 trades/day at $75 bankroll.
# Loosened from guide defaults because Kalshi has less liquidity than Polymarket.
MIN_GAP = 0.05              # 5% min price gap
MIN_DEPTH_USD = 100.0       # $100 on each side of book (was $200)
# Dollar depth systematically kills longshot books (500 contracts resting at a
# 5¢ ask is only $25 "deep") — exactly the markets the seller bot needs. A
# book is also acceptable on raw contract count; the 24h volume filter still
# screens out dead markets.
MIN_DEPTH_CONTRACTS = 100
MIN_HOURS = 2               # at least 2h to resolution
MAX_HOURS = 168             # at most 7d
MIN_24H_VOLUME = 5_000      # $5k minimum daily volume

# Sports tickers to exclude (LunarResearcher killed sports — 52% WR)
SPORTS_PREFIXES = {
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMLS", "KXATP", "KXWTA",
    "KXUFC", "KXPGA", "KXEPL", "KXLALIGA", "KXBUNDESLIGA",
    "KXCZEFL", "KXUFL", "KXBBL", "KXCS2", "KXVALORANT", "KXNCAA",
    "KXBBLGAME", "KXSCOTTISHPREM", "KXSWISSLEAGUE", "KXMLBGAME",
    "KXMLBHIT", "KXMLBHR", "KXMLBTB", "KXMLBTOTAL", "KXMLBF5",
    "KXNHLGOAL", "KXNHLFIRSTGOAL", "KXUFCMOV", "KXUFCDISTANCE",
    "KXUFCVICROUND", "KXATPCHALLENGER", "KXATPEXACTMATCH",
    "KXATPSETWINNER", "KXATPGTOTAL", "KXNBAGAME", "KXNBAMENTION",
    "KXWTACHALLENGER", "KXITFMATCH", "KXFIGHTMENTION", "KXEUROVISION",
    "KXUAEPL", "KXINTLFRIENDLY", "KXLALIGA2", "KXPGATOUR",
    "KXPGAR3LEAD", "KXPGATOP5", "KXMLBTBL", "KXWNBAGAME",
    "KXIPL", "KXIPLGAME",  # cricket
}


def _get(url: str, params: dict = None) -> dict | list:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if 400 <= r.status_code < 500:
                return {}
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return {}
            time.sleep(2)
    return {}


def discover_active_kalshi_tickers(hours_back: float = 24, limit: int = 500) -> set[str]:
    """Find Kalshi markets with recent trade activity (proxy for liquidity)."""
    cutoff_ts = int((datetime.now(timezone.utc).timestamp() - hours_back * 3600))
    tickers: dict[str, int] = {}
    cursor = None
    pages = 0
    while pages < 12 and len(tickers) < limit:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{KALSHI_API}/markets/trades", params)
        batch = data.get("trades", [])
        if not batch:
            break
        cutoff_hit = False
        for t in batch:
            ts_str = t.get("created_time", "")
            try:
                ts = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
            if ts < cutoff_ts:
                cutoff_hit = True
                break
            tk = t.get("ticker", "")
            if tk and not _is_mve(tk):
                tickers[tk] = tickers.get(tk, 0) + 1
        cursor = data.get("cursor")
        pages += 1
        time.sleep(0.15)
        if cutoff_hit or not cursor:
            break
    sorted_tickers = sorted(tickers, key=lambda t: tickers[t], reverse=True)
    return set(sorted_tickers[:limit])


def _is_mve(ticker: str) -> bool:
    """Multivariate-event parlay markets are zero-volume noise."""
    return "MVE" in ticker or "MULTIGAME" in ticker


def _is_sports(ticker: str) -> bool:
    return any(ticker.startswith(p) for p in SPORTS_PREFIXES)


def fetch_market(ticker: str) -> dict:
    return _get(f"{KALSHI_API}/markets/{ticker}").get("market", {})


def hours_until(iso_time: str) -> float:
    try:
        close = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        delta = close - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600
    except Exception:
        return -1.0


def score_market(market: dict) -> dict | None:
    """
    Score a single Kalshi market. Returns scoring dict or None if it fails any filter.
    """
    ticker = market.get("ticker", "")
    if _is_sports(ticker):
        return {"ticker": ticker, "killed": "sports"}

    try:
        yes_bid = float(market.get("yes_bid_dollars") or 0)
        yes_ask = float(market.get("yes_ask_dollars") or 1)
        bid_size = float(market.get("yes_bid_size_fp") or 0)
        ask_size = float(market.get("yes_ask_size_fp") or 0)
        vol_24h = float(market.get("volume_24h_fp") or 0)
    except (ValueError, TypeError):
        return {"ticker": ticker, "killed": "bad_data"}

    if yes_bid <= 0 or yes_ask >= 1 or yes_ask <= yes_bid:
        return {"ticker": ticker, "killed": "no_book"}

    mid = (yes_bid + yes_ask) / 2

    # Depth check: dollar value OR raw contract count (see MIN_DEPTH_CONTRACTS)
    bid_value_usd = bid_size * yes_bid
    ask_value_usd = ask_size * yes_ask
    depth_usd = min(bid_value_usd, ask_value_usd)
    depth_contracts = min(bid_size, ask_size)
    if depth_usd < MIN_DEPTH_USD and depth_contracts < MIN_DEPTH_CONTRACTS:
        return {"ticker": ticker, "killed": f"thin_book_${depth_usd:.0f}"}

    # Volume check (proxies real liquidity)
    daily_volume_usd = vol_24h  # already in dollars per Kalshi docs
    if daily_volume_usd < MIN_24H_VOLUME:
        return {"ticker": ticker, "killed": f"low_vol_${daily_volume_usd:.0f}"}

    # Time-to-resolution check
    hours_left = hours_until(market.get("close_time", ""))
    if hours_left < MIN_HOURS:
        return {"ticker": ticker, "killed": f"too_soon_{hours_left:.1f}h"}
    if hours_left > MAX_HOURS:
        return {"ticker": ticker, "killed": f"too_far_{hours_left:.1f}h"}

    return {
        "ticker": ticker,
        "title": market.get("title", ""),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": round(mid, 4),
        "bid_size": bid_size,
        "ask_size": ask_size,
        "depth_usd": round(depth_usd, 2),
        "volume_24h_usd": round(daily_volume_usd, 2),
        "hours_left": round(hours_left, 1),
        "close_time": market.get("close_time", ""),
        "rules_primary": market.get("rules_primary", "")[:300],
        "yes_sub_title": market.get("yes_sub_title", ""),
        "no_sub_title": market.get("no_sub_title", ""),
    }


def scan() -> dict:
    """Main scan loop. Returns the queue and kill stats."""
    print("Discovering active Kalshi tickers from recent trade stream...")
    tickers = discover_active_kalshi_tickers(hours_back=48, limit=500)
    print(f"  {len(tickers)} active tickers found.\n")

    print("Scoring markets (gap/depth/hours/volume)...")
    survivors = []
    kills: dict[str, int] = {}
    for i, ticker in enumerate(sorted(tickers), 1):
        m = fetch_market(ticker)
        if not m:
            kills["fetch_failed"] = kills.get("fetch_failed", 0) + 1
            time.sleep(0.05)
            continue

        result = score_market(m)
        if result is None:
            kills["score_none"] = kills.get("score_none", 0) + 1
        elif "killed" in result:
            reason = result["killed"].split("_")[0] if "_" in result["killed"] else result["killed"]
            kills[reason] = kills.get(reason, 0) + 1
        else:
            survivors.append(result)

        if i % 50 == 0:
            time.sleep(0.5)
        else:
            time.sleep(0.08)

    queue = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_scanned": len(tickers),
        "n_survivors": len(survivors),
        "kill_stats": kills,
        "markets": survivors,
    }

    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))

    print(f"\n{'='*60}")
    print(f"  SCANNER RESULTS")
    print(f"{'='*60}")
    print(f"  Markets scanned:   {len(tickers)}")
    print(f"  Survivors:         {len(survivors)} ({len(survivors)/max(1,len(tickers))*100:.1f}%)")
    print(f"  Kill stats:")
    for reason, count in sorted(kills.items(), key=lambda x: -x[1]):
        print(f"    {reason:<20} {count}")
    if survivors:
        print(f"\n  Top survivors by depth:")
        for s in sorted(survivors, key=lambda x: -x["depth_usd"])[:10]:
            print(f"    {s['ticker']:<45} "
                  f"mid={s['yes_mid']:.3f}  depth=${s['depth_usd']:>7,.0f}  "
                  f"vol24h=${s['volume_24h_usd']:>7,.0f}  {s['hours_left']:>5.1f}h")
    print(f"\n  Saved to {QUEUE_FILE}")
    return queue


def loop(interval_seconds: int = 300):
    """Continuous mode: scan every N seconds. Used by systemd service."""
    import sys as _sys
    print(f"Scanner loop starting (interval={interval_seconds}s)", flush=True)
    while True:
        try:
            scan()
        except Exception as e:
            print(f"  Scanner error: {e}", file=_sys.stderr, flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true", help="Run continuously")
    p.add_argument("--interval", type=int, default=300, help="Seconds between scans in loop mode")
    args = p.parse_args()
    if args.loop:
        loop(args.interval)
    else:
        scan()
