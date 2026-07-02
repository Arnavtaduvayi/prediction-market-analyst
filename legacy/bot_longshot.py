"""
bot_longshot.py — Bot G: Longshot basket (experimental)

Spreads small, flat stakes across many low-priced (~10%) YES markets and holds
to settlement. The thesis is the lottery intuition: lots of little bets, some
hit big.

Honest expectation (documented, not a surprise): the favorite-longshot bias
says longshots are systematically OVER-priced on Kalshi — a 5¢ contract wins
~4% of the time, not 5% — so buying them broadly is negative-EV. This bot exists
to *measure* that on our own book, not because it's expected to win. Stakes are
kept tiny so the experiment is cheap.
"""

import json
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    kalshi_fee, load_journal, save_journal, open_trades, new_trade,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_longshot_trades.json"
STRATEGY = "longshot"

LONGSHOT_MIN = 0.05          # avoid dust; below this the book is unreliable
LONGSHOT_MAX = 0.15         # "around 10%"
STAKE_USD = 0.75            # small flat stake per bet
MIN_STAKE = 0.30
MAX_OPEN_POSITIONS = 30     # it's a spray — allow a wide basket
MAX_TOTAL_EXPOSURE = 25.0   # but never risk more than this across open longshots


def in_band(mid: float) -> bool:
    return LONGSHOT_MIN <= mid <= LONGSHOT_MAX


def run():
    if not QUEUE_FILE.exists():
        print("[longshot] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])
    if not markets:
        print("[longshot] No markets in queue")
        return

    data = load_journal(JOURNAL_FILE, STRATEGY)
    all_trades = data["trades"]
    open_t = open_trades(data)
    held = {t["kalshi_ticker"] for t in open_t}
    exposure = sum(t["cost"] for t in open_t)
    slots = max(0, MAX_OPEN_POSITIONS - len(open_t))

    print(f"[longshot] Scanning {len(markets)} markets for ~10% longshots...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if ticker in held or is_in_cooldown(ticker, all_trades):
            continue
        mid = m.get("yes_mid", 0)
        if not in_band(mid):
            continue
        fill = round(m.get("yes_ask", 0), 4)
        if fill <= 0 or fill >= 1 or m.get("ask_size", 0) <= 0:
            continue
        candidates.append({"ticker": ticker, "title": m.get("title", ""),
                           "fill": fill, "mid": mid})

    # Cheapest (longest-odds) first — most "lottery" per dollar.
    candidates.sort(key=lambda c: c["fill"])
    print(f"[longshot] {len(candidates)} longshots in [{LONGSHOT_MIN:.2f}, {LONGSHOT_MAX:.2f}]")

    placed = 0
    for c in candidates:
        if placed >= slots or exposure >= MAX_TOTAL_EXPOSURE:
            break
        contracts = max(1, round(STAKE_USD / c["fill"]))
        fee = kalshi_fee(c["fill"], contracts)
        cost = round(contracts * c["fill"] + fee, 2)
        if cost < MIN_STAKE or cost > data["bankroll"] or exposure + cost > MAX_TOTAL_EXPOSURE:
            continue
        trade = new_trade(
            c["ticker"], c["title"], "yes", contracts, c["fill"], STRATEGY,
            fee=fee, hold_to_settlement=True, yes_mid_at_entry=c["mid"],
        )
        data["bankroll"] -= cost
        data["trades"].append(trade)
        exposure += cost
        placed += 1
        print(f"  [longshot] {c['ticker']:<40} YES {contracts}x @ ${c['fill']:.3f}  cost=${cost:.2f}")

    save_journal(data, JOURNAL_FILE)
    print(f"  [longshot] {placed} new bets. Exposure ${exposure:.2f}. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
