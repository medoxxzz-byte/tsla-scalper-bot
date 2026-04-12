#!/usr/bin/env python3
"""
Smart Trading Alert Bot - V5.0 Mosquito Strategy Server
Webhook Server for TSLA Mosquito V5.0 Pine Script
Features:
  - Supports TRADE_V5 signals with Grading (A+/B/C) and Warnings
  - Pulls real-time best Options contract from Yahoo Finance
  - Calculates Entry, Take Profit (40%), Stop Loss (50%)
  - Backward compatible with V4.0 and V3.3 signals
  - Arabic Telegram messages
"""

import os
import json
import time
import logging
import threading
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
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance")
    import yfinance as yf

try:
    import pandas as pd
except ImportError:
    os.system("pip install pandas")
    import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")

SERVER_HOST    = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT    = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

COOLDOWN_SECONDS_SIMILAR = 1500   # 25 min between same-direction signals
COOLDOWN_MIN_GAP         = 30     # minimum 30s between any two alerts

MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "11")) # Updated to 11 per user request
KEEP_ALIVE_INTERVAL = 600   # 10 minutes

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v50.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Flask App & State
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

alert_history = []
MAX_HISTORY   = 100

last_alert_time   = 0
last_alert_price  = ""
last_alert_signal = ""
last_call_time = 0
last_put_time  = 0

daily_alerts  = []
daily_date    = ""
blocked_today = []
liquidity_reports = []

market_state = {
    "last_price":   "--",
    "last_updated": "--",
    "bias":         "--",
    "volume":       "--",
    "momentum":     "--",
    "condition":    "--"
}

# ──────────────────────────────────────────────────────────────────────────────
# Time Utilities
# ──────────────────────────────────────────────────────────────────────────────

def get_et_now():
    return datetime.now(timezone.utc) + timedelta(hours=-4)

def get_today():
    return get_et_now().strftime("%Y-%m-%d")

def reset_daily_if_needed():
    global daily_alerts, daily_date, blocked_today, liquidity_reports
    today = get_today()
    if daily_date != today:
        daily_date        = today
        daily_alerts      = []
        blocked_today     = []
        liquidity_reports = []
        logger.info(f"New trading day: {today} -- counters reset")

def safe_get(data, key, default="--"):
    val = data.get(key, "")
    if val is None or str(val).strip() in ("", "--", "\u2014"):
        return default
    s = str(val).strip()
    if s.lower() in ("n/a", "na", "nan", "none", "undefined", "null"):
        return default
    return s

# ──────────────────────────────────────────────────────────────────────────────
# Yahoo Finance Options Fetcher
# ──────────────────────────────────────────────────────────────────────────────

def get_best_option(symbol, signal_type, current_price):
    """
    Fetches the best option contract from Yahoo Finance based on Mosquito Strategy:
    - Expiry: 0DTE or closest available
    - Strike: ATM or first ITM
    - Returns contract details.
    """
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        
        if not expirations:
            return None
            
        # Get the closest expiration
        target_expiry = expirations[0]
        opt_chain = ticker.option_chain(target_expiry)
        
        if signal_type == "CALL":
            options = opt_chain.calls
            # Sort by absolute distance to current price
            options['distance'] = abs(options['strike'] - float(current_price))
            # Filter for ATM or slightly ITM (strike <= current_price)
            # If no ITM, just get closest
            candidates = options.sort_values('distance')
        else: # PUT
            options = opt_chain.puts
            options['distance'] = abs(options['strike'] - float(current_price))
            candidates = options.sort_values('distance')
            
        if candidates.empty:
            return None
            
        best_contract = candidates.iloc[0]
        
        # Calculate TP and SL
        entry_price = float(best_contract['lastPrice'])
        if entry_price <= 0:
            entry_price = float(best_contract['ask']) # fallback
            
        if entry_price > 0:
            tp_price = entry_price * 1.40
            sl_price = entry_price * 0.50
        else:
            entry_price = tp_price = sl_price = 0.0
            
        return {
            "strike": float(best_contract['strike']),
            "expiry": target_expiry,
            "symbol": best_contract['contractSymbol'],
            "last_price": entry_price,
            "volume": int(best_contract['volume']) if not pd.isna(best_contract['volume']) else 0,
            "open_interest": int(best_contract['openInterest']) if not pd.isna(best_contract['openInterest']) else 0,
            "implied_volatility": float(best_contract['impliedVolatility']) if not pd.isna(best_contract['impliedVolatility']) else 0.0,
            "tp": tp_price,
            "sl": sl_price
        }
    except Exception as e:
        logger.error(f"Error fetching options data: {e}")
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Message Formatters
# ──────────────────────────────────────────────────────────────────────────────

def format_v5_trade_alert(data, option_data=None):
    """Format V5.0 Mosquito trade signal."""
    signal  = safe_get(data, "signal", "?")
    price   = safe_get(data, "price", "?")
    grade   = safe_get(data, "grade", "C")
    bias    = safe_get(data, "bias", "--")
    cond    = safe_get(data, "cond", "--")
    session = safe_get(data, "session", "--")
    warning = safe_get(data, "warning", "None")

    # Decision Logic
    decision = "ادخل بقوة" if grade == "A+" else "ادخل بحذر" if grade == "B" else "تجاوز"
    decision_icon = "🔥" if grade == "A+" else "🟢" if grade == "B" else "⚠️"
    
    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")
    
    warning_text = f"\n⚠️ <b>تحذير:</b> {warning}" if warning != "None" else ""

    msg = f"""{decision_icon} <b>{decision}</b> -- Mosquito V5.0
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

📊 <b>التقييم:</b> {grade} Setup
📈 <b>الاتجاه:</b> {bias}
🔍 <b>حالة السوق:</b> {cond}{warning_text}

━━━━━━━━━━━━━━━━━━━━━\n"""

    if option_data and option_data['last_price'] > 0:
        msg += f"""🎯 <b>أفضل عقد مقترح:</b>
🏷 <b>العقد:</b> {signal} ${option_data['strike']} (انتهاء {option_data['expiry']})
💵 <b>الدخول:</b> <code>${option_data['last_price']:.2f}</code>
✅ <b>الهدف (40%):</b> <code>${option_data['tp']:.2f}</code>
🛑 <b>الوقف (50%):</b> <code>${option_data['sl']:.2f}</code>
📊 <b>السيولة:</b> Vol {option_data['volume']} | OI {option_data['open_interest']}

━━━━━━━━━━━━━━━━━━━━━\n"""
    else:
        msg += """🎯 <b>العقد المقترح:</b>
اختر أقرب Strike للسعر (ATM) ينتهي اليوم.
الهدف 40% والوقف 50%.

━━━━━━━━━━━━━━━━━━━━━\n"""

    msg += f"""🕐 {timestamp} ET | {session}
⏱ <i>الوقف الزمني: 10 دقائق -- اطلع اذا ما تحرك السعر</i>"""
    return msg

def format_v5_liquidity_report(data):
    """Format V5.0 Liquidity Report."""
    time_min = safe_get(data, "time_min", "?")
    vol_avg  = safe_get(data, "vol_avg", "?")
    atr      = safe_get(data, "atr", "?")
    cond     = safe_get(data, "cond", "--")
    bias     = safe_get(data, "bias", "--")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    cond_icon = "✅" if "Trending" in cond else "⚠️"
    bias_icon = "🟢" if "Bull" in bias else "🔴" if "Bear" in bias else "⚪"

    msg = f"""📊 <b>تقرير السيولة -- {time_min} دقيقة من الافتتاح</b>
━━━━━━━━━━━━━━━━━━━━━

{bias_icon} <b>الاتجاه:</b> {bias}
💧 <b>متوسط الحجم:</b> {vol_avg}
📐 <b>ATR:</b> ${atr}
{cond_icon} <b>حالة السوق:</b> {cond}

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تقرير #{len(liquidity_reports) + 1}"""
    return msg

# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = http_requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram: message sent successfully")
            return True
        else:
            logger.error(f"Telegram error: {resp.status_code} -- {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Filters
# ──────────────────────────────────────────────────────────────────────────────

def check_data_quality(data):
    signal = safe_get(data, "signal", "")
    price  = safe_get(data, "price", "")
    if not signal or signal in ("", "--", "?", "UNKNOWN"):
        return False, "بيانات ناقصة (لا يوجد اشارة)"
    if not price or price in ("", "--", "?"):
        return False, "بيانات ناقصة (لا يوجد سعر)"
    return True, ""

def check_cooldown(data):
    global last_alert_time, last_alert_price, last_alert_signal
    global last_call_time, last_put_time

    now     = time.time()
    elapsed = now - last_alert_time
    signal  = safe_get(data, "signal", "")
    current_price = safe_get(data, "price", "")
    
    if current_price == last_alert_price and signal == last_alert_signal and elapsed < COOLDOWN_MIN_GAP:
        return False, f"مكرر (نفس السعر {current_price} بفارق {elapsed:.0f}ث)"

    if elapsed < COOLDOWN_MIN_GAP:
        return False, f"سريع جدا ({elapsed:.0f}ث < {COOLDOWN_MIN_GAP}ث)"

    if signal == "CALL":
        if now - last_call_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_call_time)
            return False, f"CALL cooldown -- انتظر {remaining/60:.0f} دقيقة"
    elif signal == "PUT":
        if now - last_put_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_put_time)
            return False, f"PUT cooldown -- انتظر {remaining/60:.0f} دقيقة"

    return True, ""

def check_daily_limit(data=None):
    reset_daily_if_needed()
    if len(daily_alerts) >= MAX_DAILY_ALERTS:
        return False, f"وصلت الحد اليومي ({MAX_DAILY_ALERTS} تنبيهات)"
    return True, ""

def apply_filters(data):
    for check in [check_data_quality, check_cooldown, check_daily_limit]:
        ok, reason = check(data)
        if not ok:
            return False, reason
    return True, ""

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    reset_daily_if_needed()
    return jsonify({
        "status":        "running",
        "service":       "Smart Trading Alert Bot -- Mosquito V5.0",
        "version":       "5.0",
        "alerts_today":  len(daily_alerts),
        "blocked_today": len(blocked_today),
        "reports_today": len(liquidity_reports),
        "remaining":     MAX_DAILY_ALERTS - len(daily_alerts),
        "timestamp":     datetime.now(timezone.utc).isoformat()
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    global last_alert_time, last_alert_price, last_alert_signal
    global last_call_time, last_put_time

    if WEBHOOK_SECRET:
        auth = request.headers.get("X-Webhook-Secret", "")
        if auth != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json() if request.is_json else json.loads(request.data.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return jsonify({"error": "Parse error"}), 400

    signal     = safe_get(data, "signal", "?")
    price      = safe_get(data, "price", "?")
    msg_type   = safe_get(data, "type", "TRADE")

    logger.info(f"Received: {signal} | Type: {msg_type} | Price: ${price}")

    if price not in ("?", "--"):
        market_state["last_price"]   = price
        market_state["last_updated"] = datetime.now(timezone.utc).isoformat()

    # ── LIQUIDITY REPORT ──
    if msg_type in ("LIQUIDITY", "LIQUIDITY_V5") and signal == "REPORT":
        tg_msg = format_v5_liquidity_report(data)
        tg_ok  = send_telegram(tg_msg)
        liquidity_reports.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_min":  safe_get(data, "time_min", "?"),
            "bias":      safe_get(data, "bias", "--"),
            "condition": safe_get(data, "cond", "--")
        })
        return jsonify({"status": "report_sent", "telegram": "sent" if tg_ok else "failed"}), 200

    # ── TRADE SIGNAL ──
    try:
        passed, rejection_reason = apply_filters(data)
    except Exception as e:
        logger.error(f"apply_filters error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 200

    if not passed:
        logger.info(f"BLOCKED: {rejection_reason}")
        blocked_today.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal":    signal,
            "price":     price,
            "reason":    rejection_reason
        })
        return jsonify({"status": "blocked", "reason": rejection_reason}), 200

    # Fetch Option Data from Yahoo Finance
    option_data = get_best_option("TSLA", signal, price)
    
    tg_msg = format_v5_trade_alert(data, option_data)
    tg_ok = send_telegram(tg_msg)

    # Update state
    now = time.time()
    last_alert_time   = now
    last_alert_price  = price
    last_alert_signal = signal
    if signal == "CALL":
        last_call_time = now
    elif signal == "PUT":
        last_put_time = now

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal":    signal,
        "price":     price,
        "grade":     safe_get(data, "grade", "C")
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)

    logger.info(f"SENT: {signal} @ ${price} (#{len(daily_alerts)} today)")

    return jsonify({"status": "processed", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_v5", methods=["GET"])
def test_v5_alert():
    """Test V5.0 CALL signal."""
    test_data = {
        "signal": "CALL",
        "type":   "TRADE_V5",
        "price":  "346.50",
        "grade":  "A+",
        "bias":   "Bullish",
        "vwap":   "Above VWAP (Bull Control)",
        "vol":    "Strong",
        "mom":    "Bullish (Valid)",
        "cond":   "Trending (Clear)",
        "session": "Morning Momentum",
        "warning": "None"
    }
    # Mock option data for testing
    option_data = {
        "strike": 347.5,
        "expiry": "2026-04-10",
        "symbol": "TSLA260410C00347500",
        "last_price": 1.50,
        "volume": 8500,
        "open_interest": 12000,
        "tp": 2.10,
        "sl": 0.75
    }
    tg_ok = send_telegram(format_v5_trade_alert(test_data, option_data))
    return jsonify({"status": "test_sent", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/reset", methods=["GET"])
def reset():
    global daily_alerts, daily_date, blocked_today, liquidity_reports
    global last_call_time, last_put_time, last_alert_price, last_alert_signal, last_alert_time
    daily_alerts      = []
    blocked_today     = []
    liquidity_reports = []
    daily_date        = get_today()
    last_call_time    = 0
    last_put_time     = 0
    last_alert_price  = ""
    last_alert_signal = ""
    last_alert_time   = 0
    return jsonify({"status": "reset", "message": "Cleared"})

def keep_alive_worker():
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
            if railway_url:
                http_requests.get(f"https://{railway_url}/", timeout=10)
        except:
            pass

if __name__ == "__main__":
    t = threading.Thread(target=keep_alive_worker, daemon=True)
    t.start()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
