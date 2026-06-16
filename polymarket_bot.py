#!/usr/bin/env python3
"""
Polymarket Simulated Trading Bot
=================================
Fetches real active markets from Polymarket API across multiple categories,
selects live markets with recent activity, and simulates trades using several
strategies to test profitability.

All trades are paper/simulated — no real funds are at risk.
"""

import json
import random
import math
import statistics
import time
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = "config.json"

DEFAULT_CONFIG = {
    "api_base": "https://gamma-api.polymarket.com",
    "simulation": {
        "portfolio_size": 10_000,
        "min_trade_size": 10,
        "max_trade_size": 500,
        "trades_per_run": 15,
        "risk_per_trade_pct": 5,
        "stop_loss_pct": 15,
        "take_profit_pct": 25,
    },
    "strategies": [
        "mean_reversion",
        "momentum",
        "value_bet",
        "arbitrage",
        "fade_extremes",
    ],
    "filters": {
        "min_volume_24h": 100,
        "min_liquidity": 1_000,
        "active_only": True,
        "ending_within_days": 365,
    },
}


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            user = json.load(f)
            cfg = {**DEFAULT_CONFIG, **user}
            for k in ("simulation", "filters"):
                if k in user and k in DEFAULT_CONFIG:
                    cfg[k] = {**DEFAULT_CONFIG[k], **user[k]}
            return cfg
    except FileNotFoundError:
        return DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Market:
    id: str
    question: str
    slug: str
    condition_id: str
    outcomes: list
    outcome_prices: list
    volume: float
    liquidity: float
    volume_24h: float
    volume_1wk: float
    volume_1mo: float
    end_date: str
    start_date: str
    category: str = ""
    event_title: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    one_month_price_change: float = 0.0
    one_year_price_change: float = 0.0
    last_trade_price: float = 0.0
    clob_token_ids: list = field(default_factory=list)
    accepting_orders: bool = True
    description: str = ""
    image: str = ""


@dataclass
class Trade:
    trade_id: str
    timestamp: str
    market_id: str
    market_question: str
    category: str
    strategy: str
    side: str  # "Yes" or "No"
    price: float
    size: float
    pnl: float = 0.0
    status: str = "open"  # open / won / lost / closed
    resolution_price: float = 0.0
    close_reason: str = ""
    signal_strength: float = 0.0
    reasoning: str = ""


@dataclass
class Portfolio:
    balance: float
    positions: list = field(default_factory=list)
    trade_history: list = field(default_factory=list)
    initial_balance: float = 0.0


# ---------------------------------------------------------------------------
# Polymarket API client
# ---------------------------------------------------------------------------

class PolymarketAPI:
    """Thin wrapper around the Polymarket Gamma API."""

    def __init__(self, base_url="https://gamma-api.polymarket.com"):
        self.base = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # -- markets -----------------------------------------------------------

    def get_markets(
        self,
        active=True,
        limit=100,
        offset=0,
        sort_by="volume",
        sort_direction="desc",
        slug_filter=None,
    ):
        """Fetch active markets."""
        params = {
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        }
        if slug_filter:
            params["slug"] = slug_filter
        r = self.session.get(f"{self.base}/markets", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_events(self, active=True, limit=100):
        """Fetch active events."""
        params = {"active": str(active).lower(), "limit": limit}
        r = self.session.get(f"{self.base}/events", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_tags(self):
        """Fetch all available tags for category filtering."""
        r = self.session.get(f"{self.base}/tags", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_spotlights(self):
        """Fetch spotlight / featured content."""
        r = self.session.get(f"{self.base}/spotlights", timeout=15)
        r.raise_for_status()
        return r.json()

    # -- parsers -----------------------------------------------------------

    @staticmethod
    def parse_market(raw):
        """Convert a raw API dict into a Market object."""
        prices_raw = json.loads(raw.get("outcomePrices", "[]"))
        outcomes_raw = json.loads(raw.get("outcomes", '["Yes","No"]'))
        clob_ids = json.loads(raw.get("clobTokenIds", "[]"))
        try:
            prices = [float(p) for p in prices_raw]
        except Exception:
            prices = [0.5, 0.5]
        return Market(
            id=raw.get("id", ""),
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            condition_id=raw.get("conditionId", ""),
            outcomes=outcomes_raw,
            outcome_prices=prices,
            volume=raw.get("volumeNum", 0),
            liquidity=raw.get("liquidityNum", 0),
            volume_24h=raw.get("volume24hr", 0),
            volume_1wk=raw.get("volume1wk", 0),
            volume_1mo=raw.get("volume1mo", 0),
            end_date=raw.get("endDateIso", ""),
            start_date=raw.get("startDateIso", ""),
            category="",  # filled in later from event
            event_title="",  # filled in later
            best_bid=raw.get("bestBid", 0),
            best_ask=raw.get("bestAsk", 0),
            spread=raw.get("spread", 0),
            one_month_price_change=raw.get("oneMonthPriceChange", 0) or 0,
            one_year_price_change=raw.get("oneYearPriceChange", 0) or 0,
            last_trade_price=raw.get("lastTradePrice", 0),
            clob_token_ids=clob_ids,
            accepting_orders=raw.get("acceptingOrders", True),
            description=raw.get("description", ""),
            image=raw.get("image", ""),
        )

    # -- collection helpers ------------------------------------------------

    def fetch_diverse_markets(self, filters):
        """
        Fetch markets from multiple pages / sort orders to get diversity,
        then de-duplicate by market id.
        """
        all_markets = []
        seen = set()
        sort_combos = [
            ("volume", "desc"),
            ("newest", "desc"),
            ("endingSoon", "asc"),
            ("liquidity", "desc"),
        ]

        for sort_by, sort_dir in sort_combos:
            try:
                raw = self.get_markets(
                    active=filters["active_only"],
                    limit=50,
                    sort_by=sort_by,
                    sort_direction=sort_dir,
                )
                for r in raw:
                    mid = r.get("id")
                    if mid not in seen:
                        seen.add(mid)
                        all_markets.append(self.parse_market(r))
            except Exception:
                continue

        # Also try pagination offsets for more diversity
        for offset in [30, 60, 90, 120, 150]:
            try:
                raw = self.get_markets(
                    active=filters["active_only"],
                    limit=30,
                    offset=offset,
                    sort_by="volume",
                    sort_direction="desc",
                )
                for r in raw:
                    mid = r.get("id")
                    if mid not in seen:
                        seen.add(mid)
                        all_markets.append(self.parse_market(r))
            except Exception:
                break

        # Enrich with event/category info
        self._enrich_categories(all_markets)

        # Apply filters
        filtered = self._apply_filters(all_markets, filters)

        return filtered

    def _enrich_categories(self, markets):
        """Assign categories using keyword heuristics + event data."""
        # Keyword-based category mapping (covers the vast majority)
        # Order matters: specific categories first, then broader ones.
        # Geopolitics before Pop Culture (so "China invades Taiwan before GTA VI" → Geopolitics)
        # "FIFA" / "World Cup" → Sports before "Space" catches "Spain"
        keyword_rules = [
            ("Sports",     ["nba", "nfl", "nhl", "mlb", "soccer", "football", "basketball",
                             "hockey", "baseball", "over/under", "spread", "beat the", "more than.*points",
                             "finish ahead", "qualify for", "euro ", "premier league", "champions league",
                             "playoff", "world series", "super bowl", "knockout", "tournament",
                             "fifa", "world cup", "world cup"]),
            ("Politics",   ["president", "election", "biden", "trump", "democratic", "republican",
                             "gop", "nominee", "vote", "ballot", "campaign", "senate", "congress",
                             "gavin newsom", "desantis", "haley", "booker", "newsom",
                             "democratic presidential", "republican presidential",
                             "xi jinning", "xi jinping", "putin", "presidential nominat"]),
            ("Legal",      ["sentenced", "prison", "verdict", "trial", "guilty",
                              "weinstein", "convicted", "jail", "parole"]),
            ("Crypto",     ["bitcoin", "btc", "ethereum", "solana", "sol",
                             "defi", "airdrop", "token", "dao", "web3", "nft floor",
                             "megaeth", "chainlink", "usdc", "stablecoin", "market cap",
                             "decentralized", "blockchain"]),
            ("Geopolitics",["war", "sanction", "invas", "ukraine", "russia", "nato",
                              "china", "taiwan", "sanctions", "conflict", "ceasefire",
                              "north korea", "iran"]),
            ("Business",   ["inflation", "gdp", "fed", "federal reserve", "interest rate",
                             "recession", "unemployment", "s&p", "dow", "nasdaq", "earnings",
                             "revenue", "profit", "bank", "swiss franc", "natural gas"]),
            ("Science",    ["gpt", "artificial intelligence", "nuclear fusion",
                             " space ", "nasa", "mars", "cure", "vaccine", "pandemic",
                             "climate", "carbon", "ai "]),
            ("Pop Culture",["rihanna", "album", "playboi", "movie", "gross", "box office",
                             "grammy", "oscar", "award", "netflix", "spotify", "streaming",
                             "before gta vi", "release",
                             "return before gta"]),
        ]

        for m in markets:
            q_lower = m.question.lower()
            assigned = False
            for category, keywords in keyword_rules:
                for kw in keywords:
                    if kw.lower() in q_lower:
                        m.category = category
                        assigned = True
                        break
                if assigned:
                    break
            if not assigned:
                m.category = "Other"

        # Also try to pull event titles/categories from events API as fallback
        try:
            events = self.get_events(active=True, limit=200)
            for m in markets:
                if m.category in ("Other", "General"):
                    for ev in events:
                        ev_title = ev.get("title", "")
                        if any(w in m.question for w in ev_title.split()[:3]):
                            m.category = ev.get("category", "Other")
                            break
        except Exception:
            pass

        # Final fallback
        for m in markets:
            if not m.category:
                m.category = "Other"

    @staticmethod
    def _apply_filters(markets, filters):
        now = datetime.now(timezone.utc)
        result = []
        for m in markets:
            if m.volume_24h < filters["min_volume_24h"]:
                continue
            if m.liquidity < filters["min_liquidity"]:
                continue
            if not m.accepting_orders:
                continue
            # Check end date is in the future
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                if end <= now:
                    continue
                days_left = (end - now).days
                if days_left < 0 or days_left > filters["ending_within_days"]:
                    continue
            except Exception:
                pass
            result.append(m)
        return result


# ---------------------------------------------------------------------------
# Trading Strategies
# ---------------------------------------------------------------------------

class Strategy:
    BASE_NAME = "base"

    def evaluate(self, market, portfolio):
        """
        Returns (side, price, signal_strength, reasoning) or None.
        side: "Yes" or "No"
        price: float (entry price)
        signal_strength: 0..1 confidence
        reasoning: human-readable
        """
        raise NotImplementedError


class MeanReversion(Strategy):
    BASE_NAME = "mean_reversion"

    def evaluate(self, market, portfolio):
        """
        Bet that extreme prices revert toward 50%.
        If Yes price > 0.80, bet No. If Yes price < 0.20, bet Yes.
        """
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5

        if yes_price > 0.80:
            return (
                "No",
                1.0 - yes_price,
                min((yes_price - 0.80) / 0.20, 1.0),
                f"Yes at {yes_price:.2f} is overbought → fade to No",
            )
        if yes_price < 0.20:
            return (
                "Yes",
                yes_price,
                min((0.20 - yes_price) / 0.20, 1.0),
                f"Yes at {yes_price:.2f} is oversold → buy Yes",
            )
        return None


class Momentum(Strategy):
    BASE_NAME = "momentum"

    def evaluate(self, market, portfolio):
        """
        Follow recent price momentum.
        If price moved significantly up in the last month, bet Yes.
        """
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        price_change = market.one_month_price_change or 0

        if abs(price_change) < 0.03:
            return None  # No significant momentum

        if price_change > 0.05:
            return (
                "Yes",
                yes_price,
                min(abs(price_change) / 0.20, 1.0),
                f"Price up {price_change:+.1%} in 1mo → momentum Yes",
            )
        if price_change < -0.05:
            side = "No"
            return (
                side,
                1.0 - yes_price,
                min(abs(price_change) / 0.20, 1.0),
                f"Price down {price_change:+.1%} in 1mo → momentum No",
            )
        return None


class ValueBet(Strategy):
    BASE_NAME = "value_bet"

    def evaluate(self, market, portfolio):
        """
        Look for mispriced markets where implied probability diverges
        from a 'fair' estimate based on volume and price stability.
        """
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        spread = market.spread or abs(market.best_ask - market.best_bid)

        # Tight spread + high volume = market is efficient → skip unless extreme
        # Wide spread = possible mispricing opportunity
        if spread > 0.04 and market.volume_24h > 500:
            # Wide spread suggests uncertainty — bet on the underdog
            if yes_price > 0.55:
                return (
                    "No",
                    1.0 - yes_price,
                    0.3 + spread * 2,
                    f"Wide spread {spread:.3f} + Yes over 55% → value in No",
                )
            elif yes_price < 0.45:
                return (
                    "Yes",
                    yes_price,
                    0.3 + spread * 2,
                    f"Wide spread {spread:.3f} + Yes under 45% → value in Yes",
                )
        return None


class Arbitrage(Strategy):
    BASE_NAME = "arbitrage"

    def evaluate(self, market, portfolio):
        """
        Detect when Yes + No prices deviate from 1.0 — a pricing anomaly.
        In reality Polymarket is efficient, so this fires rarely.
        """
        if len(market.outcome_prices) < 2:
            return None
        total = sum(market.outcome_prices)
        if abs(total - 1.0) > 0.02:
            # Price anomaly — buy the cheaper side
            yes_price = market.outcome_prices[0]
            if total < 1.0:
                # Both are cheap — buy Yes
                return (
                    "Yes",
                    yes_price,
                    abs(total - 1.0) * 5,
                    f"Yes+No={total:.3f}<1.00 → free value buying Yes@{yes_price:.3f}",
                )
            else:
                # Premium exists — short the expensive side
                return (
                    "No" if yes_price > 0.5 else "Yes",
                    market.outcome_prices[0] if yes_price > 0.5 else market.outcome_prices[1],
                    abs(total - 1.0) * 5,
                    f"Yes+No={total:.3f}>1.00 → pricing anomaly trade",
                )
        return None


class FadeExtremes(Strategy):
    BASE_NAME = "fade_extremes"

    def evaluate(self, market, portfolio):
        """
        Fade markets with extreme prices (<5% or >95%) — likely overpriced
        by recency bias or single-event FOMO.
        """
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5

        if yes_price > 0.95:
            return (
                "No",
                1.0 - yes_price,
                (yes_price - 0.95) / 0.05,
                f"Yes at {yes_price:.2f} is extreme → unlikely to resolve Yes, fade to No",
            )
        if yes_price < 0.05:
            return (
                "Yes",
                yes_price,
                (0.05 - yes_price) / 0.05,
                f"Yes at {yes_price:.2f} is extreme → insurance buy on Yes",
            )
        return None


STRATEGY_REGISTRY = [
    MeanReversion,
    Momentum,
    ValueBet,
    Arbitrage,
    FadeExtremes,
]


# ---------------------------------------------------------------------------
# Portfolio / Simulation Engine
# ---------------------------------------------------------------------------

class SimulatedPortfolio:
    def __init__(self, initial_balance=10_000):
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = {}       # market_id -> {side, size, price, ...}
        self.trade_history = []   # list of Trade
        self.closed_trades = []

    def place_trade(self, trade: Trade):
        """Record a new simulated trade."""
        cost = trade.size * trade.price
        if cost > self.balance:
            # Scale down
            trade.size = self.balance / trade.price
            cost = self.balance

        self.balance -= cost
        self.positions[trade.market_id] = {
            "trade": trade,
        }
        self.trade_history.append(trade)
        return trade

    def close_trade(self, market_id, resolution_price, reason="resolved"):
        """Close a position and compute P&L."""
        if market_id not in self.positions:
            return None
        pos = self.positions.pop(market_id)
        trade = pos["trade"]

        # P&L: if we bought Yes@p and resolves at r, PnL = size * (r - p)
        # if we bought No@p and resolves at r (Yes price), PnL = size * (1-r - (1-p)) = size*(p-r)
        if trade.side == "Yes":
            pnl = trade.size * (resolution_price - trade.price)
        else:
            pnl = trade.size * (trade.price - resolution_price)

        # Refund for the "other" outcome not bought
        if trade.side == "Yes":
            self.balance += trade.size  # No tokens refund
        else:
            self.balance += trade.size  # Yes tokens refund

        trade.pnl = pnl
        trade.status = "closed"
        trade.resolution_price = resolution_price
        trade.close_reason = reason
        self.closed_trades.append(trade)
        return trade

    def check_positions(self, market_updates):
        """
        Check all open positions against new market prices.
        Apply stop-loss and take-profit.
        market_updates: {market_id: {yes_price, no_price}}
        """
        for market_id, update in market_updates.items():
            if market_id not in self.positions:
                continue
            pos = self.positions[market_id]
            trade = pos["trade"]
            yes_price = update["yes_price"]
            no_price = update.get("no_price", 1.0 - yes_price)

            if trade.side == "Yes":
                current_value = trade.size * yes_price
                cost = trade.size * trade.price
            else:
                current_value = trade.size * no_price
                cost = trade.size * trade.price

            if cost == 0:
                continue
            change_pct = (current_value - cost) / cost

            stop_loss = -0.15
            take_profit = 0.25

            if change_pct <= stop_loss:
                self.close_trade(market_id, yes_price, f"stop-loss {change_pct:+.1%}")
            elif change_pct >= take_profit:
                self.close_trade(market_id, yes_price, f"take-profit {change_pct:+.1%}")

    def summary(self):
        total_pnl = sum(t.pnl for t in self.closed_trades)
        open_count = len(self.positions)
        win_count = sum(1 for t in self.closed_trades if t.pnl > 0)
        lose_count = sum(1 for t in self.closed_trades if t.pnl <= 0)
        total_trades = len(self.closed_trades) + open_count
        return {
            "balance": round(self.balance + sum(
                p["trade"].size * (
                    p["trade"].price  # rough estimate
                ) for p in self.positions.values()
            ), 2),
            "initial_balance": self.initial_balance,
            "total_pnl": round(total_pnl, 2),
            "open_positions": open_count,
            "closed_trades": len(self.closed_trades),
            "wins": win_count,
            "losses": lose_count,
            "win_rate": round(win_count / max(total_trades, 1) * 100, 1),
            "total_trades": total_trades,
        }


# ---------------------------------------------------------------------------
# Bot Engine
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.sim_config = self.config["simulation"]
        self.filters = self.config["filters"]
        self.api = PolymarketAPI(self.config["api_base"])
        self.portfolio = SimulatedPortfolio(self.sim_config["portfolio_size"])
        self.strategies = [cls() for cls in STRATEGY_REGISTRY
                           if cls.BASE_NAME in self.config["strategies"]]
        self.active_markets = []
        self.all_trades = []

    # -- Discovery ---------------------------------------------------------

    def discover_markets(self):
        """Fetch and filter live markets from Polymarket API."""
        print("  ⏳ Fetching markets from Polymarket API...")
        markets = self.api.fetch_diverse_markets(self.filters)

        if not markets:
            print("  ⚠️  No markets matched filters. Relaxing filters...")
            relaxed = {**self.filters, "min_volume_24h": 10, "min_liquidity": 100}
            markets = self.api.fetch_diverse_markets(relaxed)

        self.active_markets = markets
        print(f"  ✅ Found {len(markets)} eligible live markets")

        # Print category breakdown
        cats = {}
        for m in markets:
            cats[m.category] = cats.get(m.category, 0) + 1
        print(f"  📊 Categories: {dict(sorted(cats.items(), key=lambda x: -x[1]))}")

        return markets

    # -- Strategy Evaluation -----------------------------------------------

    def evaluate_strategies(self, market):
        """Run all strategies on a market and return the best signal."""
        signals = []
        for strategy in self.strategies:
            result = strategy.evaluate(market, self.portfolio)
            if result:
                signals.append(result)

        if not signals:
            return None

        # Pick the strongest signal
        signals.sort(key=lambda s: s[2], reverse=True)
        return signals[0]

    # -- Trade Execution ---------------------------------------------------

    def execute_trades(self, num_trades=None):
        """Select markets and execute simulated trades."""
        if not self.active_markets:
            self.discover_markets()

        if num_trades is None:
            num_trades = self.sim_config["trades_per_run"]

        # Shuffle markets to get randomness
        candidates = list(self.active_markets)
        random.shuffle(candidates)

        trades_placed = 0
        used_markets = set()

        for market in candidates:
            if trades_placed >= num_trades:
                break

            signal = self.evaluate_strategies(market)
            if not signal:
                continue

            side, price, strength, reasoning = signal

            # Avoid duplicate market trades in one run
            if market.id in used_markets:
                continue
            used_markets.add(market.id)

            # Determine trade size based on signal strength
            base_size = random.uniform(
                self.sim_config["min_trade_size"],
                self.sim_config["max_trade_size"],
            )
            trade_size = base_size * (0.5 + strength * 0.5)

            strategy_name = "unknown"
            for s in self.strategies:
                if s.__class__.__name__.lower().replace("_", "") == signal[3].split("→")[0].strip().split()[0]:
                    pass

            trade = Trade(
                trade_id=uuid.uuid4().hex[:12],
                timestamp=datetime.now(timezone.utc).isoformat(),
                market_id=market.id,
                market_question=market.question,
                category=market.category,
                strategy=type(signal[0]).__name__ if isinstance(signal[0], str) else "strategy",
                side=side,
                price=round(price, 4),
                size=round(trade_size, 2),
                signal_strength=round(strength, 3),
                reasoning=reasoning,
            )

            # Identify which strategy produced this signal
            for s in self.strategies:
                cls_name = type(s).__name__.lower()
                if (
                    ("fade" in reasoning.lower() and "overbought" in reasoning.lower() and cls_name == "meanreversion")
                    or ("oversold" in reasoning.lower() and cls_name == "meanreversion")
                ):
                    trade.strategy = "mean_reversion"
                elif "momentum" in reasoning.lower() and cls_name == "momentum":
                    trade.strategy = "momentum"
                elif "value" in reasoning.lower() and cls_name == "valuebet":
                    trade.strategy = "value_bet"
                elif ("anomaly" in reasoning.lower() or "free" in reasoning.lower()) and cls_name == "arbitrage":
                    trade.strategy = "arbitrage"
                elif "extreme" in reasoning.lower() and cls_name == "fadeextremes":
                    trade.strategy = "fade_extremes"

            # Fallback: match reasoning keywords to strategy name
            if trade.strategy == "strategy":
                if "oversold" in reasoning.lower() or "overbought" in reasoning.lower():
                    trade.strategy = "mean_reversion"
                elif "momentum" in reasoning.lower():
                    trade.strategy = "momentum"
                elif "value" in reasoning.lower() or "spread" in reasoning.lower():
                    trade.strategy = "value_bet"
                elif "anomaly" in reasoning.lower() or "free" in reasoning.lower():
                    trade.strategy = "arbitrage"
                elif "extreme" in reasoning.lower():
                    trade.strategy = "fade_extremes"

            self.portfolio.place_trade(trade)
            self.all_trades.append(trade)
            trades_placed += 1

        return trades_placed

    # -- Simulated Resolution ----------------------------------------------

    def simulate_resolution(self, volatility=0.08):
        """
        Simulate price movements and partially resolve open positions.
        Uses a random walk with mean-reversion bias.
        """
        market_prices = {}
        for market in self.active_markets:
            current_yes = market.outcome_prices[0] if market.outcome_prices else 0.5
            # Random walk with slight mean reversion
            drift = (0.5 - current_yes) * 0.05  # mean-reversion pull
            noise = random.gauss(0, volatility)
            new_yes = max(0.01, min(0.99, current_yes + drift + noise))
            market_prices[market.id] = {
                "yes_price": new_yes,
                "no_price": 1.0 - new_yes,
            }

        # Check stop-loss / take-profit
        self.portfolio.check_positions(market_prices)

        # Randomly resolve some old positions (10% chance per open position)
        for market_id in list(self.portfolio.positions.keys()):
            if random.random() < 0.10:
                resolution = random.choices([0.0, 1.0], weights=[0.45, 0.55])[0]
                self.portfolio.close_trade(market_id, resolution, "random_resolution")

        return market_prices

    # -- Reports -----------------------------------------------------------

    def print_market_table(self, markets=None):
        """Pretty-print the discovered markets."""
        markets = markets or self.active_markets
        if not markets:
            return

        print()
        print("=" * 120)
        print(f"{'#':<4} {'Question':<55} {'Cat':<15} {'Yes%':>5} {'No%':>5} {'Vol24h':>12} {'Liq':>10} {'End':>12}")
        print("=" * 120)

        for i, m in enumerate(markets[:30]):
            yes = f"{m.outcome_prices[0]:.1%}" if m.outcome_prices else "--"
            no_val = 1.0 - m.outcome_prices[0] if m.outcome_prices else 0.5
            no = f"{no_val:.1%}" if m.outcome_prices else "--"
            q = m.question[:53] + ".." if len(m.question) > 55 else m.question
            print(
                f"{i+1:<4} {q:<55} {m.category:<15} {yes:>5} {no:>5} "
                f"${m.volume_24h:>10,.0f} ${m.liquidity:>8,.0f} {m.end_date:>12}"
            )
        if len(markets) > 30:
            print(f"  ... and {len(markets)-30} more markets")

    def print_trade_log(self):
        """Pretty-print all trades."""
        if not self.all_trades:
            print("  No trades executed.")
            return

        print()
        print("=" * 140)
        print(f"{'ID':<14} {'Strategy':<16} {'Side':<5} {'Price':>6} {'Size':>8} {'Strength':>8} {'Reasoning':<50}")
        print("=" * 140)

        for t in self.all_trades:
            print(
                f"{t.trade_id:<14} {t.strategy:<16} {t.side:<5} "
                f"${t.price:>5.3f} ${t.size:>7,.2f} "
                f"{t.signal_strength:>7.2%} {t.reasoning[:48]:<50}"
            )

    def print_portfolio_summary(self):
        """Print portfolio status."""
        s = self.portfolio.summary()
        print()
        print("=" * 60)
        print("  📊 PORTFOLIO SUMMARY")
        print("=" * 60)
        print(f"  Initial Balance:     ${s['initial_balance']:>12,.2f}")
        print(f"  Current Balance:     ${s['balance']:>12,.2f}")
        print(f"  Realized P&L:        ${s['total_pnl']:>+12,.2f}")
        print(f"  Total Trades:        {s['total_trades']:>12}")
        print(f"  Open Positions:      {s['open_positions']:>12}")
        print(f"  Closed Trades:       {s['closed_trades']:>12}")
        print(f"  Wins / Losses:       {s['wins']:>5} / {s['losses']:>5}")
        print(f"  Win Rate:            {s['win_rate']:>11.1f}%")
        return s

    def print_closed_trades(self):
        """Print resolved trades with P&L."""
        closed = self.portfolio.closed_trades
        if not closed:
            print("  No closed trades yet.")
            return

        print()
        print("=" * 120)
        print(f"{'Side':<5} {'Entry':>6} {'Resolution':>11} {'Size':>8} {'P&L':>10} {'Reason':<20} {'Market'}")
        print("=" * 120)
        for t in closed:
            pnl_sign = "+" if t.pnl >= 0 else ""
            q = t.market_question[:55]
            print(
                f"{t.side:<5} ${t.price:>5.3f} ${t.resolution_price:>9.3f} "
                f"${t.size:>7,.2f} {pnl_sign}${t.pnl:>8,.2f} {t.close_reason:<20} {q}"
            )

    # -- Strategy Performance ------------------------------------------------

    def print_strategy_breakdown(self):
        """Show performance by strategy."""
        by_strategy = {}
        for t in self.portfolio.trade_history + self.portfolio.closed_trades:
            s = t.strategy
            if s not in by_strategy:
                by_strategy[s] = {"count": 0, "pnl": 0, "wins": 0}
            by_strategy[s]["count"] += 1
            by_strategy[s]["pnl"] += t.pnl
            if t.pnl > 0:
                by_strategy[s]["wins"] += 1

        if not by_strategy:
            print("  No trade data yet.")
            return

        print()
        print("=" * 60)
        print("  📈 STRATEGY BREAKDOWN")
        print("=" * 60)
        print(f"  {'Strategy':<18} {'Trades':>7} {'Wins':>6} {'WinRate':>8} {'Total P&L':>12}")
        print("  " + "-" * 55)
        for name, stats in sorted(by_strategy.items(), key=lambda x: -x[1]['pnl']):
            wr = stats['wins'] / max(stats['count'], 1) * 100
            print(
                f"  {name:<18} {stats['count']:>7} {stats['wins']:>6} "
                f"{wr:>7.1f}% ${stats['pnl']:>+11,.2f}"
            )

    def print_category_breakdown(self):
        """Show performance by market category."""
        by_cat = {}
        for t in self.portfolio.trade_history + self.portfolio.closed_trades:
            c = t.category or "General"
            if c not in by_cat:
                by_cat[c] = {"count": 0, "pnl": 0, "wins": 0}
            by_cat[c]["count"] += 1
            by_cat[c]["pnl"] += t.pnl
            if t.pnl > 0:
                by_cat[c]["wins"] += 1

        if not by_cat:
            return

        print()
        print("=" * 60)
        print("  🌍 CATEGORY BREAKDOWN")
        print("=" * 60)
        print(f"  {'Category':<22} {'Trades':>7} {'P&L':>12}")
        print("  " + "-" * 45)
        for name, stats in sorted(by_cat.items(), key=lambda x: -x[1]['pnl']):
            print(f"  {name:<22} {stats['count']:>7} ${stats['pnl']:>+11,.2f}")

    # -- Full Run ----------------------------------------------------------

    def run(self, cycles=3):
        """
        Full simulation run:
          1. Discover live markets
          2. Execute trades with strategies
          3. Simulate price movements (multiple cycles)
          4. Print results
        """
        print()
        print("╔" + "═" * 78 + "╗")
        print("║" + "  POLYMARKET SIMULATED TRADING BOT".center(78) + "║")
        print("║" + f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}".center(78) + "║")
        print("╚" + "═" * 78 + "╝")

        # Phase 1: Discovery
        print()
        print("── Phase 1: Market Discovery ─────────────────────────────────────────────")
        self.discover_markets()
        self.print_market_table()

        # Phase 2: Trade Execution
        print()
        print(f"── Phase 2: Executing {self.sim_config['trades_per_run']} Simulated Trades ────────────────────")
        n = self.execute_trades()
        print(f"  ✅ Placed {n} trades across {len(self.portfolio.positions)} markets")
        self.print_trade_log()

        # Phase 3: Simulate price movements over cycles
        for cycle in range(1, cycles + 1):
            print()
            print(f"── Phase 3.{cycle}: Price Simulation Cycle {cycle}/{cycles} ─────────────────────────────")
            prices = self.simulate_resolution()

            closed_this_cycle = len(self.portfolio.closed_trades)
            print(f"  📉 Positions closed this cycle: {closed_this_cycle}")

        # Phase 4: Results
        print()
        print("── Phase 4: Results ──────────────────────────────────────────────────────")
        self.print_portfolio_summary()
        self.print_strategy_breakdown()
        self.print_category_breakdown()
        self.print_closed_trades()

        # Final verdict
        s = self.portfolio.summary()
        pnl = s['total_pnl']
        print()
        if pnl > 0:
            print(f"  🟢 SIMULATION RESULT: Profitable (+${pnl:,.2f})")
        elif pnl < 0:
            print(f"  🔴 SIMULATION RESULT: Loss (${pnl:,.2f})")
        else:
            print(f"  ⚪ SIMULATION RESULT: Break even")
        print()

        return self.portfolio.summary()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    bot = PolymarketBot(config)
    result = bot.run(cycles=3)
    return result


if __name__ == "__main__":
    main()
