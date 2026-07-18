"""
bot_polyseller.py — Longshot-NO on Polymarket (Bot N).

The one strategy leg this repo's own book supports: selling longshots was
+4.6%/trade over 37 settled (vs −4.4%/trade buying favorites, n=221), on top
of Whelan's 72M-trade favorite-longshot-bias result. On Polymarket "selling
the longshot" = buying NO on markets where YES is priced at a few cents.

Why this can clear costs here when the Kalshi paper version bled −4.9%:
Kalshi's seller lost to adverse selection on 20-60¢-wide books and fees, not
to the signal (92% win rate). Polymarket books on liquid markets are 1-2¢
wide and fee-free on most markets, so the entry give-up shrinks by an order
of magnitude. Whether that's enough is measurable — this journal will say.

Discipline (each rule maps to a measured failure in CHANGELOG.md):
  - NO at 0.90-0.97 only. Above 0.97 the residual upside can't cover a
    single loss; below 0.90 the "longshot" isn't one.
  - Resolution within 4-72h. Longshot bias concentrates near expiry; long
    holds tie up bankroll and add thesis risk.
  - Liquid books only (24h volume + spread + depth gates) — a fair-looking
    price on a dead book is how paper results diverge from live ones.
  - One position per EVENT (ladder/sibling strikes are perfectly correlated).
  - Fixed small stake, capped open count: at 95¢ entries a single loss costs
    ~19 wins, so the loss cap IS the strategy.
"""

import sys
from pathlib import Path

import polylib as pl

JOURNAL_PATH = Path(__file__).resolve().parent / "live_seller_trades.json"
STRATEGY = "poly_seller"
INITIAL_BANKROLL = 100.0


# ── candidate filter (pure, tested) ─────────────────────────────────────────

def check_market(m: dict, cfg: dict) -> tuple[bool, str]:
    outs = m.get("outcomes") or []
    names = {(o.get("name") or "").lower() for o in outs}
    if names != {"yes", "no"}:
        return False, "not binary yes/no"
    yes_price = next((o.get("price") for o in outs
                      if (o.get("name") or "").lower() == "yes"), None)
    if yes_price is None or yes_price > cfg["max_yes_price"]:
        return False, f"yes {yes_price} > {cfg['max_yes_price']}"
    if yes_price <= 0:
        return False, "dead market"
    if (m.get("volume_24h") or 0) < cfg["min_volume_24h"]:
        return False, "thin volume"
    hrs = pl.hours_until(m.get("end_date") or m.get("endDateIso") or "")
    if hrs is None:
        return False, "no end date"
    if not (cfg["min_hours_to_resolution"] <= hrs <= cfg["max_hours_to_resolution"]):
        return False, f"{hrs:.0f}h to resolution"
    return True, "candidate"


def check_book(book: dict, cfg: dict) -> tuple[bool, str, float]:
    """Validate the NO book. Returns (ok, reason, no_ask)."""
    top = pl.best_ask_with_depth(book)
    if top is None:
        return False, "no asks", 0.0
    no_ask, ask_size = top
    if not (cfg["min_no_price"] <= no_ask <= cfg["max_no_price"]):
        return False, f"no_ask {no_ask:.3f} outside band", no_ask
    spread = (book.get("spread") or 1.0)
    if spread > cfg["max_spread"]:
        return False, f"spread {spread:.3f}", no_ask
    need_depth = cfg["min_depth_mult"] * cfg["stake_usd"] / max(no_ask, 0.01)
    if ask_size < need_depth:
        return False, f"depth {ask_size:.0f} < {need_depth:.0f}", no_ask
    return True, "ok", no_ask


def event_key(m: dict) -> str:
    """Sibling strikes share an event — one position per event, ever."""
    return str(m.get("event_slug") or m.get("eventSlug") or
               m.get("event_id") or m.get("slug") or "")


# ── scan + enter ────────────────────────────────────────────────────────────

def _markets(cfg: dict) -> list[dict]:
    data = pl.bp_ok("polymarket", "markets", "--active",
                    "--sort", "volume_24hr",
                    "--limit", str(cfg["scan_markets"]),
                    "--min-volume", str(cfg["min_volume_24h"]),
                    timeout=90) or {}
    rows = data.get("markets") or []
    if rows:
        return [pl.normalize_market(m) for m in rows]
    # Fallback: flatten events from discover.
    out = []
    for ev in (pl.bp_ok("polymarket", "discover", "--sort", "volume",
                        "--limit", "50", timeout=90) or {}).get("events", []):
        for m in ev.get("markets") or []:
            m.setdefault("event_slug", ev.get("slug"))
            out.append(m)
    return out


def run(cfg_all: dict):
    cfg = cfg_all["seller"]
    if not cfg["enabled"]:
        return
    journal = pl.load_journal(JOURNAL_PATH, STRATEGY, INITIAL_BANKROLL)
    open_now = pl.open_trades(journal)
    if len(open_now) >= cfg["max_open"]:
        print(f"  [seller] at max open ({cfg['max_open']})")
        return
    held_events = {t.get("event_key") for t in open_now}
    entered = 0

    for m in _markets(cfg):
        if len(open_now) + entered >= cfg["max_open"]:
            break
        slug = m.get("slug") or ""
        ok, why = check_market(m, cfg)
        if not ok:
            continue
        if pl.in_cooldown(slug) or event_key(m) in held_events:
            continue
        book = pl.fetch_orderbook(slug, "No")
        if book is None:
            continue
        ok, why, no_ask = check_book(book, cfg)
        if not ok:
            print(f"  [seller] skip {slug[:50]}: {why}")
            continue

        shares = round(cfg["stake_usd"] / no_ask, 2)
        print(f"  [seller] BUY NO {shares}sh @ ${no_ask:.3f}  {slug[:60]}")
        res = pl.execute(cfg_all, [
            "polymarket", "buy", slug, "No", f"{cfg['stake_usd']:.2f}",
            "--max-price", f"{no_ask:.3f}", "--yes",
        ], {"bot": "seller", "action": "buy_no", "slug": slug,
            "price": no_ask, "usd": cfg["stake_usd"]})

        journal["trades"].append(pl.new_trade(
            slug, m.get("question") or m.get("title") or slug, "No",
            shares, no_ask, STRATEGY,
            event_key=event_key(m),
            end_date=m.get("end_date") or m.get("endDateIso"),
            executed=bool(res.get("executed"))))
        journal["bankroll"] -= round(shares * no_ask, 2)
        held_events.add(event_key(m))
        pl.start_cooldown(slug)
        entered += 1

    if entered == 0:
        print("  [seller] no qualifying entries")
    pl.save_journal(journal, JOURNAL_PATH)


if __name__ == "__main__":
    cfg_all = pl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run(cfg_all)
    else:
        print("usage: bot_polyseller.py [run]")
