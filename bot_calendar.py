"""
bot_calendar.py — Bot C: Strike Monotonicity Arbitrage

Strategy: pure structural arbitrage. No prediction required.

For markets with multiple strikes on the same underlying (e.g., Kalshi's
KXBTCD daily Bitcoin markets), prices MUST be monotonic in strike:

    P(BTC > $74,000) ≥ P(BTC > $74,500) ≥ P(BTC > $75,000) ≥ ...

When Kalshi violates this (thin order books cause it occasionally), there's
a free arbitrage. We enter both legs to lock in profit regardless of outcome.

Example violation:
    KXBTCD-26JUN05-T74000   YES @ $0.55
    KXBTCD-26JUN05-T74500   YES @ $0.62   ← higher strike, higher YES!

If BTC > $74,500: both YES sides resolve, +$0.45 + (-$0.38) = +$0.07
If BTC <= $74,500 but > $74,000: lower YES wins, higher loses → still profit
If BTC <= $74,000: both lose → max loss = $0.62 - $0.55 = -$0.07

Wait, that doesn't lock in profit. Let me re-derive.

Correct arb:
    BUY YES on the LOW-strike contract (cheaper than it should be)
    SELL YES (= BUY NO) on the HIGH-strike contract (which is over-priced)

    Cost: $0.55 (YES low) + (1 - $0.62) (NO high)  = $0.55 + $0.38 = $0.93
    Payout in all cases:
      BTC > $74,500: YES low pays $1, NO high pays $0 → total $1.00 → profit $0.07
      BTC ∈ ($74k, $74.5k]: YES low pays $1, NO high pays $1 → total $2.00 → profit $1.07
      BTC ≤ $74,000: YES low pays $0, NO high pays $1 → total $1.00 → profit $0.07

Minimum profit: $0.07 per pair. Risk-free if both fill at quoted prices.

Filters: only act when the violation gap is >= 2¢ (must cover bid-ask spread on both legs).
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from cooldown import is_in_cooldown

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_calendar_trades.json"
INITIAL_BANKROLL = 75.0
MIN_GAP = 0.02              # 2¢ minimum arbitrage gap
MAX_PER_LEG = 4.00          # max $4 per leg ($8 per arb pair)
MAX_OPEN_POSITIONS = 10


def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return {
        "strategy": "calendar_arb",
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(d: dict):
    JOURNAL_FILE.write_text(json.dumps(d, indent=2, default=str))


def _extract_strike(ticker: str) -> float | None:
    """Pull the numeric strike from a Kalshi ticker."""
    # Try common patterns: -T76499.99, -T78, -B85.5, -4.45
    for pat in (r"-T(\d+(?:\.\d+)?)", r"-B(\d+(?:\.\d+)?)", r"-(\d+\.\d+)$"):
        m = re.search(pat, ticker)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _market_family_key(ticker: str) -> str:
    """Group tickers from the same underlying market series + date."""
    # KXBTCD-26JUN05-T76499.99 → KXBTCD-26JUN05
    # KXBTCD-26JUN0517-T76499.99 → KXBTCD-26JUN0517
    parts = ticker.split("-")
    if len(parts) < 3:
        return ticker
    return "-".join(parts[:-1])


def find_arbs(markets: list[dict]) -> list[dict]:
    """Group by family, sort by strike, look for monotonicity violations."""
    families: dict[str, list[dict]] = {}
    for m in markets:
        ticker = m["ticker"]
        strike = _extract_strike(ticker)
        if strike is None:
            continue
        key = _market_family_key(ticker)
        m["_strike"] = strike
        families.setdefault(key, []).append(m)

    arbs = []
    for family_key, items in families.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x["_strike"])
        for i in range(len(items) - 1):
            low = items[i]
            high = items[i + 1]
            try:
                low_yes_ask = float(low.get("yes_ask", 0) or 0)
                high_yes_bid = float(high.get("yes_bid", 0) or 0)
            except (ValueError, TypeError):
                continue
            if low_yes_ask <= 0 or low_yes_ask >= 1:
                continue
            if high_yes_bid <= 0 or high_yes_bid >= 1:
                continue

            # Violation: lower-strike YES is CHEAPER than higher-strike YES
            # (impossible if monotonic — buy the cheap one, sell the expensive one)
            gap = high_yes_bid - low_yes_ask
            if gap >= MIN_GAP:
                # Cost: buy YES low + buy NO high  =  yes_ask_low + (1 - yes_bid_high)
                cost_per_pair = low_yes_ask + (1.0 - high_yes_bid)
                min_payout_per_pair = 1.0  # worst case both legs net $1
                min_profit = min_payout_per_pair - cost_per_pair
                if min_profit <= 0:
                    continue
                arbs.append({
                    "family": family_key,
                    "low_ticker": low["ticker"],
                    "low_title": low.get("title", ""),
                    "low_strike": low["_strike"],
                    "low_yes_ask": low_yes_ask,
                    "high_ticker": high["ticker"],
                    "high_title": high.get("title", ""),
                    "high_strike": high["_strike"],
                    "high_yes_bid": high_yes_bid,
                    "gap": round(gap, 4),
                    "cost_per_pair": round(cost_per_pair, 4),
                    "min_profit_per_pair": round(min_profit, 4),
                    "hours_left": min(low.get("hours_left", 999), high.get("hours_left", 999)),
                })

    arbs.sort(key=lambda x: x["min_profit_per_pair"], reverse=True)
    return arbs


def run():
    if not QUEUE_FILE.exists():
        print("[calendar] No queue — run scanner.py first")
        return

    queue = json.loads(QUEUE_FILE.read_text())
    markets = queue.get("markets", [])
    arbs = find_arbs(markets)

    data = load_journal()
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    slots = max(0, MAX_OPEN_POSITIONS - open_count)
    all_trades = list(data["trades"])

    print(f"[calendar] {len(markets)} markets scanned → {len(arbs)} monotonicity violations, {slots} slots")

    new_trades = []
    for arb in arbs:
        if len(new_trades) >= slots:
            break

        # Cooldown check on both legs
        if is_in_cooldown(arb["low_ticker"], all_trades) or is_in_cooldown(arb["high_ticker"], all_trades):
            continue

        # Skip if we already have positions in either leg
        if any(t["status"] == "open" and t["kalshi_ticker"] in (arb["low_ticker"], arb["high_ticker"])
               for t in data["trades"]):
            continue

        cost_per_pair = arb["cost_per_pair"]
        if cost_per_pair >= 1.0:
            continue

        # Size: spend up to MAX_PER_LEG dollars per leg, scaled by available bankroll
        target_per_pair = min(MAX_PER_LEG, data["bankroll"] * 0.05)
        pairs = max(1, int(target_per_pair / cost_per_pair))
        # Each "pair" = 1 low YES contract + 1 high NO contract
        total_cost = round(pairs * cost_per_pair, 2)
        if total_cost > data["bankroll"]:
            continue

        # Log as a pair-trade: 2 entries in the journal sharing arb_id
        arb_id = f"arb-{int(time.time())}-{arb['low_ticker'][:20]}"
        for leg in (
            ("low", arb["low_ticker"], arb["low_title"], "yes", arb["low_yes_ask"]),
            ("high", arb["high_ticker"], arb["high_title"], "no", 1.0 - arb["high_yes_bid"]),
        ):
            kind, ticker, title, side, fill_price = leg
            leg_cost = round(pairs * fill_price, 2)
            new_trades.append({
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "strategy": "calendar_arb",
                "arb_id": arb_id,
                "leg": kind,
                "kalshi_ticker": ticker,
                "kalshi_title": title,
                "side": side,
                "contracts": pairs,
                "entry_price": fill_price,
                "cost": leg_cost,
                "yes_mid_at_entry": fill_price,
                "min_profit_per_pair": arb["min_profit_per_pair"],
                "hold_to_settlement": True,
                "status": "open",
                "resolved_yes": None,
                "pnl": None,
                "settled_at": None,
                "exit_reason": None,
            })
            data["bankroll"] -= leg_cost

        print(f"  [calendar] ARB {arb_id[-10:]}  low={arb['low_ticker']} YES @ {arb['low_yes_ask']:.3f}  "
              f"high={arb['high_ticker']} NO @ {(1-arb['high_yes_bid']):.3f}  "
              f"min_profit/pair=${arb['min_profit_per_pair']:.3f}  pairs={pairs}")

    data["trades"].extend(new_trades)
    save_journal(data)
    print(f"  [calendar] {len(new_trades)//2} new arb pairs logged. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
