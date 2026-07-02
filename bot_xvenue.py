"""
bot_xvenue.py — Bot X: Cross-venue Kalshi <-> Polymarket

The one strategy here whose edge is structural rather than statistical.
Polymarket's macro/event markets run orders of magnitude deeper than Kalshi's
(July 2026 Fed decision at verification time: $6.4M/day volume and a 1¢ spread
on Polymarket vs near-zero volume and 20-60¢ spreads on Kalshi). Academic work
consistently finds Polymarket leads price discovery. Two tiers:

  Tier 1 — HARD ARB (taker both venues, risk-free by construction):
      buy YES on one venue + NO on the other when the combined cost incl. all
      fees < $1.00. Both legs of a verified pair settle on the same source, so
      the pair pays exactly $1 whichever way the event resolves. Documented
      cross-venue spreads of 2-5% still occurred through 2026 (e.g. the Feb
      2026 LA-mayoral pair paying 7.5%), but they are rare at hourly cadence —
      this tier is a free option, like bot_arb.

  Tier 2 — FAIR-VALUE QUOTING (the bread and butter):
      treat the deep venue's mid as fair value and rest maker bids on Kalshi's
      thin book at fair_value - margin. This is market-making with an informed
      anchor: we only ever buy at least MARGIN below what the deep market says
      the contract is worth. Convergence (not settlement) is the exit, so
      capital recycles and resolution risk stays small.

Pairs come exclusively from data/xvenue_pairs.json, where every entry is
human-verified for resolution equivalence. No fuzzy matching — that is the
mistake that killed the v0 cross-platform bot. `python3 bot_xvenue.py propose`
prints candidate pairs for human review; it never trades them.

Paper-mode honesty: Kalshi fills use botlib's pessimistic printed-through
rule. Polymarket legs are priced off the live Gamma book at taker, with fees
modeled on the Polymarket US schedule (0.06·C·p·(1-p) taker), since live
execution for a US trader would happen on the regulated US exchange.
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    KALSHI_API, MAKER_FEE_RATE, get_json, kalshi_fee, load_journal,
    save_journal, open_trades, resting_orders, new_resting_order,
    check_resting_fills, new_trade, parse_iso,
)

PAIRS_FILE = Path(__file__).parent / "data" / "xvenue_pairs.json"
JOURNAL_FILE = Path(__file__).parent / "paper_xvenue_trades.json"
STRATEGY = "xvenue"

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_TAKER_RATE = 0.06      # Polymarket US: fee = 0.06 * C * p * (1-p)

MIN_ARB_PROFIT = 0.015      # Tier 1: locked cents per $1 pair, after all fees
MIN_DIVERGENCE = 0.05       # Tier 2: |poly_mid - kalshi_mid| to act
FAIR_MARGIN = 0.03          # Tier 2: quote at least this far inside fair value
MAX_POLY_SPREAD = 0.03      # poly book must be tight to count as "fair value"
# ...and deep. Depth = today's volume OR resting book liquidity: far-dated
# events trade thinly day-to-day but keep six-figure resting books (Oct 2026
# Fed event at curation time: $2.8k vol24h, $164k liquidity).
MIN_POLY_VOL24H = 10_000
MIN_POLY_LIQUIDITY = 50_000
MIN_HOURS_LEFT = 2          # resting bids need time to fill

ARB_CAP_PCT = 0.20          # Tier 1 is risk-free — biggest per-trade cap
FAIR_CAP_PCT = 0.06
MAX_OPEN_POSITIONS = 8
EXPIRE_HOURS = 24           # macro pairs move slowly; quotes live longer

MONTHS = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May",
          6: "June", 7: "July", 8: "August", 9: "September", 10: "October",
          11: "November", 12: "December"}
MON_TOKENS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def poly_taker_fee(price: float, contracts: int) -> float:
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    return math.ceil(POLY_TAKER_RATE * contracts * price * (1 - price) * 100) / 100.0


def floor_cent(x: float) -> float:
    return math.floor(x * 100) / 100.0


def parse_kalshi_date(token: str) -> tuple[int, int] | None:
    """'26JUL' -> (2026, 7)."""
    if len(token) < 5 or not token[:2].isdigit():
        return None
    mon = MON_TOKENS.get(token[2:5])
    return (2000 + int(token[:2]), mon) if mon else None


def fetch_kalshi_series(series: str) -> list[dict]:
    out, cursor = [], None
    for _ in range(5):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{KALSHI_API}/markets", params)
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def fetch_poly_event(search: str, title: str) -> dict | None:
    """Most liquid event with an exact title match (dead duplicates exist)."""
    d = get_json(f"{POLY_GAMMA}/public-search",
                 {"q": search, "limit_per_type": 20})
    hits = [ev for ev in d.get("events") or []
            if (ev.get("title") or "").strip().lower() == title.strip().lower()]
    return max(hits, key=lambda ev: float(ev.get("liquidity") or 0), default=None)


def poly_market_quotes(ev: dict) -> dict[str, dict]:
    """groupItemTitle -> {bid, ask, mid, spread} for one poly event."""
    quotes = {}
    for m in ev.get("markets") or []:
        try:
            bid = float(m.get("bestBid") or 0)
            ask = float(m.get("bestAsk") or 0)
        except (ValueError, TypeError):
            continue
        if bid <= 0 or ask <= 0 or ask >= 1 or ask <= bid:
            continue
        quotes[(m.get("groupItemTitle") or "").strip()] = {
            "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4),
            "spread": round(ask - bid, 4),
        }
    return quotes


def match_instances(pair: dict) -> list[dict]:
    """
    Join open Kalshi markets of a series to the Polymarket event for the same
    month+year. Returns [{kalshi market, poly quote, outcome}] rows.
    """
    rows = []
    markets = fetch_kalshi_series(pair["kalshi_series"])
    by_date: dict[str, list[dict]] = {}
    for m in markets:
        parts = m.get("ticker", "").split("-")
        if len(parts) != 3:
            continue
        by_date.setdefault(parts[1], []).append(m)

    for date_token, mkts in by_date.items():
        ym = parse_kalshi_date(date_token)
        if not ym:
            continue
        title = pair["poly_title_pattern"].replace("{month}", MONTHS[ym[1]])
        ev = fetch_poly_event(pair["poly_search"], title)
        if not ev:
            continue
        end = parse_iso((ev.get("endDate") or "").replace("Z", "+00:00"))
        if not end or (end.year, end.month) != ym:
            continue  # same title pattern, different meeting — not our instance
        vol24h = float(ev.get("volume24hr") or 0)
        liquidity = float(ev.get("liquidity") or 0)
        quotes = poly_market_quotes(ev)
        for m in mkts:
            suffix = m["ticker"].split("-")[2]
            group = pair["outcome_map"].get(suffix)
            if not group or group not in quotes:
                continue
            rows.append({"kalshi": m, "poly": quotes[group],
                         "poly_vol24h": vol24h, "poly_liquidity": liquidity,
                         "poly_event": ev.get("slug", ""),
                         "outcome": group, "pair_id": pair["id"]})
    return rows


def kalshi_book(m: dict) -> dict | None:
    try:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
    except (ValueError, TypeError):
        return None
    hours = 0.0
    close = parse_iso((m.get("close_time") or "").replace("Z", "+00:00"))
    if close:
        hours = (close - datetime.now(timezone.utc)).total_seconds() / 3600
    return {"bid": bid, "ask": ask, "hours": hours}


def try_hard_arb(row: dict, data: dict) -> bool:
    """Tier 1: lock YES on one venue + NO on the other below $1 all-in."""
    k, p = kalshi_book(row["kalshi"]) or {}, row["poly"]
    if not k:
        return False
    ticker = row["kalshi"]["ticker"]
    legs = []
    # A: YES Kalshi @ ask + NO Poly @ (1 - poly bid)
    if 0 < k["ask"] < 1:
        cost = (k["ask"] + kalshi_fee(k["ask"], 1)
                + (1 - p["bid"]) + poly_taker_fee(1 - p["bid"], 1))
        legs.append(("yes", k["ask"], 1 - p["bid"], cost))
    # B: NO Kalshi @ (1 - kalshi bid) + YES Poly @ poly ask
    if k["bid"] > 0:
        cost = ((1 - k["bid"]) + kalshi_fee(1 - k["bid"], 1)
                + p["ask"] + poly_taker_fee(p["ask"], 1))
        legs.append(("no", 1 - k["bid"], p["ask"], cost))

    best = min(legs, key=lambda x: x[3], default=None)
    if not best or 1.0 - best[3] < MIN_ARB_PROFIT:
        return False
    side, k_price, p_price, unit_cost = best
    contracts = max(1, int(ARB_CAP_PCT * data["bankroll"] / unit_cost))
    k_fee = kalshi_fee(k_price, contracts)
    p_fee = poly_taker_fee(p_price, contracts)
    cost = round(contracts * (k_price + p_price) + k_fee + p_fee, 2)
    profit = round(contracts * 1.0 - cost, 2)
    if cost > data["bankroll"] or profit <= 0:
        return False
    trade = new_trade(
        ticker, row["kalshi"].get("title", ""), side, contracts, k_price,
        STRATEGY, fee=round(k_fee + p_fee, 4), hold_to_settlement=True,
        arb_pair=True, poly_leg_price=round(p_price, 4),
        poly_event=row["poly_event"], pair_id=row["pair_id"],
        locked_profit=profit,
    )
    trade["cost"] = cost
    data["bankroll"] -= cost
    data["trades"].append(trade)
    print(f"  [xvenue] HARD ARB  {ticker:<36} {side.upper()} kalshi @ ${k_price:.2f} "
          f"+ poly opp @ ${p_price:.2f}  x{contracts} → locked ${profit:.2f}")
    return True


def try_fair_value(row: dict, data: dict, held: set) -> bool:
    """Tier 2: rest a Kalshi bid at least FAIR_MARGIN inside the poly mid."""
    k, p = kalshi_book(row["kalshi"]) or {}, row["poly"]
    if not k or k["hours"] < MIN_HOURS_LEFT:
        return False
    deep = (row["poly_vol24h"] >= MIN_POLY_VOL24H
            or row.get("poly_liquidity", 0) >= MIN_POLY_LIQUIDITY)
    if p["spread"] > MAX_POLY_SPREAD or not deep:
        return False
    ticker = row["kalshi"]["ticker"]
    k_mid = (k["bid"] + k["ask"]) / 2 if (k["bid"] > 0 and 0 < k["ask"] < 1) else None
    if k_mid is None:
        return False
    div = p["mid"] - k_mid
    if abs(div) < MIN_DIVERGENCE:
        return False

    if div > 0:   # Kalshi cheap → buy YES below poly fair value
        side = "yes"
        limit = floor_cent(min(p["mid"] - FAIR_MARGIN,
                               (k["ask"] - 0.01) if 0 < k["ask"] < 1 else 1.0))
        if limit < k["bid"] + 0.01:
            return False
        target = round(p["mid"], 4)
    else:          # Kalshi rich → buy NO below poly-implied NO fair value
        side = "no"
        no_fair = 1 - p["mid"]
        no_bid, no_ask = 1 - k["ask"], 1 - k["bid"]
        limit = floor_cent(min(no_fair - FAIR_MARGIN,
                               (no_ask - 0.01) if 0 < no_ask < 1 else 1.0))
        if no_bid > 0 and limit < no_bid + 0.01:
            return False
        target = round(p["mid"], 4)
    if limit <= 0 or limit >= 1:
        return False

    contracts = max(1, int(FAIR_CAP_PCT * data["bankroll"] / limit))
    order = new_resting_order(
        ticker, row["kalshi"].get("title", ""), side, contracts, limit,
        STRATEGY, expire_hours=min(EXPIRE_HOURS, max(1.0, k["hours"] - 1)),
        target_yes_mid=target, poly_mid_at_entry=p["mid"],
        yes_mid_at_entry=round(k_mid, 4), divergence_at_entry=round(div, 4),
        poly_event=row["poly_event"], pair_id=row["pair_id"],
    )
    if order["cost"] > data["bankroll"]:
        return False
    data["bankroll"] -= order["cost"]
    data["trades"].append(order)
    held.add(ticker)
    print(f"  [xvenue] FAIR VAL  {ticker:<36} {side.upper()} {contracts}x resting "
          f"@ ${limit:.2f}  poly_mid={p['mid']:.3f} kalshi_mid={k_mid:.3f} "
          f"div={div:+.3f}")
    return True


def run():
    if not PAIRS_FILE.exists():
        print("[xvenue] No pair map — create data/xvenue_pairs.json")
        return
    pairs = json.loads(PAIRS_FILE.read_text()).get("verified_pairs", [])
    data = load_journal(JOURNAL_FILE, STRATEGY)

    if resting_orders(data):
        print(f"[xvenue] Checking {len(resting_orders(data))} resting quotes...")
        check_resting_fills(data, STRATEGY)
        save_journal(data, JOURNAL_FILE)

    live = open_trades(data) + resting_orders(data)
    held = {t["kalshi_ticker"] for t in live}
    slots = MAX_OPEN_POSITIONS - len(live)

    print(f"[xvenue] {len(pairs)} verified pair rules")
    placed = 0
    for pair in pairs:
        for row in match_instances(pair):
            if placed >= slots:
                break
            ticker = row["kalshi"]["ticker"]
            if ticker in held or is_in_cooldown(ticker, data["trades"]):
                continue
            if try_hard_arb(row, data) or try_fair_value(row, data, held):
                held.add(ticker)
                placed += 1

    save_journal(data, JOURNAL_FILE)
    print(f"  [xvenue] {placed} new positions/quotes. Bankroll: ${data['bankroll']:.2f}")


def propose():
    """Print poly events resembling current Kalshi queue titles — human review only."""
    queue = Path(__file__).parent / "data" / "queue.json"
    if not queue.exists():
        print("No queue.json — run scanner.py first")
        return
    markets = json.loads(queue.read_text()).get("markets", [])
    seen = set()
    for m in markets:
        title = (m.get("title") or "")[:60]
        key = m["ticker"].split("-")[0]
        if key in seen or not title:
            continue
        seen.add(key)
        d = get_json(f"{POLY_GAMMA}/public-search", {"q": title, "limit_per_type": 2})
        hits = [(ev.get("title"), ev.get("slug")) for ev in d.get("events") or []]
        if hits:
            print(f"{key}: {title}")
            for t, s in hits:
                print(f"    candidate: {t}  ({s})")
    print("\nVerify resolution rules on BOTH venues before adding to xvenue_pairs.json.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "propose":
        propose()
    else:
        run()
