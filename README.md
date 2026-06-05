# Prediction Market Analyst — 5-Bot Paper Trading System

A multi-strategy paper-trading bot for Kalshi (CFTC-regulated, US-legal).
Runs five completely different strategies side-by-side to discover what
actually has edge.

> **Status:** Paper trading only. Currently testing 5 strategies in parallel.
> See live results in `paper_*_trades.json` files — workflows commit them
> after every run.

## The 5 bots

| Bot | Strategy | Edge thesis | Source |
|---|---|---|---|
| **A: Whale-Copy** | Follow top Polymarket whale trades to Kalshi equivalents | Polymarket leads price discovery (academic) | `warproxxx/poly_data` methodology |
| **B: Disposition** | Buy heavy favorites (YES > $0.90) / sell extreme longshots (< $0.10), hold to settlement | Whelan paper on 72M Kalshi trades | `karlwhelan.com` |
| **C: Calendar Arb** | Find strike-monotonicity violations on multi-strike markets, lock in risk-free spread | Pure math, no prediction | Original |
| **D: Spot Convergence** | Compare Kalshi BTC prices to actual BTC spot via lognormal model | Real price data must converge at settlement | CoinGecko + Black-Scholes |
| **E: Flow Momentum** | Detect 70%+ taker imbalance with thin price move, bet WITH the flow | Microstructure / informed trading | Standard OFI research |

Each bot has its own $75 paper bankroll. Combined paper-mode allocation: $375.

## Architecture

```
                       ┌────────────────┐
                       │   scanner.py   │  ← filters Kalshi universe
                       │  → queue.json  │     (depth ≥$100, vol ≥$5k,
                       └────────┬───────┘      hours 2-168, no sports)
                                │
       ┌────────────┬───────────┼───────────┬────────────┐
       ▼            ▼           ▼           ▼            ▼
  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
  │  brain  │ │disposition│ │bot_cal-  │ │bot_spot  │ │bot_flow  │
  │   .py   │ │   .py    │ │endar.py  │ │   .py    │ │   .py    │
  └────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘
       │           │             │            │            │
       ▼           ▼             ▼            ▼            ▼
  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
  │executor │ │executor_ │ │ (signal+ │ │ (signal+ │ │ (signal+ │
  │   .py   │ │ disp.py  │ │executor) │ │executor) │ │executor) │
  └────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘
       │           │             │            │            │
       ▼           ▼             ▼            ▼            ▼
   paper_cross  paper_disp   paper_cal-   paper_spot  paper_flow
   _trades     _trades      endar_trades _trades     _trades
   .json       .json        .json        .json       .json
                                                          ▲
                                                          │
                          ┌───────────────────────────────┘
                          │
                  ┌───────┴───────┐
                  │ exit_monitor  │  ← runs hourly on all 5 journals
                  │     .py       │     TARGET_HIT / VOLUME_EXIT /
                  └───────────────┘     STALE_THESIS / SETTLED
```

## Universal rules (apply to all bots)

- **Cooldown** (`cooldown.py`): no re-entering the same Kalshi ticker for 24h after a non-resolution exit. Prevents bid-ask spread bleed from re-entry loops.
- **Position cap**: each bot has its own MAX_OPEN_POSITIONS (5-12 depending on bot)
- **Kelly sizing**: Quarter-Kelly with strategy-specific per-trade caps (3-10% of bankroll)
- **Scanner filter**: shared across all bots — depth ≥$100/side, 24h vol ≥$5k, 2h-7d to resolution, no sports

## Usage

```bash
# Run the full 5-bot signal pipeline
python3 paper_cross.py signal

# Run exit checks across all 5 journals
python3 paper_cross.py exit

# Side-by-side scorecard
python3 paper_cross.py status

# JSON output (for piping/analytics)
python3 paper_cross.py status --json

# Cancel all open positions in all bots
python3 paper_cross.py cancel <reason>

# Refresh Polymarket whale list (weekly)
python3 targets.py --candidates 150
```

## GitHub Actions (current setup)

Three workflows run in the cloud — laptop independence:

| Workflow | Schedule | Job |
|---|---|---|
| `paper-signal.yml` | Every hour at :07 | Full 5-bot pipeline |
| `paper-exit.yml` | Every hour at :23 | Exit triggers + settlements |
| `paper-targets.yml` | Sundays 12:37 UTC | Refresh whale list |

All journals committed back to repo on every run.

## Production deployment — VPS

For continuous monitoring with sub-minute polling, see [`docs/VPS.md`](docs/VPS.md). One-command deploy on Hetzner CX22 ($4.55/mo).

## Current results

Live numbers in each `paper_*_trades.json`. Updated by GitHub Actions after every workflow run. The `CHANGELOG.md` tracks major findings.

## v1 (legacy)

The v1 code (weather temperature strategy + naive cross-platform matcher)
is preserved in `/legacy` for reference. It was retired after 8 settled
trades returned -$0.91 — too many false matches, no exit triggers.
