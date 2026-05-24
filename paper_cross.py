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
    """Full signal pipeline: scanner → (whale + disposition) → both executors."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] === SIGNAL PIPELINE (both bots) ===\n")

    if not TARGETS_FILE.exists():
        print("No whale target list — building one (this takes a few minutes)...")
        import targets as targets_module
        sys.argv = ["targets.py", "--candidates", "100"]
        targets_module.main()
        print()

    import scanner
    scanner.scan()
    print()

    # ── Bot A: Whale-copy ────────────────────────────────────────
    print("─── BOT A: WHALE-COPY ───")
    import brain
    brain.run()
    print()

    import executor
    executor.run()
    print()

    # ── Bot B: Disposition (longshot/favorite bias) ──────────────
    print("─── BOT B: DISPOSITION ───")
    import disposition
    disposition.run()
    print()

    import executor_disposition
    executor_disposition.run()


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
    """Cancel open trades in BOTH bot journals."""
    _cancel_journal(WHALE_JOURNAL, reason, "whale")
    _cancel_journal(DISPOSITION_JOURNAL, reason, "disposition")


def cmd_status(json_output: bool = False):
    whale = journal_stats(WHALE_JOURNAL, "whale-copy")
    disp  = journal_stats(DISPOSITION_JOURNAL, "disposition")

    if json_output:
        print(json.dumps({
            "whale_copy": whale,
            "disposition": disp,
            "combined": {
                "total_pnl": round(whale["total_pnl"] + disp["total_pnl"], 2),
                "settled": whale["settled"] + disp["settled"],
                "open": whale["open"] + disp["open"],
                "wins": whale["wins"] + disp["wins"],
                "losses": whale["losses"] + disp["losses"],
            },
        }, indent=2, default=str))
        return

    # Pre-format strings to avoid f-string nesting issues
    w_init = f"${whale['initial_bankroll']:.2f}"
    d_init = f"${disp['initial_bankroll']:.2f}"
    w_bank = f"${whale['bankroll']:.2f}"
    d_bank = f"${disp['bankroll']:.2f}"
    w_pnl  = f"${whale['total_pnl']:+.2f}"
    d_pnl  = f"${disp['total_pnl']:+.2f}"
    w_ret  = f"{whale['return_pct']:+.2f}%"
    d_ret  = f"{disp['return_pct']:+.2f}%"
    w_wr   = f"{whale['win_rate']:.0f}%"
    d_wr   = f"{disp['win_rate']:.0f}%"
    w_wl   = f"{whale['wins']}W / {whale['losses']}L"
    d_wl   = f"{disp['wins']}W / {disp['losses']}L"

    bar = "═" * 78
    sep = "─" * 25 + "  " + "─" * 22 + "  " + "─" * 22

    print(f"\n{bar}")
    print(f"  PAPER TRADING SCORECARD — Side-by-side bot comparison")
    print(bar)
    print(f"  {'':25}  {'Bot A: WHALE-COPY':>22}  {'Bot B: DISPOSITION':>22}")
    print(f"  {sep}")
    print(f"  {'Strategy':<25}  {'Follow Polymarket':>22}  {'Longshot/favorite':>22}")
    print(f"  {'':25}  {'smart money':>22}  {'bias (Whelan)':>22}")
    print(f"  {sep}")
    print(f"  {'Initial bankroll':<25}  {w_init:>22}  {d_init:>22}")
    print(f"  {'Current bankroll':<25}  {w_bank:>22}  {d_bank:>22}")
    print(f"  {'Total P&L':<25}  {w_pnl:>22}  {d_pnl:>22}")
    print(f"  {'Return %':<25}  {w_ret:>22}  {d_ret:>22}")
    print(f"  {'Settled trades':<25}  {str(whale['settled']):>22}  {str(disp['settled']):>22}")
    print(f"  {'Win rate':<25}  {w_wr:>22}  {d_wr:>22}")
    print(f"  {'W/L':<25}  {w_wl:>22}  {d_wl:>22}")
    print(f"  {'Open trades':<25}  {str(whale['open']):>22}  {str(disp['open']):>22}")
    print(bar)

    combined_pnl = whale["total_pnl"] + disp["total_pnl"]
    combined_bankroll = whale["bankroll"] + disp["bankroll"]
    combined_initial = whale["initial_bankroll"] + disp["initial_bankroll"]
    print(f"  COMBINED:  bankroll ${combined_bankroll:.2f}  /  P&L ${combined_pnl:+.2f}  "
          f"({combined_pnl/combined_initial*100:+.1f}%)")
    print(f"{'═'*78}\n")

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

    _show_recent(whale, "whale-copy")
    _show_recent(disp, "disposition")


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
