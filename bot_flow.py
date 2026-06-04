"""
bot_flow.py — Bot E: Order Flow Momentum

Strategy: when one side of the book is being aggressively hit by takers
for an extended period, that's informed flow. Bet WITH the flow.

For each surviving Kalshi market:
  1. Fetch trades from the last 30 minutes
  2. Compute volume share: taker_yes_usd / (taker_yes_usd + taker_no_usd)
  3. If > 70% one direction AND volume is meaningful (≥$200) AND price hasn't
     already fully responded (Kalshi mid didn't move more than ~5%), enter.

Holds for up to 4 hours then exits (this is a momentum strategy — fast
decay if thesis is wrong). Uses the same exit_monitor framework with a
STALE_THESIS threshold tightened for flow trades.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from cooldown import is_in_cooldown

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_flow_trades.json"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

INITIAL_BANKROLL = 75.0
FLOW_WINDOW_MIN = 30            # last N minutes of trades
MIN_TAKER_SHARE = 0.70          # 70%+ one direction = signal
MIN_FLOW_VOLUME_USD = 200       # at least $200 of one-sided pressure
MAX_RECENT_PRICE_MOVE = 0.05    # if price already moved >5%, signal is stale
PER_TRADE_CAP_PCT = 0.05        # 5% bankroll per trade
KELLY_MULT = 0.20
MAX_OPEN_POSITIONS = 6


def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return {
        "strategy": "flow_momentum",
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(d: dict):
    JOURNAL_FILE.write_text(json.dumps(d, indent=2, default=str))


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


def analyze_flow(ticker: str) -> dict | None:
    """Compute order-flow imbalance for one market over the last N minutes."""
    min_ts = int(datetime.now(timezone.utc).timestamp() - FLOW_WINDOW_MIN * 60)
    data = _get(f"{KALSHI_API}/markets/trades", {
        "ticker": ticker, "min_ts": min_ts, "limit": 1000,
    })
    trades = data.get("trades", [])
    if not trades:
        return None

    yes_usd = no_usd = 0.0
    first_price = None
    last_price = None
    sorted_trades = sorted(trades, key=lambda t: t.get("created_time", ""))
    for t in sorted_trades:
        try:
            count = float(t.get("count_fp") or 0)
            yes_p = float(t.get("yes_price_dollars") or 0)
            no_p = float(t.get("no_price_dollars") or 0)
        except (ValueError, TypeError):
            continue
        taker = (t.get("taker_outcome_side") or "").lower()
        if taker == "yes":
            yes_usd += count * yes_p
            last_price = yes_p
            if first_price is None:
                first_price = yes_p
        elif taker == "no":
            no_usd += count * no_p
            # YES-equivalent price after a NO buy
            last_price = 1.0 - no_p
            if first_price is None:
                first_price = 1.0 - no_p

    total = yes_usd + no_usd
    if total < MIN_FLOW_VOLUME_USD:
        return None
    yes_share = yes_usd / total
    price_move = (last_price - first_price) if (first_price and last_price) else 0.0

    return {
        "yes_share": yes_share,
        "total_usd": total,
        "yes_usd": yes_usd,
        "no_usd": no_usd,
        "price_move": price_move,
        "n_trades": len(trades),
    }


def kelly_size(p_win: float, fill_price: float) -> float:
    if fill_price <= 0 or fill_price >= 1:
        return 0.0
    b = (1.0 / fill_price) - 1.0
    q = 1.0 - p_win
    f_star = (p_win * b - q) / b
    if f_star <= 0:
        return 0.0
    return min(f_star * KELLY_MULT, PER_TRADE_CAP_PCT)


def run():
    if not QUEUE_FILE.exists():
        print("[flow] No queue — run scanner.py first")
        return

    queue = json.loads(QUEUE_FILE.read_text())
    markets = queue.get("markets", [])
    if not markets:
        print("[flow] No markets in queue")
        return

    data = load_journal()
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    slots = max(0, MAX_OPEN_POSITIONS - open_count)
    all_trades = list(data["trades"])

    print(f"[flow] Analyzing {len(markets)} markets for order-flow imbalance...")

    candidates = []
    for m in markets:
        ticker = m["ticker"]
        flow = analyze_flow(ticker)
        time.sleep(0.1)
        if flow is None:
            continue
        yes_share = flow["yes_share"]
        # Strong one-sided flow
        if yes_share >= MIN_TAKER_SHARE:
            direction = "yes"
            confidence = yes_share
        elif (1 - yes_share) >= MIN_TAKER_SHARE:
            direction = "no"
            confidence = 1 - yes_share
        else:
            continue

        # If the price has already moved a lot in that direction, the signal is stale
        if direction == "yes" and flow["price_move"] > MAX_RECENT_PRICE_MOVE:
            continue
        if direction == "no" and flow["price_move"] < -MAX_RECENT_PRICE_MOVE:
            continue

        candidates.append({
            "ticker": ticker,
            "title": m.get("title", ""),
            "side": direction,
            "yes_ask": m.get("yes_ask", 0),
            "yes_bid": m.get("yes_bid", 0),
            "yes_mid": m.get("yes_mid", 0),
            "flow_share": confidence,
            "flow_volume": flow["total_usd"],
            "n_trades": flow["n_trades"],
            "hours_left": m.get("hours_left", 0),
        })

    candidates.sort(key=lambda x: x["flow_share"] * x["flow_volume"], reverse=True)
    print(f"[flow] {len(candidates)} markets with strong order-flow imbalance")

    new_trades = []
    for c in candidates:
        if len(new_trades) >= slots:
            break
        if is_in_cooldown(c["ticker"], all_trades):
            continue
        if any(t["status"] == "open" and t["kalshi_ticker"] == c["ticker"] for t in data["trades"]):
            continue

        if c["side"] == "yes":
            fill_price = c["yes_ask"]
        else:
            fill_price = 1.0 - c["yes_bid"]
        if fill_price <= 0 or fill_price >= 1:
            continue

        # p_win estimate: confidence in the flow direction, capped
        p_win = min(0.50 + (c["flow_share"] - 0.50) * 0.5, 0.75)

        kf = kelly_size(p_win, fill_price)
        if kf <= 0:
            continue
        dollar_cost = kf * data["bankroll"]
        contracts = max(1, int(dollar_cost / fill_price))
        cost = round(contracts * fill_price, 2)
        if cost > data["bankroll"] or cost < 0.30:
            continue

        new_trades.append({
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "strategy": "flow_momentum",
            "kalshi_ticker": c["ticker"],
            "kalshi_title": c["title"],
            "side": c["side"],
            "contracts": contracts,
            "entry_price": fill_price,
            "cost": cost,
            "yes_mid_at_entry": c["yes_mid"],
            "flow_share_at_entry": round(c["flow_share"], 3),
            "flow_volume_at_entry": round(c["flow_volume"], 2),
            "hours_left_at_entry": c["hours_left"],
            # Flow is a short-horizon signal — tight exits
            "thesis_target_price": (
                round(c["yes_mid"] + 0.04, 4) if c["side"] == "yes"
                else round(c["yes_mid"] - 0.04, 4)
            ),
            "status": "open",
            "resolved_yes": None,
            "pnl": None,
            "settled_at": None,
            "exit_reason": None,
        })
        data["bankroll"] -= cost
        print(f"  [flow] {c['ticker']:<42} {c['side'].upper()} {contracts}x @ ${fill_price:.3f}  "
              f"share={c['flow_share']:.2f}  vol=${c['flow_volume']:.0f}  n={c['n_trades']}")

    data["trades"].extend(new_trades)
    save_journal(data)
    print(f"  [flow] {len(new_trades)} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
