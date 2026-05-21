"""
executor.py — Consensus + Kelly Sizing (Agent 3)

Reads thesis.json, treats the 3 source agents (base_rate, whale, disposition)
as 3 voting agents. Consensus rules:

  - 2 or 3 agents agree on side → FULL Quarter-Kelly position
  - 1 agent only                → HALF position
  - Disagreement                → SKIP

Uses Kelly sizing (cap at 25% of bankroll, per LunarResearcher methodology).

Output: appends new positions to paper_cross_trades.json with
  - thesis_target_price : the price we expect the market to move toward
                          (used by exit_monitor for the 85% take-profit rule)
  - entry_time_iso      : used for the 24h stale-thesis exit
  - max_position_pct    : cap honored
"""

import json
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_FILE = Path(__file__).parent / "paper_cross_trades.json"
THESIS_FILE = Path(__file__).parent / "data" / "thesis.json"

INITIAL_BANKROLL = 75.0

# Kelly settings (per LunarResearcher: cap at quarter Kelly)
KELLY_CAP = 0.25            # never bet more than 25% of bankroll
QUARTER_KELLY = 0.25        # multiplier on f*
PER_TRADE_CAP_PCT = 0.10    # hard ceiling: 10% of bankroll per single trade
MAX_OPEN_POSITIONS = 5      # don't over-concentrate at $51


def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return {
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(data: dict):
    JOURNAL_FILE.write_text(json.dumps(data, indent=2, default=str))


def kelly_size(p_win: float, market_price: float) -> float:
    """
    Kelly fraction f* = (p * b - q) / b, where b = 1/price - 1, q = 1 - p.
    Returns fraction of bankroll to bet (already quarter-Kelly).
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1.0
    q = 1.0 - p_win
    f_star = (p_win * b - q) / b
    if f_star <= 0:
        return 0.0
    return min(f_star * QUARTER_KELLY, KELLY_CAP, PER_TRADE_CAP_PCT)


def consensus_score(thesis: dict) -> int:
    """How many of the 3 source checks agreed on this thesis's side?"""
    target_side = thesis["side"]
    votes = 0
    for src in ("base_rate", "whale", "disposition"):
        s = thesis.get(src, {}).get("side_hint")
        if s == target_side:
            votes += 1
    return votes


def run(dry_run: bool = False) -> dict:
    if not THESIS_FILE.exists():
        print(f"No thesis at {THESIS_FILE} — run brain.py first.")
        return {"new_trades": []}

    data = load_journal()
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    available_slots = max(0, MAX_OPEN_POSITIONS - open_count)
    if available_slots == 0:
        print(f"Already {open_count} open positions (max {MAX_OPEN_POSITIONS}) — no new entries.")
        return {"new_trades": []}

    existing_tickers = {t["kalshi_ticker"] for t in data["trades"] if t["status"] == "open"}
    theses = json.loads(THESIS_FILE.read_text()).get("theses", [])

    print(f"Executor: {len(theses)} candidate theses, {available_slots} slots available")

    new_trades = []
    for th in theses:
        if len(new_trades) >= available_slots:
            break
        ticker = th["ticker"]
        if ticker in existing_tickers:
            continue

        # Apply consensus rule
        votes = consensus_score(th)
        if votes < 1:
            continue
        size_multiplier = 1.0 if votes >= 2 else 0.5

        # Determine the price we'd actually pay (NO side = 1 - YES mid)
        side = th["side"]
        if side == "yes":
            fill_price = th["yes_ask"]    # taker pays the ask
        else:
            fill_price = 1.0 - th["yes_bid"]  # NO ask = 1 - YES bid

        # Confidence → probability estimate
        # If we have a Polymarket estimate, use it directly; else use confidence as proxy
        p_win = th.get("estimate")
        if p_win is None:
            p_win = th["confidence"]
        if side == "no":
            p_win = 1.0 - p_win

        kelly_frac = kelly_size(p_win, fill_price) * size_multiplier
        if kelly_frac <= 0:
            continue

        dollar_cost = kelly_frac * data["bankroll"]
        contracts = max(1, int(dollar_cost / fill_price))
        cost = round(contracts * fill_price, 2)
        if cost > data["bankroll"]:
            continue
        if cost < 0.50:  # don't bother with sub-50¢ trades
            continue

        # Compute thesis target price (85% of expected move, for exit trigger)
        if th.get("estimate") is not None and th.get("yes_mid") is not None:
            expected_move = abs(th["estimate"] - th["yes_mid"])
            if side == "yes":
                target_price = th["yes_mid"] + expected_move * 0.85
            else:
                target_price = th["yes_mid"] - expected_move * 0.85
        else:
            target_price = None

        trade = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "kalshi_ticker": ticker,
            "kalshi_title": th.get("title", ""),
            "side": side,
            "contracts": contracts,
            "entry_price": fill_price,
            "cost": cost,
            "yes_mid_at_entry": th["yes_mid"],
            "thesis_estimate": th.get("estimate"),
            "thesis_target_price": round(target_price, 4) if target_price else None,
            "thesis_confidence": th["confidence"],
            "consensus_votes": votes,
            "size_multiplier": size_multiplier,
            "kelly_fraction": round(kelly_frac, 4),
            "checks_passed": th["checks_passed"],
            "hours_left_at_entry": th["hours_left"],
            "status": "open",
            "resolved_yes": None,
            "pnl": None,
            "settled_at": None,
            "exit_reason": None,
        }

        if dry_run:
            print(f"  DRY-RUN: {ticker}  {side.upper()}  {contracts}x @ ${fill_price:.3f}  "
                  f"votes={votes}  conf={th['confidence']:.2f}  cost=${cost:.2f}")
        else:
            new_trades.append(trade)
            data["bankroll"] -= cost
            print(f"  LOGGED: {ticker}  {side.upper()}  {contracts}x @ ${fill_price:.3f}  "
                  f"votes={votes}  conf={th['confidence']:.2f}  cost=${cost:.2f}  "
                  f"target=${target_price:.3f}" if target_price else "")

    if not dry_run:
        data["trades"].extend(new_trades)
        save_journal(data)
        print(f"\n  {len(new_trades)} new paper trades. Bankroll: ${data['bankroll']:.2f}")
    return {"new_trades": new_trades}


def loop(interval_seconds: int = 420, dry_run: bool = False):
    """Continuous mode: run executor every N seconds."""
    import sys as _sys, time as _time
    print(f"Executor loop starting (interval={interval_seconds}s)", flush=True)
    while True:
        try:
            run(dry_run=dry_run)
        except Exception as e:
            print(f"  Executor error: {e}", file=_sys.stderr, flush=True)
        _time.sleep(interval_seconds)


if __name__ == "__main__":
    import sys
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=420)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.loop:
        loop(args.interval, dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run)
