# Prediction Market Analyst — 7-Bot Paper Trading System

A multi-strategy paper-trading bot for Kalshi (CFTC-regulated, US-legal).
Runs seven completely different strategies side-by-side to discover what
actually has edge.

> **Status:** Paper trading only. Currently testing 7 strategies in parallel.
> See live results in `paper_*_trades.json` files — workflows commit them
> after every run.

## The 7 bots

| Bot | Strategy | Edge thesis | Type |
|---|---|---|---|
| **A: Whale-Copy** | Follow top Polymarket whale trades to Kalshi equivalents | Polymarket leads price discovery (academic) | follow |
| **B: Disposition** | Buy heavy favorites (YES > $0.90) / sell extreme longshots (< $0.10), hold to settlement | Whelan paper on 72M Kalshi trades | statistical |
| **C: Arb** | Overround Dutch-book across mutually-exclusive events + strike-ladder monotonicity, all fee-aware | Pure math — locked spread, can't lose | **math, risk-free** |
| **D: Weather** | NWS probabilistic forecast vs Kalshi temperature brackets (5 cities), bet material divergence | Short-range forecast skill beats thin temp markets | **data edge** |
| **E: Reversion** | Fade price moves away from VWAP on *below-average* volume (overreaction, not news) | Mean reversion of noise; stop-loss protected | balanced |
| **F: Theta** | Buy near-certain favorites in their final window, hold to settlement; never longshots | Late favorites converge to $1 slowly | balanced |
| **G: Consensus** | Trade only when ≥2 independent signals (disposition/flow/reversion) agree | Signal agreement filters false positives | balanced/ensemble |

Each bot has its own $75 paper bankroll. Combined paper-mode allocation: $525.

### Two correctness guards worth knowing (learned the hard way in live testing)
- **Arb only sells overround / trades ladders — never buys all YES (underround).**
  Kalshi's `mutually_exclusive` flag means *at most one* outcome wins, not that
  one of the *listed* outcomes must (a decided election's winner can drop off the
  list). Buy-all is only risk-free if collectively exhaustive, which isn't
  guaranteed; sell-all and ladder arbs are safe regardless.
- **Weather only trades future-day markets (lead ≥ 1).** A same-day high is
  mostly set by afternoon, so the market is near-settled and any "divergence"
  from our forecast is illusory.

## Architecture

```
  scanner.py → data/queue.json   (depth ≥$100, vol ≥$5k, 2h–7d, no sports)
        │  shared by the queue-driven bots (B, E, F, G)
        ▼
  ┌────────────┬────────────┬────────────┬────────────┐
  ▼            ▼            ▼            ▼            ▼
 brain+       disposition  reversion    theta       consensus
 executor     +exec_disp   (E)          (F)         (G)
 (A)          (B)
                                                    arb (C) ─┐  own discovery:
                                                    weather(D)┘  /events, /markets
        │            │            │            │            │
        ▼            ▼            ▼            ▼            ▼
   paper_cross   paper_       paper_       paper_       paper_arb /
   _trades       disposition  reversion    theta        paper_weather /
   .json         _trades      _trades      _trades      paper_consensus ...
                                   │
                                   ▼
                          ┌─────────────────┐
                          │  exit_monitor   │  hourly on all 7 journals:
                          │     .py         │  TARGET_HIT / STOP_LOSS /
                          └─────────────────┘  VOLUME_EXIT / STALE / SETTLED

  botlib.py — shared fee math, sizing, journal + microstructure helpers (C–G)
```

Bots **A, B, E, F, G** consume the shared `queue.json`. Bots **C (arb)** and
**D (weather)** do their own discovery (Kalshi `/events` and temperature
`/markets`, plus `api.weather.gov`) because their opportunities live outside the
filtered queue.

## Universal rules (apply to all bots)

- **Cooldown** (`cooldown.py`): no re-entering the same Kalshi ticker for 24h after a non-resolution exit (incl. STOP_LOSS). Prevents bid-ask spread bleed from re-entry loops.
- **Fee-aware** (`botlib.kalshi_fee`): every new bot prices Kalshi's `ceil(0.07·C·P·(1-P))` fee into cost, so no phantom edges.
- **Position cap**: each bot has its own MAX_OPEN_POSITIONS (6-12 depending on bot)
- **Kelly sizing**: Quarter-Kelly with strategy-specific per-trade caps (4-20% of bankroll; arb highest since it's risk-free)
- **Scanner filter**: shared by the queue-driven bots — depth ≥$100/side, 24h vol ≥$5k, 2h-7d to resolution, no sports

## Usage

```bash
# Run the full 7-bot signal pipeline
python3 paper_cross.py signal

# Run exit checks across all 7 journals
python3 paper_cross.py exit

# Side-by-side scorecard (one row per bot) + --json variant
python3 paper_cross.py status
python3 paper_cross.py status --json

# Run a single new bot standalone (C/D self-discover; E/F/G need a fresh queue)
python3 bot_arb.py        # or bot_weather.py / bot_reversion.py / bot_theta.py / bot_consensus.py

# Unit tests for the math + risk logic (no network)
python3 -m unittest discover -s tests -t .

# Cancel all open positions in all bots
python3 paper_cross.py cancel <reason>

# Refresh Polymarket whale list (weekly)
python3 targets.py --candidates 150
```

## GitHub Actions (current setup)

Three workflows run in the cloud — laptop independence. They call
`paper_cross.py signal`/`exit`, so the 7-bot roster is picked up automatically;
no workflow edits were needed for the v2 roster.

| Workflow | Schedule | Job |
|---|---|---|
| `paper-signal.yml` | Every hour at :07 | Full 7-bot pipeline |
| `paper-exit.yml` | Every hour at :23 | Exit triggers + settlements |
| `paper-targets.yml` | Sundays 12:37 UTC | Refresh whale list |

All journals committed back to repo on every run.

## Production deployment — VPS

For continuous monitoring with sub-minute polling, see [`docs/VPS.md`](docs/VPS.md). One-command deploy on Hetzner CX22 ($4.55/mo).

## Current results

Live numbers in each `paper_*_trades.json`. Updated by GitHub Actions after every workflow run. The `CHANGELOG.md` tracks major findings.

## Retired bots (legacy)

Preserved in `/legacy` with their full trade history for reference:

- **v1** — weather temperature strategy + naive cross-platform matcher. Retired
  after 8 settled trades returned -$0.91 (too many false matches, no exits).
- **Calendar Arb** (`bot_calendar.py`) — 0 trades its entire run; the one arb
  condition it checked never appeared. Superseded by the broader **Arb (C)** bot.
- **Spot Convergence** (`bot_spot.py`) — -39% (2W/21L). The BTC lognormal
  fair-value model was systematically on the wrong side.
- **Flow Momentum** (`bot_flow.py`) — -28% (63W/90L). Briefly +12%, then the
  momentum edge inverted. The new **Reversion (E)** bot takes the opposite
  (mean-reverting) stance on *thin* volume.
