"""
bot_polytheta.py — Late-favorite convergence, maker entries (Bot T).

The v1 Kalshi measurement: theta's taker ask was fair value TO THE CENT
(95.2% win rate at 95.2% average entry). The signal finds real favorites;
paying the ask forfeits the edge. So never pay the ask — rest a bid 1+ tick
below it on favorites (0.90-0.97) resolving within 24h, and let convergence
come to us. A fill is an entry below measured fair value; the open question
this journal answers is whether fills suffer adverse selection worse than
the discount (i.e. we only get filled when news moved against us).

Paper fills use the v5 pessimistic rule: a resting bid at L counts as filled
only if a later print goes STRICTLY below L (price priority guarantees our
order would have been consumed first). A print at exactly L does not count.
Paper P&L is therefore a floor, not a hope.
"""

import sys
from pathlib import Path

import polylib as pl

JOURNAL_PATH = Path(__file__).resolve().parent / "live_theta_trades.json"
STRATEGY = "poly_theta"
INITIAL_BANKROLL = 100.0
TICK = 0.001


# ── entry logic (pure, tested) ──────────────────────────────────────────────

def favorite_outcome(m: dict, cfg: dict) -> dict | None:
    """The outcome trading inside the favorite band, if any. Works for
    Yes/No and named two-outcome markets alike."""
    outs = m.get("outcomes") or []
    if len(outs) != 2:
        return None
    for o in outs:
        p = o.get("price")
        if p is not None and cfg["min_price"] <= p <= cfg["max_price"]:
            return o
    return None


def check_market(m: dict, cfg: dict) -> tuple[bool, str]:
    if favorite_outcome(m, cfg) is None:
        return False, "no outcome in band"
    if (m.get("volume_24h") or 0) < cfg["min_volume_24h"]:
        return False, "thin volume"
    hrs = pl.hours_until(m.get("end_date") or "")
    if hrs is None:
        return False, "no end date"
    if not (cfg["min_hours_to_resolution"] <= hrs <= cfg["max_hours_to_resolution"]):
        return False, f"{hrs:.0f}h out"
    return True, "candidate"


def maker_bid(best_bid: float, best_ask: float, cfg: dict) -> float | None:
    """Bid one tick above the current bid, but always strictly below the ask
    (never cross = never take). None if the book leaves no maker room."""
    if best_bid <= 0 or best_ask <= best_bid:
        return None
    bid = min(best_bid + TICK, best_ask - TICK)
    if bid < best_bid:  # inverted/degenerate book
        return None
    return round(bid, 3)


def print_fills(order: dict, prints: list[dict]) -> bool:
    """Pessimistic fill: any later print in our outcome strictly below our
    limit means the book traded through us."""
    placed = pl.parse_iso(order["logged_at"])
    for t in prints:
        if (t.get("outcome") or "") != order["outcome"]:
            continue
        ts = pl.parse_iso(str(t.get("timestamp") or ""))
        if placed and ts and ts <= placed:
            continue
        try:
            if 0 < float(t.get("price") or 0) < order["entry_price"]:
                return True
        except (ValueError, TypeError):
            continue
    return False


# ── scan + place ────────────────────────────────────────────────────────────

def run(cfg_all: dict):
    cfg = cfg_all["theta"]
    if not cfg["enabled"]:
        return
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    active = [t for t in journal.get("trades", [])
              if t.get("status") in ("open", "resting")]
    if len(active) >= cfg["max_open"]:
        print(f"  [theta] at max open ({cfg['max_open']})")
        return
    held = {t["slug"] for t in active}
    placed = 0

    per_event: dict[str, int] = {}
    for t in active:
        ev = t.get("event_key") or t["slug"]
        per_event[ev] = per_event.get(ev, 0) + 1

    data = pl.bp_ok("polymarket", "markets", "--active", "--sort", "volume_24hr",
                    "--limit", str(cfg["scan_markets"]),
                    "--min-volume", str(cfg["min_volume_24h"]), timeout=90) or {}
    for raw in data.get("markets") or []:
        if len(active) + placed >= cfg["max_open"]:
            break
        m = pl.normalize_market(raw)
        slug = m.get("slug") or ""
        ev_key = str(m.get("event_slug") or slug)
        if slug in held or pl.in_cooldown("theta:" + slug):
            continue
        # Don't pile a whole quote book onto one event family (the first
        # live scan put all 8 quotes on a single France-England match).
        if per_event.get(ev_key, 0) >= cfg.get("max_per_event", 3):
            continue
        ok, why = check_market(m, cfg)
        if not ok:
            continue
        fav = favorite_outcome(m, cfg)
        book = pl.fetch_orderbook(slug, fav["name"])
        if book is None:
            continue
        spread = book.get("spread") or 1.0
        if spread > cfg["max_spread"]:
            continue
        bid = maker_bid(book.get("best_bid") or 0, book.get("best_ask") or 0, cfg)
        if bid is None or not (cfg["min_price"] <= bid <= cfg["max_price"]):
            continue

        shares = round(cfg["stake_usd"] / bid, 2)
        print(f"  [theta] REST BID {shares}sh @ ${bid:.3f} "
              f"(ask {book.get('best_ask'):.3f})  {slug[:55]}")
        res = pl.execute(cfg_all, [
            "polymarket", "limit-buy", "--price", f"{bid:.3f}",
            "--shares", f"{shares:.0f}", slug, fav["name"], "--yes",
        ], {"bot": "theta", "action": "rest_bid", "slug": slug,
            "outcome": fav["name"], "price": bid})

        order = pl.new_trade(slug, m.get("question") or slug, fav["name"],
                             shares, bid, STRATEGY,
                             end_date=m.get("end_date"), event_key=ev_key,
                             executed=bool(res.get("executed")))
        order["status"] = "resting"
        oid = ((res.get("result") or {}).get("order_id")
               if res.get("executed") else None)
        if oid:
            order["order_id"] = str(oid)
        journal["trades"].append(order)
        journal["bankroll"] -= order["cost"]
        per_event[ev_key] = per_event.get(ev_key, 0) + 1
        placed += 1

    if placed == 0:
        print("  [theta] no quotes placed")
    pl.save_journal(journal, JOURNAL_PATH)


# ── resting-order lifecycle (called from manage) ────────────────────────────

def advance_resting(cfg_all: dict):
    cfg = cfg_all["theta"]
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    changed = 0
    for order in [t for t in journal.get("trades", [])
                  if t.get("status") == "resting"]:
        placed_at = pl.parse_iso(order["logged_at"])
        if placed_at is None:
            continue
        from datetime import datetime, timezone
        age_h = (datetime.now(timezone.utc) - placed_at).total_seconds() / 3600

        prints = (pl.bp_ok("polymarket", "trades", order["slug"],
                           timeout=30) or {}).get("trades") or []
        if print_fills(order, prints):
            order["status"] = "open"
            order["filled_at"] = pl.now_iso()
            print(f"  [theta] FILLED  {order['slug'][:55]} @ ${order['entry_price']:.3f}")
            changed += 1
            continue

        state = pl.bp_ok("polymarket", "market", order["slug"], timeout=30) or {}
        state = state.get("market") or state
        market_done = bool(state.get("closed") or state.get("resolved"))
        if age_h > cfg["order_expire_hours"] or market_done:
            order["status"] = "expired"
            order["pnl"] = 0.0
            order["settled_at"] = pl.now_iso()
            order["exit_reason"] = "UNFILLED_CLOSE" if market_done else "UNFILLED_EXPIRY"
            journal["bankroll"] += order["cost"]
            pl.start_cooldown("theta:" + order["slug"])
            if order.get("order_id"):
                pl.execute(cfg_all,
                           ["polymarket", "orders", "--cancel", order["order_id"]],
                           {"bot": "theta", "action": "cancel_expired",
                            "order": order["order_id"]})
            print(f"  [theta] EXPIRED {order['slug'][:55]} (unfilled {age_h:.1f}h)")
            changed += 1
    if changed:
        pl.save_journal(journal, JOURNAL_PATH)


if __name__ == "__main__":
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run(cfg_all)
    elif cmd == "advance":
        advance_resting(cfg_all)
    else:
        print("usage: bot_polytheta.py [run|advance]")
