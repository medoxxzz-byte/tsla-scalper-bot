"""
Smart Trading Alert Bot - V5.2 Mosquito Swamp Strategy Server
Webhook Server for TSLA Mosquito Swamp V5.2 Pine Script
Features:
  - Supports TRADE_V5_2 signals with Star Grading (1-5)
  - Pre-Alerts (KILL_ZONE) for early preparation
  - Pulls real-time best Options contract from Yahoo Finance
  - Calculates Entry, Take Profit (40%), Stop Loss (50%)
  - Alternative contract when 0DTE or bad spread
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

MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "11"))
KEEP_ALIVE_INTERVAL = 600   # 10 minutes

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v52.log"),
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
    "last_price": 0.0,
    "last_updated": None
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def get_et_now():
    return datetime.now(timezone.utc) - timedelta(hours=4)

def get_today():
    return get_et_now().strftime("%Y-%m-%d")

def reset_daily_if_needed():
    global daily_date, daily_alerts, blocked_today, liquidity_reports
    today = get_today()
    if daily_date != today:
        daily_date = today
        daily_alerts = []
        blocked_today = []
        liquidity_reports = []
        logger.info(f"--- Reset daily limits for {today} ---")

def safe_get(data, key, default=""):
    val = data.get(key, default)
    return str(val).strip() if val is not None else default

# ──────────────────────────────────────────────────────────────────────────────
# Options Data (Yahoo Finance)
# ──────────────────────────────────────────────────────────────────────────────

def get_best_option(ticker_symbol, signal_type, current_price):
    """Fetch nearest OTM/ATM option from Yahoo Finance."""
    try:
        current_price = float(current_price)
        tkr = yf.Ticker(ticker_symbol)
        exp_dates = tkr.options
        if not exp_dates:
            return None, None

        # Filter out past dates just in case
        today_str = get_today()
        valid_dates = [d for d in exp_dates if d >= today_str]
        if not valid_dates:
            return None, None

        # Primary = closest expiry (0DTE if today)
        primary_expiry = valid_dates[0]
        # Alternative = next expiry (at least +1 day)
        alt_expiry = valid_dates[1] if len(valid_dates) > 1 else None

        primary_opt = _find_contract(tkr, primary_expiry, signal_type, current_price)
        alt_opt     = _find_contract(tkr, alt_expiry, signal_type, current_price) if alt_expiry else None

        return primary_opt, alt_opt

    except Exception as e:
        logger.error(f"Error fetching option: {e}")
        return None, None

def _find_contract(tkr, expiry, signal_type, current_price):
    try:
        opt_chain = tkr.option_chain(expiry)
        df = opt_chain.calls if signal_type == "CALL" else opt_chain.puts
        if df.empty:
            return None

        # Find closest strike
        df['strike_diff'] = abs(df['strike'] - current_price)
        
        # Prefer slightly ITM/ATM
        if signal_type == "CALL":
            valid_strikes = df[df['strike'] <= current_price * 1.005] # Max 0.5% OTM
        else:
            valid_strikes = df[df['strike'] >= current_price * 0.995]
            
        if valid_strikes.empty:
            valid_strikes = df

        best_row = valid_strikes.loc[valid_strikes['strike_diff'].idxmin()]

        last_price = float(best_row.get('lastPrice', 0))
        ask = float(best_row.get('ask', 0))
        bid = float(best_row.get('bid', 0))
        
        # Fallback to ask if last_price is 0
        if last_price <= 0 and ask > 0:
            last_price = ask

        # TP / SL
        tp = last_price * 1.40
        sl = last_price * 0.50

        is_0dte = (expiry == get_today())

        return {
            "strike": float(best_row['strike']),
            "expiry": expiry,
            "symbol": best_row['contractSymbol'],
            "last_price": last_price,
            "ask": ask,
            "bid": bid,
            "volume": int(best_row.get('volume', 0)),
            "open_interest": int(best_row.get('openInterest', 0)),
            "implied_volatility": float(best_row.get('impliedVolatility', 0)),
            "tp": tp,
            "sl": sl,
            "is_0dte": is_0dte
        }
    except Exception as e:
        logger.error(f"Error finding contract for {expiry}: {e}")
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────────────────────────

MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

def format_expiry_ar(expiry_str):
    try:
        d = datetime.strptime(expiry_str, "%Y-%m-%d")
        return f"{d.day} {MONTHS_AR[d.month - 1]}"
    except:
        return expiry_str

def format_v5_2_trade_alert(data, primary_opt=None, alt_opt=None):
    """Format V5.2 Mosquito Swamp trade signal with Stars."""
    signal  = safe_get(data, "signal", "?")
    price   = safe_get(data, "price", "?")
    stars   = safe_get(data, "stars", "1")
    bias    = safe_get(data, "bias", "--")
    cond    = safe_get(data, "cond", "--")
    session = safe_get(data, "session", "--")
    vol     = safe_get(data, "vol", "--")
    bias_5m  = safe_get(data, "bias_5m", "--")
    bias_15m = safe_get(data, "bias_15m", "--")

    # Star Logic
    try:
        stars_int = int(stars)
    except:
        stars_int = 1
        
    stars_display = "⭐" * stars_int
    
    if stars_int >= 4:
        decision = "إشارة قوية"
        decision_icon = "🔥"
    elif stars_int == 3:
        decision = "إشارة جيدة"
        decision_icon = "🟢"
    else:
        decision = "إشارة مبدئية / خطرة"
        decision_icon = "⚠️"

    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    # Build 5m/15m status icons
    def tf_icon(bias_val, signal_dir):
        if signal_dir == "CALL":
            return "✅" if bias_val == "Bull" else "❌" if bias_val == "Bear" else "➖"
        else:
            return "✅" if bias_val == "Bear" else "❌" if bias_val == "Bull" else "➖"

    icon_5m = tf_icon(bias_5m, signal)
    icon_15m = tf_icon(bias_15m, signal)

    msg = (
        f"{decision_icon} <b>{decision}</b> -- Mosquito Swamp V5.2\n"
        f"{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌟 <b>التقييم:</b> {stars_display} ({stars}/5)\n"
        f"📈 <b>الاتجاه:</b> {bias}\n"
        f"📝 <b>ملاحظات:</b> {cond} | Vol: {vol}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟣 <b>توافق الفريمات:</b>\n"
        f"   1m: ✅ | 5m: {icon_5m} {bias_5m} | 15m: {icon_15m} {bias_15m}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if primary_opt and primary_opt.get('last_price', 0) > 0:
        is_0dte = primary_opt.get('is_0dte', False)
        dte_label = "0DTE" if is_0dte else format_expiry_ar(primary_opt['expiry'])

        msg += (
            f"\n🎯 الأساسي: {signal} ${primary_opt['strike']:.0f} {dte_label}"
            f" | ${primary_opt['last_price']:.2f}"
            f" → TP ${primary_opt['tp']:.2f}"
            f" | SL ${primary_opt['sl']:.2f}\n"
        )

        # 0DTE warning
        if is_0dte and (stars_int < 3 or "Choppy" in cond):
            msg += "⚠️ 0DTE عالي الخطورة - يفضل العقد البديل\n"

        # Alternative contract
        if alt_opt:
            alt_date = format_expiry_ar(alt_opt['expiry'])
            msg += (
                f"\n🔄 البديل: {signal} ${alt_opt['strike']:.0f} ({alt_date})"
                f" | ${alt_opt['last_price']:.2f}"
                f" → TP ${alt_opt['tp']:.2f}"
                f" | SL ${alt_opt['sl']:.2f}\n"
                f"✅ أكثر أماناً — وقت أطول\n"
            )

        msg += "\n━━━━━━━━━━━━━━━━━━━━━\n"
    else:
        msg += (
            "\n🎯 <b>العقد المقترح:</b>\n"
            "اختر أقرب Strike للسعر (ATM) ينتهي اليوم.\n"
            "الهدف 40% والوقف 50%.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
        )

    msg += (
        f"🕐 {timestamp} ET | {session}\n"
        f"⏱ <i>الوقف الزمني: 10 دقائق -- اطلع اذا ما تحرك السعر</i>"
    )
    return msg

def format_kill_zone_alert(data):
    """Format V5.2 Kill Zone Pre-Alert."""
    kz_type = safe_get(data, "type", "?")
    price   = safe_get(data, "price", "?")
    
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")
    
    if kz_type == "PRE_CALL":
        icon = "🟢"
        title = "PRE-CALL KILL ZONE"
        desc = "السعر عند دعم مهم مع بوادر ارتداد للأعلى. راقب MACD لتقاطع إيجابي."
    else:
        icon = "🔴"
        title = "PRE-PUT KILL ZONE"
        desc = "السعر عند مقاومة مهمة مع بوادر ارتداد للأسفل. راقب MACD لتقاطع سلبي."

    msg = (
        f"⚠️ <b>تنبيه تحضيري -- {title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>TSLA @ <code>${price}</code></b>\n\n"
        f"💡 {desc}\n\n"
        f"<i>ملاحظة: هذه ليست إشارة دخول، بل تنبيه للاستعداد.</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {timestamp} ET"
    )
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
        "service":       "Smart Trading Alert Bot -- Mosquito Swamp V5.2",
        "version":       "5.2",
        "alerts_today":  len(daily_alerts),
        "blocked_today": len(blocked_today),
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

    # ── KILL ZONE PRE-ALERT ──
    if signal == "KILL_ZONE":
        tg_msg = format_kill_zone_alert(data)
        tg_ok  = send_telegram(tg_msg)
        return jsonify({"status": "kill_zone_sent", "telegram": "sent" if tg_ok else "failed"}), 200

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

    # Fetch Option Data from Yahoo Finance (primary + alternative)
    primary_opt, alt_opt = get_best_option("TSLA", signal, price)

    tg_msg = format_v5_2_trade_alert(data, primary_opt, alt_opt)
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
        "stars":     safe_get(data, "stars", "1")
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)

    logger.info(f"SENT: {signal} @ ${price} | Stars: {entry['stars']} (#{len(daily_alerts)} today)")

    return jsonify({"status": "processed", "telegram": "sent" if tg_ok else "failed"}), 200

# ──────────────────────────────────────────────────────────────────────────────
# Test Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/test_5stars", methods=["GET"])
def test_5stars():
    """Test 5-Star signal."""
    test_data = {
        "signal": "CALL", "type": "TRADE_V5_2", "price": "348.50",
        "stars": "5", "bias": "Bullish", "vwap": "Above VWAP (Bull Control)",
        "vol": "Surge", "mom": "Bullish (Valid)",
        "cond": "Trending (Clear)", "session": "Morning Momentum",
        "bias_5m": "Bull", "bias_15m": "Bull"
    }
    primary_opt = {
        "strike": 348.0, "expiry": get_today(),
        "symbol": "TSLA260414C00348000",
        "last_price": 1.50, "ask": 1.60, "bid": 1.40,
        "volume": 8500, "open_interest": 12000,
        "implied_volatility": 0.65, "tp": 2.10, "sl": 0.75, "is_0dte": True
    }
    alt_opt = {
        "strike": 348.0, "expiry": "2026-04-17",
        "last_price": 3.20, "tp": 4.48, "sl": 1.60
    }
    tg_ok = send_telegram(format_v5_2_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "5 Stars", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_3stars", methods=["GET"])
def test_3stars():
    """Test 3-Star signal."""
    test_data = {
        "signal": "PUT", "type": "TRADE_V5_2", "price": "352.00",
        "stars": "3", "bias": "Bearish", "vwap": "Below VWAP (Bear Control)",
        "vol": "Normal", "mom": "Bearish (Valid)",
        "cond": "Choppy (Note)", "session": "Midday (Slow)",
        "bias_5m": "Bear", "bias_15m": "Neutral"
    }
    primary_opt = {
        "strike": 352.0, "expiry": get_today(),
        "symbol": "TSLA260414P00352000",
        "last_price": 0.80, "ask": 0.90, "bid": 0.70,
        "volume": 1200, "open_interest": 3000,
        "implied_volatility": 0.72, "tp": 1.12, "sl": 0.40, "is_0dte": True
    }
    alt_opt = {
        "strike": 352.0, "expiry": "2026-04-17",
        "last_price": 2.10, "tp": 2.94, "sl": 1.05
    }
    tg_ok = send_telegram(format_v5_2_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "3 Stars", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_kz_call", methods=["GET"])
def test_kz_call():
    """Test Pre-Call Kill Zone."""
    test_data = {"signal": "KILL_ZONE", "type": "PRE_CALL", "price": "345.00"}
    tg_ok = send_telegram(format_kill_zone_alert(test_data))
    return jsonify({"status": "test_sent", "level": "KZ Call", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_kz_put", methods=["GET"])
def test_kz_put():
    """Test Pre-Put Kill Zone."""
    test_data = {"signal": "KILL_ZONE", "type": "PRE_PUT", "price": "355.00"}
    tg_ok = send_telegram(format_kill_zone_alert(test_data))
    return jsonify({"status": "test_sent", "level": "KZ Put", "telegram": "sent" if tg_ok else "failed"}), 200

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
