# Polymarket Simulated Trading Bot

> **DISCLAIMER**: This is a **paper trading simulator**. No real money is traded or at risk. It uses real Polymarket API data to simulate trades and test trading strategies.

## What It Does

1. **Market Discovery** — Fetches active live markets from the Polymarket Gamma API across multiple categories (Politics, Sports, Crypto, Legal, Pop Culture, Geopolitics, Business, Science)
2. **Strategy Evaluation** — Runs 5 trading strategies against each market:
   - **Mean Reversion**: Bets against extreme prices (>80% or <20%) expecting reversion
   - **Momentum**: Follows recent price momentum (1-month price changes)
   - **Value Bet**: Identifies mispriced markets with wide spreads + high volume
   - **Arbitrage**: Catches Yes+No pricing anomalies that deviate from 1.0
   - **Fade Extremes**: Short-sides extreme prices (>95% or <5%) as overpriced
3. **Simulated Trading** — Places paper trades with position sizing based on signal strength, stop-loss (15%), and take-profit (25%)
4. **Price Simulation** — Runs random-walk price movements with mean-reversion bias across multiple cycles
5. **Performance Reports** — Breaks down results by strategy, category, and individual trades

## Requirements

```
pip install requests
```

## Usage

### CLI Mode
```bash
# Run with defaults ($10,000 portfolio, 15 trades, 3 cycles)
python3 polymarket_bot.py

# Customize by editing config.json
python3 polymarket_bot.py
```

### 🌐 Live Dashboard
```bash
cd dashboard
pip install flask
python3 server.py
```

Then open **http://localhost:8081** in your browser.

**Public instance:** https://slide-coffee-satisfy-ring.2n6.me/dashboard/

The dashboard provides:
- **Real-time KPI cards** — balance, P&L, win rate, open/closed positions
- **Live trade feed** — all open and closed trades with full details
- **Market browser** — all discovered markets with prices, categories, volumes
- **Strategy breakdown** — performance by strategy with visual bars
- **Activity log** — timestamped event stream
- **▶ Start Bot** button to run a fresh simulation from the browser

## Configuration (`config.json`)

```json
{
  "simulation": {
    "portfolio_size": 10000,
    "trades_per_run": 15,
    "risk_per_trade_pct": 5,
    "stop_loss_pct": 15,
    "take_profit_pct": 25
  },
  "strategies": ["mean_reversion", "momentum", "value_bet", "arbitrage", "fade_extremes"],
  "filters": {
    "min_volume_24h": 100,
    "min_liquidity": 1000
  }
}
```

## Market Categories

The bot auto-categorizes markets using keyword heuristics:
- **Politics** — Elections, presidents, candidates
- **Sports** — NBA, NFL, FIFA World Cup, soccer, etc.
- **Crypto** — Bitcoin, Ethereum, airdrops, tokens
- **Pop Culture** — Music, movies, entertainment events
- **Geopolitics** — Wars, sanctions, international conflicts
- **Legal** — Court cases, sentencing, trials
- **Business** — Economic indicators, markets, inflation
- **Science** — AI, space, technology breakthroughs

## Current Live Markets (as of June 2026)

The active market universe includes:
- **2026 FIFA World Cup** — 47+ national teams competing
- **2028 US Presidential Election** — 50+ candidate markets (Dem/GOP)
- **What will happen before GTA VI?** — Culture/geopolitics prediction set
- **MegaETH airdrop** — Crypto events
- **Harvey Weinstein sentencing** — Legal outcomes
