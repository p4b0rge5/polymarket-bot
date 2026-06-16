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
    "pnl_history": [],       # [{cycle, balance, pnl}] per cycle
    "config": {},            # current config for the editor
    "continuous": False,     # True = loop indefinitely
    "stop_requested": False,  # flag to break the loop
}


def log(msg):
    state["logs"].append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]


def request_stop():
    """Set the stop flag so the running bot exits cleanly."""
    state["stop_requested"] = True


# ── Bot runner (background thread) ──────────────────────────────────────────

def run_one_cycle(bot, cycle_num, global_cycle_counter):
    """Run a single full iteration: discover → trade → simulate 5 sub-cycles.
    Returns the global_cycle_counter incremented."""
    cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    config = load_config(cfg_file)

    # Phase 1: Discover fresh markets every iteration
    state["phase"] = "discovering"
    log("🔍 Phase 1: Market Discovery")
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
    log(f"✅ Placed {len(bot.portfolio.trade_history)} trades")

    # Phase 3: Simulate 5 sub-cycles
    for sub in range(1, 6):
        if state["stop_requested"]:
            return global_cycle_counter
        gc = global_cycle_counter + sub
        state["current_cycle"] = gc
        state["phase"] = "simulating"
        log(f"📊 Cycle {gc}: Simulating prices...")

        prices = bot.simulate_resolution()
        for mid, p in prices.items():
            state["market_updates"][mid] = p

        s = bot.portfolio.summary()
        state["portfolio"] = s
        state["pnl_history"].append({
            "cycle": gc, "balance": s["balance"], "pnl": s["total_pnl"],
            "closed_trades": s["closed_trades"], "wins": s["wins"], "losses": s["losses"],
        })

        # Refresh closed trades (keep all closed trades across iterations)
        for t in bot.portfolio.closed_trades:
            ct_entry = {
                "trade_id": t.trade_id, "market_question": t.market_question[:60],
                "side": t.side, "price": t.price, "resolution_price": t.resolution_price,
                "size": t.size, "pnl": round(t.pnl, 2), "status": t.status,
                "close_reason": t.close_reason,
            }
            if ct_entry not in state["closed_trades"]:
                state["closed_trades"].append(ct_entry)

        # Strategy stats
        by_strat = {}
        for t in bot.portfolio.trade_history + bot.portfolio.closed_trades:
            sn = t.strategy
            if sn not in by_strat:
                by_strat[sn] = {"count": 0, "pnl": 0, "wins": 0}
            by_strat[sn]["count"] += 1
            by_strat[sn]["pnl"] += t.pnl
            if t.pnl > 0:
                by_strat[sn]["wins"] += 1
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

    return global_cycle_counter + 5


def run_bot(continuous=False):
    """Execute the bot. If continuous=True, loop indefinitely (until stop requested)."""
    cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    config = load_config(cfg_file)
    sim = config["simulation"]

    state["running"] = True
    state["continuous"] = continuous
    state["stop_requested"] = False
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    state["phase"] = "discovering"

    bot = PolymarketBot(config)
    bot.portfolio.initial_balance = sim["portfolio_size"]
    bot.portfolio.balance = sim["portfolio_size"]

    iteration = 0
    global_cycle_counter = 0

    while True:
        if state["stop_requested"]:
            log("🛑 Stop requested — shutting down")
            state["phase"] = "stopped"
            state["running"] = False
            break

        iteration += 1
        log(f"🔄 Iteration {iteration}" + (" (continuous)" if continuous else ""))

        # Reset bot for fresh iteration but keep portfolio balance
        config = load_config(cfg_file)
        bot = PolymarketBot(config)
        bot.portfolio.initial_balance = sim["portfolio_size"]
        bot.portfolio.balance = state["portfolio"].get("balance", sim["portfolio_size"])

        global_cycle_counter = run_one_cycle(bot, iteration, global_cycle_counter)

        if not continuous:
            state["phase"] = "done"
            state["running"] = False
            state["last_cycle"] = datetime.now(timezone.utc).isoformat()
            log("✅ Simulation complete")
            break

        # Continuous mode: wait between iterations
        log("⏸ Waiting 10s before next iteration...")
        for _ in range(10):
            if state["stop_requested"]:
                break
            time.sleep(1)


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/start", methods=["POST"])
def api_start():
    from flask import request as r
    if state["running"]:
        return jsonify({"error": "Bot is already running"}), 409
    continuous = True  # default to continuous
    t = threading.Thread(target=run_bot, kwargs={"continuous": continuous}, daemon=True)
    t.start()
    return jsonify({"status": "started", "continuous": continuous})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not state["running"]:
        return jsonify({"error": "Bot is not running"}), 400
    request_stop()
    return jsonify({"status": "stopping"})


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


@app.route("/api/config")
def api_config():
    return jsonify(load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")))


@app.route("/api/config", methods=["POST"])
def api_config_update():
    from flask import request as r
    new_config = r.json
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    with open(cfg_path, "w") as f:
        json.dump(new_config, f, indent=2)
    state["config"] = new_config
    return jsonify({"status": "updated"})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if state["running"]:
        return jsonify({"error": "Cannot reset while bot is running"}), 409
    state["running"] = False
    state["started_at"] = None
    state["last_cycle"] = None
    state["current_cycle"] = 0
    state["phase"] = "idle"
    state["markets"] = []
    state["category_counts"] = {}
    state["trades"] = []
    state["closed_trades"] = []
    state["portfolio"] = {"initial_balance": 10000, "balance": 10000, "total_pnl": 0, "total_trades": 0, "open_positions": 0, "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0}
    state["strategy_stats"] = {}
    state["category_stats"] = {}
    state["market_updates"] = {}
    state["logs"] = []
    state["pnl_history"] = []
    state["stop_requested"] = False
    state["continuous"] = False
    return jsonify({"status": "reset"})


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
