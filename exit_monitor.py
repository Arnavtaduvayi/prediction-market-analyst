"""
exit_monitor.py — Exit Triggers (Agent 4)

This is the agent that the LunarResearcher methodology says "nobody talks
about". The top wallets don't hold to settlement — they buy at 0.40, sell at
0.65, and move on. The last 35¢ of profit isn't worth the risk.

Three exit triggers per open position:

  1. TARGET_HIT     : Kalshi price reached 85% of expected move toward thesis estimate
  2. VOLUME_EXIT   : 10-minute trade volume on this ticker > 3× baseline (smart
                     money leaving the position before us)
  3. STALE_THESIS  : 24h since entry AND |price change| < 2% → thesis isn't playing out

Also handles SETTLEMENT — if the market resolved while open, we settle here
(fallback to old behavior in case all exit triggers missed).

Output: updates paper_cross_trades.json, prints actions taken.
Designed to run hourly via cron, not just twice a day.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

WHALE_JOURNAL = Path(__file__).parent / "paper_cross_trades.json"
DISPOSITION_JOURNAL = Path(__file__).parent / "paper_disposition_trades.json"
ARB_JOURNAL = Path(__file__).parent / "paper_arb_trades.json"
WEATHER_JOURNAL = Path(__file__).parent / "paper_weather_trades.json"
REVERSION_JOURNAL = Path(__file__).parent / "paper_reversion_trades.json"
THETA_JOURNAL = Path(__file__).parent / "paper_theta_trades.json"
CONSENSUS_JOURNAL = Path(__file__).parent / "paper_consensus_trades.json"

JOURNALS = [
    (WHALE_JOURNAL, "whale"),
    (DISPOSITION_JOURNAL, "disposition"),
    (ARB_JOURNAL, "arb"),
    (WEATHER_JOURNAL, "weather"),
    (REVERSION_JOURNAL, "reversion"),
    (THETA_JOURNAL, "theta"),
    (CONSENSUS_JOURNAL, "consensus"),
]

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Exit thresholds (from the LunarResearcher methodology)
TARGET_HIT_PCT = 0.85
VOLUME_SPIKE_RATIO = 3.0
STALE_HOURS = 24
STALE_PRICE_CHANGE = 0.02


def _get(url: str, params: dict = None) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
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


def load_journal(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"started": "", "initial_bankroll": 75.0, "bankroll": 75.0, "trades": []}


def save_journal(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, default=str))


def current_kalshi_state(ticker: str) -> dict:
    """Fetch live market state + recent volume bucket."""
    market = _get(f"{KALSHI_API}/markets/{ticker}").get("market", {})
    if not market:
        return {}

    try:
        yes_bid = float(market.get("yes_bid_dollars") or 0)
        yes_ask = float(market.get("yes_ask_dollars") or 1)
        yes_mid = (yes_bid + yes_ask) / 2
    except (ValueError, TypeError):
        yes_mid = None

    status = market.get("status", "")
    settlement = market.get("settlement_value_dollars", "")
    try:
        settlement_val = float(settlement) if settlement else None
    except (ValueError, TypeError):
        settlement_val = None

    return {
        "yes_mid": yes_mid,
        "yes_bid": yes_bid if yes_mid is not None else None,
        "yes_ask": yes_ask if yes_mid is not None else None,
        "status": status,
        "settlement": settlement_val,
        "volume_24h": float(market.get("volume_24h_fp") or 0),
        "raw": market,
    }


def recent_volume(ticker: str, minutes: int = 10) -> float:
    """Sum trade volume in the last N minutes."""
    cutoff = int(datetime.now(timezone.utc).timestamp() - minutes * 60)
    data = _get(f"{KALSHI_API}/markets/trades", {
        "ticker": ticker, "min_ts": cutoff, "limit": 500,
    })
    trades = data.get("trades", [])
    return sum(float(t.get("count_fp") or 0) for t in trades)


def check_exit_triggers(trade: dict, state: dict) -> tuple[str | None, float]:
    """
    Returns (exit_reason, exit_price) or (None, 0.0) if no exit fired.
    exit_price is what we'd sell at (Kalshi YES bid if side=yes, ask-flip if side=no).

    Disposition trades (hold_to_settlement=True) skip all triggers — they only
    settle when the market resolves.
    """
    # Disposition strategy holds to settlement — no early exits
    if trade.get("hold_to_settlement"):
        return None, 0.0

    side = trade["side"]
    yes_mid = state.get("yes_mid")
    if yes_mid is None:
        return None, 0.0

    yes_bid = state.get("yes_bid", yes_mid)
    yes_ask = state.get("yes_ask", yes_mid)

    # Exit price = what we'd receive by selling our position now
    if side == "yes":
        exit_price = yes_bid          # we sell to the bid
    else:
        exit_price = 1.0 - yes_ask    # NO bid = 1 - YES ask

    # ── Side-aware target / stop (used by reversion, consensus) ──
    # These express the exit level directly as a YES-mid threshold, which is
    # unambiguous for both sides (unlike the legacy thesis_target_price below).
    tgt = trade.get("target_yes_mid")
    if tgt is not None:
        if side == "yes" and yes_mid >= tgt:
            return "TARGET_HIT", exit_price
        if side == "no" and yes_mid <= tgt:
            return "TARGET_HIT", exit_price
    stop = trade.get("stop_yes_mid")
    if stop is not None:
        if side == "yes" and yes_mid <= stop:
            return "STOP_LOSS", exit_price
        if side == "no" and yes_mid >= stop:
            return "STOP_LOSS", exit_price

    # ── Trigger 1: target hit ──
    target = trade.get("thesis_target_price")
    if target is not None:
        if side == "yes" and yes_mid >= target:
            return "TARGET_HIT", exit_price
        if side == "no" and yes_mid <= (1.0 - target if target > 0.5 else target):
            # target was specified in same direction as side: NO target < entry
            return "TARGET_HIT", exit_price

    # ── Trigger 2: volume spike ──
    # Compare 10-min volume to baseline (24h vol / 144 ten-min buckets)
    recent = recent_volume(trade["kalshi_ticker"], minutes=10)
    baseline_10min = state.get("volume_24h", 0) / 144  # 1440 minutes / 10 = 144 buckets
    if baseline_10min > 0 and recent > VOLUME_SPIKE_RATIO * baseline_10min and recent > 50:
        return "VOLUME_EXIT", exit_price

    # ── Trigger 3: stale thesis ──
    try:
        entry_time = datetime.fromisoformat(trade["logged_at"].replace("Z", "+00:00"))
    except Exception:
        entry_time = datetime.now(timezone.utc)
    hours_open = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
    if hours_open > STALE_HOURS:
        entry_mid = trade.get("yes_mid_at_entry", yes_mid)
        if abs(yes_mid - entry_mid) < STALE_PRICE_CHANGE:
            return "STALE_THESIS", exit_price

    return None, 0.0


def settle_or_exit(trade: dict) -> tuple[str, float]:
    """
    Decide what to do with this open trade:
      - if Kalshi settled it → realize win/loss
      - else check exit triggers
      - else hold
    Returns (action, pnl_delta_to_bankroll).
      action ∈ {"HOLD", "TARGET_HIT", "VOLUME_EXIT", "STALE_THESIS", "SETTLED_WIN", "SETTLED_LOSS"}
      pnl_delta is the FULL amount returned to bankroll (cost + pnl).
    """
    ticker = trade["kalshi_ticker"]
    state = current_kalshi_state(ticker)
    side = trade["side"]
    contracts = trade["contracts"]
    cost = trade["cost"]
    entry_price = trade["entry_price"]

    # Settled?
    if state.get("status") in ("settled", "determined", "finalized") and state.get("settlement") is not None:
        resolved_yes = state["settlement"] >= 0.99
        won = (resolved_yes if side == "yes" else not resolved_yes)
        pnl = round((contracts * 1.0 - cost) if won else -cost, 2)
        trade["status"] = "settled"
        trade["resolved_yes"] = resolved_yes
        trade["pnl"] = pnl
        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
        trade["exit_reason"] = "SETTLED_WIN" if won else "SETTLED_LOSS"
        return trade["exit_reason"], cost + pnl

    # Exit triggers?
    reason, exit_price = check_exit_triggers(trade, state)
    if reason:
        # Realize the PnL at the current exit price
        gross = contracts * exit_price
        pnl = round(gross - cost, 2)
        trade["status"] = "exited"
        trade["pnl"] = pnl
        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
        trade["exit_reason"] = reason
        trade["exit_price"] = round(exit_price, 4)
        return reason, gross

    return "HOLD", 0.0


def process_journal(path: Path, label: str):
    """Run exit checks for one bot's journal."""
    data = load_journal(path)
    open_trades = [t for t in data["trades"] if t["status"] == "open"]

    if not open_trades:
        print(f"  [{label}] No open trades.")
        return

    print(f"  [{label}] Checking {len(open_trades)} open trades...")
    for trade in open_trades:
        action, delta = settle_or_exit(trade)
        if action == "HOLD":
            print(f"    [{label}] HOLD       {trade['kalshi_ticker']:<42}")
        else:
            data["bankroll"] += delta
            print(f"    [{label}] {action:<14} {trade['kalshi_ticker']:<42} "
                  f"pnl=${trade.get('pnl', 0):+.2f}  bankroll → ${data['bankroll']:.2f}")
        time.sleep(0.2)
    save_journal(data, path)


def run():
    ts = datetime.now().isoformat(timespec='seconds')
    print(f"[{ts}] Exit monitor — all 5 bots")
    for path, label in JOURNALS:
        process_journal(path, label)


def loop(interval_seconds: int = 60):
    """Continuous mode: check exits every N seconds. Critical to react fast."""
    import sys as _sys
    print(f"Exit monitor loop starting (interval={interval_seconds}s)", flush=True)
    while True:
        try:
            run()
        except Exception as e:
            print(f"  Exit monitor error: {e}", file=_sys.stderr, flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=60, help="Seconds between exit checks")
    args = p.parse_args()
    if args.loop:
        loop(args.interval)
    else:
        run()
