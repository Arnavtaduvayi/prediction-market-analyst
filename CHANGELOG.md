# Changelog

A running log of the bot's evolution. Each iteration is a real lesson learned
from paper-trading on Kalshi.

---

## v4 — 7-bot portfolio (current, 2026-06-23)

Retired the three losing/idle strategies and replaced them with five new bots
chosen as 1 math-heavy + 1 data-edge + 3 balanced. Kept the two least-bad
performers (Whale-Copy, Disposition). Combined allocation now 7 × $75 = $525.

**Retired → `/legacy`** (history preserved):
- **Calendar Arb** — 0 trades all run; its single arb condition never appeared.
- **Spot Convergence** — −39% (2W/21L); the BTC lognormal model was systematically wrong.
- **Flow Momentum** — −28% (63W/90L); briefly +12% then the momentum edge inverted.

**New bots:**
- **C: Arb** (`bot_arb.py`) — fee-aware, risk-free. Overround Dutch-book across
  mutually-exclusive events + same-direction strike-ladder monotonicity.
- **D: Weather** (`bot_weather.py` + `weather_data.py`) — NWS forecast vs Kalshi
  temperature brackets (5 cities), Normal(μ,σ) bracket model, future-day only.
- **E: Reversion** (`bot_reversion.py`) — fade moves away from VWAP on
  below-average volume; STOP_LOSS protected.
- **F: Theta** (`bot_theta.py`) — buy late near-certain favorites (never
  longshots), hold to settlement.
- **G: Consensus** (`bot_consensus.py`) — trade only when ≥2 of
  {disposition, flow, reversion} agree.

**Infra:** new `botlib.py` (shared fee/sizing/journal/microstructure helpers),
a `STOP_LOSS` + side-aware target/stop in `exit_monitor.py`, a transposed
one-row-per-bot scorecard, and a `tests/` suite (34 unit tests, no network).

### Two correctness lessons from live testing
1. **`mutually_exclusive` ≠ collectively exhaustive.** The arb bot's first live
   run "found" a 62¢/contract underround on the *already-decided* 2025 Pope
   event — buying all listed YES is only safe if one of them must win. Dropped
   the underround path; kept overround + ladder, which are safe regardless.
2. **Same-day temperature markets are near-settled.** Weather's first run bet
   *against* 94%-priced same-day brackets whose highs had likely already
   occurred. Restricted to future-day markets (lead ≥ 1) where forecast skill
   still beats a thin market.

### Honest note on "flawless"
Only the **arb** bot is loss-proof (and therefore often idle — real arbs are
rare). The rest are positive-expectation, not guaranteed. "Robustness" here
means fee-aware math, unit tests, stop-losses, position caps, and
longshot/staleness guards — not a promise of profit.

---

## v3 — 5-bot portfolio

Added three more strategies running side-by-side with the original two.
All 5 bots share the scanner output and write to independent journals.

### Bot C: Calendar/Strike Arbitrage (`bot_calendar.py`)
Pure structural arb. For multi-strike markets (e.g., BTC daily contracts at
different price thresholds), enforces monotonicity: P(BTC > $X) must be ≥
P(BTC > $X+1). When Kalshi violates this (thin order books cause it
occasionally), buy YES on the cheap leg + buy NO on the expensive leg.
Locks in profit regardless of outcome.

### Bot D: BTC Spot Convergence (`bot_spot.py`)
Pulls real BTC spot price from CoinGecko (free, no auth). For each KXBTCD
market, computes fair-value YES probability using a lognormal model
(spot, strike, time-to-resolution, 55% annualized vol). When Kalshi diverges
>12% from fair value, bet on convergence. Holds to settlement.

### Bot E: Order Flow Momentum (`bot_flow.py`)
For each surviving Kalshi market, fetches last 30min of trades and computes
taker-side imbalance. When >70% one direction with >$200 volume AND price
hasn't already responded (<5% move), enter WITH the flow. Tight exit targets.

### Orchestration changes
- `paper_cross.py status` now shows a 5-column scorecard
- `exit_monitor.py` iterates over all 5 journals
- Workflows commit all 5 trade journals on every run
- Cooldown is shared across bots — a Bot A exit blocks Bot B re-entry on the same ticker

---

## v2.1 — 24h same-ticker cooldown

Added `cooldown.py`. Prevents re-entering a ticker within 24h of a
non-resolution exit (STALE_THESIS, VOLUME_EXIT, TARGET_HIT, cancelled).
SETTLED_WIN/LOSS don't trigger cooldown since the market is already closed.

**Why it matters**: previously Bot A would exit a market via STALE_THESIS
at break-even, then re-enter the same market in the next run. Each round
trip paid the bid-ask spread for nothing. The cooldown cuts this loop.

---

## v2 — 2-bot multi-strategy

Restructured from a single whale-copy approach to two parallel bots:

### Bot A: Whale-copy (existing, refined)
4-check brain (base rate / whale / disposition / edge gate). Quarter-Kelly
sizing. Exit triggers: TARGET_HIT (85% of expected move), VOLUME_EXIT
(3× baseline volume spike), STALE_THESIS (24h flat).

### Bot B: Disposition (new)
Pure systematic strategy based on Whelan / GWU 72M-trade study. Buy heavy
favorites (YES > $0.90, ~2% expected edge per Whelan), sell extreme
longshots (YES < $0.10, ~4% expected edge). Hold to settlement — exiting
early forfeits the bias.

### Brain fixes (resolved trading bugs)
1. Base rate becomes primary signal when Polymarket-Kalshi gap > 15%
2. When checks disagree on side, default to base-rate direction
3. Base + whale agreement = high confidence regardless of vote count

These fixes resolved cases where the bot wanted to "buy YES at $0.985"
when Polymarket implied only 65% probability — Kelly killed the trade
correctly but the brain was wrong about direction.

---

## v1.5 — Filter loosening + hourly cadence

Scanner thresholds (calibrated from "0 trades/day" → "3-5 trades/day"):
- Min depth: $500/side → **$100/side**
- Min 24h volume: $10k → **$5k**
- Min gap: 7% → 5%
- Min hours-to-resolution: 4 → 2

Brain valid threshold: 0.75 → **0.55** confidence floor. Allow 2/4 checks
to pass if side is unanimous (was 3/4 required).

Signal cadence: daily 13:17 UTC → **hourly at :07** (effectively also gives
us hourly whale-activity monitoring since brain.py re-fetches top wallets
on every run).

---

## v1 — Initial multi-agent whale-copy bot

Built the 4-agent LunarResearcher-style pipeline:
- `targets.py` — Polymarket whale identification (≥100 trades, ≥70% WR)
- `scanner.py` — Kalshi market scoring (depth/volume/time filters, no sports)
- `brain.py` — 4-check per-market evaluation
- `executor.py` — consensus voting + Quarter-Kelly sizing
- `exit_monitor.py` — TARGET_HIT / VOLUME_EXIT / STALE_THESIS / SETTLED

Inspired by the LunarResearcher post and `warproxxx/poly_data` data approach,
but adapted to use Polymarket data only as a SIGNAL source — execution
happens on Kalshi (US-legal).

---

## v0 — Original (legacy, in `/legacy`)

Two early prototypes that were retired:

1. **Weather temperature strategy** (`legacy/weather_*.py`): Used GFS
   ensemble forecasts from Open-Meteo to find mispriced Kalshi temperature
   markets (KXHIGHNY etc). Worked in theory but Kalshi's temperature markets
   moved to thinner intraday formats (KXTEMP) where the bot couldn't get
   meaningful fills.

2. **Naive cross-platform matcher** (`legacy/cross_signal_v1.py`): Polymarket
   leaderboard → Kalshi text-similarity matching. The matcher was too lossy
   — it kept finding semantically similar markets that weren't the same
   event (e.g., "Wes Streeting next UK PM" → "Will Starmer leave PM"). After
   8 settled trades returned -$0.91, retired in favor of the v1 multi-agent
   approach.

---

## Key takeaways so far

1. **Selectivity matters more than frequency.** v1 with 3/4 brain checks
   produced 0.25 trades/day. Loosening helped find more — but only if the
   underlying signal is real.

2. **Exit discipline is harder than entry discipline.** The VOLUME_EXIT
   trigger has killed potential winners (e.g., the KXTRUMPSAY -$3.76 loss
   in early June) and saved some losers. Net effect on whale-copy bot is
   ambiguous and being studied.

3. **The disposition strategy edge is razor-thin.** Bot B reached 47-for-0
   on heavy favorites, then started losing. At $0.95 entry prices, you need
   exactly a 95% win rate to break even. The Whelan paper's 2% edge proved
   smaller than expected in our sample.

4. **Diversification is doing real work.** When Bot A drawdowns, Bot E
   might be up. Running 5 uncorrelated strategies on the same scanner
   output reveals which signals are durable.

5. **Cooldowns prevent self-harm.** The biggest unfixed bug pre-v2.1 was
   the re-entry loop. Spread costs from churning the same ticker accumulate
   silently.
