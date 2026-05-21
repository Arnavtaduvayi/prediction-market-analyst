#!/usr/bin/env python3
"""
Polymarket Smart Money Analyzer

Fetches the top traders by all-time PnL, pulls their trade histories,
and surfaces where they're collectively putting money — i.e., which
market + outcome has the highest consensus among the best traders.

Usage:
  python3 polymarket_analyst.py                        # default: top 100 traders, last 90 days
  python3 polymarket_analyst.py --traders 200 --days 30
  python3 polymarket_analyst.py --traders 50 --output json > signals.json
  python3 polymarket_analyst.py --wallet 0xABC...      # drill into one trader
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
REQUEST_DELAY = 0.4   # seconds between API calls — stay under rate limits
MAX_PAGE_SIZE = 500   # max trades per page (API cap)


# ── API helpers ────────────────────────────────────────────────────────────────

def _get(url: str, params: dict, retries: int = 3) -> list | dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  Rate limited — waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 400 and r.status_code < 500:
                return []  # client error — don't retry
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                return []
            time.sleep(2)
    return []


def fetch_leaderboard(max_traders: int = 200) -> list[dict]:
    traders, offset, page_size = [], 0, 50
    print(f"Fetching leaderboard (up to {max_traders} traders)...")
    while len(traders) < min(max_traders, 1000):
        remaining = min(max_traders, 1000) - len(traders)
        limit = min(page_size, remaining)
        batch = _get(f"{DATA_API}/v1/leaderboard", {
            "orderBy": "PNL",
            "timePeriod": "ALL",
            "limit": limit,
            "offset": offset,
        })
        if not batch:
            break
        traders.extend(batch)
        offset += len(batch)
        time.sleep(REQUEST_DELAY)
        print(f"  {len(traders)} traders fetched...", file=sys.stderr, end="\r")
    print(f"  {len(traders)} traders fetched.   ", file=sys.stderr)
    return traders


API_OFFSET_CAP = 3000  # Polymarket Data API rejects offsets beyond this

def fetch_trades(wallet: str, since_ts: int = 0) -> list[dict]:
    all_trades, offset = [], 0
    while offset <= API_OFFSET_CAP:
        batch = _get(f"{DATA_API}/trades", {
            "user": wallet,
            "limit": MAX_PAGE_SIZE,
            "offset": offset,
        })
        if not batch:
            break
        # Trades come back newest-first; stop when we hit the time cutoff
        cutoff_hit = False
        for trade in batch:
            if trade.get("timestamp", 0) < since_ts:
                cutoff_hit = True
                break
            all_trades.append(trade)
        if cutoff_hit or len(batch) < MAX_PAGE_SIZE:
            break
        offset += MAX_PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return all_trades


def fetch_positions(wallet: str) -> list[dict]:
    return _get(f"{DATA_API}/positions", {"user": wallet, "sizeThreshold": "0.01"}) or []


# ── Filtering ──────────────────────────────────────────────────────────────────

def filter_real_traders(traders: list[dict], min_pnl: float = 5_000, min_vol: float = 50_000) -> list[dict]:
    """Drop wallets that look like bots or dust accounts."""
    return [t for t in traders
            if (t.get("pnl") or 0) >= min_pnl
            and (t.get("vol") or 0) >= min_vol]


# ── Analysis ───────────────────────────────────────────────────────────────────

def compute_net_positions(trades: list[dict]) -> dict[tuple, dict]:
    """
    For each (slug, outcome) pair, compute net shares (BUY minus SELL)
    and total USD volume, weighted average price, and trade count.
    Returns only positions with net positive shares.
    """
    positions: dict[tuple, dict] = defaultdict(lambda: {
        "title": "", "slug": "", "outcome": "",
        "net_shares": 0.0, "total_usd": 0.0, "buy_usd": 0.0,
        "prices": [], "trade_count": 0,
        "last_trade_ts": 0,
    })
    for t in trades:
        slug = t.get("slug", "")
        outcome = t.get("outcome", "")
        side = t.get("side", "BUY")
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        usd = size * price
        ts = t.get("timestamp", 0)
        key = (slug, outcome)

        p = positions[key]
        p["title"] = t.get("title", slug)
        p["slug"] = slug
        p["outcome"] = outcome
        p["trade_count"] += 1
        p["prices"].append(price)
        p["last_trade_ts"] = max(p["last_trade_ts"], ts)

        if side == "BUY":
            p["net_shares"] += size
            p["buy_usd"] += usd
            p["total_usd"] += usd
        else:
            p["net_shares"] -= size
            p["total_usd"] += usd

    return {k: v for k, v in positions.items() if v["net_shares"] > 0.5}


def build_consensus(
    trades_by_wallet: dict[str, list[dict]],
    wallet_meta: dict[str, dict],
) -> list[dict]:
    """
    For each (slug, outcome), aggregate across all traders to find consensus.
    Returns a list of signal dicts sorted by trader count then total USD.
    """
    # signal_key -> {traders: set, buy_usd: float, trade_count: int, title: str, prices: list}
    signals: dict[tuple, dict] = defaultdict(lambda: {
        "title": "", "slug": "", "outcome": "",
        "wallets": set(), "buy_usd": 0.0, "trade_count": 0,
        "prices": [], "pnl_sum": 0.0,
    })

    for wallet, trades in trades_by_wallet.items():
        net_pos = compute_net_positions(trades)
        meta = wallet_meta.get(wallet, {})
        trader_pnl = meta.get("pnl", 0) or 0

        for (slug, outcome), pos in net_pos.items():
            key = (slug, outcome)
            s = signals[key]
            s["title"] = pos["title"]
            s["slug"] = slug
            s["outcome"] = outcome
            s["wallets"].add(wallet)
            s["buy_usd"] += pos["buy_usd"]
            s["trade_count"] += pos["trade_count"]
            s["prices"].extend(pos["prices"])
            s["pnl_sum"] += trader_pnl

    result = []
    for (slug, outcome), s in signals.items():
        avg_price = sum(s["prices"]) / len(s["prices"]) if s["prices"] else 0
        result.append({
            "title": s["title"],
            "slug": slug,
            "outcome": outcome,
            "trader_count": len(s["wallets"]),
            "buy_usd": s["buy_usd"],
            "avg_entry_price": avg_price,
            "trade_count": s["trade_count"],
            "combined_pnl_of_traders": s["pnl_sum"],
        })

    result.sort(key=lambda x: (x["trader_count"], x["buy_usd"]), reverse=True)
    return result


# ── Output ─────────────────────────────────────────────────────────────────────

def print_table(signals: list[dict], top_n: int = 25, min_traders: int = 2):
    filtered = [s for s in signals if s["trader_count"] >= min_traders][:top_n]
    if not filtered:
        print("No signals found matching the filters.")
        return

    print(f"\n{'='*100}")
    print(f"  SMART MONEY SIGNALS  —  top {top_n} markets by trader consensus")
    print(f"{'='*100}")
    print(f"{'Market':<48} {'Outcome':<8} {'Traders':>7}  {'Buy $USD':>10}  {'Avg Price':>9}  {'Traders PnL':>12}")
    print(f"{'-'*100}")
    for s in filtered:
        title = s["title"][:47] if s["title"] else s["slug"][:47]
        pnl_str = f"${s['combined_pnl_of_traders']:>10,.0f}"
        print(
            f"{title:<48} {s['outcome']:<8} {s['trader_count']:>7}  "
            f"${s['buy_usd']:>9,.0f}  {s['avg_entry_price']:>9.3f}  {pnl_str:>12}"
        )
    print(f"\n{len(filtered)} signals shown (min {min_traders} traders in consensus)")


def print_wallet_detail(wallet: str, trades: list[dict], positions: list[dict]):
    print(f"\n{'='*80}")
    print(f"  WALLET: {wallet}")
    print(f"{'='*80}")

    net_pos = compute_net_positions(trades)
    if not net_pos:
        print("  No open net positions found in fetched trades.")
    else:
        print(f"\n  Open net positions ({len(net_pos)}):\n")
        print(f"  {'Market':<48} {'Outcome':<8} {'Net Shares':>10}  {'Buy USD':>9}  {'Avg Price':>9}")
        print(f"  {'-'*90}")
        for (slug, outcome), p in sorted(net_pos.items(), key=lambda x: -x[1]["buy_usd"]):
            title = p["title"][:47]
            avg_price = sum(p["prices"]) / len(p["prices"]) if p["prices"] else 0
            print(f"  {title:<48} {outcome:<8} {p['net_shares']:>10,.1f}  ${p['buy_usd']:>8,.0f}  {avg_price:>9.3f}")

    print(f"\n  Recent trades ({min(len(trades), 10)} of {len(trades)}):\n")
    print(f"  {'Date':<12} {'Side':<5} {'Market':<40} {'Outcome':<8} {'Shares':>8}  {'Price':>6}")
    print(f"  {'-'*80}")
    for t in trades[:10]:
        dt = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        title = (t.get("title") or t.get("slug", "?"))[:39]
        print(f"  {dt:<12} {t['side']:<5} {title:<40} {t.get('outcome','?'):<8} "
              f"{t['size']:>8,.1f}  {t['price']:>6.3f}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Smart Money Analyzer — find where top traders are converging"
    )
    parser.add_argument("--traders", type=int, default=100,
                        help="Number of top traders to pull from leaderboard (default: 100, max: 1000)")
    parser.add_argument("--days", type=int, default=90,
                        help="Only analyze trades from the last N days (default: 90)")
    parser.add_argument("--min-pnl", type=float, default=5_000,
                        help="Minimum all-time PnL to include a trader (default: $5,000)")
    parser.add_argument("--min-vol", type=float, default=50_000,
                        help="Minimum all-time volume to include a trader (default: $50,000)")
    parser.add_argument("--min-consensus", type=int, default=2,
                        help="Minimum traders agreeing to surface a signal (default: 2)")
    parser.add_argument("--top", type=int, default=25,
                        help="Number of top signals to display (default: 25)")
    parser.add_argument("--output", choices=["table", "json"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--wallet", type=str, default=None,
                        help="Drill into a single wallet address instead of running consensus")
    args = parser.parse_args()

    since_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

    # ── Single wallet mode ──
    if args.wallet:
        print(f"Fetching trades for {args.wallet} (last {args.days} days)...")
        trades = fetch_trades(args.wallet, since_ts=since_ts)
        positions = fetch_positions(args.wallet)
        print(f"Fetched {len(trades)} trades.")
        print_wallet_detail(args.wallet, trades, positions)
        return

    # ── Consensus mode ──
    leaderboard = fetch_leaderboard(max_traders=args.traders)
    real_traders = filter_real_traders(leaderboard, min_pnl=args.min_pnl, min_vol=args.min_vol)
    wallet_meta = {t["proxyWallet"]: t for t in real_traders if t.get("proxyWallet")}

    print(f"Filtered to {len(wallet_meta)} real traders (from {len(leaderboard)} total)")
    print(f"Fetching trades (last {args.days} days) for each trader...\n")

    trades_by_wallet: dict[str, list[dict]] = {}
    total_trades = 0
    wallets = list(wallet_meta.keys())

    for i, wallet in enumerate(wallets):
        name = wallet_meta[wallet].get("userName") or wallet[:10]
        pnl = wallet_meta[wallet].get("pnl", 0)
        print(f"[{i+1}/{len(wallets)}] {name:<25}  (PnL: ${pnl:>12,.0f})", end="  ", flush=True)
        trades = fetch_trades(wallet, since_ts=since_ts)
        trades_by_wallet[wallet] = trades
        total_trades += len(trades)
        print(f"{len(trades)} trades")
        time.sleep(REQUEST_DELAY)

    print(f"\nFetched {total_trades:,} total trades across {len(trades_by_wallet)} traders.\n")

    signals = build_consensus(trades_by_wallet, wallet_meta)

    if args.output == "json":
        out = [s for s in signals if s["trader_count"] >= args.min_consensus][:args.top]
        print(json.dumps(out, indent=2))
    else:
        print_table(signals, top_n=args.top, min_traders=args.min_consensus)
        print(f"\nTip: run with --wallet 0xADDRESS to drill into any trader's positions.")


if __name__ == "__main__":
    main()
