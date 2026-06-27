"""
botlib.py — Shared helpers for the v2 bot roster (C–G).

Centralizes the bits that were copy-pasted across the old per-bot files, plus
the pieces where correctness actually matters:

  - kalshi_fee()   : Kalshi's published per-trade fee. Get this wrong and the
                     arb bot "finds" profits that don't exist.
  - new_trade()    : one canonical trade-record shape so exit_monitor.py and
                     paper_cross.py can read every journal the same way.
  - vwap/flow      : recent-trade analytics reused by reversion + consensus.

The two kept bots (whale-copy, disposition) are intentionally NOT migrated onto
this — leaving them untouched avoids regressions in the only roughly-flat bots.
"""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
INITIAL_BANKROLL = 75.0

# Series driven by a live, continuously-trading external price (crypto strike
# ladders). Their Kalshi mid moves because the underlying moved — that's real
# information, not thin-volume noise — so mean-reversion / fade strategies must
# steer clear of them (they caused 100% of Reversion's early losses).
CONTINUOUS_PRICE_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXADA", "KXLTC")

# Kalshi general-markets trading fee: ceil(rate * C * P * (1-P)), rounded up to
# the next cent, charged per order. rate is 0.07 for general markets.
# https://kalshi.com/docs/kalshi-fee-schedule
DEFAULT_FEE_RATE = 0.07


# ── time ────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── HTTP ────────────────────────────────────────────────────────────────────

_HEADERS = {"User-Agent": "prediction-market-analyst/2.0 (paper trading research)"}


def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    """GET with retry + 429 backoff. Returns {} on failure (never raises)."""
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params=params or {},
                headers={**_HEADERS, **(headers or {})},
                timeout=15,
            )
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


# ── fees ────────────────────────────────────────────────────────────────────

def kalshi_fee(price: float, contracts: int, rate: float = DEFAULT_FEE_RATE) -> float:
    """
    Per-order trading fee in dollars. Kalshi rounds UP to the next cent:
        fee = ceil(rate * C * P * (1-P) * 100) / 100
    At P=0.50, 1 contract: 0.07*0.25 = 0.0175 -> $0.02.
    Fees are zero at the P=0 / P=1 extremes.
    """
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


# ── journals ────────────────────────────────────────────────────────────────

def load_journal(path: Path, strategy: str) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "strategy": strategy,
        "started": now_iso(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, default=str))


def open_trades(data: dict) -> list[dict]:
    return [t for t in data.get("trades", []) if t.get("status") == "open"]


def new_trade(ticker: str, title: str, side: str, contracts: int,
              entry_price: float, strategy: str, *, fee: float | None = None,
              **extra) -> dict:
    """
    Build a canonical trade record. `cost` includes the entry fee so realized
    PnL (computed by exit_monitor as contracts*payout - cost) is fee-accurate.
    `extra` carries strategy-specific fields: thesis_target_price,
    hold_to_settlement, stop_loss_price, arb_id, yes_mid_at_entry, ...
    """
    if fee is None:
        fee = kalshi_fee(entry_price, contracts)
    cost = round(contracts * entry_price + fee, 2)
    trade = {
        "logged_at": now_iso(),
        "strategy": strategy,
        "kalshi_ticker": ticker,
        "kalshi_title": title,
        "side": side,
        "contracts": contracts,
        "entry_price": round(entry_price, 4),
        "fee": round(fee, 4),
        "cost": cost,
        "status": "open",
        "resolved_yes": None,
        "pnl": None,
        "settled_at": None,
        "exit_reason": None,
    }
    trade.update(extra)
    return trade


# ── sizing ──────────────────────────────────────────────────────────────────

def kelly_size(p_win: float, fill_price: float, kelly_mult: float,
               per_trade_cap_pct: float) -> float:
    """Fraction of bankroll to stake (quarter-Kelly style), capped per trade."""
    if fill_price <= 0 or fill_price >= 1:
        return 0.0
    b = (1.0 / fill_price) - 1.0
    q = 1.0 - p_win
    f_star = (p_win * b - q) / b
    if f_star <= 0:
        return 0.0
    return min(f_star * kelly_mult, per_trade_cap_pct)


# ── market microstructure (recent trades) ───────────────────────────────────

def recent_trades(ticker: str, minutes: int) -> list[dict]:
    min_ts = int(datetime.now(timezone.utc).timestamp() - minutes * 60)
    data = get_json(f"{KALSHI_API}/markets/trades",
                    {"ticker": ticker, "min_ts": min_ts, "limit": 1000})
    return data.get("trades", [])


def vwap(trades: list[dict]) -> float | None:
    """Volume-weighted average YES price over the given trades."""
    num = den = 0.0
    for t in trades:
        try:
            count = float(t.get("count_fp") or 0)
            yes_p = float(t.get("yes_price_dollars") or 0)
        except (ValueError, TypeError):
            continue
        if count <= 0 or yes_p <= 0:
            continue
        num += count * yes_p
        den += count
    return (num / den) if den else None


def flow_imbalance(trades: list[dict]) -> dict | None:
    """Taker-side dollar imbalance over the given trades."""
    yes_usd = no_usd = 0.0
    for t in trades:
        try:
            count = float(t.get("count_fp") or 0)
            yes_p = float(t.get("yes_price_dollars") or 0)
            no_p = float(t.get("no_price_dollars") or 0)
        except (ValueError, TypeError):
            continue
        taker = (t.get("taker_outcome_side") or "").lower()
        if taker == "yes":
            yes_usd += count * yes_p
        elif taker == "no":
            no_usd += count * no_p
    total = yes_usd + no_usd
    if total <= 0:
        return None
    return {"yes_share": yes_usd / total, "total_usd": total}


# ── live market book ────────────────────────────────────────────────────────

def fetch_book(ticker: str) -> dict | None:
    """Current top-of-book for one market, prices in dollars. None if no book."""
    m = get_json(f"{KALSHI_API}/markets/{ticker}").get("market", {})
    if not m:
        return None
    try:
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        bid_size = float(m.get("yes_bid_size_fp") or 0)
        ask_size = float(m.get("yes_ask_size_fp") or 0)
    except (ValueError, TypeError):
        return None
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask >= 1:
        return None
    return {
        "ticker": ticker,
        "title": m.get("title", ""),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": round((yes_bid + yes_ask) / 2, 4),
        "bid_size": bid_size,
        "ask_size": ask_size,
        "yes_sub_title": m.get("yes_sub_title", ""),
        "no_sub_title": m.get("no_sub_title", ""),
        "rules_primary": m.get("rules_primary", ""),
        "status": m.get("status", ""),
        "close_time": m.get("close_time", ""),
    }
