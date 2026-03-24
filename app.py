#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
Smart Trading Alert Bot — V3.3 TSLA SCALPER (Enhanced Data Quality)
Webhook Server
══════════════════════════════════════════════════════════════════════════════
V3.3 Changes:
  - RSI always displayed as numeric value + description (never N/A)
  - Volume shown as actual number + comparison to average
  - OBV with clear multi-timeframe status
  - All indicators: numeric + descriptive
  - Smart fallback for missing/NaN data fields
  - Improved message formatting for instant decision-making

Customized for:
  - TSLA Options Scalping
  - 5-15 trades/day (quality over quantity)
  - $0.30-$0.60 targets
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
        logging.FileHandler("tsla_scalper_v3_3.log"),
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
            "rsi_1m": data.get("rsi_1m", "?"),
            "volume_actual": data.get("volume_actual", "?"),
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
# Helper Functions — Smart Data Extraction (V3.3)
# ──────────────────────────────────────────────────────────────────────────────

def safe_get(data, key, default="—"):
    """Get value from data, return default if missing, empty, or 'nan'."""
    val = data.get(key, "")
    if val is None or str(val).strip() == "" or str(val).strip().lower() in ("n/a", "na", "nan", "none", "?"):
        return default
    return str(val).strip()


def get_rsi_description(rsi_str):
    """Generate RSI description from numeric value."""
    try:
        rsi = float(rsi_str)
        if rsi >= 70:
            return "Overbought"
        elif rsi >= 60:
            return "Strong Bullish"
        elif rsi >= 55:
            return "Bullish"
        elif rsi >= 45:
            return "Neutral"
        elif rsi >= 40:
            return "Bearish"
        elif rsi >= 30:
            return "Strong Bearish"
        else:
            return "Oversold"
    except (ValueError, TypeError):
        return "—"


def format_rsi_line(data, tf_key, desc_key, tf_label):
    """Format a single RSI line with value + description."""
    rsi_val = safe_get(data, tf_key, "—")
    rsi_desc = safe_get(data, desc_key, "")

    # If description not provided by Pine Script, calculate it
    if rsi_desc in ("—", ""):
        rsi_desc = get_rsi_description(rsi_val)

    if rsi_val != "—":
        return f"{tf_label}: <code>{rsi_val}</code> ({rsi_desc})"
    else:
        return f"{tf_label}: —"


def format_volume_section(data):
    """Format volume section with actual value + average + description."""
    vol_actual = safe_get(data, "volume_actual", "—")
    vol_avg = safe_get(data, "volume_avg", "—")
    vol_ratio = safe_get(data, "volume_ratio", "—")
    vol_desc = safe_get(data, "volume_desc", "")
    vol_surge = safe_get(data, "volume_surge", "NO")

    # Build volume description if not provided
    if vol_desc in ("—", ""):
        try:
            ratio_num = float(vol_ratio.replace("x", ""))
            if ratio_num >= 2.0:
                vol_desc = "High Surge"
            elif ratio_num >= 1.3:
                vol_desc = "Above Avg"
            elif ratio_num >= 0.8:
                vol_desc = "Average"
            else:
                vol_desc = "Below Avg"
        except (ValueError, TypeError):
            vol_desc = "—"

    # Surge indicator
    surge_icon = " ⚡" if vol_surge == "YES" else ""

    lines = []
    if vol_actual != "—" and vol_avg != "—":
        lines.append(f"📦 <b>Volume:</b> <code>{vol_actual}</code> (Avg: {vol_avg}){surge_icon}")
    elif vol_actual != "—":
        lines.append(f"📦 <b>Volume:</b> <code>{vol_actual}</code>{surge_icon}")
    elif vol_ratio != "—":
        lines.append(f"📦 <b>Volume:</b> {vol_ratio}{surge_icon}")
    else:
        lines.append(f"📦 <b>Volume:</b> —")

    if vol_ratio != "—" and vol_desc != "—":
        lines.append(f"📊 <b>Vol Ratio:</b> <code>{vol_ratio}</code> — {vol_desc}")

    return "\n".join(lines)


def format_obv_section(data):
    """Format OBV section with multi-timeframe status."""
    obv_1m = safe_get(data, "obv_status", "—")
    obv_5m = safe_get(data, "obv_5m", "")
    obv_15m = safe_get(data, "obv_15m", "")

    parts = [f"1m: {obv_1m}"]
    if obv_5m and obv_5m not in ("—", ""):
        parts.append(f"5m: {obv_5m}")
    if obv_15m and obv_15m not in ("—", ""):
        parts.append(f"15m: {obv_15m}")

    return " | ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Message Formatter — V3.3 Enhanced
# ──────────────────────────────────────────────────────────────────────────────

def format_telegram_v3_3(data: dict) -> str:
    """Premium formatted message with full numeric data for instant decisions."""

    signal = data.get("signal", "?")
    grade  = data.get("grade", "?")
    price  = safe_get(data, "price")
    session = safe_get(data, "session")
    wave   = safe_get(data, "wave")

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
        arrow = "📈"
    elif signal == "PUT":
        sig_emoji = "🔴"
        direction = "PUT ↓ (Buy Puts)"
        arrow = "📉"
    else:
        sig_emoji = "⚪"
        direction = signal
        arrow = "📊"

    # RSI lines (all timeframes with descriptions)
    rsi_1m_line = format_rsi_line(data, "rsi_1m", "rsi_1m_desc", "1m")
    rsi_5m_line = format_rsi_line(data, "rsi_5m", "rsi_5m_desc", "5m")
    rsi_15m_line = format_rsi_line(data, "rsi_15m", "rsi_15m_desc", "15m")

    # Volume section
    volume_section = format_volume_section(data)

    # OBV section
    obv_section = format_obv_section(data)

    # MACD
    macd_status = safe_get(data, "macd_status")
    macd_hist = safe_get(data, "macd_hist", "")
    macd_line = f"{macd_status}"
    if macd_hist and macd_hist not in ("—", ""):
        macd_line += f" (Hist: {macd_hist})"

    # Candle
    candle = safe_get(data, "candle")

    # ATR
    atr_val = safe_get(data, "atr", "")

    # Momentum
    momentum = safe_get(data, "momentum", "")

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
🎯 Score: <b>{safe_get(data, 'score')}</b> | Confidence: {confidence}
📊 Trade #{trade_num} today ({remaining} remaining)

{arrow} <b>━━ TREND ANALYSIS ━━</b>

📊 <b>15m:</b> {safe_get(data, 'trend_15m')}
📊 <b>5m:</b> {safe_get(data, 'trend_5m')}

{arrow} <b>━━ INDICATORS ━━</b>

📍 <b>VWAP:</b> {safe_get(data, 'vwap_status')} (${safe_get(data, 'vwap_price')}) [{safe_get(data, 'vwap_distance')}]
📐 <b>EMA:</b> {safe_get(data, 'ema_status').replace('<', '&lt;').replace('>', '&gt;')}
📈 <b>MACD:</b> {macd_line}

📉 <b>RSI:</b>
   {rsi_1m_line}
   {rsi_5m_line}
   {rsi_15m_line}

📊 <b>OBV:</b> {obv_section}
{volume_section}
🕯 <b>Candle:</b> {candle}"""

    # Add momentum if available
    if momentum and momentum not in ("—", ""):
        message += f"\n⚡ <b>Momentum (5-bar):</b> {momentum}"

    # Add ATR if available
    if atr_val and atr_val not in ("—", ""):
        message += f"\n📏 <b>ATR:</b> ${atr_val}"

    # Entry Zone calculation (ATR-based)
    entry_zone_str = ""
    try:
        price_f = float(price)
        atr_f = float(atr_val) if atr_val and atr_val not in ('—', '') else 0.50
        # Zone = ~50% of ATR, min 10¢, max 35¢
        zone_half = max(0.10, min(0.35, round(atr_f * 0.5, 2)))
        zone_low = round(price_f - zone_half, 2)
        zone_high = round(price_f + zone_half, 2)
        zone_cents = int(zone_half * 100)
        entry_zone_str = f"📍 <b>Entry Zone:</b> <code>${zone_low:.2f}</code> — <code>${zone_high:.2f}</code> (±{zone_cents}¢)\n⏰ <i>Valid for ~2-3 min — If price moved past zone → SKIP</i>"
    except (ValueError, TypeError):
        entry_zone_str = ""

    message += f"""

💎 <b>━━ TRADE PLAN ━━</b>

🎯 Entry: <code>${price}</code>
{entry_zone_str}
🛑 SL: <code>${safe_get(data, 'stop_loss')}</code> (-{safe_get(data, 'sl_cents')}¢)
✅ TP1: <code>${safe_get(data, 'target_1')}</code> (+{safe_get(data, 'tp1_cents')}¢)
✅ TP2: <code>${safe_get(data, 'target_2')}</code> (+{safe_get(data, 'tp2_cents')}¢)

💰 <b>━━ RISK ━━</b>

📏 Max per trade: <b>{safe_get(data, 'max_risk')}</b>
📋 Contracts: <b>{safe_get(data, 'suggested_contracts')}</b>
🚫 Daily loss limit: ${safe_get(data, 'max_daily_loss', '300')}

━━━━━━━━━━━━━━━━━━━━━━━━━
🚫 <b>NO AVERAGING — NO ADDING</b> 🚫
⚠️ <i>One entry, one exit. If SL hit, move on.</i>
━━━━━━━━━━━━━━━━━━━━━━━━━"""

    return message.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Discord Formatter — V3.3
# ──────────────────────────────────────────────────────────────────────────────

def format_discord_v3_3(data: dict) -> dict:
    signal = data.get("signal", "?")
    grade = data.get("grade", "?")
    price = safe_get(data, "price")
    color = 0x00FF00 if signal == "CALL" else 0xFF0000

    # RSI formatted
    rsi_text = (
        f"1m: {safe_get(data, 'rsi_1m')} ({safe_get(data, 'rsi_1m_desc', get_rsi_description(safe_get(data, 'rsi_1m')))})\n"
        f"5m: {safe_get(data, 'rsi_5m')} ({safe_get(data, 'rsi_5m_desc', get_rsi_description(safe_get(data, 'rsi_5m')))})\n"
        f"15m: {safe_get(data, 'rsi_15m')} ({safe_get(data, 'rsi_15m_desc', get_rsi_description(safe_get(data, 'rsi_15m')))})"
    )

    # Volume formatted
    vol_text = f"{safe_get(data, 'volume_actual')} (Avg: {safe_get(data, 'volume_avg')})\nRatio: {safe_get(data, 'volume_ratio')} — {safe_get(data, 'volume_desc')}"

    embed = {
        "embeds": [{
            "title": f"{'📈' if signal == 'CALL' else '📉'} {grade} {signal} — TSLA @ ${price}",
            "color": color,
            "fields": [
                {"name": "🌊 Wave", "value": safe_get(data, "wave"), "inline": True},
                {"name": "🎯 Score", "value": safe_get(data, "score"), "inline": True},
                {"name": "📍 Session", "value": safe_get(data, "session"), "inline": True},
                {"name": "📍 VWAP", "value": f"{safe_get(data, 'vwap_status')} (${safe_get(data, 'vwap_price')})", "inline": True},
                {"name": "📐 EMA", "value": safe_get(data, "ema_status"), "inline": True},
                {"name": "📈 MACD", "value": safe_get(data, "macd_status"), "inline": True},
                {"name": "📉 RSI", "value": rsi_text, "inline": False},
                {"name": "📦 Volume", "value": vol_text, "inline": False},
                {"name": "📊 OBV", "value": format_obv_section(data), "inline": True},
                {"name": "🎯 Entry", "value": f"${price}", "inline": True},
                {"name": "📍 Entry Zone", "value": f"See Telegram for zone", "inline": True},
                {"name": "🛑 SL", "value": f"${safe_get(data, 'stop_loss')} (-{safe_get(data, 'sl_cents')}¢)", "inline": True},
                {"name": "✅ TP1/TP2", "value": f"${safe_get(data, 'target_1')} / ${safe_get(data, 'target_2')}", "inline": True},
                {"name": "📋 Contracts", "value": safe_get(data, "suggested_contracts"), "inline": True},
                {"name": "💰 Max Risk", "value": safe_get(data, "max_risk"), "inline": True},
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
        "service": "TSLA Scalper V3.3.1 (Enhanced Data Quality + Entry Zone)",
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
        send_telegram(f"⚠️ <b>Daily limit reached ({MAX_DAILY_TRADES} trades)</b>\n\nNo more alerts today. Review your trades and rest.")
        return jsonify({"status": "limit_reached", "max": MAX_DAILY_TRADES}), 200

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
    logger.info(f"Full data keys: {list(data.keys())}")

    # Track
    tracker.add_alert(data)

    # Format and send
    tg_msg = format_telegram_v3_3(data)
    tg_ok = send_telegram(tg_msg)

    dc_ok = False
    if DISCORD_WEBHOOK_URL:
        dc_ok = send_discord(format_discord_v3_3(data))

    return jsonify({
        "status": "sent",
        "telegram": "ok" if tg_ok else "failed",
        "discord": "ok" if dc_ok else "n/a",
        "trade_number": tracker.total
    }), 200


@app.route("/test", methods=["GET"])
def test():
    """Send V3.3 test alert with full data to verify formatting."""
    test_data = {
        "signal": "CALL",
        "grade": "A+",
        "symbol": "TSLA",
        "price": "245.50",
        "session": "Morning Momentum",
        "wave": "Bullish Wave (Strong)",
        "trend_15m": "Bullish CONFIRMED",
        "trend_5m": "Bullish CONFIRMED",
        "vwap_status": "Above VWAP",
        "vwap_price": "243.20",
        "vwap_distance": "+0.94%",
        "ema_status": "EMA9 above EMA21 (all TFs)",
        "macd_status": "Fresh Bull Cross",
        "macd_hist": "0.0523",
        "rsi_1m": "62.5",
        "rsi_1m_desc": "Strong Bullish",
        "rsi_5m": "60.1",
        "rsi_5m_desc": "Strong Bullish",
        "rsi_15m": "58.3",
        "rsi_15m_desc": "Bullish",
        "obv_status": "Rising (Bullish)",
        "obv_5m": "Rising",
        "obv_15m": "Rising",
        "volume_actual": "1.3M",
        "volume_avg": "890K",
        "volume_ratio": "1.46x",
        "volume_desc": "Above Avg",
        "volume_surge": "YES",
        "candle": "Strong Bullish (body>65%)",
        "momentum": "+1.25",
        "atr": "0.87",
        "score": "16/16",
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
    tg_msg = format_telegram_v3_3(test_data)
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
    logger.info("TSLA Scalper V3.3.1 (Enhanced Data Quality + Entry Zone) — Starting...")
    logger.info(f"Portfolio: $3,000 | Max Risk/Trade: 5-10%")
    logger.info(f"Daily Trade Limit: {MAX_DAILY_TRADES}")
    logger.info(f"Daily Loss Limit: ${MAX_DAILY_LOSS}")
    logger.info(f"Server: {SERVER_HOST}:{SERVER_PORT}")
    logger.info("=" * 60)
    logger.info("Endpoints:")
    logger.info(f"  POST /webhook  — TradingView alerts")
    logger.info(f"  GET  /test     — Send test alert (V3.3)")
    logger.info(f"  GET  /daily    — Daily summary")
    logger.info(f"  GET  /history  — Today's alerts")
    logger.info(f"  POST /reset    — Reset daily counter")
    logger.info("=" * 60)

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
