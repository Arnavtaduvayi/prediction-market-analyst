"""
bot_theta.py — Bot T: Late-favorite convergence, maker execution (v2)

v1 result (21 settled, now in legacy/paper_theta_v1_trades.json): 95.2% win
rate at a 95.2% average entry price — the taker ask was EXACTLY fair value.
Every trade paid the ask and the spread+fee ate the convergence edge to the
cent. That's not a broken signal; it's a broken execution model.

v2 keeps the identical signal (strong favorites in their final window) and
changes only the execution: rest a YES bid one tick above the current best bid
instead of lifting the ask. Taking v1's own measurement — true probability ≈
the ask — buying 2-3¢ below the ask is buying below fair value, and the entry
improvement IS the edge:

    edge = yes_ask - our_limit - maker_fee   (require ≥ 1¢)

The open question v2 exists to answer: do resting bids on favorites fill only
when news breaks against them (adverse selection)? If the filled-trade win
rate stays near v1's 95%, the strategy nets ~+2%/trade. If it degrades below
our entry price, maker execution doesn't rescue favorites and we retire it.
Fills use botlib's pessimistic printed-through rule, so paper P&L is a floor.
"""

import json
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    MAKER_FEE_RATE, kalshi_fee, kelly_size, load_journal, save_journal,
    open_trades, resting_orders, new_resting_order, check_resting_fills,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_theta_trades.json"
STRATEGY = "theta"

MIN_MID = 0.92              # only strong favorites
MAX_MID = 0.985             # not so extreme there's nothing left to capture
MAX_HOURS = 24              # final window only
MIN_HOURS = 2               # need time for a resting bid to fill pre-close
MIN_EDGE = 0.01             # ask - limit - maker fee, in dollars
KELLY_MULT = 0.25
PER_TRADE_CAP_PCT = 0.04
MAX_OPEN_POSITIONS = 10     # filled + resting combined
EXPIRE_HOURS = 6            # stale quotes re-price on later runs


def yes_quote(yes_bid: float, yes_ask: float) -> float:
    """Rest one tick above best YES bid, never crossing the ask."""
    return round(min(yes_bid + 0.01, yes_ask - 0.01), 4)


def run():
    data = load_journal(JOURNAL_FILE, STRATEGY)

    if resting_orders(data):
        print(f"[theta] Checking {len(resting_orders(data))} resting quotes...")
        check_resting_fills(data, STRATEGY)
        save_journal(data, JOURNAL_FILE)

    if not QUEUE_FILE.exists():
        print("[theta] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])

    all_trades = data["trades"]
    live = open_trades(data) + resting_orders(data)
    held = {t["kalshi_ticker"] for t in live}
    slots = max(0, MAX_OPEN_POSITIONS - len(live))

    print(f"[theta] Scanning {len(markets)} markets for late favorites...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if ticker in held or is_in_cooldown(ticker, all_trades):
            continue
        mid = m.get("yes_mid", 0)
        hours = m.get("hours_left", 0)
        if not (MIN_MID <= mid <= MAX_MID) or not (MIN_HOURS <= hours <= MAX_HOURS):
            continue
        yes_bid, yes_ask = m.get("yes_bid", 0), m.get("yes_ask", 0)
        if yes_bid <= 0 or yes_ask >= 1:
            continue
        limit = yes_quote(yes_bid, yes_ask)
        if limit <= 0:
            continue
        # v1's measurement: the ask is fair value. Entry improvement is the edge.
        p_win = min(yes_ask, 0.99)
        edge = p_win - limit - kalshi_fee(limit, 1, rate=MAKER_FEE_RATE)
        if edge < MIN_EDGE:
            continue
        candidates.append({
            "ticker": ticker, "title": m.get("title", ""), "limit": limit,
            "p_win": p_win, "mid": mid, "hours": hours, "edge": round(edge, 4),
        })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    print(f"[theta] {len(candidates)} late-favorite candidates")

    placed = 0
    for c in candidates:
        if placed >= slots:
            break
        kf = kelly_size(c["p_win"], c["limit"], KELLY_MULT, PER_TRADE_CAP_PCT)
        if kf <= 0:
            continue
        contracts = max(1, int((kf * data["bankroll"]) / c["limit"]))
        order = new_resting_order(
            c["ticker"], c["title"], "yes", contracts, c["limit"], STRATEGY,
            expire_hours=min(EXPIRE_HOURS, max(1.0, c["hours"] - 1)),
            hold_to_settlement=True,
            yes_mid_at_entry=c["mid"], hours_left_at_entry=c["hours"],
            edge_at_entry=c["edge"],
        )
        if order["cost"] > data["bankroll"] or order["cost"] < 0.30:
            continue
        data["bankroll"] -= order["cost"]
        data["trades"].append(order)
        placed += 1
        print(f"  [theta] {c['ticker']:<40} YES {contracts}x resting @ ${c['limit']:.3f}  "
              f"ask={c['p_win']:.3f}  {c['hours']:.1f}h left")

    save_journal(data, JOURNAL_FILE)
    print(f"  [theta] {placed} new quotes. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
