#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
Smart Trading Alert Bot — V3.1 TSLA SCALPER (Options Wave Rider)
Webhook Server
══════════════════════════════════════════════════════════════════════════════
Customized for:
  - TSLA Options Scalping
  - 20-30 trades/day
  - $0.20-$0.60 targets
  - $3,000 portfolio with strict risk management
  - Anti-averaging warnings
  - Daily P&L tracking
══════════════════════════════════════════════════════════════════════════════
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

try:
    from flask import Flask, request, jsonify
except ImportError:
    os.system("pip install flask")
    from flask import Flask, request, jsonify

try:
    import requests as http_requests
except ImportError:
    os.system("pip install requests")
    import requests as http_requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "5000")))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Risk limits
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "300"))
MAX_DAILY_TRADES = int(os.environ.get("MAX_DAILY_TRADES", "35"))

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tsla_scalper_v3_1.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Flask App
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Daily Tracker
# ──────────────────────────────────────────────────────────────────────────────

class DailyTracker:
    """Tracks daily alerts and enforces limits."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.alerts = []
        self.call_count = 0
        self.put_count = 0

    def check_new_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.date:
            logger.info(f"New trading day: {today}. Resetting tracker.")
            self.reset()

    def add_alert(self, data):
        self.check_new_day()
        signal = data.get("signal", "?")
        entry = {
            "time": datetime.now().strftime("%I:%M:%S %p"),
            "signal": signal,
            "grade": data.get("grade", "?"),
            "price": data.get("price", "?"),
            "score": data.get("score", "?"),
        }
        self.alerts.append(entry)
        if signal == "CALL":
            self.call_count += 1
        elif signal == "PUT":
            self.put_count += 1

    @property
    def total(self):
        return len(self.alerts)

    @property
    def limit_reached(self):
        return self.total >= MAX_DAILY_TRADES

    def summary(self):
        return {
            "date": self.date,
            "total": self.total,
            "calls": self.call_count,
            "puts": self.put_count,
            "limit": MAX_DAILY_TRADES,
            "remaining": max(0, MAX_DAILY_TRADES - self.total),
        }


tracker = DailyTracker()

# ──────────────────────────────────────────────────────────────────────────────
# Telegram Message Formatter — V3.1 TSLA Scalper
# ──────────────────────────────────────────────────────────────────────────────

def format_telegram_v3_1(data: dict) -> str:
    """Premium formatted message for TSLA scalping."""

    signal = data.get("signal", "?")
    grade  = data.get("grade", "?")
    price  = data.get("price", "?")
    session = data.get("session", "?")
    wave   = data.get("wave", "?")

    # Grade styling
    grade_styles = {
        "A+": ("🔥🔥🔥", "A+ PERFECT SETUP", "Maximum"),
        "A":  ("⚡⚡", "A STRONG SETUP", "Very High"),
        "B+": ("💪", "B+ GOOD SETUP", "High"),
        "B":  ("📊", "B DECENT SETUP", "Moderate"),
    }
    g_emoji, g_text, confidence = grade_styles.get(grade, ("📊", f"{grade} SETUP", "Standard"))

    # Direction
    if signal == "CALL":
        sig_emoji = "🟢"
        direction = "CALL ↑ (Buy Calls)"
    else:
        sig_emoji = "🔴"
        direction = "PUT ↓ (Buy Puts)"

    # Volume
    vol_surge = data.get("volume_surge", "NO")
    vol_ratio = data.get("volume_ratio", "?")
    vol_text = f"⚡ {vol_ratio} SURGE" if vol_surge == "YES" else f"{vol_ratio}"

    # Trade number today
    trade_num = tracker.total
    remaining = max(0, MAX_DAILY_TRADES - trade_num)

    timestamp = datetime.now().strftime("%I:%M:%S %p ET")

    message = f"""{g_emoji} <b>{g_text}</b> {g_emoji}

{sig_emoji} <b>{direction}</b>
🌊 <b>Wave:</b> {wave}
━━━━━━━━━━━━━━━━━━━━━━━━━

🏷 <b>TSLA</b> @ <code>${price}</code>
🕐 {timestamp} | {session}
🎯 Score: <b>{data.get('score', '?')}</b> | Confidence: {confidence}
📊 Trade #{trade_num} today ({remaining} remaining)

📈 <b>━━ ANALYSIS ━━</b>

📊 15m: {data.get('trend_15m', '?')} | 5m: {data.get('trend_5m', '?')}
📍 VWAP: {data.get('vwap_status', '?')} (dist: {data.get('vwap_distance', '?')})
📐 EMA: {data.get('ema_status', '?').replace('<', '&lt;').replace('>', '&gt;')}
📈 MACD: {data.get('macd_status', '?')}
📉 RSI: {data.get('rsi_1m', '?')}
📦 Vol: {vol_text}

💎 <b>━━ TRADE PLAN ━━</b>

🎯 Entry: <code>${price}</code>
🛑 SL: <code>${data.get('stop_loss', '?')}</code> (-{data.get('sl_cents', '?')}¢)
✅ TP1: <code>${data.get('target_1', '?')}</code> (+{data.get('tp1_cents', '?')}¢)
✅ TP2: <code>${data.get('target_2', '?')}</code> (+{data.get('tp2_cents', '?')}¢)

💰 <b>━━ RISK ━━</b>

📏 Max per trade: <b>${data.get('max_risk', '?')}</b>
📋 Contracts: <b>{data.get('suggested_contracts', '?')}</b>
🚫 Daily loss limit: ${data.get('max_daily_loss', '300')}

━━━━━━━━━━━━━━━━━━━━━━━━━
🚫 <b>NO AVERAGING — NO ADDING</b> 🚫
⚠️ <i>One entry, one exit. If SL hit, move on.</i>
━━━━━━━━━━━━━━━━━━━━━━━━━"""

    return message.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Discord Formatter
# ──────────────────────────────────────────────────────────────────────────────

def format_discord_v3_1(data: dict) -> dict:
    signal = data.get("signal", "?")
    grade = data.get("grade", "?")
    price = data.get("price", "?")
    color = 0x00FF00 if signal == "CALL" else 0xFF0000

    embed = {
        "embeds": [{
            "title": f"{'📈' if signal == 'CALL' else '📉'} {grade} {signal} — TSLA @ ${price}",
            "color": color,
            "fields": [
                {"name": "🌊 Wave", "value": data.get("wave", "?"), "inline": True},
                {"name": "🎯 Score", "value": data.get("score", "?"), "inline": True},
                {"name": "📍 VWAP", "value": f"{data.get('vwap_status', '?')} ({data.get('vwap_distance', '?')})", "inline": True},
                {"name": "🎯 Entry", "value": f"${price}", "inline": True},
                {"name": "🛑 SL", "value": f"${data.get('stop_loss', '?')} (-{data.get('sl_cents', '?')}¢)", "inline": True},
                {"name": "✅ TP1/TP2", "value": f"${data.get('target_1', '?')} / ${data.get('target_2', '?')}", "inline": True},
                {"name": "📋 Contracts", "value": data.get("suggested_contracts", "?"), "inline": True},
                {"name": "💰 Max Risk", "value": f"${data.get('max_risk', '?')}", "inline": True},
                {"name": "📊 Trade #", "value": str(tracker.total), "inline": True},
            ],
            "footer": {"text": "🚫 NO AVERAGING — One entry, one exit. If SL hit, move on."},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    return embed


# ──────────────────────────────────────────────────────────────────────────────
# Senders
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = http_requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram: sent")
            return True
        else:
            logger.error(f"Telegram error: {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


def send_discord(embed: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        resp = http_requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        return resp.status_code in [200, 204]
    except Exception as e:
        logger.error(f"Discord failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    tracker.check_new_day()
    return jsonify({
        "status": "running",
        "service": "TSLA Scalper V3.1",
        "daily": tracker.summary(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    # Auth
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    # Check daily limit
    tracker.check_new_day()
    if tracker.limit_reached:
        logger.warning(f"Daily limit reached ({MAX_DAILY_TRADES}). Ignoring alert.")
        # Send warning to Telegram
        send_telegram(f"⚠️ <b>Daily limit reached ({MAX_DAILY_TRADES} trades)</b>\n\nNo more alerts today. Review your trades and rest.")
        return jsonify({"status": "limit_reached", "total": tracker.total}), 200

    # Parse
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data.decode("utf-8"))
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return jsonify({"error": "parse_failed"}), 400

    logger.info(f"Alert: {data.get('signal')} {data.get('grade')} TSLA @ ${data.get('price')} ({data.get('score')})")

    # Track
    tracker.add_alert(data)

    # Format and send
    tg_msg = format_telegram_v3_1(data)
    tg_ok = send_telegram(tg_msg)

    dc_ok = False
    if DISCORD_WEBHOOK_URL:
        dc_ok = send_discord(format_discord_v3_1(data))

    return jsonify({
        "status": "sent",
        "telegram": "ok" if tg_ok else "failed",
        "discord": "ok" if dc_ok else "n/a",
        "trade_number": tracker.total
    }), 200


@app.route("/test", methods=["GET"])
def test():
    """Send test alert."""
    test_data = {
        "signal": "CALL",
        "grade": "A+",
        "symbol": "TSLA",
        "price": "245.50",
        "session": "Morning Momentum",
        "wave": "Bullish Wave",
        "trend_15m": "Bullish",
        "trend_5m": "Bullish",
        "vwap_status": "Above VWAP",
        "vwap_price": "243.20",
        "vwap_distance": "+0.35%",
        "ema_status": "EMA9 > EMA21",
        "macd_status": "Fresh Crossover",
        "rsi_1m": "58.3",
        "obv_status": "Rising",
        "volume_surge": "YES",
        "volume_ratio": "1.6x",
        "score": "14/16",
        "stop_loss": "245.20",
        "target_1": "245.80",
        "target_2": "246.10",
        "sl_cents": "30",
        "tp1_cents": "30",
        "tp2_cents": "60",
        "max_risk": "$150",
        "suggested_contracts": "1-2",
        "portfolio": "3000",
        "max_daily_loss": "300"
    }

    tracker.add_alert(test_data)
    tg_msg = format_telegram_v3_1(test_data)
    tg_ok = send_telegram(tg_msg)

    return jsonify({"status": "test_sent", "telegram": "ok" if tg_ok else "failed"}), 200


@app.route("/daily", methods=["GET"])
def daily():
    """View daily summary."""
    tracker.check_new_day()
    return jsonify(tracker.summary())


@app.route("/history", methods=["GET"])
def history():
    """View today's alerts."""
    tracker.check_new_day()
    return jsonify({
        "date": tracker.date,
        "total": tracker.total,
        "alerts": tracker.alerts
    })


@app.route("/reset", methods=["POST"])
def reset():
    """Manually reset daily tracker."""
    tracker.reset()
    return jsonify({"status": "reset", "message": "Daily tracker cleared"})


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TSLA Scalper V3.1 — Starting...")
    logger.info(f"Portfolio: $3,000 | Max Risk/Trade: 5-10%")
    logger.info(f"Daily Trade Limit: {MAX_DAILY_TRADES}")
    logger.info(f"Daily Loss Limit: ${MAX_DAILY_LOSS}")
    logger.info(f"Server: {SERVER_HOST}:{SERVER_PORT}")
    logger.info("=" * 60)
    logger.info("Endpoints:")
    logger.info(f"  POST /webhook  — TradingView alerts")
    logger.info(f"  GET  /test     — Send test alert")
    logger.info(f"  GET  /daily    — Daily summary")
    logger.info(f"  GET  /history  — Today's alerts")
    logger.info(f"  POST /reset    — Reset daily counter")
    logger.info("=" * 60)

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
