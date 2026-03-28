#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
Smart Trading Alert Bot — V4.1 Phase 2
Webhook Server
══════════════════════════════════════════════════════════════════════════════
Phase 2 Features (NEW):
  ✅ Afternoon Session (11:00-15:30 ET) — full day coverage
  ✅ Trend Continuation signals (MACD sustained)
  ✅ VWAP Context in every message (above/below)
  ✅ Royal Portfolio 👑 alerts (score 14+/16 or 9+/10 + vol 1.5x+)
  ✅ Time Stop warning (10-min expiry reminder)
  ✅ Telegram bot commands (/market /status /history /stats /help)
  ✅ Morning market digest (9:25 AM ET)
  ✅ Midday market digest (12:00 PM ET)
  ✅ End-of-day summary (4:05 PM ET)
  ✅ Volume threshold 0.4x after 10 AM (was 0.5x)
  ✅ Cooldown 25 min for similar signals (was 60s flat)

Phase 1 Features (KEPT):
  ✅ Arabic alert messages (concise, fast-read)
  ✅ Reversal Detection alerts (separate format)
  ✅ Signal verdict: ادخل / انتبه / تجاوز
  ✅ Accumulation/Distribution + Institutional activity
  ✅ Keep-alive ping (prevents Render cold starts)
  ✅ Max 15 alerts/day limit (was 10)
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
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")

SERVER_HOST    = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT    = int(os.environ.get("SERVER_PORT", "5000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# V4.1 Phase 2 Settings
COOLDOWN_SECONDS_MORNING   = 60    # 9:30-10:00 — fast market
COOLDOWN_SECONDS_NORMAL    = 90    # 10:00-15:30 — normal
COOLDOWN_SECONDS_SIMILAR   = 1500  # 25 min between same-direction signals
MIN_VOLUME_RATIO_MORNING   = 0.5   # Before 10 AM
MIN_VOLUME_RATIO_AFTERNOON = 0.4   # After 10 AM (volume naturally drops)
MAX_DAILY_ALERTS           = 15    # Increased from 10
KEEP_ALIVE_INTERVAL        = 600   # Ping self every 10 minutes

# Royal Portfolio thresholds
ROYAL_TREND_SCORE     = 14   # out of 16
ROYAL_REVERSAL_SCORE  = 9    # out of 10
ROYAL_VOLUME_RATIO    = 1.5  # minimum for royal

# Risk Management
MAX_RISK_PER_TRADE = 50
MAX_DAILY_LOSS     = 150
PORTFOLIO_SIZE     = 1000

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

alert_history  = []
MAX_HISTORY    = 100

last_alert_time    = 0
last_alert_price   = ""
last_alert_signal  = ""

# Per-direction cooldown tracking
last_call_time = 0
last_put_time  = 0

daily_alerts  = []
daily_date    = ""
blocked_today = []

# Market state (updated by Pine Script or digest)
market_state = {
    "vwap_position": "—",      # above / below / at
    "trend":         "—",      # BULL / BEAR / RANGE
    "volume_trend":  "—",      # increasing / decreasing / flat
    "accum_dist":    "—",      # Accumulation / Distribution / Neutral
    "last_price":    "—",
    "last_updated":  "—"
}


def get_et_now():
    """Return current datetime in ET (UTC-4)."""
    return datetime.now(timezone.utc) + timedelta(hours=-4)


def get_today():
    return get_et_now().strftime("%Y-%m-%d")


def get_session():
    """Return current trading session label."""
    now = get_et_now()
    h, m = now.hour, now.minute
    total_min = h * 60 + m
    if total_min < 9 * 60 + 30:
        return "pre_market"
    elif total_min < 10 * 60:
        return "opening_power"
    elif total_min < 11 * 60:
        return "morning_momentum"
    elif total_min < 14 * 60:
        return "midday"
    elif total_min < 15 * 60 + 30:
        return "power_hour"
    else:
        return "after_hours"


def reset_daily_if_needed():
    global daily_alerts, daily_date, blocked_today
    today = get_today()
    if daily_date != today:
        daily_date    = today
        daily_alerts  = []
        blocked_today = []
        logger.info(f"New trading day: {today} — counters reset")


def add_to_history(data, verdict):
    entry = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "signal":      data.get("signal", "?"),
        "signal_type": data.get("signal_type", "TREND"),
        "grade":       data.get("grade", "?"),
        "price":       data.get("price", "?"),
        "score":       data.get("score", data.get("reversal_score", "?")),
        "volume_ratio":data.get("volume_ratio", "?"),
        "verdict":     verdict,
        "session":     get_session()
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)


# ──────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def safe_get(data, key, default="—"):
    val = data.get(key, "")
    if val is None or str(val).strip() in ("", "—"):
        return default
    s = str(val).strip()
    if s.lower() in ("n/a", "na", "nan", "none", "undefined", "null"):
        return default
    return s


def parse_volume_ratio(data):
    try:
        vr = safe_get(data, "volume_ratio", "0")
        return float(vr.replace("x", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_score(data):
    try:
        return int(safe_get(data, "score", "0").split("/")[0])
    except (ValueError, TypeError):
        return 0


def parse_reversal_score(data):
    try:
        return int(safe_get(data, "reversal_score", "0").split("/")[0])
    except (ValueError, TypeError):
        return 0


def is_royal(data):
    """Check if signal qualifies for Royal Portfolio 👑."""
    signal_type = safe_get(data, "signal_type", "TREND")
    vol_ratio   = parse_volume_ratio(data)
    if signal_type == "REVERSAL":
        return parse_reversal_score(data) >= ROYAL_REVERSAL_SCORE and vol_ratio >= ROYAL_VOLUME_RATIO
    else:
        return parse_score(data) >= ROYAL_TREND_SCORE and vol_ratio >= ROYAL_VOLUME_RATIO


def get_verdict(data):
    """ادخل / انتبه / تجاوز"""
    signal_type = safe_get(data, "signal_type", "TREND")
    vol_ratio   = parse_volume_ratio(data)

    if signal_type == "REVERSAL":
        rev_score = parse_reversal_score(data)
        if rev_score >= 9 and vol_ratio >= 1.0:
            return "ادخل", "ENTER"
        elif rev_score >= 8 and vol_ratio >= 0.4:
            return "انتبه", "WATCH"
        else:
            return "تجاوز", "SKIP"
    else:
        grade = safe_get(data, "grade", "C")
        score = parse_score(data)
        if grade in ("A+", "A") and vol_ratio >= 1.0:
            return "ادخل", "ENTER"
        elif grade in ("A+", "A", "B+") and vol_ratio >= 0.4:
            return "انتبه", "WATCH"
        elif score >= 10 and vol_ratio >= 0.4:
            return "انتبه", "WATCH"
        else:
            return "تجاوز", "SKIP"


def get_vwap_context(data):
    """Return VWAP position label in Arabic."""
    vwap_pos = safe_get(data, "vwap_position", "")
    if vwap_pos.lower() in ("above", "فوق"):
        return "فوق VWAP 🟢"
    elif vwap_pos.lower() in ("below", "تحت"):
        return "تحت VWAP 🔴"
    else:
        return "عند VWAP ⚡"


def get_liquidity_analysis(data):
    accum_dist = safe_get(data, "accum_dist", "Neutral")
    if accum_dist == "Accumulation":
        return "تجميع 📈"
    elif accum_dist == "Distribution":
        return "تصريف 📉"
    else:
        return "حيادي ➡️"


def get_volume_label(vol_ratio):
    if vol_ratio >= 2.0:
        return "ارتفاع قوي 🔥"
    elif vol_ratio >= 1.3:
        return "فوق المتوسط ✅"
    elif vol_ratio >= 0.8:
        return "متوسط ⚡"
    elif vol_ratio >= 0.4:
        return "أقل من المتوسط ⚠️"
    else:
        return "ضعيف جداً 🚫"


def get_rejection_reasons(data):
    reasons = []
    vol_ratio  = parse_volume_ratio(data)
    signal     = safe_get(data, "signal", "")
    macd_hist  = safe_get(data, "macd_hist", "0")

    if vol_ratio < 0.4:
        reasons.append(f"سيولة ضعيفة ({vol_ratio:.1f}x)")
    elif vol_ratio < 0.8:
        reasons.append(f"سيولة أقل من المتوسط ({vol_ratio:.1f}x)")

    try:
        macd_val = float(macd_hist)
        if signal == "CALL" and macd_val < -0.01:
            reasons.append("MACD سلبي")
        elif signal == "PUT" and macd_val > 0.01:
            reasons.append("MACD إيجابي")
    except (ValueError, TypeError):
        pass

    try:
        rsi = float(safe_get(data, "rsi_1m", "50"))
        if signal == "CALL" and rsi >= 75:
            reasons.append(f"تشبع شراء ({rsi:.0f})")
        elif signal == "PUT" and rsi <= 25:
            reasons.append(f"تشبع بيع ({rsi:.0f})")
    except (ValueError, TypeError):
        pass

    return reasons


def get_session_label():
    s = get_session()
    labels = {
        "pre_market":       "قبل السوق",
        "opening_power":    "افتتاح قوي ⚡",
        "morning_momentum": "زخم الصباح 🌅",
        "midday":           "منتصف اليوم",
        "power_hour":       "ساعة القوة 💪",
        "after_hours":      "بعد السوق"
    }
    return labels.get(s, s)


# ──────────────────────────────────────────────────────────────────────────────
# Message Formatters
# ──────────────────────────────────────────────────────────────────────────────

def format_trend_alert(data):
    signal      = safe_get(data, "signal", "?")
    grade       = safe_get(data, "grade", "?")
    price       = safe_get(data, "price", "?")
    score       = safe_get(data, "score", "?")
    signal_subtype = safe_get(data, "signal_subtype", "TREND")  # TREND or CONTINUATION

    verdict_ar, verdict_en = get_verdict(data)
    reasons    = get_rejection_reasons(data)
    liquidity  = get_liquidity_analysis(data)
    vwap_ctx   = get_vwap_context(data)
    royal      = is_royal(data)

    vol_ratio  = parse_volume_ratio(data)
    vol_actual = safe_get(data, "volume_actual", "—")
    vol_avg    = safe_get(data, "volume_avg", "—")
    vol_label  = get_volume_label(vol_ratio)

    verdict_icon = "💚" if verdict_en == "ENTER" else "🟡" if verdict_en == "WATCH" else "🔴"
    sig_icon     = "🟢" if signal == "CALL" else "🔴"
    direction    = "CALL شراء" if signal == "CALL" else "PUT بيع"
    grade_icon   = "🔥" if grade == "A+" else "⚡" if grade == "A" else "📊" if grade == "B+" else "📋"

    rsi_1m  = safe_get(data, "rsi_1m", "—")
    rsi_5m  = safe_get(data, "rsi_5m", "—")
    rsi_15m = safe_get(data, "rsi_15m", "—")
    macd    = safe_get(data, "macd_status", "—")
    macd_hist = safe_get(data, "macd_hist", "—")
    institutional = safe_get(data, "institutional", "—")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    # Royal header
    royal_header = "\n👑 <b>المحفظة الملكية — فرصة ذهبية</b>\n" if royal else ""

    # Continuation label
    cont_label = " — 📈 استمرار" if signal_subtype == "CONTINUATION" else ""

    msg = f"""{royal_header}{verdict_icon} <b>{verdict_ar}</b>{cont_label} — {grade_icon} {grade}
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

📊 <b>التقييم:</b> {score}
📈 <b>MACD:</b> {macd} ({macd_hist})
📉 <b>RSI:</b> 1m:{rsi_1m} | 5m:{rsi_5m} | 15m:{rsi_15m}

💧 <b>السيولة:</b> {vol_actual} / {vol_avg} ({vol_ratio:.1f}x) — {vol_label}
📊 <b>التدفق:</b> {liquidity}
🏦 <b>النشاط:</b> {institutional}
📍 <b>VWAP:</b> {vwap_ctx}

🎯 <b>الدخول:</b> <code>${price}</code>
🛑 <b>وقف:</b> <code>${safe_get(data, 'stop_loss')}</code>
✅ <b>هدف 1:</b> <code>${safe_get(data, 'target_1')}</code>
✅ <b>هدف 2:</b> <code>${safe_get(data, 'target_2')}</code>
💰 <b>المخاطرة:</b> {safe_get(data, 'max_risk')} | عقد واحد"""

    if reasons:
        msg += "\n\n⚠️ <b>تنبيه:</b> " + " | ".join(reasons)

    msg += f"""

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تنبيه #{len(daily_alerts) + 1} اليوم | {get_session_label()}
⏱ <i>الوقف الزمني: 10 دقائق — اطلع إذا ما تحرك السعر</i>
⚠️ <i>تأكد من إغلاق الشمعة قبل الدخول</i>"""

    return msg


def format_reversal_alert(data):
    signal    = safe_get(data, "signal", "?")
    price     = safe_get(data, "price", "?")
    pattern   = safe_get(data, "reversal_pattern", "?")
    rev_score = safe_get(data, "reversal_score", "?")

    verdict_ar, verdict_en = get_verdict(data)
    liquidity  = get_liquidity_analysis(data)
    vwap_ctx   = get_vwap_context(data)
    royal      = is_royal(data)

    vol_ratio  = parse_volume_ratio(data)
    vol_actual = safe_get(data, "volume_actual", "—")
    vol_avg    = safe_get(data, "volume_avg", "—")
    vol_label  = get_volume_label(vol_ratio)

    verdict_icon = "💚" if verdict_en == "ENTER" else "🟡" if verdict_en == "WATCH" else "🔴"
    sig_icon     = "🟢" if signal == "CALL" else "🔴"
    direction    = "CALL شراء" if signal == "CALL" else "PUT بيع"

    if signal == "PUT":
        level      = safe_get(data, "resistance_level", "—")
        level_type = safe_get(data, "resistance_type", "—")
        zone_label = "المقاومة"
        zone_icon  = "🔴"
    else:
        level      = safe_get(data, "support_level", "—")
        level_type = safe_get(data, "support_type", "—")
        zone_label = "الدعم"
        zone_icon  = "🟢"

    pattern_ar = {
        "Rejection Wick":    "ذيل رفض",
        "Failed Breakout":   "كسر كاذب",
        "Lower High":        "قمة أقل",
        "Hammer at Support": "مطرقة عند الدعم",
        "Failed Breakdown":  "كسر كاذب للدعم",
        "Higher Low":        "قاع أعلى"
    }.get(pattern, pattern)

    level_type_ar = {
        "Rolling High":  "أعلى سعر الساعة",
        "Prev Day High": "أعلى سعر أمس",
        "VWAP Upper":    "فوق VWAP",
        "Rolling Low":   "أقل سعر الساعة",
        "Prev Day Low":  "أقل سعر أمس",
        "VWAP Lower":    "تحت VWAP"
    }.get(level_type, level_type)

    rsi_1m = safe_get(data, "rsi_1m", "—")
    rsi_5m = safe_get(data, "rsi_5m", "—")
    institutional = safe_get(data, "institutional", "—")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    royal_header = "\n👑 <b>المحفظة الملكية — انعكاس ذهبي</b>\n" if royal else ""

    msg = f"""{royal_header}{verdict_icon} <b>{verdict_ar}</b> — 🔄 انعكاس
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

⚡ <b>النمط:</b> {pattern_ar}
📊 <b>التقييم:</b> {rev_score}
{zone_icon} <b>{zone_label}:</b> ${level} ({level_type_ar})

📉 <b>RSI:</b> 1m:{rsi_1m} | 5m:{rsi_5m}
💧 <b>السيولة:</b> {vol_actual} / {vol_avg} ({vol_ratio:.1f}x) — {vol_label}
📊 <b>التدفق:</b> {liquidity}
🏦 <b>النشاط:</b> {institutional}
📍 <b>VWAP:</b> {vwap_ctx}

🎯 <b>الدخول:</b> <code>${price}</code>
🛑 <b>وقف:</b> <code>${safe_get(data, 'stop_loss')}</code>
✅ <b>هدف 1:</b> <code>${safe_get(data, 'target_1')}</code>
✅ <b>هدف 2:</b> <code>${safe_get(data, 'target_2')}</code>
💰 <b>المخاطرة:</b> {safe_get(data, 'max_risk')} | عقد واحد

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تنبيه #{len(daily_alerts) + 1} اليوم | {get_session_label()}
⏱ <i>الوقف الزمني: 10 دقائق — اطلع إذا ما تحرك السعر</i>
⚠️ <i>إشارة انعكاس — شمعة التأكيد ظهرت</i>"""

    return msg


def format_market_digest(period="morning"):
    """Format a periodic market analysis message."""
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")
    date_str  = now_et.strftime("%d/%m/%Y")

    vwap_pos   = market_state.get("vwap_position", "—")
    trend      = market_state.get("trend", "—")
    accum_dist = market_state.get("accum_dist", "—")
    last_price = market_state.get("last_price", "—")

    # Trend label
    trend_ar = {"BULL": "صاعد 🟢", "BEAR": "هابط 🔴", "RANGE": "رينج ⚡"}.get(trend, trend)

    # VWAP label
    vwap_ar = {"above": "فوق VWAP 🟢", "below": "تحت VWAP 🔴", "at": "عند VWAP ⚡"}.get(
        vwap_pos.lower(), vwap_pos)

    # Flow label
    flow_ar = {"Accumulation": "تجميع 📈", "Distribution": "تصريف 📉"}.get(accum_dist, "حيادي ➡️")

    # Session stats
    sent_today    = len(daily_alerts)
    blocked_count = len(blocked_today)

    if period == "morning":
        header = "🌅 <b>تحليل الصباح — قبل الجلسة</b>"
        footer = "⚡ <i>الجلسة تبدأ الآن — ركّز على الإشارات الأولى</i>"
    elif period == "midday":
        header = "☀️ <b>تحليل منتصف اليوم</b>"
        footer = f"📊 <i>إشارات اليوم: {sent_today} مرسلة | {blocked_count} محجوبة</i>"
    else:
        header = "🌙 <b>ملخص نهاية اليوم</b>"
        footer = f"📊 <i>إجمالي اليوم: {sent_today} إشارة | {blocked_count} محجوبة</i>"

    msg = f"""{header}
━━━━━━━━━━━━━━━━━━━━━

📅 {date_str} | 🕐 {timestamp} ET

📈 <b>الاتجاه:</b> {trend_ar}
📍 <b>VWAP:</b> {vwap_ar}
📊 <b>التدفق:</b> {flow_ar}
💰 <b>آخر سعر:</b> <code>${last_price}</code>

━━━━━━━━━━━━━━━━━━━━━
{footer}"""

    return msg


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Sender
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
            logger.error(f"Telegram error: {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def get_telegram_updates():
    """Poll Telegram for new messages/commands."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = http_requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception:
        pass
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Command Handlers
# ──────────────────────────────────────────────────────────────────────────────

def handle_command_market():
    """Reply to /market command."""
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    vwap_pos   = market_state.get("vwap_position", "—")
    trend      = market_state.get("trend", "—")
    accum_dist = market_state.get("accum_dist", "—")
    last_price = market_state.get("last_price", "—")

    trend_ar = {"BULL": "صاعد 🟢", "BEAR": "هابط 🔴", "RANGE": "رينج ⚡"}.get(trend, "—")
    vwap_ar  = {"above": "فوق VWAP 🟢", "below": "تحت VWAP 🔴", "at": "عند VWAP ⚡"}.get(
        vwap_pos.lower(), vwap_pos)
    flow_ar  = {"Accumulation": "تجميع 📈", "Distribution": "تصريف 📉"}.get(accum_dist, "حيادي ➡️")

    msg = f"""📊 <b>وضع السوق الحالي</b>
━━━━━━━━━━━━━━━━━━━━━

🕐 {timestamp} ET | {get_session_label()}

📈 <b>الاتجاه:</b> {trend_ar}
📍 <b>VWAP:</b> {vwap_ar}
📊 <b>التدفق:</b> {flow_ar}
💰 <b>آخر سعر:</b> <code>${last_price}</code>

📋 <b>إشارات اليوم:</b> {len(daily_alerts)} مرسلة | {len(blocked_today)} محجوبة
━━━━━━━━━━━━━━━━━━━━━
⚡ <i>البيانات تُحدَّث مع كل إشارة</i>"""
    send_telegram(msg)


def handle_command_status():
    """Reply to /status command."""
    last = alert_history[0] if alert_history else None
    last_str = "لا يوجد إشارات بعد" if not last else \
        f"{last['signal']} @ ${last['price']} — {last['verdict']} ({last['signal_type']})"

    msg = f"""⚙️ <b>حالة النظام</b>
━━━━━━━━━━━━━━━━━━━━━

✅ <b>السيرفر:</b> يعمل
🤖 <b>الإصدار:</b> V4.1 Phase 2
🕐 <b>الوقت:</b> {get_et_now().strftime('%I:%M %p')} ET

📊 <b>إشارات اليوم:</b> {len(daily_alerts)} / {MAX_DAILY_ALERTS}
🚫 <b>محجوبة:</b> {len(blocked_today)}
⏱ <b>الجلسة:</b> {get_session_label()}

📡 <b>آخر إشارة:</b>
{last_str}"""
    send_telegram(msg)


def handle_command_history():
    """Reply to /history command."""
    if not alert_history:
        send_telegram("📋 لا يوجد إشارات في السجل بعد.")
        return

    lines = []
    for i, a in enumerate(alert_history[:5], 1):
        t = a.get("timestamp", "")[:16].replace("T", " ")
        lines.append(f"{i}. {a['signal']} @ ${a['price']} — {a['verdict']} ({a['signal_type']})")

    msg = "📋 <b>آخر 5 إشارات:</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    send_telegram(msg)


def handle_command_stats():
    """Reply to /stats command."""
    reset_daily_if_needed()
    calls     = sum(1 for a in daily_alerts if a["signal"] == "CALL")
    puts      = sum(1 for a in daily_alerts if a["signal"] == "PUT")
    entered   = sum(1 for a in daily_alerts if a["verdict"] == "ادخل")
    watched   = sum(1 for a in daily_alerts if a["verdict"] == "انتبه")
    reversals = sum(1 for a in daily_alerts if a["signal_type"] == "REVERSAL")

    msg = f"""📈 <b>إحصائيات اليوم</b>
━━━━━━━━━━━━━━━━━━━━━

📅 {get_today()}

✅ <b>مرسلة:</b> {len(daily_alerts)}
🟢 CALL: {calls} | 🔴 PUT: {puts}
🔄 انعكاس: {reversals}

💚 ادخل: {entered}
🟡 انتبه: {watched}
🚫 محجوبة: {len(blocked_today)}

📊 <b>متبقي اليوم:</b> {MAX_DAILY_ALERTS - len(daily_alerts)} إشارة"""
    send_telegram(msg)


def handle_command_help():
    """Reply to /help command."""
    msg = """🤖 <b>أوامر البوت</b>
━━━━━━━━━━━━━━━━━━━━━

/market — وضع السوق الحالي
/status — حالة النظام
/history — آخر 5 إشارات
/stats — إحصائيات اليوم
/help — قائمة الأوامر

━━━━━━━━━━━━━━━━━━━━━
⚡ TSLA Scalper Bot V4.1 Phase 2"""
    send_telegram(msg)


# ──────────────────────────────────────────────────────────────────────────────
# Filters
# ──────────────────────────────────────────────────────────────────────────────

def check_data_quality(data):
    signal = safe_get(data, "signal", "")
    price  = safe_get(data, "price", "")
    if not signal or signal in ("", "—", "?", "UNKNOWN"):
        return False, "بيانات ناقصة (لا يوجد إشارة)"
    if not price or price in ("", "—", "?"):
        return False, "بيانات ناقصة (لا يوجد سعر)"
    signal_type = safe_get(data, "signal_type", "TREND")
    if signal_type == "TREND":
        rsi   = safe_get(data, "rsi_1m", "")
        score = safe_get(data, "score", "")
        if rsi in ("", "—", "?") and score in ("", "—", "?"):
            return False, "بيانات ناقصة (لا يوجد RSI أو تقييم)"
    return True, ""


def check_volume(data):
    vol_ratio = parse_volume_ratio(data)
    session   = get_session()
    min_vol   = MIN_VOLUME_RATIO_MORNING if session in ("opening_power", "morning_momentum") \
                else MIN_VOLUME_RATIO_AFTERNOON
    if vol_ratio > 0 and vol_ratio < min_vol:
        return False, f"سيولة ضعيفة ({vol_ratio:.2f}x < {min_vol}x)"
    return True, ""


def check_cooldown(data):
    global last_alert_time, last_alert_price, last_alert_signal
    global last_call_time, last_put_time

    now     = time.time()
    elapsed = now - last_alert_time
    signal  = safe_get(data, "signal", "")
    session = get_session()

    cooldown = COOLDOWN_SECONDS_MORNING if session == "opening_power" else COOLDOWN_SECONDS_NORMAL

    # Exact duplicate
    current_price = safe_get(data, "price", "")
    if current_price == last_alert_price and signal == last_alert_signal and elapsed < cooldown:
        return False, f"مكرر (نفس السعر {current_price} بفارق {elapsed:.0f}ث)"

    # Too fast (< 30s)
    if elapsed < 30:
        return False, f"سريع جداً ({elapsed:.0f}ث < 30ث)"

    # Same-direction cooldown (25 min)
    if signal == "CALL":
        if now - last_call_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_call_time)
            return False, f"CALL cooldown — انتظر {remaining/60:.0f} دقيقة"
    elif signal == "PUT":
        if now - last_put_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_put_time)
            return False, f"PUT cooldown — انتظر {remaining/60:.0f} دقيقة"

    return True, ""


def check_daily_limit():
    reset_daily_if_needed()
    if len(daily_alerts) >= MAX_DAILY_ALERTS:
        return False, f"وصلت الحد اليومي ({MAX_DAILY_ALERTS} تنبيهات)"
    return True, ""


def check_rsi_extreme(data):
    signal = safe_get(data, "signal", "")
    try:
        rsi_1m = float(safe_get(data, "rsi_1m", "50"))
        if signal == "CALL" and rsi_1m >= 82:
            return False, f"RSI تشبع شراء شديد ({rsi_1m:.0f})"
        elif signal == "PUT" and rsi_1m <= 18:
            return False, f"RSI تشبع بيع شديد ({rsi_1m:.0f})"
    except (ValueError, TypeError):
        pass
    return True, ""


def apply_filters(data):
    for check in [check_data_quality, check_volume, check_rsi_extreme,
                  check_cooldown, check_daily_limit]:
        ok, reason = check(data)
        if not ok:
            return False, reason
    return True, ""


# ──────────────────────────────────────────────────────────────────────────────
# Background Workers
# ──────────────────────────────────────────────────────────────────────────────

def keep_alive_worker():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        if render_url:
            try:
                resp = http_requests.get(f"{render_url}/", timeout=10)
                logger.info(f"Keep-alive ping: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Keep-alive failed: {e}")


def telegram_command_worker():
    """Poll Telegram every 5 seconds for commands."""
    last_update_id = 0
    while True:
        time.sleep(5)
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 3}
            resp = http_requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            updates = resp.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                if text in ("/market", "market"):
                    handle_command_market()
                elif text in ("/status", "status"):
                    handle_command_status()
                elif text in ("/history", "history"):
                    handle_command_history()
                elif text in ("/stats", "stats"):
                    handle_command_stats()
                elif text in ("/help", "help"):
                    handle_command_help()
        except Exception as e:
            logger.warning(f"Telegram command worker error: {e}")


def market_digest_worker():
    """Send periodic market digest messages."""
    sent_morning = False
    sent_midday  = False
    sent_eod     = False
    last_date    = ""

    while True:
        time.sleep(30)
        try:
            now   = get_et_now()
            today = now.strftime("%Y-%m-%d")
            h, m  = now.hour, now.minute

            # Reset flags on new day
            if today != last_date:
                sent_morning = sent_midday = sent_eod = False
                last_date = today

            # Morning digest at 9:25 AM
            if h == 9 and m >= 25 and m < 30 and not sent_morning:
                send_telegram(format_market_digest("morning"))
                sent_morning = True
                logger.info("Sent morning digest")

            # Midday digest at 12:00 PM
            if h == 12 and m < 5 and not sent_midday:
                send_telegram(format_market_digest("midday"))
                sent_midday = True
                logger.info("Sent midday digest")

            # EOD digest at 4:05 PM
            if h == 16 and m >= 5 and m < 10 and not sent_eod:
                send_telegram(format_market_digest("eod"))
                sent_eod = True
                logger.info("Sent EOD digest")

        except Exception as e:
            logger.warning(f"Market digest worker error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    reset_daily_if_needed()
    return jsonify({
        "status":        "running",
        "service":       "Smart Trading Alert Bot V4.1 — Phase 2",
        "version":       "4.1-P2",
        "features":      ["afternoon_session", "trend_continuation", "vwap_context",
                          "royal_portfolio", "time_stop", "telegram_commands",
                          "market_digest", "reversal_detection", "arabic_messages"],
        "alerts_today":  len(daily_alerts),
        "blocked_today": len(blocked_today),
        "remaining":     MAX_DAILY_ALERTS - len(daily_alerts),
        "session":       get_session_label(),
        "last_alert":    alert_history[0] if alert_history else None,
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
        return jsonify({"error": "Parse error"}), 400

    signal      = safe_get(data, "signal", "?")
    signal_type = safe_get(data, "signal_type", "TREND")
    price       = safe_get(data, "price", "?")
    vol_ratio   = parse_volume_ratio(data)

    # Update market state
    if price not in ("?", "—"):
        market_state["last_price"]   = price
        market_state["last_updated"] = datetime.now(timezone.utc).isoformat()
    vwap_pos = safe_get(data, "vwap_position", "")
    if vwap_pos:
        market_state["vwap_position"] = vwap_pos
    accum = safe_get(data, "accum_dist", "")
    if accum:
        market_state["accum_dist"] = accum

    logger.info(f"Received: {signal_type} {signal} @ ${price} | Vol:{vol_ratio:.2f}x | Session:{get_session()}")

    passed, rejection_reason = apply_filters(data)

    if not passed:
        logger.info(f"BLOCKED: {rejection_reason}")
        blocked_today.append({
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "signal":      signal,
            "signal_type": signal_type,
            "price":       price,
            "reason":      rejection_reason
        })
        return jsonify({"status": "blocked", "reason": rejection_reason,
                        "signal": signal, "price": price}), 200

    verdict_ar, verdict_en = get_verdict(data)

    if signal_type == "REVERSAL":
        tg_msg = format_reversal_alert(data)
    else:
        tg_msg = format_trend_alert(data)

    tg_ok = send_telegram(tg_msg)

    # Update cooldown state
    last_alert_time   = time.time()
    last_alert_price  = price
    last_alert_signal = signal
    if signal == "CALL":
        last_call_time = time.time()
    elif signal == "PUT":
        last_put_time = time.time()

    add_to_history(data, verdict_ar)

    logger.info(f"SENT: {signal_type} {signal} @ ${price} — {verdict_ar} (#{len(daily_alerts)} today)")

    return jsonify({
        "status":       "processed",
        "telegram":     "sent" if tg_ok else "failed",
        "signal":       signal,
        "signal_type":  signal_type,
        "verdict":      verdict_ar,
        "royal":        is_royal(data),
        "session":      get_session(),
        "alert_number": len(daily_alerts)
    }), 200


@app.route("/test", methods=["GET"])
def test_alert():
    test_trend = {
        "signal_type": "TREND", "signal": "CALL", "grade": "A+",
        "symbol": "TSLA", "price": "390.50", "session": "Morning Momentum",
        "macd_status": "Fresh Bull Cross", "macd_hist": "0.0523",
        "rsi_1m": "62.5", "rsi_5m": "60.1", "rsi_15m": "58.3",
        "volume_actual": "28.3K", "volume_avg": "18.5K", "volume_ratio": "1.53x",
        "score": "15/16", "stop_loss": "390.20", "target_1": "390.80", "target_2": "391.10",
        "max_risk": "$50", "accum_dist": "Accumulation", "institutional": "High Activity",
        "vwap_position": "above"
    }
    test_reversal = {
        "signal_type": "REVERSAL", "signal": "PUT", "grade": "REV",
        "symbol": "TSLA", "price": "395.80", "reversal_pattern": "Rejection Wick",
        "reversal_score": "9/10", "resistance_level": "396.20", "resistance_type": "Rolling High",
        "rsi_1m": "72.5", "rsi_5m": "68.3", "volume_ratio": "1.55x",
        "volume_actual": "22.1K", "volume_avg": "18.5K",
        "accum_dist": "Distribution", "institutional": "High Activity",
        "stop_loss": "396.10", "target_1": "395.50", "target_2": "395.20",
        "max_risk": "$50", "vwap_position": "above"
    }
    tg_ok1 = send_telegram(format_trend_alert(test_trend))
    time.sleep(2)
    tg_ok2 = send_telegram(format_reversal_alert(test_reversal))
    return jsonify({"status": "test_sent", "trend": "sent" if tg_ok1 else "failed",
                    "reversal": "sent" if tg_ok2 else "failed"}), 200


@app.route("/test_market", methods=["GET"])
def test_market():
    """Send a test market digest."""
    msg = format_market_digest("morning")
    ok  = send_telegram(msg)
    return jsonify({"status": "sent" if ok else "failed"}), 200


@app.route("/history", methods=["GET"])
def history():
    return jsonify({
        "total":         len(alert_history),
        "today_sent":    len(daily_alerts),
        "today_blocked": len(blocked_today),
        "alerts":        alert_history[:20]
    })


@app.route("/stats", methods=["GET"])
def stats():
    reset_daily_if_needed()
    calls     = sum(1 for a in daily_alerts if a["signal"] == "CALL")
    puts      = sum(1 for a in daily_alerts if a["signal"] == "PUT")
    entered   = sum(1 for a in daily_alerts if a["verdict"] == "ادخل")
    watched   = sum(1 for a in daily_alerts if a["verdict"] == "انتبه")
    reversals = sum(1 for a in daily_alerts if a["signal_type"] == "REVERSAL")

    block_reasons = {}
    for b in blocked_today:
        r = b.get("reason", "unknown")
        if any(x in r for x in ("مكرر", "cooldown", "سريع")):
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
            "total": len(daily_alerts), "calls": calls, "puts": puts,
            "reversals": reversals,
            "verdicts": {"ادخل": entered, "انتبه": watched}
        },
        "blocked":   {"total": len(blocked_today), "reasons": block_reasons},
        "remaining": MAX_DAILY_ALERTS - len(daily_alerts),
        "session":   get_session_label()
    })


@app.route("/reset", methods=["GET"])
def reset():
    global daily_alerts, daily_date, blocked_today
    daily_alerts  = []
    blocked_today = []
    daily_date    = get_today()
    return jsonify({"status": "reset", "date": daily_date})


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Smart Trading Alert Bot V4.1 Phase 2 — Starting...")
    logger.info(f"Server: {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Telegram: {'Configured' if TELEGRAM_BOT_TOKEN != 'YOUR_BOT_TOKEN_HERE' else 'NOT SET'}")
    logger.info("=" * 60)

    # Background threads
    for target in [keep_alive_worker, telegram_command_worker, market_digest_worker]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
    logger.info("Background threads started: keep-alive | telegram commands | market digest")

    logger.info("Endpoints: / | /webhook | /test | /test_market | /history | /stats | /reset")
    logger.info("=" * 60)

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
