"""
disposition.py — Alternate Bot (Bot B)

Strategy: systematic favorite-longshot bias exploitation.

Source: Karl Whelan / GWU "Makers and Takers: The Economics of the Kalshi
Prediction Market" — analysis of 72 million Kalshi trades.

Findings:
  - Contracts priced <$0.10:  buyers lose >60% of money as takers
  - Contracts priced  $0.05:  win 4.18% of time vs 5% implied (-16% mispricing)
  - Contracts priced  $0.01:  win 0.43% of time vs 1% implied (-57% mispricing)
  - Contracts priced >$0.50:  statistically positive return for buyers
  - Effect is much stronger for takers than makers

Strategy translation:
  - YES price < 0.10: BUY NO (sell the longshot — collect the bias)
  - YES price > 0.90: BUY YES (favorites are slightly under-priced)
  - Stay in until settlement (the edge plays out at resolution; early
    exits forfeit the bias)

Differences from whale-copy bot:
  - No Polymarket dependency (only Kalshi data needed)
  - No exit triggers (holds to settlement)
  - Smaller edges per trade (~1-3%) but more consistent
  - Trades any active Kalshi market, including sports (the bias is universal)

Output: data/disposition_signals.json
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
SIGNALS_FILE = Path(__file__).parent / "data" / "disposition_signals.json"

# Disposition thresholds (calibrated from Whelan paper)
LONGSHOT_MAX_PRICE = 0.10       # YES < $0.10 → BUY NO
FAVORITE_MIN_PRICE = 0.90       # YES > $0.90 → BUY YES

# Estimated edge per category (from Whelan paper)
LONGSHOT_EDGE = 0.04            # ~4% expected return on NO bets vs longshots
FAVORITE_EDGE = 0.02            # ~2% expected return on YES bets on favorites


def _get(url: str, params: dict = None) -> dict:
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


def find_disposition_signals(markets: list[dict]) -> list[dict]:
    """For each scanner survivor, check if it qualifies as a disposition signal."""
    signals = []
    for m in markets:
        mid = m.get("yes_mid", 0)
        bid = m.get("yes_bid", 0)
        ask = m.get("yes_ask", 0)

        if mid <= 0 or mid >= 1:
            continue
        if bid <= 0 or ask <= 0:
            continue

        if mid < LONGSHOT_MAX_PRICE:
            # Longshot — BUY NO. We're betting the unlikely outcome doesn't happen.
            # Our fill price for NO = (1 - YES ask), the worse side of the book.
            no_fill_price = round(1.0 - ask, 4)
            # Implied YES probability after bias correction: lower than market suggests
            # because longshots are systematically overpriced
            true_yes_prob = mid * 0.84  # 16% bias correction at the 5¢ level
            true_no_prob = 1.0 - true_yes_prob
            edge = true_no_prob - (1.0 - mid)  # how much more often NO actually wins
            signals.append({
                "ticker": m["ticker"],
                "title": m.get("title", ""),
                "type": "longshot_sell",
                "side": "no",
                "fill_price": no_fill_price,
                "yes_mid": mid,
                "true_prob": round(true_no_prob, 4),
                "edge": round(edge, 4),
                "hours_left": m.get("hours_left", 0),
                "close_time": m.get("close_time", ""),
            })

        elif mid > FAVORITE_MIN_PRICE:
            # Favorite — BUY YES. Favorites under-priced (small positive return per Whelan).
            yes_fill_price = round(ask, 4)
            true_yes_prob = mid + FAVORITE_EDGE  # small positive bias
            edge = true_yes_prob - mid
            signals.append({
                "ticker": m["ticker"],
                "title": m.get("title", ""),
                "type": "favorite_buy",
                "side": "yes",
                "fill_price": yes_fill_price,
                "yes_mid": mid,
                "true_prob": round(min(true_yes_prob, 0.99), 4),
                "edge": round(edge, 4),
                "hours_left": m.get("hours_left", 0),
                "close_time": m.get("close_time", ""),
            })

    signals.sort(key=lambda s: s["edge"], reverse=True)
    return signals


def run():
    if not QUEUE_FILE.exists():
        print(f"No queue at {QUEUE_FILE} — run scanner.py first")
        return {"signals": []}

    queue = json.loads(QUEUE_FILE.read_text())
    markets = queue.get("markets", [])
    print(f"Disposition: scanning {len(markets)} markets for longshot/favorite bias...")

    signals = find_disposition_signals(markets)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_scanned": len(markets),
        "n_signals": len(signals),
        "signals": signals,
    }

    SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNALS_FILE.write_text(json.dumps(result, indent=2))

    longshots = [s for s in signals if s["type"] == "longshot_sell"]
    favorites = [s for s in signals if s["type"] == "favorite_buy"]

    print(f"\n  DISPOSITION RESULTS")
    print(f"  Markets scanned:  {len(markets)}")
    print(f"  Longshot signals: {len(longshots)} (BUY NO on contracts < $0.10)")
    print(f"  Favorite signals: {len(favorites)} (BUY YES on contracts > $0.90)")
    if signals:
        print(f"\n  Top signals:")
        for s in signals[:8]:
            t = "LONGSHOT" if s["type"] == "longshot_sell" else "FAVORITE"
            print(f"    {t} {s['ticker']:<42} mid={s['yes_mid']:.3f}  "
                  f"side={s['side'].upper()}  fill=${s['fill_price']:.3f}  edge={s['edge']:+.3f}")
    return result


if __name__ == "__main__":
    run()
