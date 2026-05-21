"""
Weather Temperature Strategy

Compares GFS ensemble probabilities to live Kalshi KXHIGH market prices.
Finds markets where |model_probability - market_price| > MIN_EDGE.
Sizes positions with Quarter-Kelly, capped at MAX_POSITION_FRACTION of bankroll.

Edge source:
  The 31-member GFS ensemble captures forecast uncertainty better than the
  normal distribution implicitly assumed by Kalshi's market prices. Markets
  priced at, e.g., 0.40 often reflect a genuine probability of 0.52+ or 0.28-
  once the ensemble spread is properly accounted for.

Key rules from the research:
  1. Only trade when edge > MIN_EDGE (8%) — selectivity is the alpha
  2. Use limit orders only — never market orders (taker spread kills returns)
  3. Never buy contracts priced below $0.15 (favorite-longshot bias)
  4. Quarter-Kelly sizing, hard cap at 5% of bankroll per trade
"""

import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from weather_model import CityForecast, fetch_all_cities

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MIN_EDGE = 0.08          # minimum |model_prob - market_price| to trade
MIN_MARKET_PRICE = 0.15  # never buy longshots below this (longshot bias)
MAX_MARKET_PRICE = 0.90  # never buy near-certainties (tiny upside)
MAX_POSITION_FRACTION = 0.05   # hard cap: 5% of bankroll per trade
KELLY_FRACTION = 0.25    # quarter-Kelly


@dataclass
class Signal:
    ticker: str
    city: str
    threshold: float
    side: str            # "yes" (betting above threshold) or "no" (betting below)
    market_price: float  # current Kalshi price
    model_prob: float    # GFS ensemble probability
    edge: float          # |model_prob - market_price|
    kelly_fraction: float
    target_date: date

    def limit_price(self, slippage: float = 0.01) -> float:
        """
        Limit price to post as maker.
        Slightly worse than mid to ensure we get filled, but better than market.
        """
        if self.side == "yes":
            return round(min(self.market_price + slippage, self.model_prob - 0.02), 2)
        else:
            # Buying NO = equivalent YES price is (1 - no_price)
            no_market = 1.0 - self.market_price
            return round(min(no_market + slippage, 1.0 - self.model_prob - 0.02), 2)

    def contracts_for_bankroll(self, bankroll: float) -> int:
        """How many $1-face-value contracts to buy given bankroll."""
        kelly_bet = self.kelly_fraction * bankroll
        capped = min(kelly_bet, MAX_POSITION_FRACTION * bankroll)
        price = self.limit_price()
        if price <= 0:
            return 0
        contracts = int(capped / price)
        return max(contracts, 1)  # minimum 1 if we're trading at all

    def dollar_amount(self, bankroll: float) -> float:
        return self.contracts_for_bankroll(bankroll) * self.limit_price()


CITY_CODES = {
    # KXTEMP format codes → weather_model city keys
    "NYCH": "NYC",   # New York City High
    "NYC":  "NYC",
    "CHIH": "CHI",
    "CHI":  "CHI",
    "MIAH": "MIA",
    "MIA":  "MIA",
    "LAXH": "LAX",
    "LAX":  "LAX",
    "DENH": "DEN",
    "DEN":  "DEN",
    "ATLH": "ATL",
    "ATL":  "ATL",
    "SEAH": "SEA",
    "SEA":  "SEA",
    "PHXH": "PHX",
    "PHX":  "PHX",
    "DALH": "DAL",
    "DAL":  "DAL",
    "BOSH": "BOS",
    "BOS":  "BOS",
}


def _parse_threshold_from_ticker(ticker: str) -> float | None:
    """
    Supports both formats:
      KXHIGHNYC-26MAY17-B75        → 75.0   (old format)
      KXTEMPNYCH-26MAY1614-T72.99  → 72.99  (new format)
    """
    # New format: -T{float}
    m = re.search(r"-T([\d.]+)$", ticker)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # Old format: -B{int}(DOT{frac})?
    m = re.search(r"-B(\d+)(DOT(\d+))?", ticker)
    if m:
        whole = int(m.group(1))
        frac = int(m.group(3)) / 10 if m.group(3) else 0.0
        return float(whole) + frac
    return None


def _parse_city_from_ticker(ticker: str) -> str | None:
    """
    Extracts standardized city key from either ticker format.
    KXHIGHNYC-... → 'NYC'
    KXTEMPNYCH-... → 'NYC'
    """
    # New format: KXTEMP{CODE}-
    m = re.match(r"KXTEMP([A-Z]+)-", ticker)
    if m:
        code = m.group(1)
        return CITY_CODES.get(code)
    # Old format: KXHIGH{CODE}-
    m = re.match(r"KXHIGH([A-Z]+)-", ticker)
    if m:
        code = m.group(1)
        return CITY_CODES.get(code, code)
    return None


def _fetch_temp_markets(target_date: date) -> list[dict]:
    """Fetch open temperature markets (both KXTEMP and KXHIGH series)."""
    date_str = target_date.strftime("%y%b%d").upper()  # e.g. 26MAY17
    try:
        r = requests.get(f"{BASE_URL}/markets", params={
            "status": "open",
            "limit": 500,
            "mve_filter": "exclude",
        }, timeout=15)
        r.raise_for_status()
        markets = r.json().get("markets", [])
    except requests.RequestException:
        return []

    return [
        m for m in markets
        if (m.get("ticker", "").startswith(("KXTEMP", "KXHIGH"))
            and date_str in m.get("ticker", ""))
    ]


def kelly_size(model_prob: float, market_price: float, side: str) -> float:
    """
    Binary Kelly: f* = (p_true - p_market) / (1 - p_market)  [for YES bets]
    Apply quarter-Kelly multiplier.
    """
    if side == "yes":
        p = model_prob
        q = market_price
    else:
        p = 1.0 - model_prob    # probability NO wins
        q = 1.0 - market_price  # NO price

    if q >= 1.0 or p <= q:
        return 0.0

    full_kelly = (p - q) / (1.0 - q)
    return max(0.0, full_kelly * KELLY_FRACTION)


def find_signals(target_date: date | None = None) -> list[Signal]:
    """
    Main strategy function. Returns a list of Signals sorted by edge descending.
    Call this once per day before markets open.
    """
    if target_date is None:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date()
        target_date = today + timedelta(days=1)  # trade on tomorrow's markets

    print(f"\nFetching GFS ensemble for {target_date}...")
    forecasts = fetch_all_cities(target_date)
    if not forecasts:
        print("No forecast data available.")
        return []

    print(f"\nFetching temperature markets for {target_date}...")
    markets = _fetch_temp_markets(target_date)
    print(f"Found {len(markets)} temperature markets")
    time.sleep(0.2)

    signals = []
    for m in markets:
        ticker = m.get("ticker", "")
        city = _parse_city_from_ticker(ticker)
        threshold = _parse_threshold_from_ticker(ticker)

        if not city or threshold is None or city not in forecasts:
            continue

        fc: CityForecast = forecasts[city]
        model_prob_yes = fc.prob_above(threshold)  # P(temp > threshold)
        model_prob_no = fc.prob_below(threshold)   # P(temp <= threshold)

        # Current market price (YES price = P(high > threshold))
        yes_price_str = m.get("yes_bid_dollars") or m.get("last_price_dollars") or "0"
        try:
            market_yes_price = float(yes_price_str)
        except ValueError:
            continue

        if market_yes_price <= 0 or market_yes_price >= 1:
            continue

        # Check YES side
        yes_edge = model_prob_yes - market_yes_price
        if (yes_edge >= MIN_EDGE
                and market_yes_price >= MIN_MARKET_PRICE
                and market_yes_price <= MAX_MARKET_PRICE):
            k = kelly_size(model_prob_yes, market_yes_price, "yes")
            if k > 0:
                signals.append(Signal(
                    ticker=ticker, city=city, threshold=threshold,
                    side="yes", market_price=market_yes_price,
                    model_prob=model_prob_yes, edge=yes_edge,
                    kelly_fraction=k, target_date=target_date,
                ))

        # Check NO side (buy NO when market YES price is too high)
        no_edge = model_prob_no - (1.0 - market_yes_price)
        no_market_price = 1.0 - market_yes_price
        if (no_edge >= MIN_EDGE
                and no_market_price >= MIN_MARKET_PRICE
                and no_market_price <= MAX_MARKET_PRICE):
            k = kelly_size(model_prob_yes, market_yes_price, "no")
            if k > 0:
                signals.append(Signal(
                    ticker=ticker, city=city, threshold=threshold,
                    side="no", market_price=market_yes_price,
                    model_prob=model_prob_yes, edge=no_edge,
                    kelly_fraction=k, target_date=target_date,
                ))

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def print_signals(signals: list[Signal], bankroll: float):
    if not signals:
        print("\nNo signals found today. Market prices are within 8% of GFS ensemble.")
        return

    print(f"\n{'='*90}")
    print(f"  WEATHER SIGNALS  —  {len(signals)} opportunities  (bankroll: ${bankroll:.2f})")
    print(f"{'='*90}")
    print(f"{'Ticker':<40} {'City':<5} {'Thr°F':>6} {'Side':<5} {'Mkt':>6} {'Model':>6} {'Edge':>6} {'Limit':>6} {'$Bet':>6}")
    print(f"{'-'*90}")
    for s in signals:
        bet = s.dollar_amount(bankroll)
        lp = s.limit_price()
        print(
            f"{s.ticker:<40} {s.city:<5} {s.threshold:>6.1f} {s.side.upper():<5} "
            f"{s.market_price:>6.3f} {s.model_prob:>6.3f} {s.edge:>+6.3f} "
            f"{lp:>6.3f} ${bet:>5.2f}"
        )
    total = sum(s.dollar_amount(bankroll) for s in signals)
    print(f"\n  Total capital deployed: ${total:.2f} of ${bankroll:.2f} bankroll")
