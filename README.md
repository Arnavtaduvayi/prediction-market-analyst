# Prediction Market Analyst — Multi-Agent v2

A four-agent paper-trading bot that mirrors top Polymarket whales onto
Kalshi markets. Inspired by the LunarResearcher / `warproxxx/poly_data`
methodology, adapted to use Kalshi for US-resident execution.

> **v1 (weather strategy, naive cross-platform matcher) lives in `/legacy`.**

## Architecture

```
┌─────────────┐  weekly  ┌──────────┐  daily   ┌─────────┐  daily   ┌──────────┐
│  targets.py │ ───────► │ scanner  │ ───────► │  brain  │ ───────► │ executor │
└─────────────┘          │   .py    │          │   .py   │          │   .py    │
   ↓ 100+ trades         │ scores   │          │ 4 checks│          │ consensus│
   ↓ 70%+ WR             │ Kalshi   │          │ + thesis│          │ + Kelly  │
   ↓                     └──────────┘          └─────────┘          └────┬─────┘
   targets.json           queue.json            thesis.json              │
                                                                          ▼
                                                                  paper_cross_trades.json
                                                                          ▲
                                                                          │
                                                                  ┌───────┴──────┐
                                                                  │ exit_monitor │  hourly
                                                                  │     .py      │
                                                                  └──────────────┘
                                                                   • TARGET_HIT (85%)
                                                                   • VOLUME_EXIT (3×)
                                                                   • STALE_THESIS (24h)
                                                                   • SETTLED
```

## Filters (kill markets that would lose money)

**Whale targets (targets.py):**
- ≥ 100 lifetime Polymarket trades
- ≥ 70% win rate (computed from closed-position realized PnL)
- Sorted by total PnL, top 50 saved

**Market scanner (scanner.py) — kills ≥90%:**
- Min depth: $500 on each side of book
- Min 24h volume: $10,000
- Time-to-resolution: 4h to 7d
- **No sports markets** (52% WR proven in source methodology)

**Brain (brain.py) — 4 checks per surviving market:**
1. Base rate (matching Polymarket price)
2. Whale presence (top 20 wallets active in equivalent market)
3. Disposition bias (longshot bias <$0.15, favorite under-pricing >$0.85)
4. Edge gate (net edge > spread + 2% slippage)

Requires **3/4 checks pass** AND ≥75% confidence to generate a thesis.

**Executor (executor.py):**
- 2-3 source checks agree → full Quarter-Kelly position
- 1 source check only → half position
- Hard cap: 10% of bankroll per trade, 5 max open positions

**Exit monitor (exit_monitor.py) — runs hourly:**
- `TARGET_HIT`: Kalshi price hits 85% of expected move
- `VOLUME_EXIT`: 10-min volume > 3× baseline (smart money leaving)
- `STALE_THESIS`: 24h open + < 2% price move (thesis didn't play out)
- `SETTLED_*`: fallback if all triggers missed and market resolved

## Usage

```bash
# Build the whale target list (takes ~3 minutes)
python3 targets.py --candidates 100

# Run the full daily signal pipeline
python3 paper_cross.py signal

# Run exit checks (do this every hour or so)
python3 paper_cross.py exit

# Check scorecard
python3 paper_cross.py status

# Cancel all open paper trades (e.g., after a code change)
python3 paper_cross.py cancel reason-here
```

## Production deployment — VPS (recommended)

**See [docs/VPS.md](docs/VPS.md) for the full 10-minute deploy.**

One command on a fresh Ubuntu VPS sets up 4 long-running systemd services
that mirror the LunarResearcher architecture exactly:

```bash
curl -fsSL https://raw.githubusercontent.com/Arnavtaduvayi/prediction-market-analyst/main/scripts/deploy.sh | sudo bash
```

| Service | Interval | Purpose |
|---|---|---|
| `predmkt-scanner` | 300s | Score Kalshi markets |
| `predmkt-brain` | 360s | Evaluate survivors |
| `predmkt-executor` | 420s | Consensus + Kelly sizing |
| `predmkt-exit` | **60s** | Catch volume spikes / targets in near-real-time |
| `predmkt-targets.timer` | weekly | Refresh whale list |
| `predmkt-commit.timer` | hourly | Push journal state to GitHub |

Total cost: **$5/month** (Hetzner CX22).

## GitHub Actions (fallback / manual override)

Workflows still exist but `schedule` triggers are disabled — only manual
`workflow_dispatch` runs. Use these only if not yet on a VPS, or for
ad-hoc one-off runs.

## Inspiration / source repos

- `warproxxx/poly_data` — the dataset behind whale identification
- `Polymarket/agents` — the official agent framework
- LunarResearcher methodology — multi-agent + exit-trigger approach

## v1 (legacy)

The v1 code (weather temperature strategy + naive cross-platform matcher)
is preserved in `/legacy` for reference. It was retired after 8 settled
trades returned -$0.91 — too many false matches, no exit triggers.
