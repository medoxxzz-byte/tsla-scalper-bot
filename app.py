#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
Smart Trading Alert Bot — V4.0 Phase 1 (Reversal Detection + Smart Filters)
Webhook Server
══════════════════════════════════════════════════════════════════════════════
Phase 1 Features:
  ✅ Arabic alert messages (concise, fast-read)
  ✅ Reversal Detection alerts (separate format)
  ✅ Volume hard filter (blocks < 0.5x)
  ✅ 60-second cooldown (prevents duplicates)
  ✅ Empty data filter (blocks incomplete alerts)
  ✅ Signal verdict: ادخل / انتبه / تجاوز
  ✅ Rejection reason for weak/blocked alerts
  ✅ Accumulation/Distribution + Institutional activity
  ✅ Keep-alive ping (prevents Render cold starts)
  ✅ Max 10 alerts/day limit

Phase 2 (later):
  ⏸ Bollinger Bands / VWAP Bands / Stochastic
  ⏸ Hourly market analysis digest

Usage:
    1. Set environment variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    2. Run: python3 webhook_server_v4_phase1.py
    3. Set TradingView webhook URL to: http://YOUR_SERVER:5000/webhook
══════════════════════════════════════════════════════════════════════════════
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
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "5000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# V4.0 Phase 1 Settings
COOLDOWN_SECONDS = 60          # Minimum seconds between alerts
MIN_VOLUME_RATIO = 0.5         # Block alerts below this volume ratio
MAX_DAILY_ALERTS = 10          # Max alerts per day
KEEP_ALIVE_INTERVAL = 600      # Ping self every 10 minutes

# Risk Management (from trader profile)
MAX_RISK_PER_TRADE = 50        # $50 max
MAX_DAILY_LOSS = 150           # $150 max
PORTFOLIO_SIZE = 1000          # $1000

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v4.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Flask App
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# State Management
# ──────────────────────────────────────────────────────────────────────────────

alert_history = []
MAX_HISTORY = 50

# Cooldown tracking
last_alert_time = 0
last_alert_price = ""
last_alert_signal = ""

# Daily tracking
daily_alerts = []
daily_date = ""

# Blocked alerts tracking (for stats)
blocked_today = []


def get_today():
    """Get today's date in ET timezone."""
    et_offset = timedelta(hours=-4)  # EDT
    now_et = datetime.now(timezone.utc) + et_offset
    return now_et.strftime("%Y-%m-%d")


def reset_daily_if_needed():
    """Reset daily counters if new day."""
    global daily_alerts, daily_date, blocked_today
    today = get_today()
    if daily_date != today:
        daily_date = today
        daily_alerts = []
        blocked_today = []
        logger.info(f"New trading day: {today} — counters reset")


def add_to_history(data, verdict):
    """Store alert in history."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": data.get("signal", "?"),
        "signal_type": data.get("signal_type", "TREND"),
        "grade": data.get("grade", "?"),
        "price": data.get("price", "?"),
        "score": data.get("score", data.get("reversal_score", "?")),
        "volume_ratio": data.get("volume_ratio", "?"),
        "verdict": verdict
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)


# ──────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def safe_get(data, key, default="—"):
    """Get value safely, treating empty/null/placeholder values as missing."""
    val = data.get(key, "")
    if val is None or str(val).strip() == "" or str(val).strip() == "—":
        return default
    s = str(val).strip()
    if s.lower() in ("n/a", "na", "nan", "none", "undefined", "null"):
        return default
    return s


def parse_volume_ratio(data):
    """Extract numeric volume ratio from string like '1.37x'."""
    try:
        vr = safe_get(data, "volume_ratio", "0")
        return float(vr.replace("x", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_score(data):
    """Extract numeric score from string like '15/16'."""
    try:
        score_str = safe_get(data, "score", "0")
        return int(score_str.split("/")[0])
    except (ValueError, TypeError):
        return 0


def parse_reversal_score(data):
    """Extract numeric reversal score from string like '9/10'."""
    try:
        score_str = safe_get(data, "reversal_score", "0")
        return int(score_str.split("/")[0])
    except (ValueError, TypeError):
        return 0


def get_verdict(data):
    """
    Determine signal verdict: ادخل / انتبه / تجاوز
    
    Logic:
    - TREND: Grade A+/A + Volume ≥ 1.0x → ادخل
    - TREND: Grade A+/A/B+ + Volume ≥ 0.5x → انتبه
    - REVERSAL: Score ≥ 9 + Volume ≥ 1.0x → ادخل
    - REVERSAL: Score ≥ 8 + Volume ≥ 0.5x → انتبه
    - Everything else → تجاوز
    """
    signal_type = safe_get(data, "signal_type", "TREND")
    vol_ratio = parse_volume_ratio(data)

    if signal_type == "REVERSAL":
        rev_score = parse_reversal_score(data)
        if rev_score >= 9 and vol_ratio >= 1.0:
            return "ادخل", "ENTER"
        elif rev_score >= 8 and vol_ratio >= 0.5:
            return "انتبه", "WATCH"
        else:
            return "تجاوز", "SKIP"
    else:
        grade = safe_get(data, "grade", "C")
        if grade in ("A+", "A") and vol_ratio >= 1.0:
            return "ادخل", "ENTER"
        elif grade in ("A+", "A", "B+") and vol_ratio >= 0.5:
            return "انتبه", "WATCH"
        else:
            return "تجاوز", "SKIP"


def get_rejection_reasons(data):
    """Get concise reasons why a signal might be weak."""
    reasons = []
    vol_ratio = parse_volume_ratio(data)
    signal = safe_get(data, "signal", "")
    macd_hist = safe_get(data, "macd_hist", "0")

    # Volume weakness
    if vol_ratio < 0.5:
        reasons.append(f"سيولة ضعيفة ({vol_ratio:.1f}x)")
    elif vol_ratio < 0.8:
        reasons.append(f"سيولة أقل من المتوسط ({vol_ratio:.1f}x)")

    # MACD contradiction
    try:
        macd_val = float(macd_hist)
        if signal == "CALL" and macd_val < -0.01:
            reasons.append("MACD سلبي")
        elif signal == "PUT" and macd_val > 0.01:
            reasons.append("MACD إيجابي")
    except (ValueError, TypeError):
        pass

    # RSI extreme
    try:
        rsi = float(safe_get(data, "rsi_1m", "50"))
        if signal == "CALL" and rsi >= 75:
            reasons.append(f"تشبع شراء ({rsi:.0f})")
        elif signal == "PUT" and rsi <= 25:
            reasons.append(f"تشبع بيع ({rsi:.0f})")
    except (ValueError, TypeError):
        pass

    # OBV contradiction
    obv_1m = safe_get(data, "obv_1m", "")
    if signal == "CALL" and obv_1m == "Falling":
        reasons.append("OBV هابط")
    elif signal == "PUT" and obv_1m == "Rising":
        reasons.append("OBV صاعد")

    return reasons


def get_liquidity_analysis(data):
    """Analyze liquidity: تجميع / تصريف / حيادي."""
    accum_dist = safe_get(data, "accum_dist", "Neutral")
    if accum_dist == "Accumulation":
        return "تجميع 📈"
    elif accum_dist == "Distribution":
        return "تصريف 📉"
    else:
        return "حيادي ➡️"


def get_volume_label(vol_ratio):
    """Get Arabic volume description."""
    if vol_ratio >= 2.0:
        return "ارتفاع قوي 🔥"
    elif vol_ratio >= 1.3:
        return "فوق المتوسط ✅"
    elif vol_ratio >= 0.8:
        return "متوسط ⚡"
    elif vol_ratio >= 0.5:
        return "أقل من المتوسط ⚠️"
    else:
        return "ضعيف جداً 🚫"


# ──────────────────────────────────────────────────────────────────────────────
# Message Formatters — Arabic (V4.0 Phase 1)
# ──────────────────────────────────────────────────────────────────────────────

def format_trend_alert(data):
    """Format trend signal in Arabic — concise and fast-read."""
    signal = safe_get(data, "signal", "?")
    grade = safe_get(data, "grade", "?")
    price = safe_get(data, "price", "?")
    score = safe_get(data, "score", "?")

    verdict_ar, verdict_en = get_verdict(data)
    reasons = get_rejection_reasons(data)
    liquidity = get_liquidity_analysis(data)

    vol_ratio = parse_volume_ratio(data)
    vol_actual = safe_get(data, "volume_actual", "—")
    vol_avg = safe_get(data, "volume_avg", "—")
    vol_label = get_volume_label(vol_ratio)

    # Verdict emoji
    if verdict_en == "ENTER":
        verdict_icon = "💚"
    elif verdict_en == "WATCH":
        verdict_icon = "🟡"
    else:
        verdict_icon = "🔴"

    # Signal direction
    sig_icon = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    # Grade emoji
    grade_icon = "🔥" if grade == "A+" else "⚡" if grade == "A" else "📊" if grade == "B+" else "📋"

    # RSI values
    rsi_1m = safe_get(data, "rsi_1m", "—")
    rsi_5m = safe_get(data, "rsi_5m", "—")
    rsi_15m = safe_get(data, "rsi_15m", "—")

    # MACD
    macd = safe_get(data, "macd_status", "—")
    macd_hist = safe_get(data, "macd_hist", "—")

    # Institutional
    institutional = safe_get(data, "institutional", "—")

    # Timestamp
    et_offset = timedelta(hours=-4)
    now_et = datetime.now(timezone.utc) + et_offset
    timestamp = now_et.strftime("%I:%M %p")

    msg = f"""{verdict_icon} <b>{verdict_ar}</b> — {grade_icon} {grade}
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

📊 <b>التقييم:</b> {score}
📈 <b>MACD:</b> {macd} ({macd_hist})
📉 <b>RSI:</b> 1m:{rsi_1m} | 5m:{rsi_5m} | 15m:{rsi_15m}

💧 <b>السيولة:</b> {vol_actual} / {vol_avg} ({vol_ratio:.1f}x) — {vol_label}
📊 <b>التدفق:</b> {liquidity}
🏦 <b>النشاط:</b> {institutional}

🎯 <b>الدخول:</b> <code>${price}</code>
🛑 <b>وقف:</b> <code>${safe_get(data, 'stop_loss')}</code>
✅ <b>هدف 1:</b> <code>${safe_get(data, 'target_1')}</code>
✅ <b>هدف 2:</b> <code>${safe_get(data, 'target_2')}</code>
💰 <b>المخاطرة:</b> {safe_get(data, 'max_risk')} | عقد واحد"""

    # Add rejection reasons if any
    if reasons:
        msg += "\n\n⚠️ <b>تنبيه:</b> " + " | ".join(reasons)

    msg += f"""

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تنبيه #{len(daily_alerts) + 1} اليوم
⚠️ <i>تأكد من إغلاق الشمعة قبل الدخول</i>"""

    return msg


def format_reversal_alert(data):
    """Format reversal signal in Arabic — distinctive format."""
    signal = safe_get(data, "signal", "?")
    price = safe_get(data, "price", "?")
    pattern = safe_get(data, "reversal_pattern", "?")
    rev_score = safe_get(data, "reversal_score", "?")

    verdict_ar, verdict_en = get_verdict(data)
    liquidity = get_liquidity_analysis(data)

    vol_ratio = parse_volume_ratio(data)
    vol_actual = safe_get(data, "volume_actual", "—")
    vol_avg = safe_get(data, "volume_avg", "—")
    vol_label = get_volume_label(vol_ratio)

    # Verdict emoji
    if verdict_en == "ENTER":
        verdict_icon = "💚"
    elif verdict_en == "WATCH":
        verdict_icon = "🟡"
    else:
        verdict_icon = "🔴"

    sig_icon = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    # Resistance/Support info
    if signal == "PUT":
        level = safe_get(data, "resistance_level", "—")
        level_type = safe_get(data, "resistance_type", "—")
        zone_label = "المقاومة"
        zone_icon = "🔴"
    else:
        level = safe_get(data, "support_level", "—")
        level_type = safe_get(data, "support_type", "—")
        zone_label = "الدعم"
        zone_icon = "🟢"

    # Pattern translation
    pattern_ar = {
        "Rejection Wick": "ذيل رفض",
        "Failed Breakout": "كسر كاذب",
        "Lower High": "قمة أقل",
        "Hammer at Support": "مطرقة عند الدعم",
        "Failed Breakdown": "كسر كاذب للدعم",
        "Higher Low": "قاع أعلى"
    }.get(pattern, pattern)

    # Level type translation
    level_type_ar = {
        "Rolling High": "أعلى سعر الساعة",
        "Prev Day High": "أعلى سعر أمس",
        "VWAP Upper": "فوق VWAP",
        "Rolling Low": "أقل سعر الساعة",
        "Prev Day Low": "أقل سعر أمس",
        "VWAP Lower": "تحت VWAP"
    }.get(level_type, level_type)

    # RSI
    rsi_1m = safe_get(data, "rsi_1m", "—")
    rsi_5m = safe_get(data, "rsi_5m", "—")

    # Institutional
    institutional = safe_get(data, "institutional", "—")

    et_offset = timedelta(hours=-4)
    now_et = datetime.now(timezone.utc) + et_offset
    timestamp = now_et.strftime("%I:%M %p")

    msg = f"""{verdict_icon} <b>{verdict_ar}</b> — 🔄 انعكاس
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

⚡ <b>النمط:</b> {pattern_ar}
📊 <b>التقييم:</b> {rev_score}
{zone_icon} <b>{zone_label}:</b> ${level} ({level_type_ar})

📉 <b>RSI:</b> 1m:{rsi_1m} | 5m:{rsi_5m}
💧 <b>السيولة:</b> {vol_actual} / {vol_avg} ({vol_ratio:.1f}x) — {vol_label}
📊 <b>التدفق:</b> {liquidity}
🏦 <b>النشاط:</b> {institutional}

🎯 <b>الدخول:</b> <code>${price}</code>
🛑 <b>وقف:</b> <code>${safe_get(data, 'stop_loss')}</code>
✅ <b>هدف 1:</b> <code>${safe_get(data, 'target_1')}</code>
✅ <b>هدف 2:</b> <code>${safe_get(data, 'target_2')}</code>
💰 <b>المخاطرة:</b> {safe_get(data, 'max_risk')} | عقد واحد

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تنبيه #{len(daily_alerts) + 1} اليوم
⚠️ <i>إشارة انعكاس — شمعة التأكيد ظهرت</i>"""

    return msg


def format_blocked_notification(data, reason):
    """Format a short notification for blocked alerts (optional — for debugging)."""
    signal = safe_get(data, "signal", "?")
    price = safe_get(data, "price", "?")
    return f"🚫 محجوب: {signal} @ ${price} — {reason}"


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Sender
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    """Send formatted message to Telegram."""
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
            logger.info("Telegram: message sent successfully")
            return True
        else:
            logger.error(f"Telegram error: {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Filters (V4.0 Phase 1)
# ──────────────────────────────────────────────────────────────────────────────

def check_data_quality(data):
    """Check if alert has sufficient data (blocks empty/incomplete alerts)."""
    signal = safe_get(data, "signal", "")
    price = safe_get(data, "price", "")

    # Must have signal and price
    if not signal or signal in ("", "—", "?", "UNKNOWN"):
        return False, "بيانات ناقصة (لا يوجد إشارة)"
    if not price or price in ("", "—", "?"):
        return False, "بيانات ناقصة (لا يوجد سعر)"

    # For TREND signals, check essential fields
    signal_type = safe_get(data, "signal_type", "TREND")
    if signal_type == "TREND":
        rsi = safe_get(data, "rsi_1m", "")
        score = safe_get(data, "score", "")
        if rsi in ("", "—", "?") and score in ("", "—", "?"):
            return False, "بيانات ناقصة (لا يوجد RSI أو تقييم)"

    return True, ""


def check_volume(data):
    """Check if volume meets minimum threshold."""
    vol_ratio = parse_volume_ratio(data)
    if vol_ratio > 0 and vol_ratio < MIN_VOLUME_RATIO:
        return False, f"سيولة ضعيفة ({vol_ratio:.2f}x < {MIN_VOLUME_RATIO}x)"
    return True, ""


def check_cooldown(data):
    """Check if enough time passed since last alert (prevents duplicates)."""
    global last_alert_time, last_alert_price, last_alert_signal

    now = time.time()
    elapsed = now - last_alert_time

    if elapsed < COOLDOWN_SECONDS:
        current_price = safe_get(data, "price", "")
        current_signal = safe_get(data, "signal", "")

        # Exact duplicate (same price + signal within cooldown)
        if current_price == last_alert_price and current_signal == last_alert_signal:
            return False, f"مكرر (نفس السعر {current_price} بفارق {elapsed:.0f}ث)"

        # Too fast even if different price (within 30 seconds)
        if elapsed < 30:
            return False, f"سريع جداً ({elapsed:.0f}ث < 30ث)"

        # Different signal type is OK even within cooldown
        if current_signal != last_alert_signal:
            return True, ""

        return False, f"cooldown ({elapsed:.0f}ث < {COOLDOWN_SECONDS}ث)"

    return True, ""


def check_daily_limit():
    """Check if daily alert limit reached."""
    reset_daily_if_needed()
    if len(daily_alerts) >= MAX_DAILY_ALERTS:
        return False, f"وصلت الحد اليومي ({MAX_DAILY_ALERTS} تنبيهات)"
    return True, ""


def check_rsi_extreme(data):
    """Warn (but don't block) if RSI is extreme for the signal direction."""
    signal = safe_get(data, "signal", "")
    try:
        rsi_1m = float(safe_get(data, "rsi_1m", "50"))
        if signal == "CALL" and rsi_1m >= 80:
            return False, f"RSI تشبع شراء شديد ({rsi_1m:.0f}) — خطر انعكاس"
        elif signal == "PUT" and rsi_1m <= 20:
            return False, f"RSI تشبع بيع شديد ({rsi_1m:.0f}) — خطر ارتداد"
    except (ValueError, TypeError):
        pass
    return True, ""


def apply_filters(data):
    """Apply all filters in order. Returns (pass, reason)."""
    # 1. Data quality (blocks empty/incomplete)
    ok, reason = check_data_quality(data)
    if not ok:
        return False, reason

    # 2. Volume filter (blocks weak volume)
    ok, reason = check_volume(data)
    if not ok:
        return False, reason

    # 3. RSI extreme filter (blocks dangerous entries)
    ok, reason = check_rsi_extreme(data)
    if not ok:
        return False, reason

    # 4. Cooldown (blocks duplicates/rapid-fire)
    ok, reason = check_cooldown(data)
    if not ok:
        return False, reason

    # 5. Daily limit
    ok, reason = check_daily_limit()
    if not ok:
        return False, reason

    return True, ""


# ──────────────────────────────────────────────────────────────────────────────
# Keep-Alive (Background Thread)
# ──────────────────────────────────────────────────────────────────────────────

def keep_alive_worker():
    """Ping self to prevent Render cold starts."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        if render_url:
            try:
                resp = http_requests.get(f"{render_url}/", timeout=10)
                logger.info(f"Keep-alive ping: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Keep-alive failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    """Health check endpoint."""
    reset_daily_if_needed()
    return jsonify({
        "status": "running",
        "service": "Smart Trading Alert Bot V4.0 — Phase 1",
        "version": "4.0-P1",
        "features": [
            "reversal_detection",
            "confirmation_candle",
            "arabic_messages",
            "volume_filter",
            "rsi_extreme_filter",
            "cooldown_60s",
            "empty_data_filter",
            "keep_alive"
        ],
        "alerts_today": len(daily_alerts),
        "blocked_today": len(blocked_today),
        "remaining": MAX_DAILY_ALERTS - len(daily_alerts),
        "last_alert": alert_history[0] if alert_history else None,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Main webhook endpoint for TradingView alerts."""
    global last_alert_time, last_alert_price, last_alert_signal

    # Optional auth
    if WEBHOOK_SECRET:
        auth = request.headers.get("X-Webhook-Secret", "")
        if auth != WEBHOOK_SECRET:
            logger.warning("Unauthorized webhook attempt")
            return jsonify({"error": "Unauthorized"}), 401

    # Parse data
    try:
        if request.is_json:
            data = request.get_json()
        else:
            raw = request.data.decode("utf-8")
            data = json.loads(raw)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return jsonify({"error": "Parse error"}), 400

    signal = safe_get(data, "signal", "?")
    signal_type = safe_get(data, "signal_type", "TREND")
    price = safe_get(data, "price", "?")
    vol_ratio = parse_volume_ratio(data)

    logger.info(f"Received: {signal_type} {signal} @ ${price} | Vol:{vol_ratio:.2f}x")

    # Apply filters
    passed, rejection_reason = apply_filters(data)

    if not passed:
        logger.info(f"BLOCKED: {rejection_reason}")
        blocked_today.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": signal,
            "signal_type": signal_type,
            "price": price,
            "reason": rejection_reason
        })
        return jsonify({
            "status": "blocked",
            "reason": rejection_reason,
            "signal": signal,
            "price": price
        }), 200

    # Determine verdict
    verdict_ar, verdict_en = get_verdict(data)

    # Format message based on signal type
    if signal_type == "REVERSAL":
        tg_msg = format_reversal_alert(data)
    else:
        tg_msg = format_trend_alert(data)

    # Send to Telegram
    tg_ok = send_telegram(tg_msg)

    # Update cooldown state
    last_alert_time = time.time()
    last_alert_price = price
    last_alert_signal = signal

    # Save to history
    add_to_history(data, verdict_ar)

    logger.info(f"SENT: {signal_type} {signal} @ ${price} — {verdict_ar} (#{len(daily_alerts)} today)")

    return jsonify({
        "status": "processed",
        "telegram": "sent" if tg_ok else "failed",
        "signal": signal,
        "signal_type": signal_type,
        "verdict": verdict_ar,
        "alert_number": len(daily_alerts)
    }), 200


@app.route("/test", methods=["GET"])
def test_alert():
    """Send test alerts (trend + reversal) to verify everything works."""
    # Test trend alert
    test_trend = {
        "signal_type": "TREND",
        "signal": "CALL",
        "grade": "A",
        "symbol": "TSLA",
        "price": "390.50",
        "session": "Opening Power",
        "macd_status": "Fresh Bull Cross",
        "macd_hist": "0.0523",
        "rsi_1m": "62.5",
        "rsi_1m_desc": "Strong Bullish",
        "rsi_5m": "60.1",
        "rsi_15m": "58.3",
        "obv_1m": "Rising",
        "obv_5m": "Rising",
        "obv_15m": "Rising",
        "volume_actual": "25.3K",
        "volume_avg": "18.5K",
        "volume_ratio": "1.37x",
        "volume_desc": "Above Avg",
        "volume_surge": "YES",
        "momentum": "+1.85",
        "atr": "0.87",
        "score": "15/16",
        "stop_loss": "390.20",
        "target_1": "390.80",
        "target_2": "391.10",
        "max_risk": "$50",
        "suggested_contracts": "1",
        "accum_dist": "Accumulation",
        "institutional": "High Activity",
        "vol_trend": "Increasing"
    }

    # Test reversal alert
    test_reversal = {
        "signal_type": "REVERSAL",
        "signal": "PUT",
        "grade": "REV",
        "symbol": "TSLA",
        "price": "395.80",
        "session": "Morning Momentum",
        "reversal_pattern": "Rejection Wick",
        "reversal_score": "9/10",
        "resistance_level": "396.20",
        "resistance_type": "Rolling High",
        "rsi_1m": "72.5",
        "rsi_5m": "68.3",
        "macd_hist": "-0.0312",
        "volume_ratio": "1.15x",
        "volume_actual": "22.1K",
        "volume_avg": "18.5K",
        "obv_1m": "Falling",
        "accum_dist": "Distribution",
        "institutional": "High Activity",
        "stop_loss": "396.10",
        "target_1": "395.50",
        "target_2": "395.20",
        "atr": "0.92",
        "max_risk": "$50"
    }

    tg_ok1 = send_telegram(format_trend_alert(test_trend))
    time.sleep(2)
    tg_ok2 = send_telegram(format_reversal_alert(test_reversal))

    return jsonify({
        "status": "test_sent",
        "trend_alert": "sent" if tg_ok1 else "failed",
        "reversal_alert": "sent" if tg_ok2 else "failed"
    }), 200


@app.route("/history", methods=["GET"])
def history():
    """View recent alert history."""
    return jsonify({
        "total": len(alert_history),
        "today_sent": len(daily_alerts),
        "today_blocked": len(blocked_today),
        "alerts": alert_history[:20]
    })


@app.route("/stats", methods=["GET"])
def stats():
    """View detailed alert statistics."""
    reset_daily_if_needed()
    calls = sum(1 for a in daily_alerts if a["signal"] == "CALL")
    puts = sum(1 for a in daily_alerts if a["signal"] == "PUT")
    entered = sum(1 for a in daily_alerts if a["verdict"] == "ادخل")
    watched = sum(1 for a in daily_alerts if a["verdict"] == "انتبه")
    skipped = sum(1 for a in daily_alerts if a["verdict"] == "تجاوز")
    reversals = sum(1 for a in daily_alerts if a["signal_type"] == "REVERSAL")

    # Blocked reasons summary
    block_reasons = {}
    for b in blocked_today:
        r = b.get("reason", "unknown")
        # Simplify reason for grouping
        if "مكرر" in r or "cooldown" in r or "سريع" in r:
            key = "مكرر/cooldown"
        elif "سيولة" in r:
            key = "سيولة ضعيفة"
        elif "بيانات" in r:
            key = "بيانات ناقصة"
        elif "RSI" in r:
            key = "RSI متطرف"
        elif "الحد اليومي" in r:
            key = "حد يومي"
        else:
            key = r
        block_reasons[key] = block_reasons.get(key, 0) + 1

    return jsonify({
        "date": daily_date,
        "sent": {
            "total": len(daily_alerts),
            "calls": calls,
            "puts": puts,
            "reversals": reversals,
            "verdicts": {
                "ادخل": entered,
                "انتبه": watched,
                "تجاوز": skipped
            }
        },
        "blocked": {
            "total": len(blocked_today),
            "reasons": block_reasons
        },
        "remaining": MAX_DAILY_ALERTS - len(daily_alerts),
        "filters": {
            "min_volume": f"{MIN_VOLUME_RATIO}x",
            "cooldown": f"{COOLDOWN_SECONDS}s",
            "max_daily": MAX_DAILY_ALERTS
        }
    })


@app.route("/reset", methods=["GET"])
def reset():
    """Reset daily counters."""
    global daily_alerts, daily_date, blocked_today
    daily_alerts = []
    blocked_today = []
    daily_date = get_today()
    return jsonify({"status": "reset", "date": daily_date})


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Smart Trading Alert Bot V4.0 Phase 1 — Starting...")
    logger.info(f"Server: {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Telegram: {'Configured' if TELEGRAM_BOT_TOKEN != 'YOUR_BOT_TOKEN_HERE' else 'NOT SET'}")
    logger.info(f"Filters: Volume>{MIN_VOLUME_RATIO}x | Cooldown:{COOLDOWN_SECONDS}s | Max:{MAX_DAILY_ALERTS}/day | RSI extreme block")
    logger.info("=" * 60)

    # Start keep-alive background thread
    keep_alive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
    keep_alive_thread.start()
    logger.info("Keep-alive thread started (every 10 min)")

    logger.info("Endpoints:")
    logger.info("  Health:   /")
    logger.info("  Webhook:  /webhook")
    logger.info("  Test:     /test")
    logger.info("  History:  /history")
    logger.info("  Stats:    /stats")
    logger.info("  Reset:    /reset")
    logger.info("=" * 60)

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
