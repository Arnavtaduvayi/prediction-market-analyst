"""
bot_consensus.py — Bot G: Multi-signal consensus (balanced / ensemble)

Each individual signal is noisy. This bot only acts when independent signals
AGREE, which filters out most of the false positives that sink single-signal
strategies. It computes up to three votes per market and trades only when ≥2
point the same way with none pointing the other:

  1. Disposition  — favorite (mid>0.90 → YES) / longshot (mid<0.10 → NO)
  2. Order flow   — taker imbalance ≥70% one side
  3. Reversion    — mid far from VWAP anchor → fade

Size scales with how many signals agree. Exits on TARGET_HIT / STOP_LOSS plus
the standard volume/stale triggers.
"""

import json
import time
from pathlib import Path

from cooldown import is_in_cooldown
from botlib import (
    kalshi_fee, kelly_size, recent_trades, vwap, flow_imbalance,
    load_journal, save_journal, open_trades, new_trade,
)

QUEUE_FILE = Path(__file__).parent / "data" / "queue.json"
JOURNAL_FILE = Path(__file__).parent / "paper_consensus_trades.json"
STRATEGY = "consensus"

WINDOW_MIN = 60
FLOW_SHARE = 0.70
DEV_THRESHOLD = 0.08
MIN_AGREE = 2
TARGET_MOVE = 0.05
STOP_MOVE = 0.06
KELLY_MULT = 0.25
PER_TRADE_CAP_PCT = 0.05
MAX_OPEN_POSITIONS = 6
MIN_EDGE_DOLLARS = 0.02


def votes_for(m: dict, trades: list[dict]) -> dict:
    """Return {'yes': n, 'no': n} across the three sub-signals."""
    mid = m.get("yes_mid", 0)
    tally = {"yes": 0, "no": 0}

    # 1) disposition zone
    if mid > 0.90:
        tally["yes"] += 1
    elif 0 < mid < 0.10:
        tally["no"] += 1

    # 2) order-flow imbalance
    fi = flow_imbalance(trades)
    if fi:
        if fi["yes_share"] >= FLOW_SHARE:
            tally["yes"] += 1
        elif fi["yes_share"] <= (1 - FLOW_SHARE):
            tally["no"] += 1

    # 3) reversion vs VWAP
    anchor = vwap(trades)
    if anchor is not None:
        if mid - anchor > DEV_THRESHOLD:
            tally["no"] += 1     # spiked up → fade down
        elif anchor - mid > DEV_THRESHOLD:
            tally["yes"] += 1
    return tally


def run():
    if not QUEUE_FILE.exists():
        print("[consensus] No queue — run scanner.py first")
        return
    markets = json.loads(QUEUE_FILE.read_text()).get("markets", [])
    if not markets:
        print("[consensus] No markets in queue")
        return

    data = load_journal(JOURNAL_FILE, STRATEGY)
    all_trades = data["trades"]
    held = {t["kalshi_ticker"] for t in open_trades(data)}
    slots = max(0, MAX_OPEN_POSITIONS - len(held))

    print(f"[consensus] Scanning {len(markets)} markets for signal agreement...")
    candidates = []
    for m in markets:
        ticker = m["ticker"]
        if ticker in held or is_in_cooldown(ticker, all_trades):
            continue
        mid = m.get("yes_mid", 0)
        if mid <= 0 or mid >= 1:
            continue
        trades = recent_trades(ticker, WINDOW_MIN)
        time.sleep(0.1)
        tally = votes_for(m, trades)

        if tally["yes"] >= MIN_AGREE and tally["no"] == 0:
            side, fill, agree = "yes", round(m.get("yes_ask", 0), 4), tally["yes"]
        elif tally["no"] >= MIN_AGREE and tally["yes"] == 0:
            side, fill, agree = "no", round(1.0 - m.get("yes_bid", 0), 4), tally["no"]
        else:
            continue
        if fill <= 0 or fill >= 1:
            continue
        p_win = min(0.55 + 0.04 * (agree - MIN_AGREE), 0.66)
        edge = p_win - fill - kalshi_fee(fill, 1)
        if edge < MIN_EDGE_DOLLARS:
            continue
        candidates.append({
            "ticker": ticker, "title": m.get("title", ""), "side": side,
            "fill": fill, "p_win": p_win, "mid": mid, "agree": agree,
            "edge": round(edge, 3),
        })

    candidates.sort(key=lambda c: (c["agree"], c["edge"]), reverse=True)
    print(f"[consensus] {len(candidates)} markets with ≥{MIN_AGREE} agreeing signals")

    placed = 0
    for c in candidates:
        if placed >= slots:
            break
        kf = kelly_size(c["p_win"], c["fill"], KELLY_MULT, PER_TRADE_CAP_PCT)
        if kf <= 0:
            continue
        # scale stake up with agreement strength
        kf *= 1.0 + 0.5 * (c["agree"] - MIN_AGREE)
        contracts = max(1, int((kf * data["bankroll"]) / c["fill"]))
        fee = kalshi_fee(c["fill"], contracts)
        cost = round(contracts * c["fill"] + fee, 2)
        if cost > data["bankroll"] or cost < 0.30:
            continue
        if c["side"] == "yes":
            target, stop = round(c["mid"] + TARGET_MOVE, 4), round(c["mid"] - STOP_MOVE, 4)
        else:
            target, stop = round(c["mid"] - TARGET_MOVE, 4), round(c["mid"] + STOP_MOVE, 4)
        trade = new_trade(
            c["ticker"], c["title"], c["side"], contracts, c["fill"], STRATEGY,
            fee=fee, yes_mid_at_entry=c["mid"],
            target_yes_mid=target, stop_yes_mid=stop, signals_agree=c["agree"],
        )
        data["bankroll"] -= cost
        data["trades"].append(trade)
        placed += 1
        print(f"  [consensus] {c['ticker']:<40} {c['side'].upper()} {contracts}x @ ${c['fill']:.3f}  "
              f"agree={c['agree']}  mid={c['mid']:.3f}")

    save_journal(data, JOURNAL_FILE)
    print(f"  [consensus] {placed} new trades. Bankroll: ${data['bankroll']:.2f}")


if __name__ == "__main__":
    run()
