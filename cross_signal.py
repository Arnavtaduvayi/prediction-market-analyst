#!/usr/bin/env python3
"""
Cross-Platform Signal: Polymarket Top Traders → Kalshi Execution

How it works:
  1. Fetches Polymarket's top 100 traders by all-time PnL (public on-chain data)
  2. Pulls their trade history to find consensus signals (what they're all buying)
  3. For each signal, searches Kalshi for a matching market using text similarity
  4. Shows price divergence between Polymarket and Kalshi
  5. Flags the best opportunities where Kalshi is mispriced vs. Polymarket smart money

Why this works:
  - Academic paper (SSRN 5331995) confirms Polymarket LEADS Kalshi in price discovery
  - Reading Polymarket's public on-chain data is legal for US residents
  - You can't trade ON Polymarket as a US resident, but using it as a signal source
    to trade on Kalshi is completely fine

Key risk to always check:
  - Polymarket and Kalshi can have DIFFERENT resolution criteria for "the same" event
  - Always read both sets of rules before placing a trade
  - The script shows both for manual review

Usage:
  python3 cross_signal.py                        # scan top 50 traders, last 30 days
  python3 cross_signal.py --traders 100 --days 60
  python3 cross_signal.py --min-divergence 0.05  # only show 5%+ price gaps
  python3 cross_signal.py --output json > signals.json
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

# ── APIs ───────────────────────────────────────────────────────────────────────
POLY_DATA_API  = "https://data-api.polymarket.com"
POLY_GAMMA_API = "https://gamma-api.polymarket.com"
KALSHI_API     = "https://api.elections.kalshi.com/trade-api/v2"

REQUEST_DELAY = 0.4


# ── Polymarket helpers (public, no auth needed) ───────────────────────────────

def _get(url: str, params: dict = None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if 400 <= r.status_code < 500:
                return {} if "json" in r.headers.get("content-type","") else []
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                return {}
            time.sleep(2)
    return {}


def fetch_poly_leaderboard(max_traders: int = 100) -> list[dict]:
    traders, offset = [], 0
    print(f"Fetching Polymarket leaderboard (top {max_traders} traders)...")
    while len(traders) < min(max_traders, 1000):
        remaining = min(max_traders, 1000) - len(traders)
        batch = _get(f"{POLY_DATA_API}/v1/leaderboard", {
            "orderBy": "PNL", "timePeriod": "ALL",
            "limit": min(50, remaining), "offset": offset,
        })
        if not batch:
            break
        traders.extend(batch)
        offset += len(batch)
        time.sleep(REQUEST_DELAY)
    print(f"  {len(traders)} traders fetched.", file=sys.stderr)
    return traders


def fetch_poly_trades(wallet: str, since_ts: int, max_trades: int = 2000) -> list[dict]:
    all_trades, offset = [], 0
    while offset <= 3000:
        batch = _get(f"{POLY_DATA_API}/trades", {
            "user": wallet, "limit": 500, "offset": offset
        })
        if not batch:
            break
        cutoff = False
        for t in batch:
            if t.get("timestamp", 0) < since_ts:
                cutoff = True
                break
            all_trades.append(t)
        if cutoff or len(batch) < 500 or len(all_trades) >= max_trades:
            break
        offset += 500
        time.sleep(REQUEST_DELAY)
    return all_trades


def compute_poly_consensus(trades_by_wallet: dict) -> list[dict]:
    """Aggregate net positions across all top traders, return sorted by consensus."""
    from collections import defaultdict
    signals = defaultdict(lambda: {
        "title": "", "slug": "", "outcome": "",
        "wallets": set(), "buy_usd": 0.0, "prices": [],
    })
    for wallet, trades in trades_by_wallet.items():
        seen = {}
        for t in trades:
            slug = t.get("slug", "")
            outcome = t.get("outcome", "")
            side = t.get("side", "BUY")
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            key = (slug, outcome)
            if key not in seen:
                seen[key] = {"net": 0.0, "buy_usd": 0.0, "prices": [],
                             "title": t.get("title", slug)}
            if side == "BUY":
                seen[key]["net"] += size
                seen[key]["buy_usd"] += size * price
            else:
                seen[key]["net"] -= size
            seen[key]["prices"].append(price)
        for (slug, outcome), pos in seen.items():
            if pos["net"] > 0.5:
                s = signals[(slug, outcome)]
                s["title"] = pos["title"]
                s["slug"] = slug
                s["outcome"] = outcome
                s["wallets"].add(wallet)
                s["buy_usd"] += pos["buy_usd"]
                s["prices"].extend(pos["prices"])

    result = []
    for (slug, outcome), s in signals.items():
        avg_price = sum(s["prices"]) / len(s["prices"]) if s["prices"] else 0
        result.append({
            "title": s["title"],
            "slug": slug,
            "outcome": outcome,
            "trader_count": len(s["wallets"]),
            "buy_usd": s["buy_usd"],
            "avg_poly_price": avg_price,
        })
    result.sort(key=lambda x: (x["trader_count"], x["buy_usd"]), reverse=True)
    return result


def fetch_poly_current_price(slug: str, outcome: str) -> float | None:
    """Get current Polymarket price for a specific market outcome."""
    data = _get(f"{POLY_GAMMA_API}/markets", {"slug": slug})
    markets = data if isinstance(data, list) else data.get("markets", [])
    if not markets:
        return None
    market = markets[0]

    # Gamma API returns outcomes as JSON strings, e.g. '["Yes","No"]'
    # and outcomePrices as '["0.65","0.35"]'
    try:
        import json as _json
        outcomes_raw = market.get("outcomes", "[]")
        prices_raw = market.get("outcomePrices", "[]")
        outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        for o, p in zip(outcomes, prices):
            if outcome.lower() in str(o).lower() or str(o).lower() in outcome.lower():
                return float(p)
    except Exception:
        pass

    # Fallback: lastTradePrice for binary YES markets
    ltp = market.get("lastTradePrice")
    if ltp is not None and outcome.lower() in ("yes", "true", "1"):
        try:
            return float(ltp)
        except (TypeError, ValueError):
            pass

    return None


# ── Kalshi market matching ────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word tokens."""
    ta = set(_normalize(a).split())
    tb = set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def fetch_kalshi_markets(limit: int = 1000) -> list[dict]:
    """
    Fetch active Kalshi markets by scanning the recent trade stream.
    This returns markets that have ACTUAL recent volume, not the
    bulk of zero-volume markets returned by the /markets endpoint.
    """
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=72)).timestamp())
    tickers: dict[str, int] = {}  # ticker → trade count
    cursor = None
    pages = 0

    print("  Discovering active Kalshi markets from trade stream...")
    while pages < 15 and len(tickers) < limit:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{KALSHI_API}/markets/trades", params)
        batch = data.get("trades", [])
        if not batch:
            break
        cutoff = False
        for t in batch:
            ts_str = t.get("created_time", "")
            try:
                ts = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
            if ts < since_ts:
                cutoff = True
                break
            tk = t.get("ticker", "")
            if tk:
                tickers[tk] = tickers.get(tk, 0) + 1
        cursor = data.get("cursor")
        pages += 1
        time.sleep(0.15)
        if cutoff or not cursor:
            break

    # Sort by trade count, fetch top N market details
    sorted_tickers = sorted(tickers, key=lambda t: tickers[t], reverse=True)[:limit]
    print(f"  Found {len(sorted_tickers)} active tickers. Fetching market details...")

    markets = []
    for i, ticker in enumerate(sorted_tickers):
        # Skip obvious MVE parlay markets
        if "MVE" in ticker or ticker.count("-") > 3:
            continue
        data = _get(f"{KALSHI_API}/markets/{ticker}")
        m = data.get("market", {})
        if m:
            markets.append(m)
        if i % 50 == 49:
            time.sleep(0.5)
        else:
            time.sleep(0.08)

    print(f"  {len(markets)} Kalshi markets with details loaded.")
    return markets


def _extract_proper_nouns(text: str) -> set[str]:
    """
    Rough proper noun extraction: words that start with a capital letter
    and are at least 3 characters long, excluding common stop words.
    """
    stop = {"Will", "The", "Who", "What", "When", "How", "Does", "Did",
            "Was", "Are", "Has", "Have", "Been", "For", "And", "But",
            "With", "From", "That", "This", "In", "On", "At", "To",
            "Of", "By", "As", "Or", "An", "A", "Is", "It", "Be",
            "Not", "No", "Yes", "Can", "May", "Win", "Get", "Go"}
    words = re.findall(r"[A-Z][a-zA-Z]{2,}", text)
    return {w for w in words if w not in stop}


def find_kalshi_match(
    poly_title: str,
    poly_outcome: str,
    kalshi_markets: list[dict],
    min_score: float = 0.35,
) -> list[tuple[float, dict]]:
    """
    Find Kalshi markets equivalent to a Polymarket signal.

    Requires ALL of:
      1. Jaccard token overlap ≥ min_score
      2. ALL key proper nouns from Polymarket title appear in Kalshi text
         (so "Spurs Finals" only matches Spurs markets, not Timberwolves markets)
      3. Same category — if Poly mentions "NBA", Kalshi text must too
    """
    poly_nouns = _extract_proper_nouns(poly_title)
    if poly_outcome and poly_outcome.lower() not in ("yes", "no", "true", "false", "1", "0"):
        # Outcome is an entity name (e.g., a team), add to required nouns
        poly_nouns |= _extract_proper_nouns(poly_outcome)

    # Category keywords — if these appear in Poly title, require them in Kalshi too
    category_keywords = {
        "NBA": ["NBA", "Basketball", "BBL"],
        "MLB": ["MLB", "Baseball"],
        "NHL": ["NHL", "Hockey"],
        "NFL": ["NFL", "Football"],
        "WTA": ["WTA"],
        "ATP": ["ATP"],
        "FIFA": ["FIFA", "WorldCup", "World"],
        "PGA": ["PGA", "Golf"],
        "UFC": ["UFC"],
    }
    poly_upper = poly_title.upper()
    required_categories = []
    for cat, kws in category_keywords.items():
        if cat in poly_upper:
            required_categories.append([kw.upper() for kw in kws])

    query = f"{poly_title} {poly_outcome}"
    scored = []

    for m in kalshi_markets:
        kalshi_text = f"{m.get('title', '')} {m.get('yes_sub_title', '')} {m.get('no_sub_title', '')}"
        rules = m.get("rules_primary", "")
        full_kalshi = f"{kalshi_text} {rules}"
        kalshi_upper = full_kalshi.upper()
        ticker_upper = m.get("ticker", "").upper()

        score = _token_overlap(query, kalshi_text)
        if score < min_score:
            continue

        # Require at least 60% of Poly proper nouns to appear in Kalshi text.
        # This catches "Spurs" → "San Antonio" / "NBA" → "Pro Basketball" aliasing
        # while still rejecting unrelated markets.
        kalshi_text_lower = full_kalshi.lower()
        if poly_nouns:
            matched_nouns = [n for n in poly_nouns if n.lower() in kalshi_text_lower]
            match_pct = len(matched_nouns) / len(poly_nouns)
            if match_pct < 0.60:
                continue
            # Special rule: if Poly mentions a sports league + a team, require team
            # to appear (so "Spurs NBA Finals" doesn't match "Lakers NBA Finals")
            league_words = {"NBA", "NFL", "MLB", "NHL", "FIFA", "PGA", "WTA", "ATP", "UFC"}
            team_nouns = poly_nouns - league_words - {"Finals", "Championship", "Cup", "League", "World"}
            if team_nouns and not any(t.lower() in kalshi_text_lower for t in team_nouns):
                continue

        # Require category match: if Poly has NBA, Kalshi must have NBA-related word in ticker or text
        category_ok = True
        for kws in required_categories:
            if not any(kw in kalshi_upper or kw in ticker_upper for kw in kws):
                category_ok = False
                break
        if not category_ok:
            continue

        scored.append((score, m))

    scored.sort(key=lambda x: -x[0])
    return scored[:3]


# ── Price divergence ───────────────────────────────────────────────────────────

@dataclass
class CrossSignal:
    poly_title: str
    poly_slug: str
    poly_outcome: str
    poly_trader_count: int
    poly_buy_usd: float
    poly_avg_price: float          # price top traders paid on average
    poly_current_price: float | None  # current live Polymarket price
    kalshi_ticker: str
    kalshi_title: str
    kalshi_yes_bid: float
    kalshi_yes_ask: float
    kalshi_match_score: float
    kalshi_rules: str
    divergence: float              # poly_current - kalshi_mid (positive = Kalshi cheap)
    side: str                      # "yes" or "no" on Kalshi


def build_cross_signals(
    poly_signals: list[dict],
    kalshi_markets: list[dict],
    min_divergence: float = 0.03,
    min_traders: int = 2,
) -> list[CrossSignal]:
    results = []
    print(f"\nMatching {len(poly_signals)} Polymarket signals to Kalshi markets...")

    for sig in poly_signals:
        if sig["trader_count"] < min_traders:
            continue

        title = sig["title"]
        outcome = sig["outcome"]

        # Find matching Kalshi markets
        matches = find_kalshi_match(title, outcome, kalshi_markets)
        if not matches:
            continue

        # Fetch current Polymarket price (live)
        poly_live = fetch_poly_current_price(sig["slug"], outcome)
        time.sleep(0.2)

        poly_price = poly_live if poly_live is not None else sig["avg_poly_price"]

        for score, km in matches:
            ticker = km.get("ticker", "")
            bid_str = km.get("yes_bid_dollars") or "0"
            ask_str = km.get("yes_ask_dollars") or "1"
            try:
                bid = float(bid_str)
                ask = float(ask_str)
            except ValueError:
                continue

            if bid <= 0 or ask <= 0 or ask > 1:
                continue

            kalshi_mid = (bid + ask) / 2

            # Determine which side to trade on Kalshi.
            # poly_price is for the SPECIFIC outcome top traders bought on Polymarket.
            # If they bought YES @ 0.65, poly_price = 0.65 → compare to Kalshi YES.
            # If they bought NO @ 0.77, poly_price = 0.77 → compare to Kalshi NO (= 1 - kalshi_yes_mid).
            if outcome.lower() in ("yes", "true", "1"):
                divergence = poly_price - kalshi_mid
                side = "yes"
            else:
                kalshi_no_mid = 1.0 - kalshi_mid
                divergence = poly_price - kalshi_no_mid
                side = "no"

            if abs(divergence) < min_divergence:
                continue

            rules = km.get("rules_primary", "")[:200]

            results.append(CrossSignal(
                poly_title=title,
                poly_slug=sig["slug"],
                poly_outcome=outcome,
                poly_trader_count=sig["trader_count"],
                poly_buy_usd=sig["buy_usd"],
                poly_avg_price=sig["avg_poly_price"],
                poly_current_price=poly_live,
                kalshi_ticker=ticker,
                kalshi_title=km.get("title", ticker),
                kalshi_yes_bid=bid,
                kalshi_yes_ask=ask,
                kalshi_match_score=score,
                kalshi_rules=rules,
                divergence=divergence,
                side=side,
            ))

    results.sort(key=lambda s: abs(s.divergence), reverse=True)
    return results


# ── Output ─────────────────────────────────────────────────────────────────────

def print_signals(signals: list[CrossSignal], top_n: int = 20):
    if not signals:
        print("\nNo cross-platform signals found above the divergence threshold.")
        print("Try --min-divergence 0.02 or --days 90 to widen the search.")
        return

    print(f"\n{'='*105}")
    print(f"  CROSS-PLATFORM SIGNALS  —  Polymarket smart money → Kalshi opportunities")
    print(f"{'='*105}")
    print(f"  {'Polymarket market':<40} {'Out':<5} {'Trdrs':>5} {'Poly$':>8} "
          f"{'PolyP':>6} {'KalshiMid':>9} {'Div':>6} {'Side':<5} {'Match':>5}")
    print(f"  {'-'*105}")

    for s in signals[:top_n]:
        poly_p = f"{s.poly_current_price:.3f}" if s.poly_current_price else f"~{s.poly_avg_price:.3f}"
        kalshi_mid = (s.kalshi_yes_bid + s.kalshi_yes_ask) / 2
        print(
            f"  {s.poly_title[:39]:<40} {s.poly_outcome[:4]:<5} {s.poly_trader_count:>5} "
            f"${s.poly_buy_usd:>7,.0f} {poly_p:>6} {kalshi_mid:>9.3f} "
            f"{s.divergence:>+6.3f} {s.side.upper():<5} {s.kalshi_match_score:>5.2f}"
        )

    print(f"\n{'='*105}")
    print("  Div = Polymarket price - Kalshi price. Positive = Kalshi is CHEAP vs. Polymarket smart money.")
    print("  Match = text similarity score (0-1). Higher = more confident it's the same event.")
    print(f"\n  ⚠  ALWAYS verify resolution rules match before trading. Shown below for each signal.")

    for i, s in enumerate(signals[:top_n], 1):
        kalshi_mid = (s.kalshi_yes_bid + s.kalshi_yes_ask) / 2
        print(f"\n  [{i}] {s.poly_title}")
        print(f"      Polymarket: {s.poly_outcome} @ {s.poly_current_price or s.poly_avg_price:.3f}  "
              f"slug: {s.poly_slug}")
        print(f"      Kalshi:     {s.kalshi_ticker}  mid={kalshi_mid:.3f}  "
              f"bid={s.kalshi_yes_bid:.3f} ask={s.kalshi_yes_ask:.3f}")
        print(f"      Action:     BUY {s.side.upper()} on Kalshi  (divergence {s.divergence:+.1%})")
        print(f"      Kalshi rules: {s.kalshi_rules[:120]}...")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cross-platform signal: Polymarket top traders → Kalshi trades"
    )
    parser.add_argument("--traders", type=int, default=50,
                        help="Top Polymarket traders to analyze (default: 50)")
    parser.add_argument("--days", type=int, default=30,
                        help="Trade lookback window in days (default: 30)")
    parser.add_argument("--min-traders", type=int, default=2,
                        help="Min top traders in consensus to surface a signal (default: 2)")
    parser.add_argument("--min-divergence", type=float, default=0.03,
                        help="Min Poly-Kalshi price gap to show (default: 0.03 = 3%%)")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of signals to display (default: 20)")
    parser.add_argument("--output", choices=["table", "json"], default="table")
    args = parser.parse_args()

    since_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

    # 1. Get top Polymarket traders
    leaderboard = fetch_poly_leaderboard(max_traders=args.traders)
    real_traders = [t for t in leaderboard
                    if (t.get("pnl") or 0) >= 5000 and (t.get("vol") or 0) >= 50000]
    wallet_keys = [t["proxyWallet"] for t in real_traders if t.get("proxyWallet")]
    print(f"Analyzing {len(wallet_keys)} qualified traders...")

    # 2. Fetch their trade histories
    trades_by_wallet = {}
    total = 0
    for i, wallet in enumerate(wallet_keys):
        name = next((t.get("userName") for t in real_traders if t.get("proxyWallet") == wallet), wallet[:8])
        print(f"  [{i+1}/{len(wallet_keys)}] {name:<25}", end="  ", flush=True)
        trades = fetch_poly_trades(wallet, since_ts)
        trades_by_wallet[wallet] = trades
        total += len(trades)
        print(f"{len(trades)} trades")
        time.sleep(REQUEST_DELAY)

    print(f"\n{total:,} total trades fetched.")

    # 3. Compute Polymarket consensus signals
    poly_signals = compute_poly_consensus(trades_by_wallet)
    print(f"{len(poly_signals)} Polymarket consensus signals computed.")

    # 4. Fetch all Kalshi markets
    print("\nFetching Kalshi open markets...")
    kalshi_markets = fetch_kalshi_markets()
    print(f"  {len(kalshi_markets)} Kalshi markets loaded.")

    # 5. Match and compute divergences
    cross_signals = build_cross_signals(
        poly_signals, kalshi_markets,
        min_divergence=args.min_divergence,
        min_traders=args.min_traders,
    )

    # 6. Output
    if args.output == "json":
        out = []
        for s in cross_signals[:args.top]:
            out.append({
                "poly_title": s.poly_title,
                "poly_slug": s.poly_slug,
                "poly_outcome": s.poly_outcome,
                "poly_trader_count": s.poly_trader_count,
                "poly_buy_usd": round(s.poly_buy_usd, 2),
                "poly_current_price": s.poly_current_price,
                "kalshi_ticker": s.kalshi_ticker,
                "kalshi_title": s.kalshi_title,
                "kalshi_yes_bid": s.kalshi_yes_bid,
                "kalshi_yes_ask": s.kalshi_yes_ask,
                "kalshi_match_score": round(s.kalshi_match_score, 3),
                "divergence": round(s.divergence, 4),
                "kalshi_side": s.side,
                "kalshi_rules": s.kalshi_rules,
            })
        print(json.dumps(out, indent=2))
    else:
        print_signals(cross_signals, top_n=args.top)

    if cross_signals and args.output == "table":
        print(f"\nNext step: review the resolution rules above, then run:")
        print(f"  python3 live_trader.py --bankroll 51 --dry-run")
        print(f"  python3 live_trader.py --bankroll 51   (to place real orders)")


if __name__ == "__main__":
    main()
