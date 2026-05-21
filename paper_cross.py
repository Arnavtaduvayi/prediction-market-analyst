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

JOURNAL_FILE = Path(__file__).parent / "paper_cross_trades.json"
TARGETS_FILE = Path(__file__).parent / "data" / "targets.json"
INITIAL_BANKROLL = 51.0


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


def cmd_signal():
    """Full signal pipeline: scanner → brain → executor."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] === SIGNAL PIPELINE ===\n")

    # Need a targets list — auto-build if missing
    if not TARGETS_FILE.exists():
        print("No whale target list — building one (this takes a few minutes)...")
        import targets as targets_module
        sys.argv = ["targets.py", "--candidates", "100"]
        targets_module.main()
        print()

    import scanner
    scanner.scan()
    print()

    import brain
    brain.run()
    print()

    import executor
    executor.run()


def cmd_exit():
    """Run exit monitor over all open positions."""
    import exit_monitor
    exit_monitor.run()


def cmd_targets():
    """Refresh whale target list."""
    import targets as targets_module
    sys.argv = ["targets.py"]
    targets_module.main()


def cmd_cancel(reason: str = "manual"):
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
        print(f"  CANCELLED: {t['kalshi_ticker']:<42} refund=${t['cost']:.2f}")
    save_journal(data)
    print(f"\n{len(open_trades)} trades cancelled. ${refunded:.2f} refunded. Bankroll: ${data['bankroll']:.2f}")


def cmd_status(json_output: bool = False):
    data = load_journal()
    trades = data["trades"]
    settled = [t for t in trades if t["status"] in ("settled", "exited")]
    open_t = [t for t in trades if t["status"] == "open"]
    wins = [t for t in settled if t.get("pnl") is not None and t["pnl"] > 0]
    losses = [t for t in settled if t.get("pnl") is not None and t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in settled if t.get("pnl") is not None)
    win_rate = len(wins) / len(settled) if settled else 0.0

    # Exit reason breakdown
    exit_reasons: dict[str, int] = {}
    for t in settled:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

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
            "exit_reasons": exit_reasons,
            "trades": trades,
        }, indent=2, default=str))
        return

    print(f"\n{'='*72}")
    print(f"  PAPER TRADING SCORECARD — Multi-Agent v2")
    print(f"{'='*72}")
    print(f"  Initial bankroll:  ${data['initial_bankroll']:.2f}")
    print(f"  Current bankroll:  ${data['bankroll']:.2f}")
    print(f"  Total P&L:         ${total_pnl:+.2f}  ({total_pnl/data['initial_bankroll']*100:+.1f}%)")
    print(f"  Settled trades:    {len(settled)}  ({len(wins)}W / {len(losses)}L, win rate {win_rate*100:.0f}%)")
    print(f"  Open trades:       {len(open_t)}")
    if exit_reasons:
        print(f"  Exit reasons:")
        for r, c in sorted(exit_reasons.items(), key=lambda x: -x[1]):
            print(f"    {r:<18} {c}")

    if settled:
        print(f"\n  Recent settled (last 12):")
        for t in settled[-12:]:
            tag = "✓" if t.get("pnl", 0) > 0 else "✗"
            reason = t.get("exit_reason", "?")
            print(f"    {tag} {t['kalshi_ticker']:<38} {t['side'].upper():<3} "
                  f"{t['contracts']}x @ ${t['entry_price']:.3f}  "
                  f"pnl=${t.get('pnl', 0):+.2f}  [{reason}]")

    if open_t:
        print(f"\n  Open positions:")
        for t in open_t:
            tgt = f"target=${t.get('thesis_target_price', 0):.3f}" if t.get('thesis_target_price') else ""
            print(f"    → {t['kalshi_ticker']:<38} {t['side'].upper():<3} "
                  f"{t['contracts']}x @ ${t['entry_price']:.3f}  "
                  f"votes={t.get('consensus_votes', '?')}  conf={t.get('thesis_confidence', 0):.2f}  {tgt}")
    print()


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
