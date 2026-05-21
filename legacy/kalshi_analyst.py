#!/usr/bin/env python3
"""
Kalshi Smart Money Analyzer

Kalshi is a CFTC-regulated US prediction market exchange.
Unlike Polymarket (on-chain/public wallets), Kalshi is a centralized exchange —
individual account histories are private. Smart money signals are instead derived
from aggregate public trade flow using order flow imbalance and volume analysis.

How it works:
  1. Fetch all open Kalshi markets
  2. Pull recent public trade flow for each market (GET /markets/trades — no auth)
  3. Compute order flow imbalance, volume spikes, and price-vs-flow divergences
  4. Surface the markets where informed buying/selling pressure is strongest

Usage:
  python3 kalshi_analyst.py                          # scan all open markets
  python3 kalshi_analyst.py --category politics      # filter by category
  python3 kalshi_analyst.py --ticker INXD-24DEC31-B4900  # single market deep-dive
  python3 kalshi_analyst.py --hours 6                # tighten time window
  python3 kalshi_analyst.py --output json > out.json
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_DELAY = 0.15   # seconds between requests — well under Basic tier 20 req/s
PAGE_SIZE = 1000


# ── API helpers ────────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, retries: int = 3) -> dict | list:
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  Rate limited — waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 400 and r.status_code < 500:
                return {}
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                return {}
            time.sleep(2)
    return {}


def fetch_active_tickers(hours: float = 24, max_tickers: int = 200) -> list[str]:
    """
    Discover which market tickers have had recent trades by sampling the
    platform-wide trade stream. This is more reliable than filtering the
    markets list (which is dominated by zero-volume MVE parlay markets).
    """
    min_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    tickers_seen: dict[str, int] = {}  # ticker -> trade count
    cursor = None
    pages = 0
    max_pages = 20  # cap at 20k trades scanned

    print(f"Discovering active tickers from trade stream (last {hours}h)...", file=sys.stderr)
    while pages < max_pages:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets/trades", params)
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
            if ts < min_ts:
                cutoff_hit = True
                break
            ticker = t.get("ticker", "")
            tickers_seen[ticker] = tickers_seen.get(ticker, 0) + 1

        cursor = data.get("cursor")
        pages += 1
        time.sleep(REQUEST_DELAY)

        if cutoff_hit or not cursor:
            break

    # Sort by trade count, return top N tickers
    sorted_tickers = sorted(tickers_seen, key=lambda t: tickers_seen[t], reverse=True)
    print(f"  Found {len(sorted_tickers)} active tickers ({sum(tickers_seen.values())} trades scanned)", file=sys.stderr)
    return sorted_tickers[:max_tickers]


def fetch_market(ticker: str) -> dict:
    """Fetch metadata for a single market."""
    data = _get(f"/markets/{ticker}")
    return data.get("market", {})


def fetch_market_trades(ticker: str, min_ts: int, max_ts: int) -> list[dict]:
    """Fetch all public trades for one market within a time window."""
    trades, cursor = [], None
    while True:
        params = {
            "ticker": ticker,
            "min_ts": min_ts,
            "max_ts": max_ts,
            "limit": PAGE_SIZE,
        }
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets/trades", params)
        batch = data.get("trades", [])
        trades.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(REQUEST_DELAY)
    return trades


def fetch_single_market(ticker: str) -> dict:
    return fetch_market(ticker)


# ── Signal computation ─────────────────────────────────────────────────────────

@dataclass
class MarketSignal:
    ticker: str
    title: str
    yes_price: float       # current YES price (0–1)
    volume_contracts: float
    trade_count: int
    ofi_score: float       # order flow imbalance: +1 = all buys, -1 = all sells
    yes_buy_usd: float     # total USD going into YES
    no_buy_usd: float      # total USD going into NO
    volume_spike: float    # ratio of recent vol to baseline (>1 = spike)
    price_drift: float     # price change over window (positive = rising)
    close_time: Optional[str] = None
    category: str = ""
    signal_score: float = 0.0


def compute_signal(ticker: str, market: dict, trades: list[dict], baseline_trades: list[dict]) -> Optional[MarketSignal]:
    if not trades:
        return None

    yes_price_str = market.get("yes_ask_dollars") or market.get("last_price_dollars") or "0.5"
    yes_price = float(yes_price_str) if yes_price_str else 0.5

    yes_buy_usd = no_buy_usd = 0.0
    yes_vol = no_vol = 0

    for t in trades:
        count = float(t.get("count_fp") or t.get("count") or 0)
        yes_p = float(t.get("yes_price_dollars") or "0.5")
        no_p = float(t.get("no_price_dollars") or "0.5")
        taker_side = t.get("taker_outcome_side", "yes")  # which side the taker bought
        usd = count * yes_p if taker_side == "yes" else count * no_p

        if taker_side == "yes":
            yes_buy_usd += usd
            yes_vol += count
        else:
            no_buy_usd += usd
            no_vol += count

    total_vol = yes_vol + no_vol
    if total_vol == 0:
        return None

    ofi = (yes_vol - no_vol) / total_vol  # order flow imbalance: +1 = all YES buys

    # Price drift: compare first vs last trade price
    sorted_trades = sorted(trades, key=lambda t: t.get("created_time", ""))
    first_price = float(sorted_trades[0].get("yes_price_dollars") or "0.5")
    last_price = float(sorted_trades[-1].get("yes_price_dollars") or "0.5")
    price_drift = last_price - first_price

    # Volume spike: compare recent window to baseline window
    baseline_vol = sum(
        float(t.get("count_fp") or t.get("count") or 0) for t in baseline_trades
    ) if baseline_trades else 0
    volume_spike = (total_vol / baseline_vol) if baseline_vol > 0 else (2.0 if total_vol > 10 else 1.0)

    # Signal score: combines OFI strength, volume spike, and price movement alignment
    ofi_magnitude = abs(ofi)
    alignment = 1.0 if (ofi > 0 and price_drift > 0) or (ofi < 0 and price_drift < 0) else 0.3
    score = ofi_magnitude * min(volume_spike, 5.0) * alignment * (1 + abs(price_drift) * 10)

    return MarketSignal(
        ticker=ticker,
        title=market.get("title", ticker),
        yes_price=yes_price,
        volume_contracts=total_vol,
        trade_count=len(trades),
        ofi_score=ofi,
        yes_buy_usd=yes_buy_usd,
        no_buy_usd=no_buy_usd,
        volume_spike=volume_spike,
        price_drift=price_drift,
        close_time=market.get("close_time", ""),
        category=market.get("event_ticker", "").split("-")[0] if market.get("event_ticker") else "",
        signal_score=score,
    )


# ── Output ─────────────────────────────────────────────────────────────────────

DIRECTION_EMOJI = {True: "YES ↑", False: "NO  ↓"}


def direction_label(sig: MarketSignal) -> str:
    buying_yes = sig.yes_buy_usd >= sig.no_buy_usd
    return "→ YES" if buying_yes else "→ NO "


def print_table(signals: list[MarketSignal], top_n: int = 25):
    filtered = [s for s in signals if s.trade_count >= 3][:top_n]
    if not filtered:
        print("No signals found. Try widening --hours or removing --category filter.")
        return

    print(f"\n{'='*110}")
    print(f"  KALSHI SMART MONEY SIGNALS  —  order flow imbalance + volume analysis")
    print(f"{'='*110}")
    print(f"{'Market':<50} {'Curr':>5} {'Dir':>6} {'OFI':>6} {'Vol Spike':>9} {'YES $':>9} {'NO $':>9} {'Score':>7}")
    print(f"{'-'*110}")
    for s in filtered:
        title = s.title[:49]
        ofi_str = f"{s.ofi_score:+.2f}"
        spike_str = f"{s.volume_spike:.1f}x"
        direction = direction_label(s)
        print(
            f"{title:<50} {s.yes_price:>5.3f} {direction:>6} {ofi_str:>6} {spike_str:>9} "
            f"${s.yes_buy_usd:>8,.0f} ${s.no_buy_usd:>8,.0f} {s.signal_score:>7.3f}"
        )
    print(f"\n{'='*110}")
    print(f"OFI = Order Flow Imbalance (+1 = all YES buys, -1 = all NO buys)")
    print(f"Vol Spike = recent volume / baseline volume. >2x = unusual activity.")
    print(f"Score = OFI × spike × alignment with price movement")
    print(f"\n{len(filtered)} signals shown.")


def print_single_market(ticker: str, market: dict, trades: list[dict]):
    yes_price_str = market.get("yes_ask_dollars") or market.get("last_price_dollars") or "N/A"
    print(f"\n{'='*80}")
    print(f"  {market.get('title', ticker)}")
    print(f"  Ticker: {ticker}  |  YES price: {yes_price_str}  |  Status: {market.get('status', '?')}")
    print(f"  Closes: {market.get('close_time', 'unknown')}")
    print(f"{'='*80}")

    if not trades:
        print("  No recent trades found.")
        return

    print(f"\n  Recent {min(len(trades), 20)} trades:\n")
    print(f"  {'Time (UTC)':<22} {'Side':<6} {'Contracts':>10}  {'YES Price':>9}")
    print(f"  {'-'*52}")
    for t in trades[:20]:
        dt = t.get("created_time", "?")[:19].replace("T", " ")
        side = t.get("taker_outcome_side", "?").upper()
        count = float(t.get("count_fp") or t.get("count") or 0)
        yes_p = t.get("yes_price_dollars", "?")
        print(f"  {dt:<22} {side:<6} {count:>10.2f}  {yes_p:>9}")

    yes_vol = sum(float(t.get("count_fp") or t.get("count") or 0)
                  for t in trades if t.get("taker_outcome_side") == "yes")
    no_vol = sum(float(t.get("count_fp") or t.get("count") or 0)
                 for t in trades if t.get("taker_outcome_side") == "no")
    total = yes_vol + no_vol
    ofi = (yes_vol - no_vol) / total if total > 0 else 0.0

    print(f"\n  Summary ({len(trades)} trades):")
    print(f"    YES contracts bought: {yes_vol:,.1f}")
    print(f"    NO  contracts bought: {no_vol:,.1f}")
    print(f"    Order flow imbalance: {ofi:+.3f}  ({'smart money buying YES' if ofi > 0.2 else 'smart money buying NO' if ofi < -0.2 else 'balanced'})")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Smart Money Analyzer — detect order flow imbalances in US prediction markets"
    )
    parser.add_argument("--hours", type=float, default=24,
                        help="Analyze trades from the last N hours (default: 24)")
    parser.add_argument("--baseline-hours", type=float, default=72,
                        help="Baseline window for volume spike detection (default: 72h)")
    parser.add_argument("--markets", type=int, default=150,
                        help="Max open markets to scan (default: 150)")
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum trades in window to include a market (default: 3)")
    parser.add_argument("--top", type=int, default=25,
                        help="Number of top signals to show (default: 25)")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter by event_ticker prefix (e.g. 'INX', 'KXBTC', 'PRES')")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Deep-dive into a single market ticker")
    parser.add_argument("--output", choices=["table", "json"], default="table")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    recent_min_ts = int((now - timedelta(hours=args.hours)).timestamp())
    recent_max_ts = int(now.timestamp())
    baseline_min_ts = int((now - timedelta(hours=args.baseline_hours)).timestamp())

    # ── Single ticker deep-dive ──
    if args.ticker:
        print(f"Fetching market data for {args.ticker}...")
        market = fetch_single_market(args.ticker)
        if not market:
            print(f"Market {args.ticker} not found.")
            sys.exit(1)
        print(f"Fetching trades (last {args.hours}h)...")
        trades = fetch_market_trades(args.ticker, recent_min_ts, recent_max_ts)
        print_single_market(args.ticker, market, trades)
        return

    # ── Full market scan ──
    # Discover which tickers are actually active from the trade stream
    active_tickers = fetch_active_tickers(hours=max(args.hours * 3, args.baseline_hours), max_tickers=args.markets)

    if args.category:
        active_tickers = [t for t in active_tickers if args.category.upper() in t.upper()]
        print(f"Filtered to {len(active_tickers)} tickers matching '{args.category}'")

    if not active_tickers:
        print("No active markets found. Try widening --hours or removing --category filter.")
        sys.exit(1)

    print(f"\nAnalyzing {len(active_tickers)} active markets (last {args.hours}h vs {args.baseline_hours}h baseline)...\n")

    signals = []
    for i, ticker in enumerate(active_tickers):
        market = fetch_market(ticker)
        title = (market.get("title") or ticker)[:45]
        print(f"[{i+1}/{len(active_tickers)}] {title:<45}", end="  ", flush=True)

        recent_trades = fetch_market_trades(ticker, recent_min_ts, recent_max_ts)
        baseline_trades = fetch_market_trades(ticker, baseline_min_ts, recent_min_ts)
        time.sleep(REQUEST_DELAY)

        print(f"{len(recent_trades)} trades")

        if len(recent_trades) < args.min_trades:
            continue

        sig = compute_signal(ticker, market, recent_trades, baseline_trades)
        if sig:
            signals.append(sig)

    signals.sort(key=lambda s: s.signal_score, reverse=True)

    if args.output == "json":
        out = []
        for s in signals[:args.top]:
            out.append({
                "ticker": s.ticker,
                "title": s.title,
                "yes_price": s.yes_price,
                "direction": "YES" if s.yes_buy_usd >= s.no_buy_usd else "NO",
                "ofi_score": round(s.ofi_score, 4),
                "volume_spike": round(s.volume_spike, 2),
                "yes_buy_usd": round(s.yes_buy_usd, 2),
                "no_buy_usd": round(s.no_buy_usd, 2),
                "trade_count": s.trade_count,
                "price_drift": round(s.price_drift, 4),
                "signal_score": round(s.signal_score, 4),
                "close_time": s.close_time,
            })
        print(json.dumps(out, indent=2))
    else:
        print_table(signals, top_n=args.top)


if __name__ == "__main__":
    main()
