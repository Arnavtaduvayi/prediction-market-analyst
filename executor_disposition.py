"""
executor_disposition.py — Bot B Executor

Reads disposition_signals.json and logs paper trades to its own journal
(paper_disposition_trades.json). Independent bankroll, independent stats.

Sizing: Quarter-Kelly with a tighter per-trade cap (3%) because edges are
small (1-4%) and we want to spread bets across many trades to capture the
statistical bias over time.

Hold-to-settlement: no exit triggers. The favorite-longshot bias plays out
at resolution, so exiting early forfeits the edge.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_FILE = Path(__file__).parent / "paper_disposition_trades.json"
SIGNALS_FILE = Path(__file__).parent / "data" / "disposition_signals.json"

INITIAL_BANKROLL = 75.0

# Tighter sizing — disposition edges are small but consistent
KELLY_CAP = 0.15
QUARTER_KELLY = 0.25
PER_TRADE_CAP_PCT = 0.03        # 3% max per trade (vs 10% for whale-copy)
MAX_OPEN_POSITIONS = 12         # more diversification since edges are small
MIN_TRADE_COST = 0.30


def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return {
        "strategy": "disposition",
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(data: dict):
    JOURNAL_FILE.write_text(json.dumps(data, indent=2, default=str))


def kelly_size(p_win: float, fill_price: float) -> float:
    if fill_price <= 0 or fill_price >= 1:
        return 0.0
    b = (1.0 / fill_price) - 1.0
    q = 1.0 - p_win
    f_star = (p_win * b - q) / b
    if f_star <= 0:
        return 0.0
    return min(f_star * QUARTER_KELLY, KELLY_CAP, PER_TRADE_CAP_PCT)


def run():
    if not SIGNALS_FILE.exists():
        print("No disposition signals — run disposition.py first.")
        return

    data = load_journal()
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    available_slots = max(0, MAX_OPEN_POSITIONS - open_count)
    if available_slots == 0:
        print(f"[disposition] Already {open_count} open positions (max {MAX_OPEN_POSITIONS}).")
        return

    existing_tickers = {t["kalshi_ticker"] for t in data["trades"] if t["status"] == "open"}
    signals = json.loads(SIGNALS_FILE.read_text()).get("signals", [])

    print(f"[disposition] {len(signals)} candidate signals, {available_slots} slots available")

    new_trades = []
    for s in signals:
        if len(new_trades) >= available_slots:
            break
        if s["ticker"] in existing_tickers:
            continue

        kelly_frac = kelly_size(s["true_prob"], s["fill_price"])
        if kelly_frac <= 0:
            continue

        dollar_cost = kelly_frac * data["bankroll"]
        contracts = max(1, int(dollar_cost / s["fill_price"]))
        cost = round(contracts * s["fill_price"], 2)
        if cost > data["bankroll"] or cost < MIN_TRADE_COST:
            continue

        trade = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "strategy": "disposition",
            "kalshi_ticker": s["ticker"],
            "kalshi_title": s["title"],
            "type": s["type"],
            "side": s["side"],
            "contracts": contracts,
            "entry_price": s["fill_price"],
            "cost": cost,
            "yes_mid_at_entry": s["yes_mid"],
            "true_prob_estimate": s["true_prob"],
            "edge_at_entry": s["edge"],
            "kelly_fraction": round(kelly_frac, 4),
            "hours_left_at_entry": s["hours_left"],
            "status": "open",
            "resolved_yes": None,
            "pnl": None,
            "settled_at": None,
            "exit_reason": None,
            # Disposition trades hold to settlement
            "hold_to_settlement": True,
        }
        new_trades.append(trade)
        data["bankroll"] -= cost
        existing_tickers.add(s["ticker"])

        kind = "LONGSHOT" if s["type"] == "longshot_sell" else "FAVORITE"
        print(f"  [disposition] {kind} {s['ticker']:<42} {s['side'].upper()}  "
              f"{contracts}x @ ${s['fill_price']:.3f}  cost=${cost:.2f}  "
              f"edge={s['edge']:+.3f}")

    data["trades"].extend(new_trades)
    save_journal(data)
    print(f"\n  [disposition] {len(new_trades)} new trades. Bankroll: ${data['bankroll']:.2f}")
    return {"new_trades": new_trades}


if __name__ == "__main__":
    run()
