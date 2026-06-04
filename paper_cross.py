"""
paper_cross.py — Orchestrator

Coordinates the 4-agent pipeline (targets → scanner → brain → executor) and
the exit monitor. Same file name as the v1 paper trader so the GitHub Actions
workflows don't need renaming, but the internals are now multi-agent.

Commands:
  python3 paper_cross.py signal    # full pipeline: scan + brain + execute
  python3 paper_cross.py exit      # exit_monitor: check triggers, settle resolved
  python3 paper_cross.py settle    # alias for exit (back-compat)
  python3 paper_cross.py targets   # refresh whale target list
  python3 paper_cross.py status    # scorecard
  python3 paper_cross.py cancel <reason>
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

WHALE_JOURNAL = Path(__file__).parent / "paper_cross_trades.json"
DISPOSITION_JOURNAL = Path(__file__).parent / "paper_disposition_trades.json"
CALENDAR_JOURNAL = Path(__file__).parent / "paper_calendar_trades.json"
SPOT_JOURNAL = Path(__file__).parent / "paper_spot_trades.json"
FLOW_JOURNAL = Path(__file__).parent / "paper_flow_trades.json"

ALL_JOURNALS = [
    (WHALE_JOURNAL, "whale-copy"),
    (DISPOSITION_JOURNAL, "disposition"),
    (CALENDAR_JOURNAL, "calendar-arb"),
    (SPOT_JOURNAL, "spot-conv"),
    (FLOW_JOURNAL, "flow-mom"),
]

TARGETS_FILE = Path(__file__).parent / "data" / "targets.json"
INITIAL_BANKROLL = 75.0


def load_journal(path: Path = WHALE_JOURNAL) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "started": datetime.now(timezone.utc).isoformat(),
        "initial_bankroll": INITIAL_BANKROLL,
        "bankroll": INITIAL_BANKROLL,
        "trades": [],
    }


def save_journal(data: dict, path: Path = WHALE_JOURNAL):
    path.write_text(json.dumps(data, indent=2, default=str))


def journal_stats(path: Path, label: str) -> dict:
    """Compute summary stats for one journal."""
    data = load_journal(path)
    trades = data.get("trades", [])
    settled = [t for t in trades if t.get("status") in ("settled", "exited")]
    open_t = [t for t in trades if t.get("status") == "open"]
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
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100, 1),
        "recent_settled": settled[-5:],
        "open_trades": open_t,
    }


def cmd_signal():
    """Full signal pipeline: scanner → 5 bots in sequence."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] === 5-BOT SIGNAL PIPELINE ===\n")

    if not TARGETS_FILE.exists():
        print("No whale target list — building one (this takes a few minutes)...")
        import targets as targets_module
        sys.argv = ["targets.py", "--candidates", "100"]
        targets_module.main()
        print()

    import scanner
    scanner.scan()
    print()

    # Bot A: Whale-copy
    print("─── BOT A: WHALE-COPY ───")
    import brain; brain.run(); print()
    import executor; executor.run(); print()

    # Bot B: Disposition (longshot/favorite bias)
    print("─── BOT B: DISPOSITION ───")
    import disposition; disposition.run(); print()
    import executor_disposition; executor_disposition.run(); print()

    # Bot C: Strike monotonicity arbitrage
    print("─── BOT C: CALENDAR ARB ───")
    import bot_calendar; bot_calendar.run(); print()

    # Bot D: BTC spot convergence
    print("─── BOT D: SPOT CONVERGENCE ───")
    import bot_spot; bot_spot.run(); print()

    # Bot E: Order flow momentum
    print("─── BOT E: FLOW MOMENTUM ───")
    import bot_flow; bot_flow.run()


def cmd_exit():
    """Run exit monitor over all open positions."""
    import exit_monitor
    exit_monitor.run()


def cmd_targets():
    """Refresh whale target list."""
    import targets as targets_module
    sys.argv = ["targets.py"]
    targets_module.main()


def _cancel_journal(path: Path, reason: str, label: str):
    data = load_journal(path)
    open_trades = [t for t in data["trades"] if t.get("status") == "open"]
    if not open_trades:
        print(f"[{label}] No open trades to cancel.")
        return
    refunded = 0.0
    for t in open_trades:
        t["status"] = "cancelled"
        t["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        t["cancel_reason"] = reason
        t["pnl"] = 0.0
        refunded += t["cost"]
        data["bankroll"] += t["cost"]
        print(f"  [{label}] CANCELLED: {t['kalshi_ticker']:<42} refund=${t['cost']:.2f}")
    save_journal(data, path)
    print(f"  [{label}] {len(open_trades)} cancelled, ${refunded:.2f} refunded. Bankroll: ${data['bankroll']:.2f}")


def cmd_cancel(reason: str = "manual"):
    """Cancel open trades in ALL bot journals."""
    for path, label in ALL_JOURNALS:
        _cancel_journal(path, reason, label)


def cmd_status(json_output: bool = False):
    stats_by_label = {label: journal_stats(path, label) for path, label in ALL_JOURNALS}
    whale = stats_by_label["whale-copy"]
    disp  = stats_by_label["disposition"]
    cal   = stats_by_label["calendar-arb"]
    spot  = stats_by_label["spot-conv"]
    flow  = stats_by_label["flow-mom"]

    if json_output:
        combined = {
            "total_pnl": round(sum(s["total_pnl"] for s in stats_by_label.values()), 2),
            "settled": sum(s["settled"] for s in stats_by_label.values()),
            "open": sum(s["open"] for s in stats_by_label.values()),
            "wins": sum(s["wins"] for s in stats_by_label.values()),
            "losses": sum(s["losses"] for s in stats_by_label.values()),
        }
        out = {"combined": combined, **stats_by_label}
        print(json.dumps(out, indent=2, default=str))
        return

    bots = [("A", "WHALE-COPY", whale, "Polymarket whales"),
            ("B", "DISPOSITION", disp, "Longshot/favorite"),
            ("C", "CALENDAR ARB", cal, "Strike monotonicity"),
            ("D", "SPOT CONV", spot, "BTC fair-value model"),
            ("E", "FLOW MOM", flow, "Order flow imbalance")]

    bar = "═" * 95
    print(f"\n{bar}")
    print(f"  PAPER TRADING — 5-BOT SCORECARD")
    print(bar)
    header = "  " + f"{'Metric':<18}"
    for letter, name, _, _ in bots:
        header += f"{f'Bot {letter}':>15}"
    print(header)
    print("  " + f"{'':<18}" + "".join(f"{name:>15}" for _, name, _, _ in bots))
    print("  " + f"{'─'*18}" + "".join(f"{'─'*15}" for _ in bots))

    def row(label, fmt_fn):
        line = "  " + f"{label:<18}"
        for _, _, s, _ in bots:
            line += f"{fmt_fn(s):>15}"
        print(line)

    row("Bankroll",      lambda s: f"${s['bankroll']:.2f}")
    row("P&L",           lambda s: f"${s['total_pnl']:+.2f}")
    row("Return %",      lambda s: f"{s['return_pct']:+.2f}%")
    row("Settled",       lambda s: str(s['settled']))
    row("Win rate",      lambda s: f"{s['win_rate']:.0f}%" if s['settled'] else "—")
    row("W / L",         lambda s: f"{s['wins']}W/{s['losses']}L")
    row("Open",          lambda s: str(s['open']))
    print(bar)

    total_pnl = sum(s["total_pnl"] for _, _, s, _ in bots)
    total_bank = sum(s["bankroll"] for _, _, s, _ in bots)
    total_init = sum(s["initial_bankroll"] for _, _, s, _ in bots)
    total_settled = sum(s["settled"] for _, _, s, _ in bots)
    total_open = sum(s["open"] for _, _, s, _ in bots)
    print(f"  COMBINED: bankroll ${total_bank:.2f}  P&L ${total_pnl:+.2f}  "
          f"({total_pnl/total_init*100:+.2f}%)  "
          f"settled={total_settled}  open={total_open}")
    print(f"{bar}\n")

    # Detail: recent trades from each
    def _show_recent(stats, name):
        if not stats["recent_settled"] and not stats["open_trades"]:
            return
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
                if t.get("thesis_target_price"):
                    extra = f"target=${t['thesis_target_price']:.3f}"
                elif t.get("type"):
                    extra = t["type"]
                print(f"    → {t['kalshi_ticker']:<40} {t.get('side','?').upper():<3} "
                      f"{t.get('contracts',0)}x @ ${t.get('entry_price',0):.3f}  {extra}")
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
    elif cmd == "targets":
        cmd_targets()
    elif cmd == "status":
        cmd_status(json_output=json_flag)
    elif cmd == "cancel":
        reason = sys.argv[2] if len(sys.argv) > 2 else "manual"
        cmd_cancel(reason=reason)
    else:
        print("Usage: paper_cross.py {signal|exit|settle|targets|status|cancel <reason>}")
        sys.exit(1)


if __name__ == "__main__":
    main()
