"""
bot_theta.py — Bot F: Settlement-convergence on near-certain favorites (balanced)

Heavy favorites near the end of their life converge to $1.00 slowly: a market
trading at $0.95 with 6 hours left and nothing left to decide is, on average,
underpriced by the last few cents. We harvest that convergence.

Distinct from Bot B (Disposition):
  - FAVORITES ONLY. We never buy longshots — the <$0.10 "buy NO" leg is exactly
    the trap that keeps Disposition net-negative, so this bot refuses to touch it.
  - TIME-GATED to the final window, where convergence is most reliable and theta
    is fastest. Disposition enters at any horizon.

Holds to settlement (the edge is the convergence to resolution). Position cap +
small per-trade cap keep the occasional favorite upset survivable.
"""

import json
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    kalshi_fee, kelly_size, load_journal, save_journal, open_trades, new_trade,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_theta_trades.json"
STRATEGY = "theta"

MIN_MID = 0.92              # only strong favorites
MAX_MID = 0.985            # not so extreme there's nothing left to capture
MAX_HOURS = 24             # final window only
MIN_HOURS = 1
# Convergence edge: late favorites settle YES slightly more often than their
# price implies (Whelan: buyers of >$0.50 contracts earn a small positive
# return). This bump is what must clear the bid-ask spread for theta to act —
# without it, buying at yes_ask (always > mid) can never show positive EV.
FAVORITE_EDGE = 0.025
KELLY_MULT = 0.25
PER_TRADE_CAP_PCT = 0.04
MAX_OPEN_POSITIONS = 10
MIN_EDGE_DOLLARS = 0.005   # convergence edges are small


def run():
    if not QUEUE_FILE.exists():
        print("[theta] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])
    if not markets:
        print("[theta] No markets in queue")
        return

    data = load_journal(JOURNAL_FILE, STRATEGY)
    all_trades = data["trades"]
    held = {t["kalshi_ticker"] for t in open_trades(data)}
    slots = max(0, MAX_OPEN_POSITIONS - len(held))

    print(f"[theta] Scanning {len(markets)} markets for late favorites...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if ticker in held or is_in_cooldown(ticker, all_trades):
            continue
        mid = m.get("yes_mid", 0)
        hours = m.get("hours_left", 0)
        if not (MIN_MID <= mid <= MAX_MID):
            continue
        if not (MIN_HOURS <= hours <= MAX_HOURS):
            continue
        fill = round(m.get("yes_ask", 0), 4)   # buy the favorite (YES)
        if fill <= 0 or fill >= 1 or m.get("ask_size", 0) <= 0:
            continue
        p_win = min(mid + FAVORITE_EDGE, 0.99)   # convergence-adjusted probability
        edge = p_win - fill - kalshi_fee(fill, 1)
        if edge < MIN_EDGE_DOLLARS:
            continue
        candidates.append({
            "ticker": ticker, "title": m.get("title", ""), "fill": fill,
            "p_win": p_win, "mid": mid, "hours": hours, "edge": round(edge, 4),
        })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    print(f"[theta] {len(candidates)} late-favorite candidates")

    placed = 0
    for c in candidates:
        if placed >= slots:
            break
        kf = kelly_size(c["p_win"], c["fill"], KELLY_MULT, PER_TRADE_CAP_PCT)
        if kf <= 0:
            continue
        contracts = max(1, int((kf * data["bankroll"]) / c["fill"]))
        fee = kalshi_fee(c["fill"], contracts)
        cost = round(contracts * c["fill"] + fee, 2)
        if cost > data["bankroll"] or cost < 0.30:
            continue
        trade = new_trade(
            c["ticker"], c["title"], "yes", contracts, c["fill"], STRATEGY,
            fee=fee, hold_to_settlement=True,
            yes_mid_at_entry=c["mid"], hours_left_at_entry=c["hours"],
        )
        data["bankroll"] -= cost
        data["trades"].append(trade)
        placed += 1
        print(f"  [theta] {c['ticker']:<40} YES {contracts}x @ ${c['fill']:.3f}  "
              f"mid={c['mid']:.3f}  {c['hours']:.1f}h left")

    save_journal(data, JOURNAL_FILE)
    print(f"  [theta] {placed} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
