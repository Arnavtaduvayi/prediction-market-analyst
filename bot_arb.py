"""
bot_arb.py — Bot C: Arbitrage (math-heavy, risk-free)

Two genuinely risk-free patterns, both fee-aware (the old Calendar bot only
ever checked one of them and never found a thing):

  1. SUM-ARB (Dutch book) on mutually-exclusive events
     Exactly one outcome resolves YES.
       - underround: Σ yes_ask < $1 - fees  → buy YES on every outcome
                     → one pays $1, basket cost < $1 → locked profit.
       - overround : Σ yes_bid > $1 + fees  → sell YES (buy NO) on every outcome
                     → locked profit.

  2. LADDER-ARB (monotonicity) on same-direction "X or above" strike ladders
     P(≥strike) must be non-increasing in strike. If a lower strike's YES is
     cheaper than a higher strike's YES (yes_bid(high) > yes_ask(low) + fees),
     buy YES(low) + sell YES(high). Payout is ALWAYS ≥ $1 (≥high ⟹ ≥low), so a
     basket cost < $1 is locked profit. We size on the guaranteed minimum.

Each leg is written as its own trade record sharing an `arb_id`, all with
hold_to_settlement=True, so exit_monitor.py settles every leg at resolution and
the legs net to the locked spread. No bespoke settlement code needed.

Reality check: completable arbs are rare (every leg needs a real book). Expect
this bot to sit idle a lot. That is correct behaviour — it never loses.
"""

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from botlib import (
    KALSHI_API, get_json, kalshi_fee, load_journal, save_journal,
    open_trades, new_trade, now_iso,
)

JOURNAL_FILE = Path(__file__).parent / "paper_arb_trades.json"
STRATEGY = "arbitrage"

MIN_PROFIT_PER_CONTRACT = 0.01   # need ≥1¢ locked AFTER fees, else it's noise
PER_TRADE_CAP_PCT = 0.20         # risk-free → can stake more, still bankroll-bound
MAX_NEW_BASKETS = 4              # per run
MAX_EVENTS_SCAN = 120            # bound runtime / rate limits
MAX_BOOK_FETCH = 350            # hard ceiling on /markets/{t} calls per run
MIN_LEGS = 2
MAX_LEGS = 16

_STRIKE_RE = re.compile(r"-T(\d+(?:\.\d+)?)")


# ── discovery ───────────────────────────────────────────────────────────────

def discover_events(max_events: int) -> list[dict]:
    """Open events with their nested markets. Paginated via cursor."""
    events: list[dict] = []
    cursor = None
    while len(events) < max_events:
        params = {"limit": 200, "with_nested_markets": "true", "status": "open"}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_API}/events", params)
        batch = data.get("events", [])
        if not batch:
            break
        events.extend(batch)
        cursor = data.get("cursor")
        if not cursor:
            break
    return events[:max_events]


def _strike_of(ticker: str, sub_title: str) -> float | None:
    m = _STRIKE_RE.search(ticker or "")
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)", sub_title or "")
    return float(m.group(1)) if m else None


def _is_above(sub_title: str) -> bool:
    return "above" in (sub_title or "").lower()


def _is_below(sub_title: str) -> bool:
    return "below" in (sub_title or "").lower()


# ── books ───────────────────────────────────────────────────────────────────

def _book_from_nested(m: dict) -> dict | None:
    """Pull top-of-book from a nested market dict; None if it lacks one."""
    try:
        yes_bid = m.get("yes_bid_dollars")
        yes_ask = m.get("yes_ask_dollars")
        yes_bid = float(yes_bid) if yes_bid is not None else None
        yes_ask = float(yes_ask) if yes_ask is not None else None
        bid_size = float(m.get("yes_bid_size_fp") or 0)
        ask_size = float(m.get("yes_ask_size_fp") or 0)
    except (ValueError, TypeError):
        return None
    if not yes_bid or not yes_ask or yes_ask >= 1 or yes_bid <= 0:
        return None
    return {
        "ticker": m.get("ticker", ""),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "sub_title": m.get("yes_sub_title", "") or m.get("subtitle", ""),
        "title": m.get("title", ""),
    }


def _fetch_book(ticker: str, sub_title: str, title: str) -> dict | None:
    time.sleep(0.05)  # rare fallback path — be gentle on the API
    m = get_json(f"{KALSHI_API}/markets/{ticker}").get("market", {})
    if not m:
        return None
    b = _book_from_nested(m)
    if b:
        b["sub_title"] = b["sub_title"] or sub_title
        b["title"] = b["title"] or title
    return b


def legs_for_event(ev: dict, budget: list[int]) -> list[dict]:
    """
    Resolve a book for every market in the event. `budget` is a one-element
    list used as a mutable counter on /markets fetches across the whole run.
    """
    legs = []
    now = datetime.now(timezone.utc)
    for m in ev.get("markets", []):
        if m.get("status") != "active":
            continue
        close = m.get("close_time", "")
        if close:
            try:
                if datetime.fromisoformat(close.replace("Z", "+00:00")) <= now:
                    continue  # already past close — stale/settled, not tradeable
            except (ValueError, TypeError):
                pass
        b = _book_from_nested(m)
        if b is None:
            if budget[0] <= 0:
                continue
            budget[0] -= 1
            b = _fetch_book(m.get("ticker", ""),
                            m.get("yes_sub_title", ""), m.get("title", ""))
        if b and b["ticker"]:
            legs.append(b)
    return legs


# ── arb detection (pure functions — unit tested) ─────────────────────────────

def detect_sum_arb(legs: list[dict]) -> dict | None:
    """
    Overround Dutch book across mutually-exclusive outcomes: sell YES on every
    listed outcome when Σ yes_bid > $1 + fees.

    We deliberately do NOT trade the underround (buy-all-YES) case. Kalshi's
    `mutually_exclusive` flag only guarantees AT MOST one outcome wins, not that
    one of the LISTED outcomes must win. Buy-all is only risk-free if the set is
    collectively exhaustive, which Kalshi does not guarantee (e.g. a decided
    election whose winner is no longer a listed candidate would show a fat fake
    underround and lose the whole basket). Selling all YES is safe regardless:
    at most one leg ever pays $1, so the worst case is Σ yes_bid - 1 > 0.
    """
    if not (MIN_LEGS <= len(legs) <= MAX_LEGS):
        return None

    # Overround: sell YES (buy NO at 1-yes_bid) on every outcome.
    if all(l["bid_size"] > 0 for l in legs):
        contracts = 1
        sum_bid = sum(l["yes_bid"] for l in legs)
        no_fills = [1.0 - l["yes_bid"] for l in legs]
        fees = sum(kalshi_fee(p, contracts) for p in no_fills)
        profit = sum_bid - 1.0 - fees
        if profit >= MIN_PROFIT_PER_CONTRACT:
            return {
                "kind": "sum_overround",
                "profit_per_contract": round(profit, 4),
                "legs": [{"ticker": l["ticker"], "side": "no",
                          "price": round(1.0 - l["yes_bid"], 4), "size": l["bid_size"],
                          "title": l["title"], "sub_title": l["sub_title"]}
                         for l in legs],
            }
    return None


def detect_ladder_arb(legs: list[dict]) -> dict | None:
    """
    Monotonicity violation on a same-direction 'X or above' strike ladder.
    Buy YES(low strike) + sell YES(high strike) when yes_bid(high) is richer
    than yes_ask(low). Returns the single best (most profitable) pair.
    """
    rungs = []
    for l in legs:
        if not _is_above(l["sub_title"]):
            return None  # not a clean 'above' ladder — skip (sum-arb may cover it)
        k = _strike_of(l["ticker"], l["sub_title"])
        if k is None:
            return None
        rungs.append((k, l))
    if len(rungs) < 2:
        return None
    rungs.sort(key=lambda r: r[0])

    best = None
    for i in range(len(rungs)):
        for j in range(i + 1, len(rungs)):
            low = rungs[i][1]    # lower strike
            high = rungs[j][1]   # higher strike
            if low["ask_size"] <= 0 or high["bid_size"] <= 0:
                continue
            # buy YES(low) @ ask, sell YES(high) @ bid (= buy NO(high) @ 1-bid)
            no_high_fill = 1.0 - high["yes_bid"]
            fees = kalshi_fee(low["yes_ask"], 1) + kalshi_fee(no_high_fill, 1)
            # guaranteed minimum payout is $1 → profit = bid(high) - ask(low) - fees
            profit = high["yes_bid"] - low["yes_ask"] - fees
            if profit >= MIN_PROFIT_PER_CONTRACT and (best is None or profit > best["profit_per_contract"]):
                best = {
                    "kind": "ladder",
                    "profit_per_contract": round(profit, 4),
                    "legs": [
                        {"ticker": low["ticker"], "side": "yes", "price": low["yes_ask"],
                         "size": low["ask_size"], "title": low["title"], "sub_title": low["sub_title"]},
                        {"ticker": high["ticker"], "side": "no", "price": round(no_high_fill, 4),
                         "size": high["bid_size"], "title": high["title"], "sub_title": high["sub_title"]},
                    ],
                }
    return best


# ── execution ───────────────────────────────────────────────────────────────

def _basket_unit_cost(arb: dict) -> float:
    """Cost of one contract per leg, fees included."""
    return sum(leg["price"] + kalshi_fee(leg["price"], 1) for leg in arb["legs"])


def run():
    data = load_journal(JOURNAL_FILE, STRATEGY)
    held = {t["kalshi_ticker"] for t in open_trades(data)}

    print("[arb] Discovering events (with nested markets)...")
    events = discover_events(MAX_EVENTS_SCAN)
    print(f"[arb] {len(events)} events to inspect")

    budget = [MAX_BOOK_FETCH]
    opportunities = []
    for ev in events:
        markets = ev.get("markets", [])
        if not (MIN_LEGS <= len(markets) <= MAX_LEGS):
            continue
        legs = legs_for_event(ev, budget)
        if len(legs) < MIN_LEGS:
            continue
        if any(l["ticker"] in held for l in legs):
            continue  # already in this basket

        arb = None
        if ev.get("mutually_exclusive"):
            arb = detect_sum_arb(legs)
        if arb is None:
            arb = detect_ladder_arb(legs)
        if arb:
            arb["event_ticker"] = ev.get("event_ticker", "")
            arb["event_title"] = ev.get("title", "")
            opportunities.append(arb)
        if budget[0] <= 0:
            break

    opportunities.sort(key=lambda a: a["profit_per_contract"], reverse=True)
    print(f"[arb] {len(opportunities)} risk-free opportunities found")

    placed = 0
    for arb in opportunities:
        if placed >= MAX_NEW_BASKETS:
            break
        unit_cost = _basket_unit_cost(arb)
        if unit_cost <= 0:
            continue
        max_by_bankroll = int((PER_TRADE_CAP_PCT * data["bankroll"]) / unit_cost)
        max_by_depth = int(min(leg["size"] for leg in arb["legs"]))
        contracts = max(0, min(max_by_bankroll, max_by_depth))
        if contracts < 1:
            continue
        basket_cost = round(contracts * unit_cost, 2)
        if basket_cost > data["bankroll"]:
            continue

        arb_id = f"{arb['kind']}-{arb['event_ticker']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        locked = round(contracts * arb["profit_per_contract"], 2)
        for leg in arb["legs"]:
            fee = kalshi_fee(leg["price"], contracts)
            trade = new_trade(
                leg["ticker"], leg.get("title", ""), leg["side"], contracts,
                leg["price"], STRATEGY, fee=fee,
                hold_to_settlement=True,
                arb_id=arb_id,
                arb_kind=arb["kind"],
                arb_locked_profit=locked,
                event_ticker=arb["event_ticker"],
            )
            data["bankroll"] -= trade["cost"]
            data["trades"].append(trade)
        placed += 1
        print(f"  [arb] {arb['kind']:<15} {arb['event_ticker']:<22} "
              f"{len(arb['legs'])} legs x{contracts}  "
              f"locked=${locked:+.2f}  ({arb['profit_per_contract']*100:.1f}¢/contract)")

    save_journal(data, JOURNAL_FILE)
    print(f"  [arb] {placed} baskets placed. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
