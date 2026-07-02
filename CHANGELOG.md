# Changelog

A running log of the bot's evolution. Each iteration is a real lesson learned
from paper-trading on Kalshi.

---

## v5 — data-driven rebuild: sell longshots, quote as maker, anchor to Polymarket (2026-07-02)

Full P&L decomposition of v1-v4 (380 settled trades, combined **−$30.63 /
−5.83%**) showed the losses were a *cost-structure* problem, not a signal
problem:

- **Disposition: 93.0% win rate and still −11.7%.** At a $0.949 average entry
  the breakeven win rate is 94.9% — the taker ask plus fees ate the entire
  documented favorites edge. Split by leg: **favorite_buy −4.4%/trade
  (n=221)** vs **longshot_sell +4.6%/trade (n=37, 97.3% WR)**. Our own book
  reproduces the Whelan favorite-longshot asymmetry: the tradeable side is
  SELLING longshots, not buying favorites.
- **Crypto favorite-buying was the single worst segment** (−7.9%/trade,
  −$9.41 of the −$8.79 total): KXBTCD hourlies are the venue's most
  efficiently-priced series (market makers price them off live spot).
  Interestingly longshot-*selling* worked fine there (+8.5%, 12/12) — lottery
  buyers overpay for cheap YES everywhere.
- **Theta: 95.2% WR at 95.2% average entry** — the taker ask was fair value
  to the cent. The signal finds real favorites; paying the ask forfeits it.
- **Whale-copy: 48.4% WR vs 53.7% breakeven** over 93 settled — negative
  signal, retired. **Reversion −28.8%** — retired. **Consensus** built on the
  retired signals — retired. The legacy bots also charged **zero paper fees**,
  so their live numbers would have been worse.

**The v5 roster** (S seller / T theta / C arb / X xvenue) attacks the cost
structure directly:

1. **Maker execution infrastructure** (`botlib`): resting limit orders at 25%
   of taker fees (June 2026 schedule), with a *pessimistic* paper fill rule —
   filled only if a later trade prints strictly through the limit. Maker P&L
   is a floor by construction.
2. **Bot S (seller)** — the empirically profitable leg promoted to its own
   bot: buy NO at 85-96¢ on ~3-10¢ longshots, maker entries, one position per
   event, hold to settlement.
3. **Bot T (theta v2)** — identical signal to v1, entries via resting bids.
   Using v1's own measurement (ask = fair value), the entry improvement is
   the edge. The experiment: does adverse selection on fills eat the 2-3¢?
4. **Bot X (xvenue)** — Kalshi↔Polymarket verified pairs. Polymarket relaunched
   US-regulated in Dec 2025; its macro books are institutional-grade (July
   2026 Fed: $6.4M/day, 1¢ spread) while Kalshi's same-event books show
   20-60¢ spreads. Tier 1 locks hard arbs (YES+NO across venues < $1
   all-in, settles $1/pair regardless of outcome); Tier 2 rests Kalshi bids
   ≥3¢ inside Polymarket's mid. Pairs live in `data/xvenue_pairs.json`,
   human-verified only (curation immediately caught KXCPIYOY=headline vs
   Polymarket=core CPI — the v0 fuzzy-match trap, dodged).
   First live run placed a YES bid at $0.62 on KXFEDDECISION-26OCT-H0 against
   a Polymarket fair value of 0.655 ($164k resting book).
5. **Honesty fixes**: exits now pay the taker fee they'd actually incur;
   scanner accepts contract-count depth (dollar-depth silently excluded every
   longshot book — 500 contracts at 5¢ is $25 "deep"); arb-pair settlement
   pays the lock regardless of which side resolves.

Retired journals preserved in `/legacy` (whale, disposition, reversion,
consensus, theta-v1). Fresh $75 bankrolls; combined allocation now 4 × $75 =
$300.

---

## v4.2 — retire Weather (2026-07-01)

Weather finished at **−45% (10W/22L)** — a **31% win rate before *and* after** the
2026-06-27 σ recalibration. Consistent across 32 settled trades = not variance:
the NWS-normal model doesn't beat the market on temperature brackets. Retired to
`legacy/` (code, journal, tests). Roster is now **6 bots (A–F)**; Reversion,
Theta, Consensus re-lettered D/E/F. Reversion's crypto-exclusion fix, by
contrast, is working — it recovered from −$26 toward −$21 with no new losses.

---

## v4.1 — first-week tuning (2026-06-27)

After ~4 days of (finally-persisting) live data:

- **Workflow persistence bug fixed (2026-06-25):** both workflows `git add`-ed the
  retired calendar/spot/flow journal paths, which no longer exist — a missing
  pathspec makes `git add` fail atomically, so *nothing* committed for ~2 days
  post-deploy. Switched to a `paper_*_trades.json` glob.
- **Reversion (E) was −34.7% (2W/12L); 100% of losses were crypto strike
  ladders.** Those reprice on the real underlying move, not noise, so fading
  them fights genuine information. Added `CONTINUOUS_PRICE_PREFIXES` exclusion
  (KXBTC/ETH/SOL/...) to Reversion *and* Consensus (shared VWAP signal).
- **Weather (D) was −15.2% (5W/11L):** σ was too tight → overconfident → bet its
  own forecast error. Widened σ (next-day 3→4 °F, etc.), raised the divergence
  gate 0.10→0.15 and min edge 0.02→0.03. Now far pickier (13 → 4 candidates).
- Winners so far: **Theta** (+1.1%, 7W/0L) and **Consensus** (+0.9%). **Arb**
  correctly idle.

---

## v4 — 7-bot portfolio (2026-06-23)

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
