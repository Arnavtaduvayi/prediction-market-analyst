"""
bot_weather.py — Bot D: Weather forecast edge (data-edge-heavy)

Kalshi runs daily high-temperature bracket markets for several cities
(KXHIGHNY, KXHIGHCHI, KXHIGHLAX, KXHIGHMIA, KXHIGHAUS). The market's implied
probability for a bracket is often off from what the National Weather Service
forecast says — especially in the tails, and these books are thin so they
reprice slowly.

For each open bracket:
  1. Parse the target date + temperature bounds from the market.
  2. Get the NWS forecast high for that city/date and a lead-time sigma.
  3. Model P(high in bracket) with a Normal distribution.
  4. If |model_p - market_p| > threshold AND the edge survives fee+spread, bet
     the side the forecast favours. Hold to same-day settlement.

This is NOT the naive v1 weather bot (that lost -$0.91 on 8 trades by betting a
point forecast directly). This prices the whole bracket probabilistically, only
acts on material divergence, and is fee-aware. It is still a statistical edge,
not a guarantee.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from botlib import (
    KALSHI_API, get_json, kalshi_fee, kelly_size,
    load_journal, save_journal, open_trades, new_trade,
)
from weather_data import STATIONS, forecast_high, bracket_probability

JOURNAL_FILE = Path(__file__).parent / "paper_weather_trades.json"
STRATEGY = "weather"

MIN_DIVERGENCE = 0.10        # model vs market gap required to act
MIN_EDGE_DOLLARS = 0.02      # expected $ edge/contract after fee
# Only trade FUTURE days. A same-day high is mostly already set by afternoon, so
# the market is near-settled and "divergence" from our forecast is illusory —
# we'd just be betting against a correct, nearly-resolved price.
MIN_LEAD_DAYS = 1
MAX_LEAD_DAYS = 3            # beyond this, forecast skill is too weak to claim edge
KELLY_MULT = 0.25
PER_TRADE_CAP_PCT = 0.06
MAX_OPEN_POSITIONS = 8
MIN_PRICE = 0.03             # avoid dust extremes both sides
MAX_PRICE = 0.97


def parse_bounds(sub_title: str) -> tuple[float | None, float | None] | None:
    """'90° or above' -> (90,None); '92° or below' -> (None,92); '87° to 88°' -> (87,88)."""
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", sub_title or "")]
    s = (sub_title or "").lower()
    if not nums:
        return None
    if "above" in s or "greater" in s or "or more" in s:
        return (nums[0], None)
    if "below" in s or "less" in s or "under" in s:
        return (None, nums[0])
    if len(nums) >= 2:
        return (min(nums[0], nums[1]), max(nums[0], nums[1]))
    return (nums[0], nums[0])  # single-degree bucket


def parse_target_date(ticker: str):
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1].title(), "%y%b%d").date()
    except ValueError:
        return None


def _book(m: dict) -> dict | None:
    try:
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        bid_size = float(m.get("yes_bid_size_fp") or 0)
        ask_size = float(m.get("yes_ask_size_fp") or 0)
    except (ValueError, TypeError):
        return None
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask >= 1:
        return None
    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "yes_mid": (yes_bid + yes_ask) / 2,
            "bid_size": bid_size, "ask_size": ask_size}


def fetch_markets(series_prefix: str) -> list[dict]:
    data = get_json(f"{KALSHI_API}/markets",
                    {"series_ticker": series_prefix, "status": "open", "limit": 200})
    return data.get("markets", [])


def evaluate(m: dict, series_prefix: str) -> dict | None:
    """Return a trade candidate for one bracket market, or None."""
    ticker = m.get("ticker", "")
    book = _book(m)
    if not book:
        return None
    bounds = parse_bounds(m.get("yes_sub_title", ""))
    target = parse_target_date(ticker)
    if not bounds or not target:
        return None
    lead = (target - datetime.now(timezone.utc).date()).days
    if not (MIN_LEAD_DAYS <= lead <= MAX_LEAD_DAYS):
        return None
    mu, sigma = forecast_high(series_prefix, target)
    if mu is None or sigma <= 0:
        return None

    model_p = bracket_probability(bounds[0], bounds[1], mu, sigma)
    market_p = book["yes_mid"]

    if model_p - market_p > MIN_DIVERGENCE and book["ask_size"] > 0:
        side, fill, p_win = "yes", book["yes_ask"], model_p
    elif market_p - model_p > MIN_DIVERGENCE and book["bid_size"] > 0:
        side, fill, p_win = "no", round(1.0 - book["yes_bid"], 4), 1.0 - model_p
    else:
        return None

    if fill < MIN_PRICE or fill > MAX_PRICE:
        return None
    edge = p_win * 1.0 - fill - kalshi_fee(fill, 1)
    if edge < MIN_EDGE_DOLLARS:
        return None

    return {
        "ticker": ticker, "title": m.get("title", ""), "side": side,
        "fill": fill, "p_win": p_win, "model_p": round(model_p, 3),
        "market_p": round(market_p, 3), "mu": round(mu, 1), "sigma": sigma,
        "bracket": m.get("yes_sub_title", ""), "edge": round(edge, 3),
        "yes_mid": round(book["yes_mid"], 4),
    }


def run():
    data = load_journal(JOURNAL_FILE, STRATEGY)
    held = {t["kalshi_ticker"] for t in open_trades(data)}
    slots = max(0, MAX_OPEN_POSITIONS - len(held))

    print(f"[weather] Scanning temp markets in {len(STATIONS)} cities...")
    candidates = []
    for series_prefix in STATIONS:
        for m in fetch_markets(series_prefix):
            if m.get("ticker") in held:
                continue
            c = evaluate(m, series_prefix)
            if c:
                candidates.append(c)

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    print(f"[weather] {len(candidates)} brackets diverge from forecast by >{MIN_DIVERGENCE:.0%}")

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
            c["ticker"], c["title"], c["side"], contracts, c["fill"], STRATEGY,
            fee=fee, hold_to_settlement=True,
            yes_mid_at_entry=c["yes_mid"],
            forecast_high=c["mu"], forecast_sigma=c["sigma"],
            model_prob=c["model_p"], market_prob=c["market_p"], bracket=c["bracket"],
        )
        data["bankroll"] -= cost
        data["trades"].append(trade)
        placed += 1
        print(f"  [weather] {c['ticker']:<28} {c['side'].upper()} {contracts}x @ ${c['fill']:.3f}  "
              f"model={c['model_p']:.2f} mkt={c['market_p']:.2f} fc={c['mu']}°±{c['sigma']:.0f}  [{c['bracket']}]")

    save_journal(data, JOURNAL_FILE)
    print(f"  [weather] {placed} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
