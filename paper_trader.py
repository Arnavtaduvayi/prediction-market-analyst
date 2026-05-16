"""
Paper Trader — runs the weather strategy against live Kalshi markets
but logs signals without placing real orders.

Tracks hypothetical P&L against actual Kalshi settlement values so you
can validate the strategy for 1-2 weeks before going live.

Usage:
  # Morning run — log today's signals (run this every day ~7am)
  python3 paper_trader.py signal

  # Evening check — mark resolved markets and update P&L
  python3 paper_trader.py settle

  # View current paper P&L
  python3 paper_trader.py status
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from strategy import MIN_EDGE, MIN_MARKET_PRICE, MAX_MARKET_PRICE, find_signals, kelly_size

PAPER_FILE = Path(__file__).parent / "paper_trades.json"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
INITIAL_BANKROLL = 51.0


def load_paper_trades() -> dict:
    if PAPER_FILE.exists():
        return json.loads(PAPER_FILE.read_text())
    return {"bankroll": INITIAL_BANKROLL, "trades": []}


def save_paper_trades(data: dict):
    PAPER_FILE.write_text(json.dumps(data, indent=2, default=str))


def cmd_signal(bankroll_override: float = None):
    """Find and log today's signals as paper trades."""
    data = load_paper_trades()
    bankroll = bankroll_override or data["bankroll"]

    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    signals = find_signals(target_date=tomorrow)

    if not signals:
        print("No signals today — no paper trades logged.")
        return

    new_trades = []
    for s in signals:
        bet = s.dollar_amount(bankroll)
        contracts = s.contracts_for_bankroll(bankroll)
        lp = s.limit_price()

        trade = {
            "date_logged": datetime.now(timezone.utc).isoformat(),
            "ticker": s.ticker,
            "city": s.city,
            "threshold": s.threshold,
            "target_date": s.target_date.isoformat(),
            "side": s.side,
            "market_price": s.market_price,
            "model_prob": s.model_prob,
            "edge": round(s.edge, 4),
            "limit_price": round(lp, 3),
            "contracts": contracts,
            "cost": round(bet, 2),
            "status": "open",
            "settled_yes": None,
            "pnl": None,
        }
        new_trades.append(trade)
        print(f"  PAPER: {s.ticker}  {s.side.upper()}  {contracts} contracts @ ${lp:.3f}  "
              f"(edge {s.edge:+.1%}  model={s.model_prob:.1%}  mkt={s.market_price:.1%})")

    data["trades"].extend(new_trades)
    save_paper_trades(data)
    total = sum(t["cost"] for t in new_trades)
    print(f"\n{len(new_trades)} paper trades logged. Total exposure: ${total:.2f}")


def cmd_settle():
    """Check each open trade and mark as settled when market resolves."""
    data = load_paper_trades()
    open_trades = [t for t in data["trades"] if t["status"] == "open"]

    if not open_trades:
        print("No open paper trades to settle.")
        return

    print(f"Checking {len(open_trades)} open trades against Kalshi settlements...")

    for trade in open_trades:
        ticker = trade["ticker"]
        try:
            r = requests.get(f"{BASE_URL}/markets/{ticker}", timeout=10)
            r.raise_for_status()
            market = r.json().get("market", {})
        except requests.RequestException:
            continue

        status = market.get("status", "")
        if status not in ("settled", "determined"):
            continue

        settlement_str = market.get("settlement_value_dollars", "")
        try:
            settlement = float(settlement_str)
        except (ValueError, TypeError):
            continue

        resolved_yes = settlement >= 0.99

        side = trade["side"]
        contracts = trade["contracts"]
        lp = trade["limit_price"]

        if side == "yes":
            won = resolved_yes
        else:
            won = not resolved_yes

        pnl = contracts * (1.0 - lp) if won else -trade["cost"]

        trade["status"] = "settled"
        trade["settled_yes"] = resolved_yes
        trade["pnl"] = round(pnl, 2)
        data["bankroll"] += pnl

        result_str = "WIN" if won else "LOSS"
        print(f"  {result_str}  {ticker}  resolved_yes={resolved_yes}  pnl=${pnl:+.2f}")
        time.sleep(0.2)

    save_paper_trades(data)
    print(f"\nPaper bankroll: ${data['bankroll']:.2f}")


def cmd_status():
    """Print current paper trading P&L summary."""
    data = load_paper_trades()
    trades = data["trades"]
    settled = [t for t in trades if t["status"] == "settled"]
    open_t  = [t for t in trades if t["status"] == "open"]

    total_pnl = sum(t["pnl"] for t in settled)
    wins = sum(1 for t in settled if t["pnl"] and t["pnl"] > 0)
    win_rate = wins / len(settled) if settled else 0

    print(f"\n{'='*55}")
    print(f"  PAPER TRADING STATUS")
    print(f"{'='*55}")
    print(f"  Initial bankroll:  ${INITIAL_BANKROLL:.2f}")
    print(f"  Current bankroll:  ${data['bankroll']:.2f}")
    print(f"  Total P&L:         ${total_pnl:+.2f}  ({total_pnl/INITIAL_BANKROLL*100:+.1f}%)")
    print(f"  Settled trades:    {len(settled)}  ({win_rate*100:.0f}% win rate)")
    print(f"  Open trades:       {len(open_t)}")

    if settled:
        print(f"\n  Recent settled trades:")
        for t in settled[-10:]:
            result = "✓" if t["pnl"] > 0 else "✗"
            print(f"    {result} {t['ticker']:<40} pnl=${t['pnl']:+.2f}")

    if open_t:
        print(f"\n  Open paper trades:")
        for t in open_t:
            print(f"    → {t['ticker']:<40} {t['side'].upper()}  {t['contracts']} contracts @ ${t['limit_price']:.3f}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "signal":
        cmd_signal()
    elif cmd == "settle":
        cmd_settle()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: {cmd}. Use: signal | settle | status")
        sys.exit(1)


if __name__ == "__main__":
    main()
