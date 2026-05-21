"""
Live Trader — places real limit orders on Kalshi for weather signals.

Requires:
  - .env with KALSHI_API_KEY_ID=<your-key-id>
  - keys/kalshi_private.pem (your downloaded private key file)

Usage:
  python3 live_trader.py --bankroll 51 --dry-run   # preview without placing orders
  python3 live_trader.py --bankroll 51              # place real orders
  python3 live_trader.py --bankroll 51 --city NYC  # single city only
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from strategy import find_signals, print_signals

LOG_FILE = Path(__file__).parent / "live_trades.json"


def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(trades: list):
    LOG_FILE.write_text(json.dumps(trades, indent=2, default=str))


def run(bankroll: float, dry_run: bool = True, city_filter: str = None):
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)

    print(f"{'[DRY RUN] ' if dry_run else ''}Finding weather signals for {tomorrow}...")
    signals = find_signals(target_date=tomorrow)

    if city_filter:
        signals = [s for s in signals if s.city == city_filter.upper()]

    if not signals:
        print("No signals today.")
        return

    print_signals(signals, bankroll)

    if dry_run:
        print("\nDry run — no orders placed. Remove --dry-run to go live.")
        return

    # Confirm before placing real orders
    total = sum(s.dollar_amount(bankroll) for s in signals)
    print(f"\nAbout to place {len(signals)} real limit orders totaling ${total:.2f}.")
    confirm = input("Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    client = KalshiClient()
    actual_balance = client.balance()
    print(f"\nKalshi balance: ${actual_balance:.2f}")

    if actual_balance < total:
        print(f"Insufficient balance (${actual_balance:.2f}) for planned trades (${total:.2f}).")
        print("Scaling down to available balance...")
        bankroll = actual_balance

    log = load_log()
    placed = 0

    for s in signals:
        contracts = s.contracts_for_bankroll(bankroll)
        lp = s.limit_price()
        if contracts < 1 or lp <= 0:
            continue

        print(f"\nPlacing: {s.ticker}  {s.side.upper()}  {contracts}x @ ${lp:.3f}  "
              f"(edge {s.edge:+.1%})")

        try:
            order = client.place_limit_order(
                ticker=s.ticker,
                side=s.side,
                count=contracts,
                limit_price=lp,
            )
            order_id = order.get("order", {}).get("order_id", "unknown")
            print(f"  ✓ Order placed: {order_id}")

            log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "order_id": order_id,
                "ticker": s.ticker,
                "city": s.city,
                "threshold": s.threshold,
                "target_date": str(s.target_date),
                "side": s.side,
                "contracts": contracts,
                "limit_price": lp,
                "cost": round(contracts * lp, 2),
                "model_prob": s.model_prob,
                "market_price": s.market_price,
                "edge": round(s.edge, 4),
                "status": "open",
            })
            placed += 1

        except Exception as e:
            print(f"  ✗ Order failed: {e}")

    save_log(log)
    print(f"\n{placed}/{len(signals)} orders placed. Log saved to live_trades.json")


def main():
    parser = argparse.ArgumentParser(description="Live weather signal trader for Kalshi")
    parser.add_argument("--bankroll", type=float, default=51.0,
                        help="Trading bankroll in dollars (default: 51)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview signals without placing orders")
    parser.add_argument("--city", type=str, default=None,
                        help="Only trade one city (e.g. NYC)")
    args = parser.parse_args()

    run(bankroll=args.bankroll, dry_run=args.dry_run, city_filter=args.city)


if __name__ == "__main__":
    main()
