"""
targets.py — Whale Identification

Implements the warproxxx/poly_data analysis approach via the public
Polymarket Data API.

Filter criteria (from the LunarResearcher methodology):
  - ≥ 100 trades on Polymarket
  - Win rate ≥ 70% (defined as: position resolved profitably)
  - Sorted by total PnL, top 50 saved

A "win" is computed from settled positions: if a trader bought YES at price
P and the market resolved YES, they profit (1 - P). If it resolved NO, they
lose P. NO bets are mirrored.

Output: targets.json — list of dicts with wallet, username, n_trades,
win_rate, total_pnl, primary_categories.

Usage:
  python3 targets.py                   # default: scan top 200 by PnL, filter
  python3 targets.py --candidates 500  # broader candidate pool
  python3 targets.py --refresh         # bypass cache, refetch everything
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
TARGETS_FILE = Path(__file__).parent / "data" / "targets.json"
CACHE_DIR = Path(__file__).parent / "data" / "wallet_cache"

REQUEST_DELAY = 0.4
MAX_TRADES_PER_WALLET = 3000  # API offset cap

MIN_TRADES = 100
MIN_WIN_RATE = 0.70
TOP_N = 50


def _get(url: str, params: dict = None) -> list | dict:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if 400 <= r.status_code < 500:
                return []
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return []
            time.sleep(2)
    return []


def fetch_candidate_pool(n: int = 500) -> list[dict]:
    """Pull n candidate wallets from the all-time PnL leaderboard."""
    candidates, offset = [], 0
    print(f"Fetching {n} candidates from Polymarket leaderboard...")
    while len(candidates) < n and offset < 1000:
        batch = _get(f"{DATA_API}/v1/leaderboard", {
            "orderBy": "PNL", "timePeriod": "ALL",
            "limit": min(50, n - len(candidates)), "offset": offset,
        })
        if not batch:
            break
        candidates.extend(batch)
        offset += len(batch)
        time.sleep(REQUEST_DELAY)
        print(f"  {len(candidates)}/{n}...", end="\r")
    print(f"  {len(candidates)} candidates loaded.")
    return candidates


def fetch_wallet_trades(wallet: str, use_cache: bool = True) -> list[dict]:
    """Fetch all (up to 3000) trades for one wallet. Cached per wallet."""
    cache_file = CACHE_DIR / f"{wallet}.json"
    if use_cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if cached.get("trades"):
                return cached["trades"]
        except Exception:
            pass

    trades, offset = [], 0
    while offset <= MAX_TRADES_PER_WALLET:
        batch = _get(f"{DATA_API}/trades", {
            "user": wallet, "limit": 500, "offset": offset,
        })
        if not batch:
            break
        trades.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(REQUEST_DELAY)

    if trades:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "wallet": wallet,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "trades": trades,
        }))
    return trades


def fetch_wallet_positions(wallet: str) -> list[dict]:
    """Fetch resolved positions to determine win/loss outcomes."""
    return _get(f"{DATA_API}/closed-positions", {"user": wallet}) or []


def compute_wallet_stats(wallet: str, trades: list[dict], positions: list[dict]) -> dict:
    """
    Compute trade count, win rate, total PnL, and category breakdown.

    Win rate methodology: a position is a "win" if realized PnL > 0.
    Closed positions give us the ground-truth outcome per market.
    """
    n_trades = len(trades)

    # Build per-market position view from closed positions
    wins = 0
    losses = 0
    total_pnl = 0.0
    categories = Counter()

    for p in positions:
        realized = float(p.get("realizedPnl") or 0)
        total_pnl += realized
        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1
        # Use eventSlug or market title as category proxy
        cat = p.get("eventSlug", "").split("-")[0][:15] if p.get("eventSlug") else "other"
        categories[cat] += 1

    closed_count = wins + losses
    win_rate = wins / closed_count if closed_count > 0 else 0.0

    return {
        "wallet": wallet,
        "n_trades": n_trades,
        "n_closed_positions": closed_count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "top_categories": [c for c, _ in categories.most_common(3)],
    }


def main():
    parser = argparse.ArgumentParser(description="Identify top Polymarket whales")
    parser.add_argument("--candidates", type=int, default=200,
                        help="Number of leaderboard candidates to evaluate")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES)
    parser.add_argument("--min-win-rate", type=float, default=MIN_WIN_RATE)
    parser.add_argument("--top", type=int, default=TOP_N)
    parser.add_argument("--refresh", action="store_true",
                        help="Bypass per-wallet cache")
    args = parser.parse_args()

    candidates = fetch_candidate_pool(n=args.candidates)
    leaderboard_meta = {c["proxyWallet"]: c for c in candidates if c.get("proxyWallet")}

    print(f"\nEvaluating {len(leaderboard_meta)} candidate wallets...")
    print(f"  Criteria: ≥{args.min_trades} trades AND win rate ≥{args.min_win_rate:.0%}\n")

    qualified = []
    for i, (wallet, meta) in enumerate(leaderboard_meta.items(), 1):
        name = meta.get("userName") or wallet[:10]
        trades = fetch_wallet_trades(wallet, use_cache=not args.refresh)
        if len(trades) < args.min_trades:
            print(f"  [{i}/{len(leaderboard_meta)}] {name:<22}  {len(trades):>5} trades  SKIP")
            continue

        positions = fetch_wallet_positions(wallet)
        time.sleep(REQUEST_DELAY)
        stats = compute_wallet_stats(wallet, trades, positions)
        stats["username"] = name
        stats["leaderboard_pnl"] = meta.get("pnl", 0)
        stats["leaderboard_vol"] = meta.get("vol", 0)

        mark = "✓" if stats["win_rate"] >= args.min_win_rate else "✗"
        print(f"  [{i}/{len(leaderboard_meta)}] {name:<22}  "
              f"{stats['n_trades']:>5} trades  WR {stats['win_rate']:>5.1%}  "
              f"PnL ${stats['total_pnl']:>10,.0f}  {mark}")

        if stats["win_rate"] >= args.min_win_rate:
            qualified.append(stats)

    qualified.sort(key=lambda s: s["total_pnl"], reverse=True)
    top = qualified[:args.top]

    TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TARGETS_FILE.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_trades": args.min_trades,
            "min_win_rate": args.min_win_rate,
        },
        "n_qualified": len(qualified),
        "targets": top,
    }, indent=2))

    print(f"\n{'='*70}")
    print(f"  WHALE TARGET LIST — {len(top)} of {len(qualified)} qualified wallets")
    print(f"{'='*70}")
    for t in top[:20]:
        print(f"  {t['username']:<22}  {t['n_trades']:>5} trades  "
              f"WR {t['win_rate']:>5.1%}  PnL ${t['total_pnl']:>12,.0f}")
    if len(top) > 20:
        print(f"  ... and {len(top) - 20} more in targets.json")

    print(f"\nSaved to {TARGETS_FILE}")


if __name__ == "__main__":
    main()
