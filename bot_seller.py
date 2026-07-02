"""
bot_seller.py — Bot S: Longshot seller (maker execution)

Sells overpriced longshots: buys NO on markets priced ~3-10¢ YES and holds to
settlement. This is the *empirically profitable side* of the favorite-longshot
bias, on two independent bodies of evidence:

  1. Whelan's GWU study of 72M Kalshi trades: longshots are systematically
     overpriced (a 5¢ contract wins ~4% of the time, not 5%) — roughly 2-4%
     edge to the seller, the largest documented bias on the venue.
  2. Our own book: disposition's longshot_sell leg returned +4.6% per trade
     over 37 settled trades (+2.4% non-crypto, +8.5% crypto) while its
     favorite_buy leg lost -4.4% per trade over 221. We kept the winning leg.

Execution is maker-only: we rest a NO bid one tick above the current best NO
bid instead of paying the NO ask. On a book like YES 0.05/0.08 that's buying
NO at 0.93 instead of 0.95 — the 2¢ difference is roughly the size of the
entire edge, which is why the taker version of this strategy only barely won.
Fills follow botlib's pessimistic printed-through rule, so results are a floor.

Risk: one position per event (ladder strikes are perfectly correlated), capped
positions per series, flat small stakes across many independent events.
"""

import json
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    MAKER_FEE_RATE, kalshi_fee, load_journal, save_journal, open_trades,
    resting_orders, new_resting_order, check_resting_fills,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_seller_trades.json"
STRATEGY = "seller"

YES_MID_MIN = 0.03          # below this the book is dust and fees dominate
YES_MID_MAX = 0.10          # the band where Whelan's longshot overpricing lives
NO_LIMIT_MAX = 0.96         # never quote where one upset erases 25+ wins
LONGSHOT_BIAS = 0.02        # conservative end of the documented 2-4% overpricing
MIN_EDGE = 0.005            # required: p(NO) - limit - maker fee

STAKE_USD = 3.0             # flat stake per event — diversification over conviction
MAX_OPEN_POSITIONS = 15     # filled + resting combined
MAX_PER_SERIES = 3          # correlated-outcome guard (e.g. crypto hourlies)
EXPIRE_HOURS = 8            # unfilled quotes die and re-price next runs


def event_key(ticker: str) -> str:
    """KXBTCD-26JUL0317-T61999.99 → KXBTCD-26JUL0317 (strikes share an event)."""
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ticker


def series_key(ticker: str) -> str:
    return ticker.split("-")[0]


def no_quote(yes_bid: float, yes_ask: float) -> float:
    """Rest one tick above best NO bid, never crossing the NO ask."""
    no_bid = round(1.0 - yes_ask, 4)
    no_ask = round(1.0 - yes_bid, 4)
    return round(min(no_bid + 0.01, no_ask - 0.01), 4)


def run():
    data = load_journal(JOURNAL_FILE, STRATEGY)

    # Advance resting quotes first: fills promote to open, stale ones expire.
    if resting_orders(data):
        print(f"[seller] Checking {len(resting_orders(data))} resting quotes...")
        check_resting_fills(data, STRATEGY)
        save_journal(data, JOURNAL_FILE)

    if not QUEUE_FILE.exists():
        print("[seller] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])

    all_trades = data["trades"]
    live = open_trades(data) + resting_orders(data)
    held_events = {event_key(t["kalshi_ticker"]) for t in live}
    series_count: dict[str, int] = {}
    for t in live:
        s = series_key(t["kalshi_ticker"])
        series_count[s] = series_count.get(s, 0) + 1
    slots = max(0, MAX_OPEN_POSITIONS - len(live))

    print(f"[seller] Scanning {len(markets)} markets for longshots to sell...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if event_key(ticker) in held_events or is_in_cooldown(ticker, all_trades):
            continue
        mid = m.get("yes_mid", 0)
        if not (YES_MID_MIN <= mid <= YES_MID_MAX):
            continue
        yes_bid, yes_ask = m.get("yes_bid", 0), m.get("yes_ask", 0)
        if yes_bid <= 0 or yes_ask >= 1:
            continue
        limit = no_quote(yes_bid, yes_ask)
        if limit <= 0 or limit > NO_LIMIT_MAX:
            continue
        p_no = min(1.0 - mid + LONGSHOT_BIAS, 0.99)
        edge = p_no - limit - kalshi_fee(limit, 1, rate=MAKER_FEE_RATE)
        if edge < MIN_EDGE:
            continue
        candidates.append({"ticker": ticker, "title": m.get("title", ""),
                           "limit": limit, "mid": mid, "edge": round(edge, 4)})

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    print(f"[seller] {len(candidates)} sellable longshots "
          f"(yes_mid in [{YES_MID_MIN:.2f}, {YES_MID_MAX:.2f}])")

    placed = 0
    for c in candidates:
        if placed >= slots:
            break
        s = series_key(c["ticker"])
        if series_count.get(s, 0) >= MAX_PER_SERIES:
            continue
        contracts = max(1, round(STAKE_USD / c["limit"]))
        order = new_resting_order(
            c["ticker"], c["title"], "no", contracts, c["limit"], STRATEGY,
            expire_hours=EXPIRE_HOURS, hold_to_settlement=True,
            yes_mid_at_entry=c["mid"], edge_at_entry=c["edge"],
        )
        if order["cost"] > data["bankroll"]:
            continue
        data["bankroll"] -= order["cost"]
        data["trades"].append(order)
        held_events.add(event_key(c["ticker"]))
        series_count[s] = series_count.get(s, 0) + 1
        placed += 1
        print(f"  [seller] {c['ticker']:<40} NO {contracts}x resting @ ${c['limit']:.3f}  "
              f"yes_mid={c['mid']:.3f}  edge={c['edge']:.3f}")

    save_journal(data, JOURNAL_FILE)
    print(f"  [seller] {placed} new quotes. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
