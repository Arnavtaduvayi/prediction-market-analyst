"""
bot_polyarb.py — Dutch-book arbitrage on Polymarket (Bot D).

Two basket shapes, both locked at entry:

  1. Single multi-outcome market (one market, N outcome tokens, exactly one
     wins): buy 1 share of EVERY outcome at ask. Payout is exactly $1 per
     basket (Polymarket 50-50 rules still sum to $1). Edge = 1 − Σ asks.
     Note binary markets with mirrored books can never trigger this; it fires
     only when independent outcome books drift apart.

  2. negRisk family (event → N separate binary markets, mutually exclusive by
     the negRisk adapter): buy NO on every market. At most one market can
     resolve YES, so payout ≥ N−1 GUARANTEED — even if the family is not
     exhaustive (if nothing resolves YES the basket pays N, better).
     Edge = (N−1) − Σ no_asks.

     We deliberately do NOT trade the YES-side basket (Σ yes_asks < 1):
     it is only risk-free when the family is exhaustive, and exhaustiveness
     is a resolution-rules judgment call — exactly the kind of "semantically
     similar but different" trap that lost money in v0.

Execution is marketable limit orders at the observed ask (price-capped, so a
moved book means an unfilled resting order, never a worse fill). Leg risk is
contained by requiring every leg's ask depth ≥ min_leg_depth_mult × our size
before firing, and manage() cancels any stale unfilled legs.
"""

import json
import sys
from pathlib import Path

import polylib as pl

JOURNAL_PATH = Path(__file__).resolve().parent / "live_arb_trades.json"
STRATEGY = "poly_arb"
INITIAL_BANKROLL = 100.0


# ── edge math (pure, tested) ────────────────────────────────────────────────

def basket_edge(leg_asks: list[float], payout: float, fee_bps: int) -> float:
    """Guaranteed profit per basket-share after fees. Negative = no trade."""
    cost = sum(leg_asks)
    fees = cost * fee_bps / 10_000
    return payout - cost - fees


def size_basket(legs: list[dict], cost_per_share: float, max_stake: float,
                depth_mult: float) -> float:
    """Shares per leg: depth-limited on every leg, then capped by stake.
    legs: [{ask, ask_size}]. Returns 0 if any leg lacks depth for even 1sh."""
    if not legs or cost_per_share <= 0:
        return 0.0
    depth_cap = min(leg["ask_size"] / depth_mult for leg in legs)
    stake_cap = max_stake / cost_per_share
    shares = min(depth_cap, stake_cap)
    return float(int(shares)) if shares >= 1 else 0.0


# ── scanning ────────────────────────────────────────────────────────────────

def _events(limit: int) -> list[dict]:
    for args in (["polymarket", "events", "--active", "--sort", "volume",
                  "--limit", str(limit)],
                 ["polymarket", "discover", "--sort", "volume",
                  "--limit", str(limit)]):
        data = pl.bp_ok(*args, timeout=60) or {}
        evs = data.get("events") or []
        if evs:
            for ev in evs:
                ev["markets"] = [pl.normalize_market(m, ev)
                                 for m in (ev.get("markets") or [])]
            return evs
    return []


def _outcome_books(market_slug: str, outcomes: list[str]) -> list[dict] | None:
    legs = []
    for name in outcomes:
        book = pl.fetch_orderbook(market_slug, name)
        top = pl.best_ask_with_depth(book) if book else None
        if top is None:
            return None
        legs.append({"market": market_slug, "outcome": name,
                     "ask": top[0], "ask_size": top[1]})
    return legs


def scan(cfg_all: dict) -> list[dict]:
    """Return executable baskets: [{kind, event, legs, payout, edge, shares}]."""
    cfg = cfg_all["arb"]
    found = []
    events = _events(cfg["scan_events"])
    print(f"  [arb] scanning {len(events)} events")
    for ev in events:
        markets = ev.get("markets") or []
        active = [m for m in markets
                  if m.get("active") and not m.get("closed") and
                  m.get("enable_order_book", True)]

        # Shape 1: one market, >2 independent outcome tokens.
        for m in active:
            outs = [o["name"] for o in (m.get("outcomes") or [])]
            if len(outs) < 3 or len(outs) > cfg["max_legs"]:
                continue
            legs = _outcome_books(m.get("slug", ""), outs)
            if legs is None:
                continue
            edge = basket_edge([l["ask"] for l in legs], 1.0, cfg["fee_bps"])
            if edge >= cfg["min_edge"]:
                found.append(_basket("multi_outcome", ev, legs, 1.0, edge, cfg))

        # Shape 2: negRisk family — NO basket across sibling binary markets.
        family = [m for m in active if m.get("enable_neg_risk")
                  and len(m.get("outcomes") or []) == 2]
        if 2 <= len(family) <= cfg["max_legs"]:
            legs = []
            for m in family:
                book = pl.fetch_orderbook(m.get("slug", ""), "No")
                top = pl.best_ask_with_depth(book) if book else None
                if top is None:
                    legs = None
                    break
                legs.append({"market": m.get("slug", ""), "outcome": "No",
                             "ask": top[0], "ask_size": top[1]})
            if legs:
                payout = float(len(legs) - 1)
                edge = basket_edge([l["ask"] for l in legs], payout,
                                   cfg["fee_bps"])
                if edge >= cfg["min_edge"]:
                    found.append(_basket("negrisk_no", ev, legs, payout, edge, cfg))
    return [b for b in found if b["shares"] >= 1]


def _basket(kind, ev, legs, payout, edge, cfg) -> dict:
    cost = sum(l["ask"] for l in legs)
    shares = size_basket(legs, cost, cfg["max_stake_usd"],
                         cfg["min_leg_depth_mult"])
    return {"kind": kind, "event_slug": ev.get("slug", ""),
            "title": ev.get("title", ""), "legs": legs, "payout": payout,
            "cost_per_share": round(cost, 4), "edge": round(edge, 4),
            "shares": shares}


# ── execution ───────────────────────────────────────────────────────────────

def enter(cfg_all: dict, baskets: list[dict]):
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    held = {t.get("event_slug") for t in pl.open_trades(journal)}
    for b in baskets:
        if b["event_slug"] in held or pl.in_cooldown(b["event_slug"]):
            continue
        total_cost = b["cost_per_share"] * b["shares"]
        locked = b["edge"] * b["shares"]
        print(f"  [arb] {b['kind']}: {b['title'][:60]}  "
              f"edge=${b['edge']:.3f}/sh × {b['shares']:.0f}sh  "
              f"locked=${locked:.2f}")
        all_ok = True
        for leg in b["legs"]:
            res = pl.execute(cfg_all, [
                "polymarket", "limit-buy",
                "--price", f"{leg['ask']:.3f}",
                "--shares", f"{b['shares']:.0f}",
                leg["market"], leg["outcome"], "--yes",
            ], {"bot": "arb", "action": "leg_buy", "basket": b["event_slug"],
                **leg, "shares": b["shares"]})
            if not (res.get("executed") or res.get("dry_run")):
                all_ok = False
                break
        if not all_ok:
            print("  [arb] leg failed — check open orders / positions!")
        journal["trades"].append(pl.new_trade(
            b["event_slug"], b["title"], b["kind"], b["shares"],
            b["cost_per_share"], STRATEGY,
            legs=b["legs"], payout_per_share=b["payout"],
            locked_profit=round(locked, 4),
            executed=bool(cfg_all.get("live")), legs_ok=all_ok))
        journal["bankroll"] -= total_cost
        pl.start_cooldown(b["event_slug"])
    pl.save_journal(journal, JOURNAL_PATH)


def run(cfg_all: dict):
    if not cfg_all["arb"]["enabled"]:
        return
    baskets = scan(cfg_all)
    if baskets:
        enter(cfg_all, baskets)
    else:
        print("  [arb] no baskets above edge threshold")


if __name__ == "__main__":
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "scan":
        for b in scan(cfg_all):
            print(json.dumps(b, indent=2))
    elif cmd == "run":
        run(cfg_all)
    else:
        print("usage: bot_polyarb.py [scan|run]")
