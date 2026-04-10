#!/usr/bin/env python3
"""
Smart Trading Alert Bot - V4.0 Mosquito Strategy Server
Webhook Server for TSLA Mosquito V4.0 Pine Script
Features:
  - Supports TRADE signals (CALL/PUT) from Mosquito V4.0
  - Supports LIQUIDITY REPORT signals (every 20 min, first 3 hours)
  - Backward compatible with V3.3 signals
  - Arabic Telegram messages
  - Cooldown, daily limits, market digest
  - Telegram commands: /market /status /history /stats /help /reset
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
SERVER_PORT    = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Cooldown settings
COOLDOWN_SECONDS_SIMILAR = 1500   # 25 min between same-direction signals
COOLDOWN_MIN_GAP         = 30     # minimum 30s between any two alerts

# Volume thresholds
MIN_VOLUME_RATIO_MORNING   = 0.5
MIN_VOLUME_RATIO_AFTERNOON = 0.4

# Limits
MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "35"))
KEEP_ALIVE_INTERVAL = 600   # 10 minutes

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v40.log"),
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
    "vwap_status":  "--",
    "trend_15m":    "--",
    "trend_5m":     "--",
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

def get_session():
    now = get_et_now()
    h, m = now.hour, now.minute
    total_min = h * 60 + m
    if total_min < 9 * 60 + 35:
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

def get_session_label():
    labels = {
        "pre_market":       "قبل السوق",
        "opening_power":    "افتتاح قوي",
        "morning_momentum": "زخم الصباح",
        "midday":           "منتصف اليوم",
        "power_hour":       "ساعة القوة",
        "after_hours":      "بعد السوق"
    }
    return labels.get(get_session(), get_session())

def reset_daily_if_needed():
    global daily_alerts, daily_date, blocked_today, liquidity_reports
    today = get_today()
    if daily_date != today:
        daily_date        = today
        daily_alerts      = []
        blocked_today     = []
        liquidity_reports = []
        logger.info(f"New trading day: {today} -- counters reset")


# ──────────────────────────────────────────────────────────────────────────────
# Data Helpers
# ──────────────────────────────────────────────────────────────────────────────

def safe_get(data, key, default="--"):
    val = data.get(key, "")
    if val is None or str(val).strip() in ("", "--", "\u2014"):
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

def add_to_history(data, verdict):
    entry = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "signal":       data.get("signal", "?"),
        "grade":        data.get("grade", "?"),
        "price":        data.get("price", "?"),
        "score":        data.get("score", "?"),
        "volume_ratio": data.get("volume_ratio", "?"),
        "verdict":      verdict,
        "session":      get_session()
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)


# ──────────────────────────────────────────────────────────────────────────────
# Signal Analysis (V4.0 + V3.3 backward compatible)
# ──────────────────────────────────────────────────────────────────────────────

def is_v4_signal(data):
    """Check if this is a V4.0 Mosquito signal (has 'type' field)."""
    return "type" in data

def get_verdict_v4(data):
    """V4.0 verdict based on conditions alignment."""
    bias = safe_get(data, "bias", "")
    vol  = safe_get(data, "vol", "")
    mom  = safe_get(data, "mom", "")
    cond = safe_get(data, "cond", "")

    if "Choppy" in cond:
        return "تجاوز", "SKIP"
    if "Valid" in mom and vol in ("Strong", "Normal"):
        return "ادخل", "ENTER"
    if "Valid" in mom:
        return "انتبه", "WATCH"
    return "تجاوز", "SKIP"

def get_verdict_v3(data):
    """V3.3 backward compatible verdict."""
    grade     = safe_get(data, "grade", "C")
    vol_ratio = parse_volume_ratio(data)
    score     = parse_score(data)

    if grade in ("A+", "A") and vol_ratio >= 1.0:
        return "ادخل", "ENTER"
    elif grade in ("A+", "A", "B+") and vol_ratio >= 0.4:
        return "انتبه", "WATCH"
    elif score >= 11 and vol_ratio >= 0.4:
        return "انتبه", "WATCH"
    else:
        return "تجاوز", "SKIP"

def get_verdict(data):
    if is_v4_signal(data):
        return get_verdict_v4(data)
    return get_verdict_v3(data)

def is_royal(data):
    grade     = safe_get(data, "grade", "C")
    vol_ratio = parse_volume_ratio(data)
    return grade in ("A+", "A") and vol_ratio >= 1.3


# ──────────────────────────────────────────────────────────────────────────────
# Message Formatters
# ──────────────────────────────────────────────────────────────────────────────

def format_v4_trade_alert(data):
    """Format V4.0 Mosquito trade signal."""
    signal = safe_get(data, "signal", "?")
    price  = safe_get(data, "price", "?")
    bias   = safe_get(data, "bias", "--")
    vwap   = safe_get(data, "vwap", "--")
    vol    = safe_get(data, "vol", "--")
    mom    = safe_get(data, "mom", "--")
    cond   = safe_get(data, "cond", "--")

    verdict_ar, verdict_en = get_verdict_v4(data)
    verdict_icon = "💚" if verdict_en == "ENTER" else "🟡" if verdict_en == "WATCH" else "🔴"

    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    msg = f"""{verdict_icon} <b>{verdict_ar}</b> -- Mosquito V4.0
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

📊 <b>الاتجاه:</b> {bias}
📍 <b>VWAP:</b> {vwap}
💧 <b>الحجم:</b> {vol}
⚡ <b>الزخم:</b> {mom}
🔍 <b>حالة السوق:</b> {cond}

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | {get_session_label()}
⏱ <i>الوقف الزمني: 10 دقائق -- اطلع اذا ما تحرك السعر</i>
⚠️ <i>تأكد من اغلاق الشمعة قبل الدخول</i>"""
    return msg


def format_v4_liquidity_report(data):
    """Format V4.0 Liquidity Report."""
    time_min = safe_get(data, "time_min", "?")
    vol_avg  = safe_get(data, "vol_avg", "?")
    atr      = safe_get(data, "atr", "?")
    cond     = safe_get(data, "cond", "--")
    bias     = safe_get(data, "bias", "--")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    # Determine condition icon
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


def format_v33_alert(data):
    """Format V3.3 backward compatible alert."""
    signal  = safe_get(data, "signal", "?")
    grade   = safe_get(data, "grade", "?")
    price   = safe_get(data, "price", "?")
    score   = safe_get(data, "score", "?")
    session = safe_get(data, "session", get_session_label())

    verdict_ar, verdict_en = get_verdict_v3(data)
    verdict_icon = "💚" if verdict_en == "ENTER" else "🟡" if verdict_en == "WATCH" else "🔴"

    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"
    grade_icon = "🔥" if grade == "A+" else "⚡" if grade == "A" else "📊" if grade == "B+" else "📋"

    royal        = is_royal(data)
    royal_header = "\n👑 <b>المحفظة الملكية -- فرصة ذهبية</b>\n" if royal else ""

    macd_status = safe_get(data, "macd_status", "--")
    macd_hist   = safe_get(data, "macd_hist", "--")

    rsi_1m       = safe_get(data, "rsi_1m", "--")
    rsi_1m_desc  = safe_get(data, "rsi_1m_desc", "")
    rsi_5m       = safe_get(data, "rsi_5m", "--")
    rsi_5m_desc  = safe_get(data, "rsi_5m_desc", "")
    rsi_15m      = safe_get(data, "rsi_15m", "--")
    rsi_15m_desc = safe_get(data, "rsi_15m_desc", "")

    rsi_1m_str  = f"{rsi_1m} ({rsi_1m_desc})"  if rsi_1m_desc  else rsi_1m
    rsi_5m_str  = f"{rsi_5m} ({rsi_5m_desc})"  if rsi_5m_desc  else rsi_5m
    rsi_15m_str = f"{rsi_15m} ({rsi_15m_desc})" if rsi_15m_desc else rsi_15m

    obv_status = safe_get(data, "obv_status", "--")
    obv_5m     = safe_get(data, "obv_5m", "--")
    obv_15m    = safe_get(data, "obv_15m", "--")

    vol_ratio  = parse_volume_ratio(data)
    vol_actual = safe_get(data, "volume_actual", "--")
    vol_avg    = safe_get(data, "volume_avg", "--")
    vol_desc   = safe_get(data, "volume_desc", "--")
    vol_surge  = safe_get(data, "volume_surge", "NO")
    surge_tag  = " 🔥" if vol_surge.upper() == "YES" else ""

    vwap_status   = safe_get(data, "vwap_status", "--")
    vwap_price    = safe_get(data, "vwap_price", "--")
    vwap_distance = safe_get(data, "vwap_distance", "--")

    ema_status = safe_get(data, "ema_status", "--")
    trend_15m  = safe_get(data, "trend_15m", "--")
    trend_5m   = safe_get(data, "trend_5m", "--")
    wave       = safe_get(data, "wave", "--")
    candle     = safe_get(data, "candle", "--")
    momentum   = safe_get(data, "momentum", "--")

    stop_loss = safe_get(data, "stop_loss", "--")
    target_1  = safe_get(data, "target_1", "--")
    target_2  = safe_get(data, "target_2", "--")
    sl_cents  = safe_get(data, "sl_cents", "--")
    tp1_cents = safe_get(data, "tp1_cents", "--")
    tp2_cents = safe_get(data, "tp2_cents", "--")

    max_risk             = safe_get(data, "max_risk", "--")
    suggested_contracts  = safe_get(data, "suggested_contracts", "--")
    portfolio            = safe_get(data, "portfolio", "--")
    atr                  = safe_get(data, "atr", "--")

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    msg = f"""{royal_header}{verdict_icon} <b>{verdict_ar}</b> -- {grade_icon} درجة {grade}
{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>
━━━━━━━━━━━━━━━━━━━━━

📊 <b>التقييم:</b> {score} | موجة: {wave}
📈 <b>الاتجاه:</b> 15m: {trend_15m} | 5m: {trend_5m}

📍 <b>VWAP:</b> {vwap_status} @ <code>${vwap_price}</code> ({vwap_distance})
📐 <b>EMA:</b> {ema_status}
📉 <b>MACD:</b> {macd_status} ({macd_hist})

📊 <b>RSI:</b>
  1m: {rsi_1m_str}
  5m: {rsi_5m_str}
  15m: {rsi_15m_str}

📦 <b>OBV:</b> 1m: {obv_status} | 5m: {obv_5m} | 15m: {obv_15m}
💧 <b>الحجم:</b> {vol_actual} / {vol_avg} ({vol_ratio:.1f}x){surge_tag} -- {vol_desc}

🕯 <b>الشمعة:</b> {candle}
⚡ <b>الزخم:</b> {momentum}

━━━━━━━━━━━━━━━━━━━━━
🎯 <b>الدخول:</b> <code>${price}</code>
🛑 <b>وقف الخسارة:</b> <code>${stop_loss}</code> ({sl_cents} سنت)
✅ <b>هدف 1:</b> <code>${target_1}</code> (+{tp1_cents} سنت)
✅ <b>هدف 2:</b> <code>${target_2}</code> (+{tp2_cents} سنت)

💰 <b>المخاطرة القصوى:</b> {max_risk}
📋 <b>العقود المقترحة:</b> {suggested_contracts} عقد
💼 <b>المحفظة:</b> ${portfolio} | ATR: ${atr}

━━━━━━━━━━━━━━━━━━━━━
🕐 {timestamp} ET | تنبيه #{len(daily_alerts) + 1} اليوم | {session}
⏱ <i>الوقف الزمني: 10 دقائق -- اطلع اذا ما تحرك السعر</i>"""

    return msg


def format_market_digest(period="morning"):
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")
    date_str  = now_et.strftime("%d/%m/%Y")

    last_price = market_state.get("last_price", "--")
    bias       = market_state.get("bias", "--")
    volume     = market_state.get("volume", "--")
    condition  = market_state.get("condition", "--")

    sent_today    = len(daily_alerts)
    blocked_count = len(blocked_today)
    report_count  = len(liquidity_reports)

    if period == "morning":
        header = "🌅 <b>تحليل الصباح -- قبل الجلسة</b>"
        footer = "⚡ <i>الجلسة تبدأ الآن -- ركز على الاشارات الاولى</i>"
    elif period == "midday":
        header = "☀️ <b>تحليل منتصف اليوم</b>"
        footer = f"📊 <i>اشارات اليوم: {sent_today} مرسلة | {blocked_count} محجوبة | {report_count} تقارير</i>"
    else:
        header = "🌙 <b>ملخص نهاية اليوم</b>"
        footer = f"📊 <i>اجمالي اليوم: {sent_today} اشارة | {blocked_count} محجوبة | {report_count} تقارير</i>"

    msg = f"""{header}
━━━━━━━━━━━━━━━━━━━━━

📅 {date_str} | 🕐 {timestamp} ET

📊 <b>الاتجاه:</b> {bias}
💧 <b>الحجم:</b> {volume}
🔍 <b>حالة السوق:</b> {condition}
💰 <b>آخر سعر:</b> <code>${last_price}</code>

━━━━━━━━━━━━━━━━━━━━━
{footer}"""
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
# Telegram Command Handlers
# ──────────────────────────────────────────────────────────────────────────────

def handle_command_market():
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    last_price = market_state.get("last_price", "--")
    bias       = market_state.get("bias", "--")
    volume     = market_state.get("volume", "--")
    momentum   = market_state.get("momentum", "--")
    condition  = market_state.get("condition", "--")

    msg = f"""📊 <b>وضع السوق الحالي</b>
━━━━━━━━━━━━━━━━━━━━━

🕐 {timestamp} ET | {get_session_label()}

📊 <b>الاتجاه:</b> {bias}
💧 <b>الحجم:</b> {volume}
⚡ <b>الزخم:</b> {momentum}
🔍 <b>حالة السوق:</b> {condition}
💰 <b>آخر سعر:</b> <code>${last_price}</code>

📋 <b>اشارات اليوم:</b> {len(daily_alerts)} مرسلة | {len(blocked_today)} محجوبة
📊 <b>تقارير السيولة:</b> {len(liquidity_reports)}
━━━━━━━━━━━━━━━━━━━━━
⚡ <i>البيانات تُحدّث مع كل اشارة</i>"""
    send_telegram(msg)


def handle_command_status():
    last = alert_history[0] if alert_history else None
    last_str = "لا يوجد اشارات بعد" if not last else \
        f"{last['signal']} @ ${last['price']} -- {last['verdict']} (درجة {last.get('grade', '?')})"

    msg = f"""⚙️ <b>حالة النظام</b>
━━━━━━━━━━━━━━━━━━━━━

✅ <b>السيرفر:</b> يعمل
🤖 <b>الاصدار:</b> Mosquito V4.0 (متوافق مع V3.3)
🕐 <b>الوقت:</b> {get_et_now().strftime('%I:%M %p')} ET

📊 <b>اشارات اليوم:</b> {len(daily_alerts)} / {MAX_DAILY_ALERTS}
🚫 <b>محجوبة:</b> {len(blocked_today)}
📊 <b>تقارير السيولة:</b> {len(liquidity_reports)}
⏱ <b>الجلسة:</b> {get_session_label()}

📡 <b>آخر اشارة:</b>
{last_str}"""
    send_telegram(msg)


def handle_command_history():
    if not alert_history:
        send_telegram("📋 لا يوجد اشارات في السجل بعد.")
        return

    lines = []
    for i, a in enumerate(alert_history[:5], 1):
        lines.append(
            f"{i}. {a['signal']} @ ${a['price']} -- {a['verdict']} "
            f"(درجة {a.get('grade', '?')}, {a.get('score', '?')})"
        )

    msg = "📋 <b>آخر 5 اشارات:</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    send_telegram(msg)


def handle_command_stats():
    reset_daily_if_needed()
    calls   = sum(1 for a in daily_alerts if a["signal"] == "CALL")
    puts    = sum(1 for a in daily_alerts if a["signal"] == "PUT")
    entered = sum(1 for a in daily_alerts if a["verdict"] == "ادخل")
    watched = sum(1 for a in daily_alerts if a["verdict"] == "انتبه")

    msg = f"""📈 <b>احصائيات اليوم</b>
━━━━━━━━━━━━━━━━━━━━━

📅 {get_today()}

✅ <b>مرسلة:</b> {len(daily_alerts)}
🟢 CALL: {calls} | 🔴 PUT: {puts}

💚 ادخل: {entered}
🟡 انتبه: {watched}
🚫 محجوبة: {len(blocked_today)}
📊 تقارير سيولة: {len(liquidity_reports)}

📊 <b>متبقي اليوم:</b> {MAX_DAILY_ALERTS - len(daily_alerts)} اشارة"""
    send_telegram(msg)


def handle_command_help():
    msg = """🤖 <b>أوامر البوت -- Mosquito V4.0</b>
━━━━━━━━━━━━━━━━━━━━━

/market  -- وضع السوق الحالي
/status  -- حالة النظام
/history -- آخر 5 اشارات
/stats   -- احصائيات اليوم
/reset   -- اعادة تعيين العداد والـ cooldown
/help    -- قائمة الأوامر

━━━━━━━━━━━━━━━━━━━━━
⚡ Mosquito V4.0 -- Fast Options Scalping"""
    send_telegram(msg)


def handle_command_reset():
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
    send_telegram("✅ <b>تم الاعادة</b>\nتم مسح العداد اليومي والـ cooldown بالكامل.\nالنظام جاهز للاشارات.")
    logger.info("Manual reset via Telegram /reset command")


# ──────────────────────────────────────────────────────────────────────────────
# Filters (for TRADE signals only)
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
# Background Workers
# ──────────────────────────────────────────────────────────────────────────────

def keep_alive_worker():
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
            if railway_url:
                resp = http_requests.get(f"https://{railway_url}/", timeout=10)
                logger.info(f"Keep-alive ping: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive failed: {e}")


def telegram_command_worker():
    last_update_id = 0
    while True:
        time.sleep(5)
        try:
            url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 3}
            resp   = http_requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            updates = resp.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg  = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                if text in ("/market", "market"):
                    handle_command_market()
                elif text in ("/status", "status"):
                    handle_command_status()
                elif text in ("/history", "history"):
                    handle_command_history()
                elif text in ("/stats", "stats"):
                    handle_command_stats()
                elif text in ("/help", "help", "/start", "start"):
                    handle_command_help()
                elif text in ("/reset", "reset"):
                    handle_command_reset()
        except Exception as e:
            logger.warning(f"Telegram command worker error: {e}")


def market_digest_worker():
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

            if today != last_date:
                sent_morning = sent_midday = sent_eod = False
                last_date = today

            if h == 9 and m >= 25 and m < 30 and not sent_morning:
                send_telegram(format_market_digest("morning"))
                sent_morning = True
                logger.info("Sent morning digest")

            if h == 12 and m < 5 and not sent_midday:
                send_telegram(format_market_digest("midday"))
                sent_midday = True
                logger.info("Sent midday digest")

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
        "service":       "Smart Trading Alert Bot -- Mosquito V4.0",
        "version":       "4.0",
        "compatible":    "V3.3 + V4.0",
        "alerts_today":  len(daily_alerts),
        "blocked_today": len(blocked_today),
        "reports_today": len(liquidity_reports),
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
        logger.error(f"JSON parse error: {e}")
        return jsonify({"error": "Parse error"}), 400

    signal     = safe_get(data, "signal", "?")
    price      = safe_get(data, "price", "?")
    msg_type   = safe_get(data, "type", "TRADE")

    logger.info(f"Received: {signal} | Type: {msg_type} | Price: ${price} | Session: {get_session()}")

    # Update market state
    if price not in ("?", "--"):
        market_state["last_price"]   = price
        market_state["last_updated"] = datetime.now(timezone.utc).isoformat()

    # V4.0 fields
    bias = safe_get(data, "bias", "")
    if bias:
        market_state["bias"] = bias
    vol = safe_get(data, "vol", "")
    if vol:
        market_state["volume"] = vol
    mom = safe_get(data, "mom", "")
    if mom:
        market_state["momentum"] = mom
    cond = safe_get(data, "cond", "")
    if cond:
        market_state["condition"] = cond

    # V3.3 backward compat fields
    vwap_status = safe_get(data, "vwap_status", "")
    if vwap_status:
        market_state["vwap_status"] = vwap_status
    trend_15m = safe_get(data, "trend_15m", "")
    if trend_15m:
        market_state["trend_15m"] = trend_15m
    trend_5m = safe_get(data, "trend_5m", "")
    if trend_5m:
        market_state["trend_5m"] = trend_5m

    # ── LIQUIDITY REPORT ──
    if msg_type == "LIQUIDITY" and signal == "REPORT":
        tg_msg = format_v4_liquidity_report(data)
        tg_ok  = send_telegram(tg_msg)
        liquidity_reports.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_min":  safe_get(data, "time_min", "?"),
            "bias":      bias,
            "condition": cond
        })
        logger.info(f"LIQUIDITY REPORT #{len(liquidity_reports)} sent")
        return jsonify({
            "status":  "report_sent",
            "telegram": "sent" if tg_ok else "failed",
            "report_number": len(liquidity_reports)
        }), 200

    # ── TRADE SIGNAL ──
    try:
        passed, rejection_reason = apply_filters(data)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"apply_filters error: {e}\n{tb}")
        return jsonify({"status": "error", "error": str(e)}), 200

    if not passed:
        logger.info(f"BLOCKED: {rejection_reason}")
        blocked_today.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal":    signal,
            "price":     price,
            "reason":    rejection_reason
        })
        return jsonify({"status": "blocked", "reason": rejection_reason,
                        "signal": signal, "price": price}), 200

    # Format message based on version
    if is_v4_signal(data):
        verdict_ar, verdict_en = get_verdict_v4(data)
        tg_msg = format_v4_trade_alert(data)
    else:
        verdict_ar, verdict_en = get_verdict_v3(data)
        tg_msg = format_v33_alert(data)

    tg_ok = send_telegram(tg_msg)

    # Update cooldown state
    now = time.time()
    last_alert_time   = now
    last_alert_price  = price
    last_alert_signal = signal
    if signal == "CALL":
        last_call_time = now
    elif signal == "PUT":
        last_put_time = now

    add_to_history(data, verdict_ar)

    logger.info(f"SENT: {signal} @ ${price} -- {verdict_ar} (#{len(daily_alerts)} today)")

    return jsonify({
        "status":       "processed",
        "telegram":     "sent" if tg_ok else "failed",
        "signal":       signal,
        "verdict":      verdict_ar,
        "version":      "V4.0" if is_v4_signal(data) else "V3.3",
        "session":      get_session(),
        "alert_number": len(daily_alerts)
    }), 200


@app.route("/test", methods=["GET"])
def test_alert():
    """Test V4.0 CALL signal."""
    test_data = {
        "signal": "CALL",
        "type":   "TRADE",
        "price":  "285.50",
        "bias":   "Bullish",
        "vwap":   "Above VWAP (Bull Control)",
        "vol":    "Strong",
        "mom":    "Bullish (Valid)",
        "cond":   "Trending (Clear)"
    }
    tg_ok = send_telegram(format_v4_trade_alert(test_data))
    return jsonify({"status": "test_sent", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_put", methods=["GET"])
def test_put_alert():
    """Test V4.0 PUT signal."""
    test_data = {
        "signal": "PUT",
        "type":   "TRADE",
        "price":  "282.10",
        "bias":   "Bearish",
        "vwap":   "Below VWAP (Bear Control)",
        "vol":    "Strong",
        "mom":    "Bearish (Valid)",
        "cond":   "Trending (Clear)"
    }
    tg_ok = send_telegram(format_v4_trade_alert(test_data))
    return jsonify({"status": "test_put_sent", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_report", methods=["GET"])
def test_report():
    """Test Liquidity Report."""
    test_data = {
        "signal":   "REPORT",
        "type":     "LIQUIDITY",
        "time_min": "40",
        "vol_avg":  "125000",
        "atr":      "0.45",
        "cond":     "Trending (Clear)",
        "bias":     "Bullish"
    }
    tg_ok = send_telegram(format_v4_liquidity_report(test_data))
    return jsonify({"status": "test_report_sent", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_v33", methods=["GET"])
def test_v33_alert():
    """Test V3.3 backward compatible CALL signal."""
    test_data = {
        "signal":       "CALL",
        "grade":        "A+",
        "symbol":       "TSLA",
        "price":        "285.50",
        "session":      "Morning Momentum",
        "wave":         "Bullish Wave (Strong)",
        "trend_15m":    "Bullish CONFIRMED",
        "trend_5m":     "Bullish CONFIRMED",
        "vwap_status":  "Above VWAP",
        "vwap_price":   "283.20",
        "vwap_distance": "0.82%",
        "ema_status":   "EMA9 above EMA21 (all TFs)",
        "macd_status":  "Fresh Bull Cross",
        "macd_hist":    "0.0412",
        "rsi_1m":       "62.5",
        "rsi_1m_desc":  "Strong Bullish",
        "rsi_5m":       "58.3",
        "rsi_5m_desc":  "Bullish",
        "rsi_15m":      "55.1",
        "rsi_15m_desc": "Bullish",
        "obv_status":   "Rising (Bullish)",
        "obv_5m":       "Rising",
        "obv_15m":      "Rising",
        "volume_actual": "32.5K",
        "volume_avg":   "21.3K",
        "volume_ratio": "1.53x",
        "volume_desc":  "Above Avg",
        "volume_surge": "YES",
        "candle":       "Strong Bullish (body>55%)",
        "momentum":     "1.25",
        "score":        "15/16",
        "stop_loss":    "285.20",
        "target_1":     "285.80",
        "target_2":     "286.10",
        "sl_cents":     "30",
        "tp1_cents":    "30",
        "tp2_cents":    "60",
        "atr":          "0.45",
        "max_risk":     "$150",
        "suggested_contracts": "1-3",
        "portfolio":    "3000",
        "max_daily_loss": "300"
    }
    tg_ok = send_telegram(format_v33_alert(test_data))
    return jsonify({"status": "test_v33_sent", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/history", methods=["GET"])
def history():
    return jsonify({
        "total":         len(alert_history),
        "today_sent":    len(daily_alerts),
        "today_blocked": len(blocked_today),
        "today_reports": len(liquidity_reports),
        "alerts":        alert_history[:20]
    })


@app.route("/stats", methods=["GET"])
def stats():
    reset_daily_if_needed()
    calls   = sum(1 for a in daily_alerts if a["signal"] == "CALL")
    puts    = sum(1 for a in daily_alerts if a["signal"] == "PUT")
    entered = sum(1 for a in daily_alerts if a["verdict"] == "ادخل")
    watched = sum(1 for a in daily_alerts if a["verdict"] == "انتبه")

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
            "verdicts": {"ادخل": entered, "انتبه": watched}
        },
        "blocked":   {"total": len(blocked_today), "reasons": block_reasons},
        "reports":   len(liquidity_reports),
        "remaining": MAX_DAILY_ALERTS - len(daily_alerts),
        "session":   get_session_label()
    })


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
    logger.info("Reset via /reset endpoint")
    return jsonify({"status": "reset", "date": daily_date, "cooldowns": "cleared",
                    "message": "Daily counter, cooldowns, and reports cleared"})


@app.route("/daily", methods=["GET"])
def daily():
    return jsonify({
        "alerts":  daily_alerts,
        "blocked": blocked_today,
        "reports": liquidity_reports
    })


# ──────────────────────────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────────────────────────

def start_background_threads():
    for target in [keep_alive_worker, telegram_command_worker, market_digest_worker]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
    logger.info("Background threads started: keep-alive | telegram commands | market digest")

start_background_threads()

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Smart Trading Alert Bot -- Mosquito V4.0 -- Starting...")
    logger.info(f"Server: {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Telegram: {'Configured' if TELEGRAM_BOT_TOKEN != 'YOUR_BOT_TOKEN_HERE' else 'NOT SET'}")
    logger.info("Endpoints: / | /webhook | /test | /test_put | /test_report | /test_v33 | /history | /stats | /reset | /daily")
    logger.info("=" * 60)
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
