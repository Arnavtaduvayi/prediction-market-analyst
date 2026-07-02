# Prediction Market Analyst — v5 Paper Trading System

A multi-strategy paper-trading bot for Kalshi (CFTC-regulated, US-legal), with
Polymarket as a cross-venue signal source. v5 is a data-driven rebuild: six
weeks and 380 settled paper trades of v1-v4 showed that *taker* strategies on
efficient series bleed to spread+fees no matter how good the win rate looks
(disposition hit 93% and still lost money). v5 keeps only what the data and
the math support, and executes as a **maker** wherever possible.

> **Status:** Paper trading only. Live results in `paper_*_trades.json`,
> committed by GitHub Actions after every run. `CHANGELOG.md` has the full
> v4 post-mortem with the P&L decomposition behind this roster.

## The 4 bots

| Bot | Strategy | Edge thesis | Type |
|---|---|---|---|
| **S: Seller** | Sell longshots (buy NO at 85-96¢ when YES ≲ 10¢), maker entries, hold to settlement | Favorite-longshot bias — Whelan's 72M-trade study **and** our own book: +4.6%/trade over 37 sells vs −4.4%/trade over 221 favorite buys | statistical |
| **T: Theta** | Late-favorite convergence, maker entries | v1 measured the taker ask = exactly fair value (95.2% WR at 95.2% avg entry). Buying 2-3¢ *below* the ask is buying below measured fair value | execution-alpha |
| **C: Arb** | Overround Dutch-book + strike-ladder monotonicity, fee-aware | Pure math — locked spread | **math, risk-free** |
| **X: Xvenue** | Kalshi↔Polymarket verified pairs: hard arb when YES+NO < $1 all-in; otherwise rest Kalshi bids ≥3¢ inside Polymarket's fair value | Polymarket leads price discovery (deep, 1¢-spread books vs Kalshi's 20-60¢ spreads on the same event) | structural |

Each bot has its own $75 paper bankroll ($300 total).

## Why maker execution is the load-bearing change

Kalshi taker fee: `ceil(0.07·C·P·(1−P))`; maker fee is **25% of that** (June
2026 schedule). On a $0.95 favorite the taker pays the ask *plus* ~1¢/contract
— that combination single-handedly turned two positive-signal bots negative in
v1-v4. Resting one tick above the bid instead flips the spread from a cost to
an income.

Paper fills are simulated **pessimistically**: a resting bid at `L` counts as
filled only if a later trade prints *strictly through* `L` (price priority
guarantees our order would have filled first, regardless of queue position).
A print at exactly `L` does not count. Paper maker P&L is therefore a floor.

## Cross-venue pair discipline (the v0 lesson)

`data/xvenue_pairs.json` is the only source of Kalshi↔Polymarket pairs, and
every entry is human-verified for resolution equivalence (same source, same
number). No fuzzy matching — v0 lost money on "semantically similar but
different" markets, and curation found live traps (Kalshi `KXCPIYOY` is
headline CPI; Polymarket's monthly event is *core* CPI — not a pair).
`python3 bot_xvenue.py propose` suggests candidates for human review only.

## Architecture

```
  scanner.py → data/queue.json   (vol ≥$5k, depth $100/side OR 100 contracts,
        │                         2h-7d, no sports)
        ▼
  ┌────────────┬────────────┐
  ▼            ▼            ▼ (own discovery)
 seller       theta        arb (Kalshi /events)
 (S)          (T)          xvenue (pair map + Polymarket Gamma API)
        │            │            │
        ▼            ▼            ▼
  paper_seller_  paper_theta_  paper_arb_ / paper_xvenue_
  trades.json    trades.json   trades.json
                     │
                     ▼
            ┌─────────────────┐   hourly on all 4 journals:
            │  exit_monitor   │   resting-fill checks, TARGET_HIT /
            │      .py        │   STOP_LOSS / SETTLED, arb-pair locks,
            └─────────────────┘   exit-side taker fees charged

  botlib.py — fee math (taker + maker), resting-order lifecycle,
              pessimistic fill simulation, sizing, journals
```

## Universal rules

- **Cooldown** (`cooldown.py`): no re-entry for 24h after a non-resolution exit. Expired (unfilled) quotes do NOT trigger cooldown — re-quoting is normal.
- **Fee-aware**: taker `0.07·C·P·(1−P)` and maker `0.0175·C·P·(1−P)` on Kalshi; Polymarket US taker `0.06·C·P·(1−P)` modeled on xvenue's poly legs. Exits pay the taker fee too.
- **Correlation guards**: seller takes one position per *event* (ladder strikes are perfectly correlated) and caps positions per series.
- **Position caps + quarter-Kelly** with per-trade caps (arb/xvenue-lock highest — risk-free).

## Usage

```bash
python3 paper_cross.py signal      # scanner + all 4 bots
python3 paper_cross.py exit        # resting fills + exits + settlements
python3 paper_cross.py status      # scorecard (+ --json)
python3 paper_cross.py cancel <reason>

python3 bot_xvenue.py propose      # candidate pairs for HUMAN review

python3 -m unittest discover -s tests -t .   # 48 tests, no network
```

## GitHub Actions

| Workflow | Schedule | Job |
|---|---|---|
| `paper-signal.yml` | Hourly at :07 | Full 4-bot pipeline |
| `paper-exit.yml` | Hourly at :23 | Resting fills + exits + settlements |

## Honest expectations

Only **arb** and **xvenue hard-arb** trades are loss-proof, and they are rare
at hourly cadence. Seller and theta are positive-expectation bets on a
documented bias plus a cheaper execution path — the paper phase exists to
measure whether maker fills suffer adverse selection worse than the entry
improvement. Nothing here is a guarantee; the system is built so that *if* it
loses, the journals say exactly which assumption failed.

## Retired strategies (full history in `/legacy`)

- **Whale-copy** (v1-v4): −0.3% over 93 settled; 48.4% WR vs 53.7% breakeven — no edge.
- **Disposition** (v2-v4): −11.7%; its favorite_buy leg (−4.4%/trade, n=221) was the entire loss; its longshot_sell leg (+4.6%/trade, n=37) was promoted to Bot S.
- **Reversion** (v4): −28.8%. Fading moves fights real information.
- **Consensus** (v4): +0.9% on n=3; its input signals were retired underneath it.
- **Weather** (v4): −45%, 31% WR before and after recalibration.
- **Longshot-buy** (built, never deployed): buying longshots is the documented −EV side; superseded by Bot S selling them.
- Older: Calendar Arb (0 trades), Spot Convergence (−39%), Flow Momentum (−28%), v0 weather + naive cross-matcher.
