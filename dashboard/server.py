#!/usr/bin/env python3
"""
Dashboard API server — thin Flask layer that runs the bot in the background
and exposes real-time data via JSON endpoints + a React-like HTML dashboard.
"""

import json
import os
import sys
import threading
import time
import copy
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, Response

# Add parent dir so we can import the bot
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_bot import PolymarketBot, load_config

app = Flask(__name__, template_folder=".")

# ── Global state ────────────────────────────────────────────────────────────
state = {
    "running": False,
    "started_at": None,
    "last_cycle": None,
    "current_cycle": 0,
    "max_cycles": None,
    "phase": "idle",  # idle | discovering | trading | simulating | done
    "markets": [],
    "category_counts": {},
    "trades": [],
    "closed_trades": [],
    "portfolio": {
        "initial_balance": 10000,
        "balance": 10000,
        "total_pnl": 0,
        "total_trades": 0,
        "open_positions": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    },
    "strategy_stats": {},
    "category_stats": {},
    "market_updates": {},
    "logs": [],
}


def log(msg):
    state["logs"].append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]


# ── Bot runner (background thread) ──────────────────────────────────────────

def run_bot():
    """Execute the bot and update global state at each phase."""
    config = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"))
    sim = config["simulation"]
    bot = PolymarketBot(config)
    bot.portfolio.initial_balance = sim["portfolio_size"]
    bot.portfolio.balance = sim["portfolio_size"]

    state["running"] = True
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    state["max_cycles"] = 5
    state["phase"] = "discovering"
    log("🔍 Phase 1: Market Discovery")

    # Phase 1: Discover
    markets = bot.discover_markets()
    state["markets"] = [
        {
            "id": m.id,
            "question": m.question,
            "category": m.category,
            "yes_price": m.outcome_prices[0] if m.outcome_prices else 0.5,
            "volume_24h": m.volume_24h,
            "liquidity": m.liquidity,
            "end_date": m.end_date,
        }
        for m in markets
    ]
    cats = {}
    for m in markets:
        cats[m.category] = cats.get(m.category, 0) + 1
    state["category_counts"] = cats
    log(f"✅ Found {len(markets)} live markets")

    # Phase 2: Trade
    state["phase"] = "trading"
    log("📝 Phase 2: Executing trades")
    bot.execute_trades()

    state["trades"] = []
    for t in bot.portfolio.trade_history:
        state["trades"].append({
            "trade_id": t.trade_id,
            "timestamp": t.timestamp,
            "market_question": t.market_question[:60],
            "category": t.category,
            "strategy": t.strategy,
            "side": t.side,
            "price": t.price,
            "size": t.size,
            "signal_strength": t.signal_strength,
            "reasoning": t.reasoning[:50],
            "pnl": 0,
            "status": "open",
        })
    log(f"✅ Placed {len(state['trades'])} trades")

    # Phase 3: Simulation cycles
    for cycle in range(1, 6):
        state["current_cycle"] = cycle
        state["phase"] = "simulating"
        log(f"📊 Cycle {cycle}/5: Simulating prices...")

        prices = bot.simulate_resolution()

        # Update market prices
        for mid, p in prices.items():
            state["market_updates"][mid] = p

        # Update portfolio state
        s = bot.portfolio.summary()
        state["portfolio"] = s

        state["closed_trades"] = []
        for t in bot.portfolio.closed_trades:
            state["closed_trades"].append({
                "trade_id": t.trade_id,
                "market_question": t.market_question[:60],
                "side": t.side,
                "price": t.price,
                "resolution_price": t.resolution_price,
                "size": t.size,
                "pnl": round(t.pnl, 2),
                "status": t.status,
                "close_reason": t.close_reason,
            })

        # Strategy stats
        by_strat = {}
        for t in bot.portfolio.trade_history + bot.portfolio.closed_trades:
            s_name = t.strategy
            if s_name not in by_strat:
                by_strat[s_name] = {"count": 0, "pnl": 0, "wins": 0}
            by_strat[s_name]["count"] += 1
            by_strat[s_name]["pnl"] += t.pnl
            if t.pnl > 0:
                by_strat[s_name]["wins"] += 1
        state["strategy_stats"] = {
            k: {**v, "win_rate": round(v["wins"] / max(v["count"], 1) * 100, 1)}
            for k, v in by_strat.items()
        }

        # Category stats
        by_cat = {}
        for t in bot.portfolio.trade_history + bot.portfolio.closed_trades:
            c = t.category or "Other"
            if c not in by_cat:
                by_cat[c] = {"count": 0, "pnl": 0}
            by_cat[c]["count"] += 1
            by_cat[c]["pnl"] += t.pnl
        state["category_stats"] = {
            k: {**v, "pnl": round(v["pnl"], 2)} for k, v in by_cat.items()
        }

        time.sleep(2)

    state["phase"] = "done"
    state["running"] = False
    state["last_cycle"] = datetime.now(timezone.utc).isoformat()
    log("✅ Simulation complete")


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]:
        return jsonify({"error": "Bot is already running"}), 409
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/markets")
def api_markets():
    return jsonify(state["markets"])


@app.route("/api/trades")
def api_trades():
    return jsonify({
        "open": state["trades"],
        "closed": state["closed_trades"],
    })


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(state["portfolio"])


@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 50))
    return jsonify(state["logs"][-limit:])


from flask import request


@app.route("/api/strategies")
def api_strategies():
    return jsonify(state["strategy_stats"])


@app.route("/api/categories")
def api_categories():
    return jsonify(state["category_stats"])


# ── SSE Stream ──────────────────────────────────────────────────────────────

def event_stream():
    """Server-sent events for real-time dashboard updates."""
    while True:
        payload = json.dumps({
            "phase": state["phase"],
            "cycle": state["current_cycle"],
            "portfolio": state["portfolio"],
            "trades_count": len(state["trades"]),
            "closed_count": len(state["closed_trades"]),
            "logs": state["logs"][-5:],
            "strategy_stats": state["strategy_stats"],
        })
        yield f"data: {payload}\n\n"
        time.sleep(2)


@app.route("/api/stream")
def api_stream():
    return Response(event_stream(), mimetype="text/event-stream")


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
