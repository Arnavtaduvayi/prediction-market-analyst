"""
brain.py — Per-market Evaluation (Agent 2)

For each market in queue.json, run 4 checks:
  1. Base rate     — what does historical Polymarket pricing say?
  2. Whale check   — are any of the 47 target wallets active here?
  3. Disposition   — is the crowd showing a cognitive bias (longshot, recency)?
  4. Edge gate     — does the implied edge exceed the slippage tax?

Generate a thesis with confidence score. If 3 of 4 agree → thesis is valid.
Confidence > 75% → size for full Kelly. 50-75% → half Kelly. <50% → skip.

We omit the "news" check from the original methodology because no free
news API exists with enough coverage. Three checks are required to pass.

Output: thesis.json — list of evaluated opportunities ready for the executor
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
TARGETS_FILE = Path(__file__).parent / "data" / "targets.json"
THESIS_FILE = Path(__file__).parent / "data" / "thesis.json"

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_DATA = "https://data-api.polymarket.com"


def _get(url: str, params: dict = None) -> dict | list:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, timeout=15)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if 400 <= r.status_code < 500:
                return {}
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return {}
            time.sleep(2)
    return {}


def _extract_keywords(text: str) -> set[str]:
    """Extract distinctive words from a market title for cross-platform search."""
    stop = {"will", "the", "who", "what", "when", "how", "does", "did", "be",
            "in", "on", "at", "to", "of", "by", "as", "or", "an", "a", "is",
            "it", "and", "but", "with", "from", "for", "this", "that", "than"}
    words = re.findall(r"[A-Za-z]{3,}", text.lower())
    return {w for w in words if w not in stop}


# ── Check 1: Base rate (Polymarket equivalent) ────────────────────────────────

def check_base_rate(market: dict) -> dict:
    """
    Find the equivalent Polymarket market and use its midpoint as the base rate.
    Implication: if Polymarket has the same event at price P, P is our prior.
    """
    title_keywords = _extract_keywords(market.get("title", ""))
    if not title_keywords:
        return {"pass": False, "reason": "no_keywords", "estimate": None}

    # Search Polymarket Gamma for a matching market via keyword
    search_term = " ".join(list(title_keywords)[:3])
    data = _get(f"{POLY_GAMMA}/markets", {"search": search_term, "limit": 10, "active": True})
    candidates = data if isinstance(data, list) else data.get("markets", [])

    best_match = None
    best_overlap = 0
    for c in candidates:
        c_title = c.get("question") or c.get("title") or ""
        c_keywords = _extract_keywords(c_title)
        overlap = len(title_keywords & c_keywords) / max(len(title_keywords), 1)
        if overlap > best_overlap and overlap >= 0.40:
            best_overlap = overlap
            best_match = c

    if not best_match:
        # No equivalent — return neutral signal, mark check as ambiguous
        return {"pass": False, "reason": "no_poly_match", "estimate": None}

    # Extract Polymarket YES price
    poly_price = None
    try:
        prices_raw = best_match.get("outcomePrices", "[]")
        outcomes_raw = best_match.get("outcomes", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        for o, p in zip(outcomes, prices):
            if str(o).lower() in ("yes", "true", "1"):
                poly_price = float(p)
                break
    except Exception:
        pass

    if poly_price is None:
        ltp = best_match.get("lastTradePrice")
        if ltp is not None:
            try:
                poly_price = float(ltp)
            except (TypeError, ValueError):
                pass

    if poly_price is None:
        return {"pass": False, "reason": "poly_no_price", "estimate": None}

    kalshi_mid = market["yes_mid"]
    gap = poly_price - kalshi_mid
    return {
        "pass": abs(gap) >= 0.05,
        "estimate": round(poly_price, 4),
        "kalshi_mid": kalshi_mid,
        "gap": round(gap, 4),
        "side_hint": "yes" if gap > 0 else "no",
        "poly_slug": best_match.get("slug", ""),
        "match_overlap": round(best_overlap, 3),
    }


# ── Check 2: Whale presence ───────────────────────────────────────────────────

def check_whale(market: dict, target_wallets: list[str], since_ts: int) -> dict:
    """
    Are any of our 50 target whales actively trading the equivalent Polymarket?
    """
    title_keywords = _extract_keywords(market.get("title", ""))
    whale_hits = 0
    whale_signal = "none"
    whale_avg_price = None
    matched_wallets = []
    prices_sum = 0.0
    prices_n = 0

    for wallet in target_wallets[:20]:  # check top 20 only (perf)
        trades = _get(f"{POLY_DATA}/trades", {"user": wallet, "limit": 200})
        if not trades:
            continue
        for t in trades:
            if t.get("timestamp", 0) < since_ts:
                continue
            t_title = (t.get("title") or "").lower()
            t_keywords = _extract_keywords(t_title)
            overlap = len(title_keywords & t_keywords) / max(len(title_keywords), 1)
            if overlap >= 0.40:
                whale_hits += 1
                matched_wallets.append(wallet[:10])
                prices_sum += float(t.get("price", 0))
                prices_n += 1
                # Side hint: whale BUYing YES = bullish on YES
                side = (t.get("side") or "").upper()
                outcome = (t.get("outcome") or "").lower()
                if side == "BUY" and outcome in ("yes", "true"):
                    whale_signal = "yes"
                elif side == "BUY" and outcome in ("no", "false"):
                    whale_signal = "no"
                break  # one trade per wallet is enough to count it
        time.sleep(0.2)

    if prices_n:
        whale_avg_price = prices_sum / prices_n

    return {
        "pass": whale_hits >= 2,
        "n_whales": whale_hits,
        "side_hint": whale_signal,
        "whale_avg_price": round(whale_avg_price, 4) if whale_avg_price else None,
        "matched_wallets": matched_wallets[:5],
    }


# ── Check 3: Disposition (crowd bias) ─────────────────────────────────────────

def check_disposition(market: dict) -> dict:
    """
    Detect well-known prediction-market mispricings:
      - Longshot bias: contracts priced <$0.15 lose money on average
      - Favorite under-pricing: contracts >$0.85 tend to win slightly more than priced
    From Whelan/GWU "Makers and Takers" paper on 72M Kalshi trades.
    """
    mid = market["yes_mid"]
    if mid < 0.15:
        return {
            "pass": True,
            "type": "longshot_bias",
            "side_hint": "no",  # longshots are overpriced → bet NO (i.e., against)
            "expected_edge": 0.04,
        }
    if mid > 0.85:
        return {
            "pass": True,
            "type": "favorite_under_pricing",
            "side_hint": "yes",
            "expected_edge": 0.02,
        }
    return {"pass": False, "type": "neutral", "side_hint": None}


# ── Check 4: Edge gate ────────────────────────────────────────────────────────

def check_edge_gate(market: dict, estimate: float | None) -> dict:
    """
    Final gate: implied edge must beat the spread + slippage tax (~2%).
    """
    if estimate is None:
        return {"pass": False, "reason": "no_estimate", "edge": None}
    spread = market["yes_ask"] - market["yes_bid"]
    edge = abs(estimate - market["yes_mid"])
    net_edge = edge - max(spread, 0.02)
    return {
        "pass": net_edge >= 0.02,
        "edge": round(edge, 4),
        "spread": round(spread, 4),
        "net_edge": round(net_edge, 4),
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

def evaluate(market: dict, target_wallets: list[str], since_ts: int) -> dict:
    """Run all 4 checks, build a thesis."""
    base = check_base_rate(market)
    whale = check_whale(market, target_wallets, since_ts)
    disp = check_disposition(market)

    # Best estimate: base rate from Polymarket > whale avg > disposition heuristic
    estimate = base.get("estimate") or whale.get("whale_avg_price")
    if estimate is None and disp["pass"]:
        # Use disposition bias as soft estimate
        mid = market["yes_mid"]
        bias = disp.get("expected_edge", 0.0)
        estimate = mid + bias if disp["side_hint"] == "yes" else mid - bias

    edge = check_edge_gate(market, estimate)

    checks_passed = sum([base["pass"], whale["pass"], disp["pass"], edge["pass"]])

    # Pick side: agreement among the checks that have an opinion
    side_votes = [c.get("side_hint") for c in (base, whale, disp) if c.get("side_hint") in ("yes", "no")]
    if not side_votes:
        side = None
    else:
        yes_votes = side_votes.count("yes")
        no_votes = side_votes.count("no")
        side = "yes" if yes_votes > no_votes else "no" if no_votes > yes_votes else None

    # Confidence: 3/4 checks pass AND side has majority
    if checks_passed >= 3 and side is not None:
        confidence = 0.50 + 0.10 * checks_passed + (0.05 if len(set(side_votes)) == 1 else 0)
    else:
        confidence = 0.0

    return {
        "ticker": market["ticker"],
        "title": market.get("title", ""),
        "yes_mid": market["yes_mid"],
        "yes_bid": market["yes_bid"],
        "yes_ask": market["yes_ask"],
        "hours_left": market["hours_left"],
        "estimate": estimate,
        "side": side,
        "confidence": round(min(confidence, 0.95), 3),
        "checks_passed": checks_passed,
        "base_rate": base,
        "whale": whale,
        "disposition": disp,
        "edge_gate": edge,
        "valid": checks_passed >= 3 and side is not None and confidence >= 0.75,
    }


def run() -> dict:
    if not QUEUE_FILE.exists():
        print(f"Run scanner.py first — no queue at {QUEUE_FILE}")
        return {"theses": []}
    if not TARGETS_FILE.exists():
        print(f"Run targets.py first — no targets at {TARGETS_FILE}")
        return {"theses": []}

    queue = json.loads(QUEUE_FILE.read_text())
    targets = json.loads(TARGETS_FILE.read_text())
    target_wallets = [t["wallet"] for t in targets.get("targets", [])]

    since_ts = int(datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600)  # last 7d

    markets = queue.get("markets", [])
    print(f"Evaluating {len(markets)} markets through 4-check brain...")
    print(f"  (whale check using {len(target_wallets)} target wallets)\n")

    theses = []
    for i, market in enumerate(markets, 1):
        t = evaluate(market, target_wallets, since_ts)
        tag = "✓" if t["valid"] else "·"
        print(f"  [{i}/{len(markets)}] {tag} {t['ticker']:<42} "
              f"checks={t['checks_passed']}/4  conf={t['confidence']:.2f}  "
              f"side={t['side'] or '-'}")
        if t["valid"]:
            theses.append(t)
        time.sleep(0.1)

    theses.sort(key=lambda x: x["confidence"], reverse=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_evaluated": len(markets),
        "n_valid": len(theses),
        "theses": theses,
    }

    THESIS_FILE.parent.mkdir(parents=True, exist_ok=True)
    THESIS_FILE.write_text(json.dumps(result, indent=2))

    print(f"\n{'='*60}")
    print(f"  BRAIN RESULTS")
    print(f"{'='*60}")
    print(f"  Evaluated:    {len(markets)}")
    print(f"  Valid theses: {len(theses)}")
    if theses:
        print(f"\n  Top theses:")
        for th in theses[:10]:
            print(f"    {th['ticker']:<45} "
                  f"{th['side'].upper():<3}  conf={th['confidence']:.2f}  "
                  f"est={th['estimate']:.3f}  mid={th['yes_mid']:.3f}")
    print(f"\n  Saved to {THESIS_FILE}")
    return result


if __name__ == "__main__":
    run()
