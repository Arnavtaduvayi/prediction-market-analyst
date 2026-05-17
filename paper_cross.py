"""
Paper Trading Harness — Cross-Platform Polymarket→Kalshi Strategy

Runs the cross_signal.py strategy and logs hypothetical trades to a JSON
journal without placing real orders. Each evening, checks resolved markets
to update P&L.

Usage:
  python3 paper_cross.py signal           # log today's signals as paper trades
  python3 paper_cross.py settle           # mark resolved trades and update P&L
  python3 paper_cross.py status           # print running scorecard
  python3 paper_cross.py status --json    # JSON output

Designed to be invoked by cron for a 1-week paper trading experiment.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from cross_signal import (
    KALSHI_API,
    REQUEST_DELAY,
    _get,
    build_cross_signals,
    compute_poly_consensus,
    fetch_kalshi_markets,
    fetch_poly_leaderboard,
    fetch_poly_trades,
)

JOURNAL_FILE = Path(__file__).parent / "paper_cross_trades.json"
INITIAL_BANKROLL = 51.0

# Filters for the paper experiment — calibrated to log a few trades per day
# so we can actually measure performance after a week
PAPER_MIN_DIVERGENCE = 0.02   # 2% Polymarket vs Kalshi gap
PAPER_MIN_TRADERS = 2         # at least 2 top traders agreeing
PAPER_MIN_MATCH_SCORE = 0.35  # text-match confidence (lowered from 0.45)
PAPER_TRADERS = 50            # number of top Polymarket traders to scan
PAPER_DAYS_LOOKBACK = 30
PAPER_TOP_SIGNALS = 5         # max new trades to log per signal run


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


# ── Position sizing (quarter-Kelly with $51 bankroll) ─────────────────────────

def size_position(divergence: float, bankroll: float, kalshi_price: float) -> tuple[int, float]:
    """
    Returns (contracts, dollar_cost) using quarter-Kelly capped at 5% of bankroll.
    Each Kalshi contract pays $1 on YES win.
    """
    if kalshi_price <= 0 or kalshi_price >= 1:
        return 0, 0.0
    # f* = (p_true - p_market) / (1 - p_market) using divergence as p_true - p_market
    full_kelly = abs(divergence) / (1.0 - kalshi_price)
    quarter_kelly = full_kelly * 0.25
    capped_fraction = min(quarter_kelly, 0.05)  # never more than 5% of bankroll
    dollar_cost = capped_fraction * bankroll
    contracts = max(1, int(dollar_cost / kalshi_price))
    return contracts, contracts * kalshi_price


# ── Signal command ────────────────────────────────────────────────────────────

def cmd_signal():
    """Find today's cross-platform signals and log them as paper trades."""
    data = load_journal()
    existing_tickers = {t["kalshi_ticker"] for t in data["trades"] if t["status"] == "open"}

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Running paper signal capture...")
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=PAPER_DAYS_LOOKBACK)).timestamp())

    # 1. Pull top Polymarket traders
    leaderboard = fetch_poly_leaderboard(max_traders=PAPER_TRADERS)
    real = [t for t in leaderboard
            if (t.get("pnl") or 0) >= 5000 and (t.get("vol") or 0) >= 50000]
    wallets = [t["proxyWallet"] for t in real if t.get("proxyWallet")]
    print(f"  {len(wallets)} qualified traders")

    # 2. Fetch their trade histories
    trades_by_wallet = {}
    for i, wallet in enumerate(wallets):
        trades = fetch_poly_trades(wallet, since_ts)
        trades_by_wallet[wallet] = trades
        time.sleep(REQUEST_DELAY)
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(wallets)}] traders processed...")

    # 3. Compute Polymarket consensus
    poly_signals = compute_poly_consensus(trades_by_wallet)
    print(f"  {len(poly_signals)} Polymarket consensus signals")

    # 4. Match to Kalshi markets
    print("  Fetching live Kalshi markets...")
    kalshi_markets = fetch_kalshi_markets(limit=500)

    cross_signals = build_cross_signals(
        poly_signals, kalshi_markets,
        min_divergence=PAPER_MIN_DIVERGENCE,
        min_traders=PAPER_MIN_TRADERS,
    )

    # Filter by match quality
    cross_signals = [s for s in cross_signals if s.kalshi_match_score >= PAPER_MIN_MATCH_SCORE]

    # 5. Log top N as paper trades
    new_trades = []
    for s in cross_signals[:PAPER_TOP_SIGNALS]:
        if s.kalshi_ticker in existing_tickers:
            continue  # don't double-log if already tracking this market

        kalshi_price = (s.kalshi_yes_bid + s.kalshi_yes_ask) / 2
        # If trading NO, our "entry price" is 1 - YES mid
        entry_price = kalshi_price if s.side == "yes" else (1.0 - kalshi_price)
        # Use the ask side as our limit (we'd be paying the market to fill)
        fill_price = s.kalshi_yes_ask if s.side == "yes" else (1.0 - s.kalshi_yes_bid)

        contracts, cost = size_position(s.divergence, data["bankroll"], fill_price)
        if contracts < 1 or cost > data["bankroll"]:
            continue

        trade = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "kalshi_ticker": s.kalshi_ticker,
            "kalshi_title": s.kalshi_title,
            "poly_title": s.poly_title,
            "poly_slug": s.poly_slug,
            "poly_outcome": s.poly_outcome,
            "side": s.side,
            "contracts": contracts,
            "entry_price": round(fill_price, 3),
            "cost": round(cost, 2),
            "poly_price_at_entry": s.poly_current_price or s.poly_avg_price,
            "kalshi_yes_mid_at_entry": round(kalshi_price, 3),
            "divergence_at_entry": round(s.divergence, 4),
            "poly_trader_count": s.poly_trader_count,
            "match_score": round(s.kalshi_match_score, 3),
            "status": "open",
            "resolved_yes": None,
            "pnl": None,
            "settled_at": None,
        }
        new_trades.append(trade)
        existing_tickers.add(s.kalshi_ticker)

        print(f"  LOGGED: {s.kalshi_ticker}  {s.side.upper()}  "
              f"{contracts}x @ ${fill_price:.3f}  cost=${cost:.2f}  "
              f"(div={s.divergence:+.1%}, match={s.kalshi_match_score:.2f})")

    data["trades"].extend(new_trades)
    # Reserve the cost of new trades from bankroll
    reserved = sum(t["cost"] for t in new_trades)
    data["bankroll"] -= reserved
    save_journal(data)

    if new_trades:
        print(f"\n  {len(new_trades)} new paper trades logged.")
        print(f"  Bankroll after entries: ${data['bankroll']:.2f}")
    else:
        print("  No qualifying signals today.")


# ── Settle command ────────────────────────────────────────────────────────────

def cmd_settle():
    """Mark resolved Kalshi markets as settled and credit/debit bankroll."""
    data = load_journal()
    open_trades = [t for t in data["trades"] if t["status"] == "open"]

    if not open_trades:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] No open paper trades to settle.")
        return

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Checking {len(open_trades)} open trades...")
    settled_count = 0

    for trade in open_trades:
        ticker = trade["kalshi_ticker"]
        market = _get(f"{KALSHI_API}/markets/{ticker}").get("market", {})
        status = market.get("status", "")

        if status not in ("settled", "determined", "finalized"):
            continue

        settlement_str = market.get("settlement_value_dollars", "")
        try:
            settlement = float(settlement_str)
        except (ValueError, TypeError):
            continue

        resolved_yes = settlement >= 0.99
        side = trade["side"]
        contracts = trade["contracts"]
        entry_price = trade["entry_price"]

        # If we bought YES and YES resolved: each contract pays $1
        # If we bought NO and YES did NOT resolve: each contract pays $1
        # Otherwise: we lose what we paid
        if side == "yes":
            won = resolved_yes
        else:
            won = not resolved_yes

        # PnL = (payout - cost) per contract
        gross = contracts * 1.0 if won else 0.0
        cost = trade["cost"]
        pnl = round(gross - cost, 2)

        trade["status"] = "settled"
        trade["resolved_yes"] = resolved_yes
        trade["pnl"] = pnl
        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
        # Return cost + pnl to bankroll
        data["bankroll"] += cost + pnl
        settled_count += 1

        result = "WIN" if won else "LOSS"
        print(f"  {result}  {ticker}  resolved_yes={resolved_yes}  pnl=${pnl:+.2f}")
        time.sleep(0.1)

    save_journal(data)
    print(f"\n  Settled {settled_count} trade(s). Bankroll: ${data['bankroll']:.2f}")


# ── Status command ────────────────────────────────────────────────────────────

def cmd_status(json_output: bool = False):
    data = load_journal()
    trades = data["trades"]
    settled = [t for t in trades if t["status"] == "settled"]
    open_t = [t for t in trades if t["status"] == "open"]
    wins = [t for t in settled if t["pnl"] is not None and t["pnl"] > 0]
    losses = [t for t in settled if t["pnl"] is not None and t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in settled if t["pnl"] is not None)
    win_rate = len(wins) / len(settled) if settled else 0.0

    if json_output:
        print(json.dumps({
            "started": data.get("started"),
            "initial_bankroll": data["initial_bankroll"],
            "bankroll": data["bankroll"],
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_pnl / data["initial_bankroll"] * 100, 2),
            "open_trades": len(open_t),
            "settled_trades": len(settled),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate * 100, 1),
            "trades": trades,
        }, indent=2, default=str))
        return

    print(f"\n{'='*65}")
    print(f"  PAPER TRADING SCORECARD — Cross-Platform Polymarket→Kalshi")
    print(f"{'='*65}")
    print(f"  Started:           {data.get('started', '?')[:19]}")
    print(f"  Initial bankroll:  ${data['initial_bankroll']:.2f}")
    print(f"  Current bankroll:  ${data['bankroll']:.2f}")
    print(f"  Total P&L:         ${total_pnl:+.2f}  ({total_pnl/data['initial_bankroll']*100:+.1f}%)")
    print(f"  Settled trades:    {len(settled)}  ({len(wins)} wins, {len(losses)} losses, win rate {win_rate*100:.0f}%)")
    print(f"  Open trades:       {len(open_t)}")

    if settled:
        print(f"\n  Settled trades:")
        for t in settled[-12:]:
            tag = "✓" if t["pnl"] and t["pnl"] > 0 else "✗"
            print(f"    {tag} {t['kalshi_ticker']:<38} {t['side'].upper():<4} "
                  f"{t['contracts']}x @ ${t['entry_price']:.3f}  pnl=${t['pnl']:+.2f}")

    if open_t:
        print(f"\n  Open paper trades:")
        for t in open_t:
            print(f"    → {t['kalshi_ticker']:<38} {t['side'].upper():<4} "
                  f"{t['contracts']}x @ ${t['entry_price']:.3f}  "
                  f"(div {t['divergence_at_entry']:+.1%})")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def cmd_cancel(reason: str = "matcher-bug"):
    """
    Void all currently open paper trades, refunding their cost to bankroll.
    Use after fixing matcher bugs that caused bad entries.
    Settled trades are untouched.
    """
    data = load_journal()
    open_trades = [t for t in data["trades"] if t["status"] == "open"]

    if not open_trades:
        print("No open trades to cancel.")
        return

    refunded = 0.0
    for t in open_trades:
        t["status"] = "cancelled"
        t["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        t["cancel_reason"] = reason
        t["pnl"] = 0.0
        refunded += t["cost"]
        data["bankroll"] += t["cost"]
        print(f"  CANCELLED: {t['kalshi_ticker']:<40} refund=${t['cost']:.2f}")

    save_journal(data)
    print(f"\n{len(open_trades)} trades cancelled. ${refunded:.2f} refunded.")
    print(f"Bankroll now: ${data['bankroll']:.2f}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    json_flag = "--json" in sys.argv
    if cmd == "signal":
        cmd_signal()
    elif cmd == "settle":
        cmd_settle()
    elif cmd == "status":
        cmd_status(json_output=json_flag)
    elif cmd == "cancel":
        reason = sys.argv[2] if len(sys.argv) > 2 else "manual"
        cmd_cancel(reason=reason)
    else:
        print("Usage: paper_cross.py {signal|settle|status [--json]|cancel [reason]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
