"""
live_cross.py — Orchestrator for the live Polymarket roster.

  signal   run all three bots' entry logic (copy reconcile, arb scan, seller)
  manage   settle resolved trades, redeem winnings, cancel stale orders,
           evaluate the kill switch
  cycle    signal + manage (what the scheduler runs)
  status   scorecard across journals + subscriptions + balances
  halt / resume   engage or clear the kill switch by hand

Runs safely in any state: not logged in → read-only scanning still works and
every would-be order is journaled as a dry-run; live=false (default) → same;
halted → nothing mutates until `resume`.
"""

import json
import sys
from pathlib import Path

import polylib as pl
import bot_copy
import bot_polyarb
import bot_polyseller
import bot_polytheta
import bot_whaleflow

ROOT = Path(__file__).resolve().parent
JOURNALS = {
    "copy": (bot_copy.JOURNAL_PATH, bot_copy.STRATEGY,
             bot_copy.INITIAL_BANKROLL),
    "arb": (bot_polyarb.JOURNAL_PATH, bot_polyarb.STRATEGY,
            bot_polyarb.INITIAL_BANKROLL),
    "seller": (bot_polyseller.JOURNAL_PATH, bot_polyseller.STRATEGY,
               bot_polyseller.INITIAL_BANKROLL),
    "theta": (bot_polytheta.JOURNAL_PATH, bot_polytheta.STRATEGY,
              bot_polytheta.INITIAL_BANKROLL),
    "whale": (bot_whaleflow.JOURNAL_PATH, bot_whaleflow.STRATEGY,
              bot_whaleflow.INITIAL_BANKROLL),
}
STALE_ORDER_HOURS = 4


# ── settlement ──────────────────────────────────────────────────────────────

def _market_state(slug: str) -> dict:
    m = pl.bp_ok("polymarket", "market", slug, timeout=30) or {}
    return m.get("market") or m


def _resolved_outcome_price(state: dict, outcome: str) -> float | None:
    """If the market is resolved/closed, price of `outcome` (→ 1.0 or 0.0)."""
    if not (state.get("resolved") or state.get("closed")):
        return None
    for o in state.get("outcomes") or []:
        if (o.get("name") or "").lower() == outcome.lower():
            p = o.get("price")
            return float(p) if p is not None else None
    return None


def settle_journals(cfg_all: dict):
    for name, (path, strategy, bankroll) in JOURNALS.items():
        journal = pl.load_journal(path, strategy, bankroll)
        changed = 0
        for t in pl.open_trades(journal):
            if name == "arb":
                changed += _settle_arb(t, journal)
            else:
                changed += _settle_single(t, journal)
        if changed:
            pl.save_journal(journal, path)
            print(f"  [manage] {name}: {changed} trades settled")


def _settle_single(t: dict, journal: dict) -> int:
    state = _market_state(t["slug"])
    price = _resolved_outcome_price(state, t["outcome"])
    if price is None:
        return 0
    won = price >= 0.95
    lost = price <= 0.05
    if not (won or lost):
        return 0  # 50-50 style resolutions handled manually
    payout = t["shares"] * (1.0 if won else 0.0)
    t["pnl"] = round(payout - t["cost"], 4)
    t["status"] = "settled"
    t["settled_at"] = pl.now_iso()
    t["exit_reason"] = "SETTLED_WIN" if won else "SETTLED_LOSS"
    journal["bankroll"] += payout
    print(f"    {'✓' if won else '✗'} {t['slug'][:55]}  pnl=${t['pnl']:+.2f}")
    return 1


def _settle_arb(t: dict, journal: dict) -> int:
    """An arb basket settles when every leg's market has resolved."""
    payout = 0.0
    for leg in t.get("legs", []):
        state = _market_state(leg["market"])
        price = _resolved_outcome_price(state, leg["outcome"])
        if price is None:
            return 0
        payout += t["shares"] * (1.0 if price >= 0.95 else 0.0)
    # cost = shares × entry_price where entry_price is the basket cost/share
    t["pnl"] = round(payout - t["cost"], 4)
    t["status"] = "settled"
    t["settled_at"] = pl.now_iso()
    t["exit_reason"] = "BASKET_SETTLED"
    journal["bankroll"] += payout
    print(f"    ⚖ {t['slug'][:55]}  basket pnl=${t['pnl']:+.2f}")
    return 1


# ── housekeeping ────────────────────────────────────────────────────────────

def redeem(cfg_all: dict):
    if cfg_all.get("live"):
        pl.execute(cfg_all, ["polymarket", "redeem", "--yes"],
                   {"bot": "manage", "action": "redeem"})


def cancel_stale_orders(cfg_all: dict):
    data = pl.bp_ok("polymarket", "orders", timeout=45) or {}
    orders = data.get("orders") if isinstance(data, dict) else data
    if not orders:
        return
    for o in orders:
        created = pl.parse_iso(str(o.get("created_at") or o.get("created") or ""))
        if created is None:
            continue
        from datetime import datetime, timezone
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        oid = str(o.get("id") or o.get("order_id") or "")
        if age_h > STALE_ORDER_HOURS and oid:
            print(f"  [manage] cancelling stale order {oid[:16]} ({age_h:.1f}h)")
            pl.execute(cfg_all, ["polymarket", "orders", "--cancel", oid],
                       {"bot": "manage", "action": "cancel_stale", "order": oid})


# ── kill switch ─────────────────────────────────────────────────────────────

def realized_pnl() -> float:
    total = 0.0
    for path, strategy, bankroll in JOURNALS.values():
        j = pl.load_journal(path, strategy, bankroll)
        total += sum(t.get("pnl") or 0 for t in j.get("trades", []))
    stats = pl.bp_ok("tracker", "copy", "stats", timeout=45) or {}
    for key in ("total_pnl", "realized_pnl", "pnl"):
        if isinstance(stats.get(key), (int, float)):
            total += stats[key]
            break
    return round(total, 2)


def check_kill_switch(cfg_all: dict):
    pnl = realized_pnl()
    limit = -abs(cfg_all["kill_switch_drawdown_usd"])
    if pnl <= limit and not pl.halted():
        print(f"  [manage] KILL SWITCH: realized pnl ${pnl:.2f} ≤ ${limit:.2f}")
        pl.set_halt(f"drawdown: realized pnl ${pnl:.2f}")
        for sub in bot_copy._list_subs():
            addr, sub_id, state = bot_copy._sub_fields(sub)
            if state == "active" and sub_id:
                pl.bp_ok("tracker", "copy", "pause", sub_id)
        if cfg_all.get("live"):
            pl.bp_ok("polymarket", "orders", "--cancel-all", "--yes")


# ── commands ────────────────────────────────────────────────────────────────

def signal(cfg_all: dict):
    print("── copy ──")
    bot_copy.run(cfg_all)
    print("── arb ──")
    bot_polyarb.run(cfg_all)
    print("── seller ──")
    bot_polyseller.run(cfg_all)
    print("── theta ──")
    bot_polytheta.run(cfg_all)
    print("── whaleflow ──")
    bot_whaleflow.run(cfg_all)


def manage(cfg_all: dict):
    bot_polytheta.advance_resting(cfg_all)
    settle_journals(cfg_all)
    redeem(cfg_all)
    cancel_stale_orders(cfg_all)
    check_kill_switch(cfg_all)


def status(cfg_all: dict):
    mode = "LIVE" if cfg_all.get("live") else "DRY-RUN"
    stop = pl.halted()
    auth = "authed" if pl.logged_in() else "NOT LOGGED IN"
    print("═" * 78)
    print(f"  LIVE POLYMARKET ROSTER — {mode} — {auth}"
          + (f" — ⛔ HALTED: {stop['reason']}" if stop else ""))
    print("═" * 78)
    for name, (path, strategy, bankroll) in JOURNALS.items():
        j = pl.load_journal(path, strategy, bankroll)
        trades = j.get("trades", [])
        settled = [t for t in trades if t.get("status") == "settled"]
        resting = [t for t in trades if t.get("status") == "resting"]
        wins = [t for t in settled if (t.get("pnl") or 0) > 0]
        pnl = sum(t.get("pnl") or 0 for t in settled)
        print(f"  {name:<8} pnl=${pnl:+8.2f}  settled={len(settled):>3} "
              f"won={len(wins):>3}  open={len(pl.open_trades(j)):>3}  "
              f"rest={len(resting):>3}  bankroll=${j['bankroll']:.2f}")
    print("  copy subscriptions:")
    bot_copy.status()
    bal = pl.bp_ok("portfolio", "balances", timeout=45)
    if bal:
        print("  balances: " + json.dumps(bal)[:300])


def main():
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in ("signal", "manage", "cycle"):
        # One writer at a time: scheduler ticks can outlive the 10-min
        # interval on slow endpoints, and overlapping cycles would clobber
        # the journals' load-modify-save.
        import fcntl
        lock = (pl.DATA / "live_cycle.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another cycle is running — skipping this tick")
            return
    if cmd == "signal":
        signal(cfg_all)
    elif cmd == "manage":
        manage(cfg_all)
    elif cmd == "cycle":
        signal(cfg_all)
        manage(cfg_all)
    elif cmd == "status":
        status(cfg_all)
    elif cmd == "halt":
        pl.set_halt(" ".join(sys.argv[2:]) or "manual")
        print("halted.")
    elif cmd == "resume":
        pl.clear_halt()
        print("resumed.")
    else:
        print("usage: live_cross.py [signal|manage|cycle|status|halt|resume]")


if __name__ == "__main__":
    main()
