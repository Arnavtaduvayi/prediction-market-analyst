"""
bot_reversion.py — Bot E: Mean-reversion on thin-volume overreactions (balanced)

The dead Flow bot bet WITH heavy one-sided volume and got run over when the
momentum inverted. This is the deliberate complement: when price has jumped away
from its recent volume-weighted average but on BELOW-average volume (i.e. nobody
informed is trading — it's noise / a stale quote, not news), fade it back toward
the anchor.

  - anchor   = VWAP of the last few hours of trades
  - signal   = |yes_mid - VWAP| > threshold AND recent volume < baseline
  - trade    = bet toward the anchor
  - exit     = TARGET_HIT at the anchor, or STOP_LOSS if it keeps running, plus
               the standard VOLUME_EXIT / STALE_THESIS triggers.

The stop-loss is the key discipline a fade strategy needs — a wrong reversion
bet must be cut, never ridden to settlement.
"""

import json
import time
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    kalshi_fee, kelly_size, recent_trades, vwap,
    load_journal, save_journal, open_trades, new_trade,
    CONTINUOUS_PRICE_PREFIXES,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_reversion_trades.json"
STRATEGY = "reversion"

WINDOW_MIN = 240             # 4h VWAP anchor
DEV_THRESHOLD = 0.08        # mid must be ≥8¢ from anchor
TARGET_FRACTION = 1.0        # exit when fully reverted to anchor
STOP_DIST = 0.06            # cut if it runs another 6¢ against us
KELLY_MULT = 0.20
PER_TRADE_CAP_PCT = 0.05
MAX_OPEN_POSITIONS = 6
MIN_EDGE_DOLLARS = 0.02


def run():
    if not QUEUE_FILE.exists():
        print("[reversion] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])
    if not markets:
        print("[reversion] No markets in queue")
        return

    data = load_journal(JOURNAL_FILE, STRATEGY)
    all_trades = data["trades"]
    held = {t["kalshi_ticker"] for t in open_trades(data)}
    slots = max(0, MAX_OPEN_POSITIONS - len(held))

    print(f"[reversion] Scanning {len(markets)} markets for thin-volume overreactions...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if ticker.startswith(CONTINUOUS_PRICE_PREFIXES):
            continue  # price-driven underlying: moves are information, not noise
        if ticker in held or is_in_cooldown(ticker, all_trades):
            continue
        trades = recent_trades(ticker, WINDOW_MIN)
        time.sleep(0.1)
        anchor = vwap(trades)
        if anchor is None:
            continue
        mid = m.get("yes_mid", 0)
        if mid <= 0 or mid >= 1:
            continue
        dev = mid - anchor
        if abs(dev) < DEV_THRESHOLD:
            continue
        # require CALM tape: recent window volume below its 24h-implied baseline
        recent_usd = sum(float(t.get("count_fp") or 0) * float(t.get("yes_price_dollars") or 0)
                         for t in trades)
        baseline_usd = m.get("volume_24h_usd", 0) * (WINDOW_MIN / 1440)
        if baseline_usd > 0 and recent_usd > baseline_usd:
            continue  # this is a volume spike, not a stale overreaction

        if dev > 0:   # price spiked up → fade down → buy NO
            side, fill = "no", round(1.0 - m.get("yes_bid", 0), 4)
        else:         # price dropped → fade up → buy YES
            side, fill = "yes", round(m.get("yes_ask", 0), 4)
        if fill <= 0 or fill >= 1:
            continue
        p_win = min(0.50 + (abs(dev) - DEV_THRESHOLD), 0.62)
        edge = p_win - fill - kalshi_fee(fill, 1)
        if edge < MIN_EDGE_DOLLARS:
            continue
        candidates.append({
            "ticker": ticker, "title": m.get("title", ""), "side": side,
            "fill": fill, "p_win": p_win, "mid": mid, "anchor": round(anchor, 4),
            "dev": round(dev, 4), "edge": round(edge, 3),
        })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    print(f"[reversion] {len(candidates)} fade candidates")

    placed = 0
    for c in candidates:
        if placed >= slots:
            break
        kf = kelly_size(c["p_win"], c["fill"], KELLY_MULT, PER_TRADE_CAP_PCT)
        if kf <= 0:
            continue
        contracts = max(1, int((kf * data["bankroll"]) / c["fill"]))
        fee = kalshi_fee(c["fill"], contracts)
        cost = round(contracts * c["fill"] + fee, 2)
        if cost > data["bankroll"] or cost < 0.30:
            continue
        if c["side"] == "yes":
            stop = round(c["mid"] - STOP_DIST, 4)
        else:
            stop = round(c["mid"] + STOP_DIST, 4)
        trade = new_trade(
            c["ticker"], c["title"], c["side"], contracts, c["fill"], STRATEGY,
            fee=fee, yes_mid_at_entry=c["mid"],
            target_yes_mid=c["anchor"], stop_yes_mid=stop,
            anchor_vwap=c["anchor"], deviation_at_entry=c["dev"],
        )
        data["bankroll"] -= cost
        data["trades"].append(trade)
        placed += 1
        print(f"  [reversion] {c['ticker']:<40} {c['side'].upper()} {contracts}x @ ${c['fill']:.3f}  "
              f"mid={c['mid']:.3f} anchor={c['anchor']:.3f} dev={c['dev']:+.3f}")

    save_journal(data, JOURNAL_FILE)
    print(f"  [reversion] {placed} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
