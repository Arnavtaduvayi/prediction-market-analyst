"""
bot_spot.py — Bot D: BTC Spot Convergence

Strategy: Kalshi BTC markets must converge to actual BTC spot price at
settlement. When Kalshi diverges meaningfully from a fair-value model based
on current spot + time remaining + historical volatility, bet on convergence.

Fair-value model:
  Assume log-normal price distribution. Given current spot S, time remaining
  T (hours), and annualized vol σ (~50% for BTC), the probability that BTC
  at settlement exceeds strike K is:

      P(S_T > K) = Φ( [ln(S/K) + (σ²/2) * (T/8760)] / [σ * sqrt(T/8760)] )

  Approximated with the cumulative normal distribution.

Strategy:
  - For each KXBTCD-... market, compute fair-value P(YES)
  - If |Kalshi YES mid - fair value| > 12%, enter
  - Direction: bet TOWARD the fair value (toward spot reality)

This is the most fundamental of all our strategies — actual price data, not
crowd sentiment.

Holds to settlement (no early exits — the convergence happens at resolution).
"""

import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from cooldown import is_in_cooldown

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_spot_trades.json"
INITIAL_BANKROLL = 75.0

MIN_DIVERGENCE = 0.12       # 12% min gap between Kalshi and fair value
MAX_DIVERGENCE = 0.45       # if >45%, skip — probably a model mismatch
BTC_ANNUAL_VOL = 0.55       # ~55% annualized BTC volatility
PER_TRADE_CAP_PCT = 0.04    # 4% of bankroll per trade
KELLY_MULT = 0.20
MAX_OPEN_POSITIONS = 8


def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return {
        "strategy": "spot_convergence",
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(d: dict):
    JOURNAL_FILE.write_text(json.dumps(d, indent=2, default=str))


def fetch_btc_spot() -> float | None:
    """Get current BTC USD spot price from CoinGecko (free, no auth)."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["bitcoin"]["usd"])
    except Exception as e:
        print(f"  [spot] CoinGecko fetch failed: {e}")
        return None


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value_yes_prob(spot: float, strike: float, hours_remaining: float,
                       annual_vol: float = BTC_ANNUAL_VOL) -> float:
    """
    Probability that BTC at settlement exceeds strike, given current spot,
    time remaining, and assumed lognormal dynamics.
    """
    if strike <= 0 or spot <= 0 or hours_remaining <= 0:
        return 1.0 if spot > strike else 0.0
    t_years = hours_remaining / (24 * 365)
    if t_years <= 0:
        return 1.0 if spot > strike else 0.0
    sigma_sqrt_t = annual_vol * math.sqrt(t_years)
    d = (math.log(spot / strike) + 0.5 * annual_vol ** 2 * t_years) / sigma_sqrt_t
    return _normal_cdf(d)


def _extract_strike(ticker: str) -> float | None:
    m = re.search(r"-T(\d+(?:\.\d+)?)", ticker)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


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
        print("[spot] No queue — run scanner.py first")
        return

    queue = json.loads(QUEUE_FILE.read_text())
    markets = [m for m in queue.get("markets", []) if m.get("ticker", "").startswith("KXBTCD")]

    if not markets:
        print("[spot] No KXBTCD markets in queue")
        return

    spot = fetch_btc_spot()
    if spot is None:
        print("[spot] Cannot proceed without spot price")
        return

    print(f"[spot] BTC spot = ${spot:,.2f} | scanning {len(markets)} KXBTCD markets...")

    data = load_journal()
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    slots = max(0, MAX_OPEN_POSITIONS - open_count)
    all_trades = list(data["trades"])

    candidates = []
    for m in markets:
        ticker = m["ticker"]
        strike = _extract_strike(ticker)
        if strike is None:
            continue
        hours = m.get("hours_left", 0)
        if hours <= 0:
            continue

        fair_yes = fair_value_yes_prob(spot, strike, hours)
        kalshi_yes_mid = m.get("yes_mid", 0)
        divergence = fair_yes - kalshi_yes_mid

        if abs(divergence) < MIN_DIVERGENCE or abs(divergence) > MAX_DIVERGENCE:
            continue

        side = "yes" if divergence > 0 else "no"
        if side == "yes":
            fill_price = m.get("yes_ask", kalshi_yes_mid)
            p_win = fair_yes
        else:
            fill_price = 1.0 - m.get("yes_bid", kalshi_yes_mid)
            p_win = 1.0 - fair_yes

        candidates.append({
            "ticker": ticker,
            "title": m.get("title", ""),
            "strike": strike,
            "spot": spot,
            "kalshi_mid": kalshi_yes_mid,
            "fair_yes": round(fair_yes, 4),
            "divergence": round(divergence, 4),
            "side": side,
            "fill_price": fill_price,
            "p_win": p_win,
            "hours_left": hours,
        })

    candidates.sort(key=lambda x: abs(x["divergence"]), reverse=True)

    print(f"[spot] {len(candidates)} candidates found above {MIN_DIVERGENCE:.0%} divergence")

    new_trades = []
    for c in candidates:
        if len(new_trades) >= slots:
            break
        if is_in_cooldown(c["ticker"], all_trades):
            continue
        if any(t["status"] == "open" and t["kalshi_ticker"] == c["ticker"] for t in data["trades"]):
            continue

        kf = kelly_size(c["p_win"], c["fill_price"])
        if kf <= 0:
            continue
        dollar_cost = kf * data["bankroll"]
        contracts = max(1, int(dollar_cost / c["fill_price"]))
        cost = round(contracts * c["fill_price"], 2)
        if cost > data["bankroll"] or cost < 0.30:
            continue

        new_trades.append({
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "strategy": "spot_convergence",
            "kalshi_ticker": c["ticker"],
            "kalshi_title": c["title"],
            "side": c["side"],
            "contracts": contracts,
            "entry_price": c["fill_price"],
            "cost": cost,
            "strike": c["strike"],
            "spot_at_entry": spot,
            "fair_yes_estimate": c["fair_yes"],
            "kalshi_mid_at_entry": c["kalshi_mid"],
            "divergence_at_entry": c["divergence"],
            "hours_left_at_entry": c["hours_left"],
            "hold_to_settlement": True,
            "status": "open",
            "resolved_yes": None,
            "pnl": None,
            "settled_at": None,
            "exit_reason": None,
        })
        data["bankroll"] -= cost
        print(f"  [spot] {c['ticker']:<42} {c['side'].upper()} {contracts}x @ ${c['fill_price']:.3f}  "
              f"strike=${c['strike']:,.0f}  spot=${spot:,.0f}  fair={c['fair_yes']:.3f}  "
              f"mkt={c['kalshi_mid']:.3f}  div={c['divergence']:+.2%}")

    data["trades"].extend(new_trades)
    save_journal(data)
    print(f"  [spot] {len(new_trades)} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
