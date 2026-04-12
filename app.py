#!/usr/bin/env python3
"""
Smart Trading Alert Bot - V5.1 Mosquito Strategy Server
Webhook Server for TSLA Mosquito V5.1 Pine Script
Features:
  - Supports TRADE_V5 signals with Grading (A+/B/C) and Warnings
  - Multi-Timeframe Alignment: Scout / Attack / Elite / Conflict
  - Pulls real-time best Options contract from Yahoo Finance
  - Calculates Entry, Take Profit (40%), Stop Loss (50%)
  - Alternative contract when 0DTE or bad spread
  - 0DTE high-risk warning
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

MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "11"))
KEEP_ALIVE_INTERVAL = 600   # 10 minutes

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v51.log"),
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
    Fetches the best option contract from Yahoo Finance.
    Returns (primary, alternative) where alternative may be None.
    """
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options

        if not expirations:
            return None, None

        target_expiry = expirations[0]
        opt_chain = ticker.option_chain(target_expiry)

        if signal_type == "CALL":
            options = opt_chain.calls.copy()
        else:
            options = opt_chain.puts.copy()

        options['distance'] = abs(options['strike'] - float(current_price))
        candidates = options.sort_values('distance')

        if candidates.empty:
            return None, None

        best = candidates.iloc[0]

        entry_price = float(best['lastPrice'])
        ask_price = float(best['ask']) if not pd.isna(best['ask']) else 0.0
        bid_price = float(best['bid']) if not pd.isna(best['bid']) else 0.0

        if entry_price <= 0:
            entry_price = ask_price

        if entry_price > 0:
            tp_price = round(entry_price * 1.40, 2)
            sl_price = round(entry_price * 0.50, 2)
        else:
            entry_price = tp_price = sl_price = 0.0

        is_0dte = (target_expiry == get_today())

        primary = {
            "strike": float(best['strike']),
            "expiry": target_expiry,
            "symbol": best['contractSymbol'],
            "last_price": entry_price,
            "ask": ask_price,
            "bid": bid_price,
            "volume": int(best['volume']) if not pd.isna(best['volume']) else 0,
            "open_interest": int(best['openInterest']) if not pd.isna(best['openInterest']) else 0,
            "implied_volatility": float(best['impliedVolatility']) if not pd.isna(best['impliedVolatility']) else 0.0,
            "tp": tp_price,
            "sl": sl_price,
            "is_0dte": is_0dte
        }

        # Determine if alternative is needed
        spread = ask_price - bid_price
        spread_pct = (spread / bid_price) if bid_price > 0 else 0
        bad_spread = spread_pct > 0.15
        needs_alt = is_0dte or bad_spread

        alt_data = None
        if needs_alt and len(expirations) > 1:
            alt_expiry = None
            today_date = datetime.strptime(get_today(), "%Y-%m-%d")
            for exp in expirations[1:]:
                exp_date = datetime.strptime(exp, "%Y-%m-%d")
                days_out = (exp_date - today_date).days
                if 1 <= days_out <= 7:
                    alt_expiry = exp
                    break

            if not alt_expiry:
                alt_expiry = expirations[1]

            alt_chain = ticker.option_chain(alt_expiry)
            if signal_type == "CALL":
                alt_options = alt_chain.calls.copy()
            else:
                alt_options = alt_chain.puts.copy()

            alt_options['distance'] = abs(alt_options['strike'] - float(current_price))
            alt_candidates = alt_options.sort_values('distance')

            if not alt_candidates.empty:
                alt_best = alt_candidates.iloc[0]
                alt_entry = float(alt_best['lastPrice'])
                if alt_entry <= 0:
                    alt_entry = float(alt_best['ask']) if not pd.isna(alt_best['ask']) else 0.0

                if alt_entry > 0:
                    alt_tp = round(alt_entry * 1.40, 2)
                    alt_sl = round(alt_entry * 0.50, 2)
                    alt_data = {
                        "strike": float(alt_best['strike']),
                        "expiry": alt_expiry,
                        "last_price": alt_entry,
                        "tp": alt_tp,
                        "sl": alt_sl
                    }

        return primary, alt_data
    except Exception as e:
        logger.error(f"Error fetching options data: {e}")
        return None, None

# ──────────────────────────────────────────────────────────────────────────────
# MTF Alignment Helpers
# ──────────────────────────────────────────────────────────────────────────────

ALIGN_CONFIG = {
    "Elite": {
        "icon": "★★★",
        "label_ar": "توافق كامل",
        "desc_ar": "1m + 5m + 15m متوافقة",
        "color_icon": "🟣"
    },
    "Attack": {
        "icon": "★★",
        "label_ar": "توافق جيد",
        "desc_ar": "1m + 5m متوافقة",
        "color_icon": "🔵"
    },
    "Scout": {
        "icon": "★",
        "label_ar": "إشارة أولية",
        "desc_ar": "1m فقط -- مراقبة",
        "color_icon": "🟡"
    },
    "Conflict": {
        "icon": "✕",
        "label_ar": "تعارض",
        "desc_ar": "الفريمات الأعلى تعارض",
        "color_icon": "🔴"
    }
}

def get_align_info(align_level):
    """Return alignment config dict, defaulting to Scout if unknown."""
    return ALIGN_CONFIG.get(align_level, ALIGN_CONFIG["Scout"])

# ──────────────────────────────────────────────────────────────────────────────
# Message Formatters
# ──────────────────────────────────────────────────────────────────────────────

MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

def format_expiry_ar(expiry_str):
    """Convert 2026-04-17 to '17 أبريل'."""
    try:
        d = datetime.strptime(expiry_str, "%Y-%m-%d")
        return f"{d.day} {MONTHS_AR[d.month - 1]}"
    except:
        return expiry_str

def format_v5_trade_alert(data, primary_opt=None, alt_opt=None):
    """Format V5.1 Mosquito trade signal with MTF alignment + compact option lines."""
    signal  = safe_get(data, "signal", "?")
    price   = safe_get(data, "price", "?")
    grade   = safe_get(data, "grade", "C")
    bias    = safe_get(data, "bias", "--")
    cond    = safe_get(data, "cond", "--")
    session = safe_get(data, "session", "--")
    warning = safe_get(data, "warning", "None")
    align   = safe_get(data, "align", "Scout")
    bias_5m  = safe_get(data, "bias_5m", "--")
    bias_15m = safe_get(data, "bias_15m", "--")

    # Decision Logic
    decision = "ادخل بقوة" if grade == "A+" else "ادخل بحذر" if grade == "B" else "تجاوز"
    decision_icon = "🔥" if grade == "A+" else "🟢" if grade == "B" else "⚠️"

    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    warning_text = f"\n⚠️ <b>تحذير:</b> {warning}" if warning != "None" else ""

    # MTF Alignment section
    align_info = get_align_info(align)

    # Build 5m/15m status icons
    def tf_icon(bias_val, signal_dir):
        """Green check if aligned, red X if opposed, yellow dash if neutral."""
        if signal_dir == "CALL":
            if bias_val == "Bull":
                return "✅"
            elif bias_val == "Bear":
                return "❌"
            else:
                return "➖"
        else:  # PUT
            if bias_val == "Bear":
                return "✅"
            elif bias_val == "Bull":
                return "❌"
            else:
                return "➖"

    icon_5m = tf_icon(bias_5m, signal)
    icon_15m = tf_icon(bias_15m, signal)

    msg = (
        f"{decision_icon} <b>{decision}</b> -- Mosquito V5.1\n"
        f"{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>التقييم:</b> {grade} Setup\n"
        f"📈 <b>الاتجاه:</b> {bias}\n"
        f"🔍 <b>حالة السوق:</b> {cond}{warning_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{align_info['color_icon']} <b>توافق الفريمات:</b> {align_info['icon']} <b>{align}</b>\n"
        f"   1m: ✅ | 5m: {icon_5m} {bias_5m} | 15m: {icon_15m} {bias_15m}\n"
        f"   📝 {align_info['desc_ar']}\n\n"
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
        if is_0dte and (grade in ("B", "C") or "Choppy" in cond):
            msg += "⚠️ 0DTE عالي الخطورة\n"

        # Alternative contract
        if alt_opt:
            alt_date = format_expiry_ar(alt_opt['expiry'])
            msg += (
                f"\n🔄 البديل: {signal} ${alt_opt['strike']:.0f} ({alt_date})"
                f" | ${alt_opt['last_price']:.2f}"
                f" → TP ${alt_opt['tp']:.2f}"
                f" | SL ${alt_opt['sl']:.2f}\n"
                f"✅ أكثر أماناً — Spread ضيق\n"
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


def format_v5_liquidity_report(data):
    """Format V5.1 Liquidity Report."""
    time_min = safe_get(data, "time_min", "?")
    vol_avg  = safe_get(data, "vol_avg", "?")
    atr      = safe_get(data, "atr", "?")
    cond     = safe_get(data, "cond", "--")
    bias     = safe_get(data, "bias", "--")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    cond_icon = "✅" if "Trending" in cond else "⚠️"
    bias_icon = "🟢" if "Bull" in bias else "🔴" if "Bear" in bias else "⚪"

    msg = (
        f"📊 <b>تقرير السيولة -- {time_min} دقيقة من الافتتاح</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{bias_icon} <b>الاتجاه:</b> {bias}\n"
        f"💧 <b>متوسط الحجم:</b> {vol_avg}\n"
        f"📐 <b>ATR:</b> ${atr}\n"
        f"{cond_icon} <b>حالة السوق:</b> {cond}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {timestamp} ET | تقرير #{len(liquidity_reports) + 1}"
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
        "service":       "Smart Trading Alert Bot -- Mosquito V5.1 MTF",
        "version":       "5.1",
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
    align      = safe_get(data, "align", "Scout")

    logger.info(f"Received: {signal} | Type: {msg_type} | Price: ${price} | Align: {align}")

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

    # Fetch Option Data from Yahoo Finance (primary + alternative)
    primary_opt, alt_opt = get_best_option("TSLA", signal, price)

    tg_msg = format_v5_trade_alert(data, primary_opt, alt_opt)
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
        "grade":     safe_get(data, "grade", "C"),
        "align":     align
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)

    logger.info(f"SENT: {signal} @ ${price} | {align} (#{len(daily_alerts)} today)")

    return jsonify({"status": "processed", "telegram": "sent" if tg_ok else "failed", "align": align}), 200

# ──────────────────────────────────────────────────────────────────────────────
# Test Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/test_elite", methods=["GET"])
def test_elite():
    """Test Elite signal: 1m + 5m + 15m aligned, A+ grade."""
    test_data = {
        "signal": "CALL", "type": "TRADE_V5", "price": "348.50",
        "grade": "A+", "align": "Elite",
        "bias": "Bullish", "vwap": "Above VWAP (Bull Control)",
        "vol": "Strong", "mom": "Bullish (Valid)",
        "cond": "Trending (Clear)", "session": "Morning Momentum",
        "warning": "None", "bias_5m": "Bull", "bias_15m": "Bull"
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
    tg_ok = send_telegram(format_v5_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "Elite", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_attack", methods=["GET"])
def test_attack():
    """Test Attack signal: 1m + 5m aligned, B grade."""
    test_data = {
        "signal": "CALL", "type": "TRADE_V5", "price": "349.00",
        "grade": "B", "align": "Attack",
        "bias": "Bullish", "vwap": "Above VWAP (Bull Control)",
        "vol": "Normal", "mom": "Bullish (Valid)",
        "cond": "Trending (Clear)", "session": "Morning Momentum",
        "warning": "None", "bias_5m": "Bull", "bias_15m": "Neutral"
    }
    primary_opt = {
        "strike": 349.0, "expiry": "2026-04-14",
        "symbol": "TSLA260414C00349000",
        "last_price": 2.40, "ask": 2.50, "bid": 2.30,
        "volume": 5200, "open_interest": 9000,
        "implied_volatility": 0.58, "tp": 3.36, "sl": 1.20, "is_0dte": False
    }
    tg_ok = send_telegram(format_v5_trade_alert(test_data, primary_opt, None))
    return jsonify({"status": "test_sent", "level": "Attack", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_scout", methods=["GET"])
def test_scout():
    """Test Scout signal: 1m only, C grade."""
    test_data = {
        "signal": "PUT", "type": "TRADE_V5", "price": "352.00",
        "grade": "C", "align": "Scout",
        "bias": "Bearish", "vwap": "Below VWAP (Bear Control)",
        "vol": "Weak", "mom": "Bearish (Valid)",
        "cond": "Choppy (High Risk)", "session": "Midday (Slow)",
        "warning": "Weak Volume", "bias_5m": "Neutral", "bias_15m": "Neutral"
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
    tg_ok = send_telegram(format_v5_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "Scout", "telegram": "sent" if tg_ok else "failed"}), 200

@app.route("/test_conflict", methods=["GET"])
def test_conflict():
    """Test Conflict: 1m says CALL but 5m/15m oppose -- should NOT be sent in production."""
    test_data = {
        "signal": "CALL", "type": "TRADE_V5", "price": "350.00",
        "grade": "C", "align": "Conflict",
        "bias": "Bullish", "vwap": "Above VWAP (Bull Control)",
        "vol": "Normal", "mom": "Bullish (Valid)",
        "cond": "Choppy (High Risk)", "session": "Midday (Slow)",
        "warning": "MTF Conflict -- Higher TF opposes",
        "bias_5m": "Bear", "bias_15m": "Bear"
    }
    tg_ok = send_telegram(format_v5_trade_alert(test_data, None, None))
    return jsonify({"status": "test_sent", "level": "Conflict", "telegram": "sent" if tg_ok else "failed"}), 200

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
