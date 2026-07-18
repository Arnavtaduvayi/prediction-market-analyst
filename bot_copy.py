"""
bot_copy.py — Copy-trade manager (Bot P: "piggyback").

Copies top Polymarket traders via Bullpen's server-side copy-trade
subscriptions. This is deliberately NOT the retired v1-v4 whale-copy design:
that bot polled fills and re-derived entries minutes later (−0.3% over 93
settled, no edge after lag). Bullpen subscriptions mirror both entries AND
exits server-side in near-real-time, with per-trade / daily / budget caps
enforced by their infra — the copying itself needs no local uptime.

This bot's job is therefore *selection and rotation*, the part the data said
matters most:

  select   pull the leaderboard, filter hard (recent activity, real track
           record, not a farm bot), write data/copy_roster.json
  apply    reconcile the roster against live subscriptions: start new subs,
           pause dropped/underperforming ones (NEVER delete — deleting kills
           mirror-sells on open positions), resume re-selected ones
  status   subscriptions + execution P&L summary

Hard filters (each one exists because of a concrete failure mode):
  - last trade ≤ max_stale_days ago    (top all-time PnL wallets are often
                                        long dormant — #1 last traded 2024)
  - lifetime trades ≥ min_lifetime     (one lucky whale bet ≠ a track record)
  - not flagged is_likely_bot          (leaderboard farmers wash-trade)
Ranking among survivors: leaderboard PnL for the shortest window available.
"""

import json
import sys
from pathlib import Path

import polylib as pl

ROSTER_PATH = pl.DATA / "copy_roster.json"
JOURNAL_PATH = Path(__file__).resolve().parent / "live_copy_trades.json"
STRATEGY = "poly_copy"
INITIAL_BANKROLL = 100.0
CANDIDATE_POOL = 15  # leaderboard rows to stat-check per select


# ── selection (pure logic separated for tests) ──────────────────────────────

def eligible(row: dict, stats: dict, cfg: dict) -> tuple[bool, str]:
    """(ok, reason). `stats` is the wallet-stats payload for row['address']."""
    bounds = ((stats.get("activity_bounds") or {}).get("data")) or {}
    behavior = ((stats.get("behavior_stats") or {}).get("data")) or {}

    if not bounds:
        return False, "no stats data"
    total_trades = bounds.get("total_trades") or 0
    if total_trades < cfg["min_lifetime_trades"]:
        return False, f"only {total_trades} lifetime trades"

    last_ts = bounds.get("last_trade_timestamp")
    if not last_ts:
        return False, "no activity data"
    import time as _t
    stale_days = (_t.time() - last_ts) / 86400
    if stale_days > cfg["max_stale_days"]:
        return False, f"stale {stale_days:.0f}d"

    if behavior.get("is_likely_bot"):
        return False, "flagged likely bot/farmer"

    if (row.get("pnl") or 0) <= 0:
        return False, "non-positive pnl"
    return True, "ok"


def pick_roster(candidates: list[dict], cfg: dict) -> list[dict]:
    """candidates: [{row, stats, ok, reason}] in leaderboard order."""
    picked = [c for c in candidates if c["ok"]][: cfg["n_traders"]]
    return [{
        "address": c["row"]["address"],
        "username": c["row"].get("username") or c["row"]["address"][:10],
        "lb_pnl": c["row"].get("pnl"),
        "selected_at": pl.now_iso(),
    } for c in picked]


# ── leaderboard + stats I/O ─────────────────────────────────────────────────

def fetch_leaderboard(cfg: dict) -> list[dict]:
    """Prefer the copyability-ranked path (needs auth + gRPC); fall back to
    the public path, shortest time window first so ranking reflects current
    form rather than 2024 election-era wins."""
    attempts = [
        ["polymarket", "data", "leaderboard", "--time-period", "7d",
         "--sort", "copyability", "--hide-farmers", "--limit", str(CANDIDATE_POOL)],
        ["polymarket", "data", "leaderboard", "--time-period", "30d",
         "--limit", str(CANDIDATE_POOL)],
        ["polymarket", "data", "leaderboard", "--time-period", "7d",
         "--limit", str(CANDIDATE_POOL)],
        # Last resort: all-time board. Dominated by dormant election-era
        # wallets — the stale filter rejects most of it, and an empty roster
        # is the correct outcome until the windowed paths work (post-login).
        ["polymarket", "data", "leaderboard", "--limit", str(CANDIDATE_POOL)],
    ]
    for args in attempts:
        data = pl.bp_ok(*args, timeout=45, retries=1)
        rows = (data or {}).get("leaderboard") or []
        if rows:
            return rows
    return []


def select(cfg: dict) -> list[dict]:
    rows = fetch_leaderboard(cfg)
    if not rows:
        print("  [copy] leaderboard unavailable — keeping existing roster")
        return load_roster()
    candidates = []
    for row in rows:
        stats = pl.bp_ok("prediction", "wallet-stats", row["address"],
                         timeout=45, retries=1) or {}
        ok, reason = eligible(row, stats, cfg)
        candidates.append({"row": row, "stats": stats, "ok": ok, "reason": reason})
        mark = "+" if ok else "-"
        print(f"  [copy] {mark} {row.get('username', row['address'][:10]):<20} "
              f"pnl=${row.get('pnl', 0):,.0f}  {reason}")
    roster = pick_roster(candidates, cfg)
    ROSTER_PATH.write_text(json.dumps(
        {"selected_at": pl.now_iso(), "roster": roster}, indent=2))
    print(f"  [copy] roster: {[r['username'] for r in roster]}")
    return roster


def load_roster() -> list[dict]:
    if ROSTER_PATH.exists():
        return json.loads(ROSTER_PATH.read_text()).get("roster", [])
    return []


def roster_age_hours() -> float:
    if not ROSTER_PATH.exists():
        return 1e9
    sel = json.loads(ROSTER_PATH.read_text()).get("selected_at", "")
    ts = pl.parse_iso(sel)
    if ts is None:
        return 1e9
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


# ── subscription reconcile ──────────────────────────────────────────────────

def _list_subs() -> list[dict]:
    data = pl.bp_ok("tracker", "copy", "list", timeout=45) or {}
    if isinstance(data, list):
        return data
    return data.get("subscriptions") or data.get("copy_trades") or []


def _sub_fields(sub: dict) -> tuple[str, str, str]:
    addr = (sub.get("address") or sub.get("target_address")
            or sub.get("copied_address") or "").lower()
    sub_id = str(sub.get("id") or sub.get("subscription_id") or "")
    state = (sub.get("status") or sub.get("state") or "").lower()
    return addr, sub_id, state


def start_args(address: str, cfg: dict) -> list[str]:
    a = cfg["amount_per_trade_usd"]
    return [
        "tracker", "copy", "start", address,
        "--amount", f"{a:.2f}",
        "--execution-mode", "auto",
        "--max-trade-size", f"{a:.2f}",
        "--daily-limit", f"{cfg['daily_limit_usd']:.2f}",
        "--mirror-percent-cap", "5",
        "--budget", f"{cfg['budget_usd']:.2f}",
        "--max-per-market", f"{cfg['max_per_market_usd']:.2f}",
        "--exit-behavior", "mirror_sells",
        "--min-time-to-resolution", str(cfg["min_time_to_resolution_h"]),
        "--slippage", f"{cfg['slippage_pct']:.1f}",
        "--yes",
    ]


def apply(cfg_all: dict):
    cfg = cfg_all["copy"]
    roster = {r["address"].lower(): r for r in load_roster()}
    subs = _list_subs()
    seen = set()

    for sub in subs:
        addr, sub_id, state = _sub_fields(sub)
        if not addr:
            continue
        seen.add(addr)
        if addr in roster and state == "paused":
            print(f"  [copy] resuming {addr[:10]}")
            pl.execute(cfg_all, ["tracker", "copy", "resume", sub_id],
                       {"bot": "copy", "action": "resume", "address": addr})
        elif addr not in roster and state == "active":
            print(f"  [copy] pausing {addr[:10]} (dropped from roster)")
            pl.execute(cfg_all, ["tracker", "copy", "pause", sub_id],
                       {"bot": "copy", "action": "pause", "address": addr})

    for addr, r in roster.items():
        if addr in seen:
            continue
        print(f"  [copy] starting subscription: {r['username']} ({addr[:10]})")
        pl.execute(cfg_all, start_args(addr, cfg),
                   {"bot": "copy", "action": "start", "address": addr,
                    "username": r["username"]})


def check_performance(cfg_all: dict):
    """Pause any subscription whose copied P&L is below the floor after
    enough executions to mean something. Pause, never delete."""
    cfg = cfg_all["copy"]
    stats = pl.bp_ok("tracker", "copy", "stats", timeout=45) or {}
    rows = stats.get("per_subscription") or stats.get("subscriptions") or []
    for row in rows:
        addr, sub_id, state = _sub_fields(row)
        pnl = row.get("pnl") or row.get("realized_pnl") or 0
        n = row.get("executions") or row.get("trade_count") or 0
        if (state == "active" and n >= cfg["pause_min_executions"]
                and pnl < cfg["pause_if_copied_pnl_below_usd"]):
            print(f"  [copy] pausing {addr[:10]}: pnl ${pnl:.2f} over {n} copies")
            pl.execute(cfg_all, ["tracker", "copy", "pause", sub_id],
                       {"bot": "copy", "action": "pause_perf",
                        "address": addr, "pnl": pnl, "executions": n})


def status():
    subs = _list_subs()
    if not subs:
        print("  [copy] no subscriptions")
    for sub in subs:
        addr, sub_id, state = _sub_fields(sub)
        print(f"  [copy] {state:<8} {addr[:12]}  id={sub_id}")
    stats = pl.bp_ok("tracker", "copy", "stats", timeout=45)
    if stats:
        print(json.dumps(stats, indent=2)[:2000])


# ── paper simulation ────────────────────────────────────────────────────────
#
# In dry-run mode there are no server-side subscriptions, so nothing would
# ever be measured. Instead: add the roster wallets to the (harmless,
# reversible) tracker watchlist, then each cycle mirror their fills from
# `tracker feed` into a paper journal — BUYs open a fixed-stake position at
# the trader's own fill price, their SELLs close ours (mirror_sells), and
# resolutions settle via the shared journal machinery. This is exactly what
# a live subscription would do, minus slippage — treat paper P&L as a mild
# ceiling, not a floor.

def ensure_tracked(roster: list[dict]):
    data = pl.bp_ok("tracker", "list", timeout=45) or {}
    tracked = {str(a.get("address") or a).lower()
               for a in data.get("tracked_addresses") or []}
    for r in roster:
        addr = r["address"].lower()
        if addr not in tracked:
            print(f"  [copy] tracking {r['username']} ({addr[:10]})")
            pl.bp_ok("tracker", "add", r["address"],
                     "--nickname", r["username"][:24], timeout=45)


def sim_row(journal: dict, row: dict, cfg: dict) -> str | None:
    """Apply one roster-trader feed row to the paper journal. Pure of I/O."""
    slug = row.get("market_slug") or ""
    outcome = row.get("outcome") or ""
    side = (row.get("side") or "").upper()
    try:
        price = float(row.get("price") or 0)
    except (ValueError, TypeError):
        return None
    if not slug or not outcome or not (0 < price < 1):
        return None
    held = [t for t in pl.open_trades(journal)
            if t["slug"] == slug and t["outcome"] == outcome]

    if side == "SELL" and held:
        for t in held:
            proceeds = round(t["shares"] * price, 4)
            t["pnl"] = round(proceeds - t["cost"], 4)
            t["status"] = "settled"
            t["settled_at"] = pl.now_iso()
            t["exit_reason"] = "MIRROR_SELL"
            journal["bankroll"] += proceeds
        return "exit"

    if side == "BUY" and not held:
        open_now = pl.open_trades(journal)
        # Prolific traders (swisstony fires dozens of fills/hour) would
        # flood the journal without a global cap; live subs have budget_usd
        # for the same reason.
        if len(open_now) >= cfg.get("max_open_paper", 25):
            return None
        per_market = sum(t["cost"] for t in open_now if t["slug"] == slug)
        if per_market + cfg["amount_per_trade_usd"] > cfg["max_per_market_usd"]:
            return None
        shares = round(cfg["amount_per_trade_usd"] / price, 2)
        journal["trades"].append(pl.new_trade(
            slug, row.get("market_title") or slug, outcome, shares, price,
            STRATEGY, copied_from=row.get("user_address"),
            executed=False))
        journal["bankroll"] -= round(shares * price, 4)
        return "entry"
    return None


def paper_cycle(cfg_all: dict):
    cfg = cfg_all["copy"]
    roster = load_roster()
    if not roster:
        return
    ensure_tracked(roster)
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    last_ts = pl.parse_feed_ts(journal.get("last_feed_ts"))
    addrs = {r["address"].lower() for r in roster}
    rows = (pl.bp_ok("tracker", "feed", timeout=45) or {}).get("trades") or []
    newest = journal.get("last_feed_ts")
    acted = 0
    for row in sorted(rows, key=lambda r: str(r.get("timestamp") or "")):
        ts = pl.parse_feed_ts(row.get("timestamp"))
        if ts is None or (last_ts and ts <= last_ts):
            continue
        if (row.get("user_address") or "").lower() not in addrs:
            continue
        action = sim_row(journal, row, cfg)
        if action:
            acted += 1
            print(f"  [copy] paper {action}: {row.get('market_slug', '')[:45]} "
                  f"{row.get('outcome')} @ {row.get('price')} "
                  f"(from {row.get('user_name') or row.get('user_address', '')[:10]})")
        newest = str(row.get("timestamp") or newest)
    journal["last_feed_ts"] = newest
    if acted == 0:
        print("  [copy] no new roster fills in feed")
    pl.save_journal(journal, JOURNAL_PATH)


# ── entrypoint ──────────────────────────────────────────────────────────────

def run(cfg_all: dict):
    """One scheduler tick: refresh roster when stale, reconcile, health-check."""
    cfg = cfg_all["copy"]
    if not cfg["enabled"]:
        return
    # An empty roster is never cached — retry every cycle until the windowed
    # leaderboard (post-login) yields candidates that survive the filters.
    if not load_roster() or roster_age_hours() > cfg["reselect_hours"]:
        select(cfg)
    if cfg_all.get("live"):
        apply(cfg_all)
        check_performance(cfg_all)
    else:
        paper_cycle(cfg_all)


if __name__ == "__main__":
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "select":
        select(cfg_all["copy"])
    elif cmd == "apply":
        apply(cfg_all)
    elif cmd == "status":
        status()
    elif cmd == "run":
        run(cfg_all)
    else:
        print("usage: bot_copy.py [select|apply|status|run]")
