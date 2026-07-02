"""
paper_cross.py — Orchestrator (v5 roster)

Coordinates the 4-bot pipeline and the exit monitor. Same file name as the v1
paper trader so the GitHub Actions workflows don't need renaming.

The v5 roster keeps only strategies with either a structural edge or a
measured positive edge on our own book (see CHANGELOG for the data):

  S seller  — sell overpriced longshots, maker entries, hold to settlement
  T theta   — late-favorite convergence, maker entries (execution-alpha test)
  C arb     — intra-Kalshi Dutch-book + ladder (risk-free, mostly idle)
  X xvenue  — Kalshi<->Polymarket verified pairs: hard arb + fair-value quoting

Commands:
  python3 paper_cross.py signal    # full pipeline: scan + all four bots
  python3 paper_cross.py exit      # exit_monitor: fills, triggers, settlements
  python3 paper_cross.py settle    # alias for exit (back-compat)
  python3 paper_cross.py status    # scorecard
  python3 paper_cross.py cancel <reason>
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SELLER_JOURNAL = Path(__file__).parent / "paper_seller_trades.json"
THETA_JOURNAL = Path(__file__).parent / "paper_theta_trades.json"
ARB_JOURNAL = Path(__file__).parent / "paper_arb_trades.json"
XVENUE_JOURNAL = Path(__file__).parent / "paper_xvenue_trades.json"

# (letter, label, journal, strategy one-liner) — drives signal order + scorecard.
BOTS = [
    ("S", "seller", SELLER_JOURNAL, "Sell longshots (maker)"),
    ("T", "theta", THETA_JOURNAL, "Late-favorite convergence (maker)"),
    ("C", "arb", ARB_JOURNAL, "Dutch-book + ladder (risk-free)"),
    ("X", "xvenue", XVENUE_JOURNAL, "Kalshi<->Polymarket pairs"),
]

ALL_JOURNALS = [(path, label) for _, label, path, _ in BOTS]

INITIAL_BANKROLL = 75.0


def load_journal(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, default=str))


def journal_stats(path: Path, label: str) -> dict:
    """Compute summary stats for one journal."""
    data = load_journal(path)
    trades = data.get("trades", [])
    settled = [t for t in trades if t.get("status") in ("settled", "exited")]
    open_t = [t for t in trades if t.get("status") == "open"]
    resting = [t for t in trades if t.get("status") == "resting"]
    wins = [t for t in settled if t.get("pnl") is not None and t["pnl"] > 0]
    losses = [t for t in settled if t.get("pnl") is not None and t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in settled if t.get("pnl") is not None)
    win_rate = len(wins) / len(settled) if settled else 0.0
    return {
        "label": label,
        "initial_bankroll": data.get("initial_bankroll", INITIAL_BANKROLL),
        "bankroll": data.get("bankroll", INITIAL_BANKROLL),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round(total_pnl / data.get("initial_bankroll", INITIAL_BANKROLL) * 100, 2),
        "settled": len(settled),
        "open": len(open_t),
        "resting": len(resting),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100, 1),
        "recent_settled": settled[-5:],
        "open_trades": open_t,
        "resting_orders": resting,
    }


def cmd_signal():
    """Full signal pipeline: scanner → 4 bots in sequence."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] === 4-BOT SIGNAL PIPELINE ===\n")

    import scanner
    scanner.scan()
    print()

    # Bot S: Longshot seller (maker) — queue-driven
    print("─── BOT S: SELLER ───")
    import bot_seller; bot_seller.run(); print()

    # Bot T: Late-favorite convergence (maker) — queue-driven
    print("─── BOT T: THETA ───")
    import bot_theta; bot_theta.run(); print()

    # Bot C: Intra-Kalshi arbitrage — own discovery
    print("─── BOT C: ARB ───")
    import bot_arb; bot_arb.run(); print()

    # Bot X: Cross-venue Kalshi<->Polymarket — own discovery via pair map
    print("─── BOT X: XVENUE ───")
    import bot_xvenue; bot_xvenue.run()


def cmd_exit():
    """Run exit monitor over all open positions + resting quotes."""
    import exit_monitor
    exit_monitor.run()


def _cancel_journal(path: Path, reason: str, label: str):
    data = load_journal(path)
    live = [t for t in data["trades"] if t.get("status") in ("open", "resting")]
    if not live:
        print(f"[{label}] No open trades or resting quotes to cancel.")
        return
    refunded = 0.0
    for t in live:
        t["status"] = "cancelled"
        t["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        t["cancel_reason"] = reason
        t["pnl"] = 0.0
        refunded += t["cost"]
        data["bankroll"] += t["cost"]
        print(f"  [{label}] CANCELLED: {t['kalshi_ticker']:<42} refund=${t['cost']:.2f}")
    save_journal(data, path)
    print(f"  [{label}] {len(live)} cancelled, ${refunded:.2f} refunded. Bankroll: ${data['bankroll']:.2f}")


def cmd_cancel(reason: str = "manual"):
    """Cancel open trades + resting quotes in ALL bot journals."""
    for path, label in ALL_JOURNALS:
        _cancel_journal(path, reason, label)


def cmd_status(json_output: bool = False):
    stats_by_label = {label: journal_stats(path, label) for path, label in ALL_JOURNALS}

    if json_output:
        combined = {
            "total_pnl": round(sum(s["total_pnl"] for s in stats_by_label.values()), 2),
            "settled": sum(s["settled"] for s in stats_by_label.values()),
            "open": sum(s["open"] for s in stats_by_label.values()),
            "resting": sum(s["resting"] for s in stats_by_label.values()),
            "wins": sum(s["wins"] for s in stats_by_label.values()),
            "losses": sum(s["losses"] for s in stats_by_label.values()),
        }
        out = {"combined": combined, **stats_by_label}
        print(json.dumps(out, indent=2, default=str))
        return

    hdr = (f"  {'Bot':<12}{'Strategy':<35}{'Bankroll':>10}{'P&L':>9}"
           f"{'Ret%':>8}{'Setl':>6}{'Win%':>6}{'W/L':>9}{'Open':>6}{'Rest':>6}")
    bar = "═" * len(hdr)
    print(f"\n{bar}")
    print(f"  PAPER TRADING — {len(BOTS)}-BOT SCORECARD (v5)")
    print(bar)
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for letter, label, _, desc in BOTS:
        s = stats_by_label[label]
        win = f"{s['win_rate']:.0f}%" if s["settled"] else "—"
        wl = f"{s['wins']}/{s['losses']}"
        print(f"  {letter + ' ' + label:<12}{desc[:34]:<35}"
              f"{'$' + format(s['bankroll'], '.2f'):>10}{format(s['total_pnl'], '+.2f'):>9}"
              f"{format(s['return_pct'], '+.1f'):>7}%{s['settled']:>6}{win:>6}{wl:>9}"
              f"{s['open']:>6}{s['resting']:>6}")
    print(bar)

    total_pnl = sum(s["total_pnl"] for s in stats_by_label.values())
    total_bank = sum(s["bankroll"] for s in stats_by_label.values())
    total_init = sum(s["initial_bankroll"] for s in stats_by_label.values())
    total_settled = sum(s["settled"] for s in stats_by_label.values())
    total_open = sum(s["open"] for s in stats_by_label.values())
    total_resting = sum(s["resting"] for s in stats_by_label.values())
    print(f"  COMBINED: bankroll ${total_bank:.2f}  P&L ${total_pnl:+.2f}  "
          f"({total_pnl / total_init * 100:+.2f}%)  "
          f"settled={total_settled}  open={total_open}  resting={total_resting}")
    print(f"{bar}\n")

    def _show_recent(stats, name):
        if not (stats["recent_settled"] or stats["open_trades"] or stats["resting_orders"]):
            return
        if stats["recent_settled"]:
            print(f"  [{name}] recent settled:")
            for t in stats["recent_settled"]:
                tag = "✓" if (t.get("pnl") or 0) > 0 else "✗"
                reason = t.get("exit_reason", "?")
                print(f"    {tag} {t['kalshi_ticker']:<40} {t.get('side','?').upper():<3} "
                      f"{t.get('contracts',0)}x @ ${t.get('entry_price',0):.3f}  "
                      f"pnl=${t.get('pnl',0):+.2f}  [{reason}]")
        if stats["open_trades"]:
            print(f"  [{name}] open:")
            for t in stats["open_trades"]:
                extra = ""
                if t.get("arb_pair"):
                    extra = f"locked=${t.get('locked_profit', 0):.2f}"
                elif t.get("target_yes_mid"):
                    extra = f"target={t['target_yes_mid']:.3f}"
                elif t.get("type"):
                    extra = t["type"]
                print(f"    → {t['kalshi_ticker']:<40} {t.get('side','?').upper():<3} "
                      f"{t.get('contracts',0)}x @ ${t.get('entry_price',0):.3f}  {extra}")
        if stats["resting_orders"]:
            print(f"  [{name}] resting quotes:")
            for t in stats["resting_orders"]:
                print(f"    ◌ {t['kalshi_ticker']:<40} {t.get('side','?').upper():<3} "
                      f"{t.get('contracts',0)}x bid ${t.get('entry_price',0):.3f}")
        print()

    for label, s in stats_by_label.items():
        _show_recent(s, label)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    json_flag = "--json" in sys.argv
    if cmd == "signal":
        cmd_signal()
    elif cmd in ("exit", "settle"):
        cmd_exit()
    elif cmd == "status":
        cmd_status(json_output=json_flag)
    elif cmd == "cancel":
        reason = sys.argv[2] if len(sys.argv) > 2 else "manual"
        cmd_cancel(reason=reason)
    else:
        print("Usage: paper_cross.py {signal|exit|settle|status|cancel <reason>}")
        sys.exit(1)


if __name__ == "__main__":
    main()
