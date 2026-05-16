# Prediction Market Analyst

Scripts to detect smart money signals and analyze top trader behavior on prediction markets.

- **Kalshi** (`kalshi_analyst.py`) — CFTC-regulated, US-legal. Uses order flow imbalance and volume spike detection on public trade data to surface where informed money is moving.
- **Polymarket** (`polymarket_analyst.py`) — Non-US only. Tracks specific top-trader wallet addresses via public on-chain data, computes consensus across the top 100+ wallets by all-time PnL.

## Setup

```bash
pip install requests
```

No API keys required for read-only analytics. Both scripts use only public endpoints.

## Kalshi (US-legal)

Kalshi is a CFTC-regulated exchange — fully legal for US residents.

### How smart money detection works on Kalshi

Kalshi is a centralized exchange: individual account histories are private (unlike Polymarket where every trade is on-chain). Smart money signals are instead derived from **aggregate public trade flow**:

- **Order Flow Imbalance (OFI)**: ratio of YES-side contracts bought vs. NO-side contracts bought. +1 = all buyers want YES, -1 = all buyers want NO.
- **Volume Spike**: recent window volume vs. a longer baseline. A 3x spike during quiet hours is a stronger signal than the same volume during normal trading.
- **Price-Flow Alignment**: strong OFI that also moves the price is a more reliable signal than OFI that doesn't.
- **Signal Score** = OFI magnitude × volume spike × alignment × price drift weight

### Usage

```bash
# Scan all open markets (last 24h vs 72h baseline)
python3 kalshi_analyst.py

# Tighten to last 6 hours — catches fresher moves
python3 kalshi_analyst.py --hours 6

# Filter by market category prefix
python3 kalshi_analyst.py --category KXBTC
python3 kalshi_analyst.py --category INXD
python3 kalshi_analyst.py --category PRES

# Deep-dive into one market
python3 kalshi_analyst.py --ticker KXBTCD-25DEC31-B110000

# JSON output for further processing
python3 kalshi_analyst.py --output json > signals.json

# Adjust sensitivity
python3 kalshi_analyst.py --min-trades 5 --top 40 --markets 300
```

### Output explained

| Column | Meaning |
|--------|---------|
| `Curr` | Current YES price (0–1) |
| `Dir` | Which side smart money is buying |
| `OFI` | Order flow imbalance (-1 to +1) |
| `Vol Spike` | Recent vol / baseline vol |
| `YES $` | Total USD into YES contracts |
| `NO $` | Total USD into NO contracts |
| `Score` | Composite smart money signal strength |

### Getting started with trading on Kalshi

1. Sign up at [kalshi.com](https://kalshi.com)
2. Fund your account (ACH, wire, or card)
3. Use the API or web interface to trade
4. For programmatic trading: generate an RSA key pair in Account > API Keys

## Polymarket (non-US only)

Polymarket is an on-chain prediction market on Polygon. US residents are not permitted to trade.

The `polymarket_analyst.py` script fetches the top 100 traders by all-time PnL from the public leaderboard, pulls their trade histories, and finds where multiple top traders are converging.

```bash
# Default: top 100 traders, last 90 days
python3 polymarket_analyst.py

# Drill into one wallet
python3 polymarket_analyst.py --wallet 0x56687bf447db6ffa42ffe2204a05edaa20f55839

# Faster scan
python3 polymarket_analyst.py --traders 50 --days 30 --min-consensus 3
```

## Related resources

- [Kalshi API docs](https://docs.kalshi.com)
- [predicting.top](https://predicting.top) — leaderboard for opt-in Kalshi/Polymarket traders
- [pmxt](https://github.com/pmxt-dev/pmxt) — unified Python SDK across Kalshi, Polymarket, and others
- [prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis) — 36 GiB historical dataset (Kalshi + Polymarket)
