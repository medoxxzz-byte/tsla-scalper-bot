"""
Smart Trading Alert Bot - V5.5 Mosquito Swamp Strategy Server
Webhook Server for TSLA Mosquito Swamp V5.5 Pine Script

PHILOSOPHY CHANGE in V5.5:
  - No more repeated "enter now" signals
  - Instead: Opening Map (once at 9:25 AM ET) + Reversal Alerts (Divergence + Fibonacci + Volume)
  - VOL TRAP stays on chart only (no Telegram)
  - Divergence Cooldown: once per 30 minutes
  - Fibonacci levels calculated from day's high/low

Features:
  - TRADE_V5_5 signals (CALL/PUT with Star Grading 1-5) — same as V5.4
  - OPENING_MAP: Morning briefing at 9:25 AM ET with Fibonacci levels
  - REVERSAL_ALERT: Divergence-based reversal warnings with Fibonacci + Volume
  - Loss Counter: 3 consecutive signals without movement = 30 mins silence
  - Pulls real-time best Options contract from Yahoo Finance
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

COOLDOWN_SECONDS_SIMILAR = 1500   # 25 min between same-direction trade signals
COOLDOWN_MIN_GAP         = 30     # minimum 30s between any two alerts
REVERSAL_COOLDOWN_SECS   = 1800   # 30 min between reversal alerts (new in V5.5)

MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "11"))
KEEP_ALIVE_INTERVAL = 600   # 10 minutes

LOSS_COUNTER_MAX      = 3
LOSS_COOLDOWN_SECONDS = 1800  # 30 minutes silence

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v55.log"),
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
last_call_time    = 0
last_put_time     = 0

# V5.5: Reversal alert cooldown
last_reversal_time = 0

# Loss Counter State
consecutive_no_move = 0
loss_cooldown_until = 0

daily_alerts  = []
daily_date    = ""
blocked_today = []

market_state = {
    "last_price": 0.0,
    "last_updated": None,
    "day_high": 0.0,
    "day_low": 0.0
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def get_et_now():
    return datetime.now(timezone.utc) - timedelta(hours=4)

def get_today():
    return get_et_now().strftime("%Y-%m-%d")

def reset_daily_if_needed():
    global daily_date, daily_alerts, blocked_today
    global consecutive_no_move, loss_cooldown_until, last_reversal_time
    today = get_today()
    if daily_date != today:
        daily_date = today
        daily_alerts = []
        blocked_today = []
        consecutive_no_move = 0
        loss_cooldown_until = 0
        last_reversal_time = 0
        logger.info(f"--- Reset daily limits for {today} ---")

def safe_get(data, key, default=""):
    val = data.get(key, default)
    return str(val).strip() if val is not None else default

# ──────────────────────────────────────────────────────────────────────────────
# Fibonacci Calculator
# ──────────────────────────────────────────────────────────────────────────────

def calc_fibonacci(high, low):
    """
    Calculate Fibonacci retracement levels from day's high and low.
    Returns dict with levels 23.6%, 38.2%, 50%, 61.8%, 78.6%
    """
    diff = high - low
    return {
        "high":   round(high, 2),
        "low":    round(low, 2),
        "fib_236": round(high - diff * 0.236, 2),
        "fib_382": round(high - diff * 0.382, 2),
        "fib_500": round(high - diff * 0.500, 2),
        "fib_618": round(high - diff * 0.618, 2),
        "fib_786": round(high - diff * 0.786, 2),
    }

def get_tsla_day_data():
    """
    Fetch TSLA intraday high/low and current price from Yahoo Finance.
    Returns (current_price, day_high, day_low) or (None, None, None) on error.
    """
    try:
        tkr = yf.Ticker("TSLA")
        info = tkr.fast_info
        day_high  = float(info.day_high)  if info.day_high  else 0.0
        day_low   = float(info.day_low)   if info.day_low   else 0.0
        last_price = float(info.last_price) if info.last_price else 0.0
        return last_price, day_high, day_low
    except Exception as e:
        logger.error(f"Error fetching TSLA day data: {e}")
        return None, None, None

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

        today_str = get_today()
        valid_dates = [d for d in exp_dates if d >= today_str]
        if not valid_dates:
            return None, None

        primary_expiry = valid_dates[0]
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

        df['strike_diff'] = abs(df['strike'] - current_price)

        if signal_type == "CALL":
            valid_strikes = df[df['strike'] <= current_price * 1.005]
        else:
            valid_strikes = df[df['strike'] >= current_price * 0.995]

        if valid_strikes.empty:
            valid_strikes = df

        best_row = valid_strikes.loc[valid_strikes['strike_diff'].idxmin()]

        last_price = float(best_row.get('lastPrice', 0))
        ask = float(best_row.get('ask', 0))
        bid = float(best_row.get('bid', 0))

        if last_price <= 0 and ask > 0:
            last_price = ask

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

MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

def format_expiry_ar(expiry_str):
    try:
        d = datetime.strptime(expiry_str, "%Y-%m-%d")
        return f"{d.day} {MONTHS_AR[d.month - 1]}"
    except:
        return expiry_str

def format_v5_5_trade_alert(data, primary_opt=None, alt_opt=None):
    """Format V5.5 Mosquito Swamp trade signal with Stars."""
    signal  = safe_get(data, "signal", "?")
    price   = safe_get(data, "price", "?")
    stars   = safe_get(data, "stars", "1")
    bias    = safe_get(data, "bias", "--")
    cond    = safe_get(data, "cond", "--")
    session = safe_get(data, "session", "--")
    vol     = safe_get(data, "vol", "--")
    bias_5m  = safe_get(data, "bias_5m", "--")
    bias_15m = safe_get(data, "bias_15m", "--")

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

    def tf_icon(bias_val, signal_dir):
        if signal_dir == "CALL":
            return "✅" if bias_val == "Bull" else "❌" if bias_val == "Bear" else "➖"
        else:
            return "✅" if bias_val == "Bear" else "❌" if bias_val == "Bull" else "➖"

    icon_5m  = tf_icon(bias_5m, signal)
    icon_15m = tf_icon(bias_15m, signal)

    msg = (
        f"{decision_icon} <b>{decision}</b> — Mosquito Swamp V5.5\n"
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
        is_0dte   = primary_opt.get('is_0dte', False)
        dte_label = "0DTE" if is_0dte else format_expiry_ar(primary_opt['expiry'])

        msg += (
            f"\n🎯 الأساسي: {signal} ${primary_opt['strike']:.0f} {dte_label}"
            f" | ${primary_opt['last_price']:.2f}"
            f" → TP ${primary_opt['tp']:.2f}"
            f" | SL ${primary_opt['sl']:.2f}\n"
        )

        if is_0dte and (stars_int < 3 or "Choppy" in cond):
            msg += "⚠️ 0DTE عالي الخطورة — يفضل العقد البديل\n"

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
        f"⏱ <i>الوقف الزمني: 10 دقائق — اطلع إذا ما تحرك السعر</i>"
    )
    return msg


def format_opening_map(price, day_high, day_low, fib, trend, liquidity):
    """
    Format the opening map message sent once at 9:25 AM ET.
    
    Args:
        price:     current TSLA price
        day_high:  today's high so far (or yesterday's high if pre-market)
        day_low:   today's low so far
        fib:       dict from calc_fibonacci()
        trend:     "صاعد" / "هابط" / "جانبي"
        liquidity: "عالية" / "طبيعية" / "منخفضة"
    """
    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")
    date_str  = now_et.strftime("%d/%m/%Y")

    msg = (
        f"📊 <b>خريطة TSLA — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>السعر الحالي:</b> <code>${price:.2f}</code>\n"
        f"📈 <b>الاتجاه المتوقع:</b> {trend}\n"
        f"💧 <b>السيولة:</b> {liquidity}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📐 <b>مستويات Fibonacci (من ${day_low:.2f} إلى ${day_high:.2f}):</b>\n\n"
        f"   🔴 مقاومة قوية:  <code>${fib['high']:.2f}</code>\n"
        f"   🟠 23.6%:        <code>${fib['fib_236']:.2f}</code>\n"
        f"   🟡 38.2%:        <code>${fib['fib_382']:.2f}</code>\n"
        f"   🟢 50.0%:        <code>${fib['fib_500']:.2f}</code>\n"
        f"   🔵 61.8%:        <code>${fib['fib_618']:.2f}</code>\n"
        f"   🟣 78.6%:        <code>${fib['fib_786']:.2f}</code>\n"
        f"   🟤 دعم قوي:      <code>${fib['low']:.2f}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>استخدم هذه المستويات كأهداف خروج ونقاط انعكاس محتملة.</i>\n"
        f"🕐 {timestamp} ET"
    )
    return msg


def format_reversal_alert(data, fib=None):
    """
    Format a reversal warning alert (Divergence-based).
    
    Expected data fields:
      - price:       current TSLA price
      - div_type:    "BULL" (bullish divergence) or "BEAR" (bearish divergence)
      - timeframe:   "5m" or "15m"
      - vol_confirm: "true" or "false" — does volume support the reversal?
      - day_high:    today's high (optional, used for Fibonacci)
      - day_low:     today's low  (optional, used for Fibonacci)
    """
    price       = safe_get(data, "price", "?")
    div_type    = safe_get(data, "div_type", "BEAR")
    timeframe   = safe_get(data, "timeframe", "15m")
    vol_confirm = safe_get(data, "vol_confirm", "false").lower() == "true"

    # Fibonacci from payload or from market_state
    try:
        d_high = float(safe_get(data, "day_high", "0")) or market_state["day_high"]
        d_low  = float(safe_get(data, "day_low",  "0")) or market_state["day_low"]
    except:
        d_high = market_state["day_high"]
        d_low  = market_state["day_low"]

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    if div_type == "BULL":
        direction_ar = "صعود محتمل"
        div_ar       = "Divergence إيجابي"
        arrow        = "📈"
    else:
        direction_ar = "هبوط محتمل"
        div_ar       = "Divergence سلبي"
        arrow        = "📉"

    vol_icon = "✅ يدعم الانعكاس" if vol_confirm else "⚠️ ضعيف — تأكد قبل الدخول"

    msg = (
        f"⚠️ <b>قرب انعكاس — TSLA @ <code>${price}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{arrow} <b>النوع:</b> {direction_ar} ({div_ar} {timeframe})\n"
        f"📦 <b>الحجم:</b> {vol_icon}\n\n"
    )

    # Add Fibonacci levels if we have day high/low
    if d_high > 0 and d_low > 0 and d_high > d_low:
        fib_levels = fib if fib else calc_fibonacci(d_high, d_low)
        try:
            current_p = float(price)
        except:
            current_p = 0

        msg += f"📐 <b>مستويات Fibonacci (دعم/مقاومة):</b>\n"

        if div_type == "BEAR":
            # For bearish reversal, show support levels below current price
            msg += (
                f"   38.2% → <code>${fib_levels['fib_382']:.2f}</code>\n"
                f"   50.0% → <code>${fib_levels['fib_500']:.2f}</code>\n"
                f"   61.8% → <code>${fib_levels['fib_618']:.2f}</code>\n"
            )
            if current_p > 0:
                nearest = min(
                    [fib_levels['fib_382'], fib_levels['fib_500'], fib_levels['fib_618']],
                    key=lambda x: abs(x - current_p)
                )
                msg += f"\n   🎯 <b>أقرب مستوى دعم:</b> <code>${nearest:.2f}</code>\n"
        else:
            # For bullish reversal, show resistance levels above current price
            msg += (
                f"   61.8% → <code>${fib_levels['fib_618']:.2f}</code>\n"
                f"   50.0% → <code>${fib_levels['fib_500']:.2f}</code>\n"
                f"   38.2% → <code>${fib_levels['fib_382']:.2f}</code>\n"
            )
            if current_p > 0:
                nearest = min(
                    [fib_levels['fib_382'], fib_levels['fib_500'], fib_levels['fib_618']],
                    key=lambda x: abs(x - current_p)
                )
                msg += f"\n   🎯 <b>أقرب مستوى مقاومة:</b> <code>${nearest:.2f}</code>\n"
    else:
        msg += "📐 <i>مستويات Fibonacci: غير متاحة (انتظر بيانات اليوم)</i>\n"

    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>الوقت المتوقع للانعكاس:</b> 30–60 دقيقة\n"
        f"✅ <b>القرار لك</b> — راجع Cheddar Flow للتأكيد\n"
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
        return False, "بيانات ناقصة (لا يوجد إشارة)"
    if not price or price in ("", "--", "?"):
        return False, "بيانات ناقصة (لا يوجد سعر)"
    return True, ""

def check_cooldown(data):
    global last_alert_time, last_alert_price, last_alert_signal
    global last_call_time, last_put_time
    global loss_cooldown_until

    now = time.time()

    if now < loss_cooldown_until:
        remaining = loss_cooldown_until - now
        return False, f"وضع الراحة مفعل — انتظر {remaining/60:.0f} دقيقة"

    elapsed = now - last_alert_time
    signal  = safe_get(data, "signal", "")
    current_price = safe_get(data, "price", "")

    if current_price == last_alert_price and signal == last_alert_signal and elapsed < COOLDOWN_MIN_GAP:
        return False, f"مكرر (نفس السعر {current_price} بفارق {elapsed:.0f}ث)"

    if elapsed < COOLDOWN_MIN_GAP:
        return False, f"سريع جداً ({elapsed:.0f}ث < {COOLDOWN_MIN_GAP}ث)"

    if signal == "CALL":
        if now - last_call_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_call_time)
            return False, f"CALL cooldown — انتظر {remaining/60:.0f} دقيقة"
    elif signal == "PUT":
        if now - last_put_time < COOLDOWN_SECONDS_SIMILAR:
            remaining = COOLDOWN_SECONDS_SIMILAR - (now - last_put_time)
            return False, f"PUT cooldown — انتظر {remaining/60:.0f} دقيقة"

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
        "service":       "Smart Trading Alert Bot — Mosquito Swamp V5.5",
        "version":       "5.5",
        "philosophy":    "Opening Map + Reversal Alerts (no repeated entry signals)",
        "alerts_today":  len(daily_alerts),
        "blocked_today": len(blocked_today),
        "remaining":     MAX_DAILY_ALERTS - len(daily_alerts),
        "timestamp":     datetime.now(timezone.utc).isoformat()
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    global last_alert_time, last_alert_price, last_alert_signal
    global last_call_time, last_put_time
    global last_reversal_time
    global consecutive_no_move, loss_cooldown_until

    if WEBHOOK_SECRET:
        auth = request.headers.get("X-Webhook-Secret", "")
        if auth != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json() if request.is_json else json.loads(request.data.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return jsonify({"error": "Parse error"}), 400

    signal   = safe_get(data, "signal", "?")
    price    = safe_get(data, "price", "?")
    msg_type = safe_get(data, "type", "TRADE")

    logger.info(f"Received: signal={signal} | type={msg_type} | price=${price}")

    # Update market state
    if price not in ("?", "--"):
        market_state["last_price"]   = price
        market_state["last_updated"] = datetime.now(timezone.utc).isoformat()

    # ── OPENING MAP ──────────────────────────────────────────────────────────
    if signal == "OPENING_MAP":
        logger.info("Processing OPENING_MAP request")
        try:
            # Try to get fresh data from Yahoo Finance
            live_price, d_high, d_low = get_tsla_day_data()

            # Fallback to payload data if Yahoo fails
            if not live_price:
                live_price = float(safe_get(data, "price", "0") or 0)
                d_high = float(safe_get(data, "day_high", "0") or 0)
                d_low  = float(safe_get(data, "day_low",  "0") or 0)

            # Update market state
            if d_high > 0:
                market_state["day_high"] = d_high
            if d_low > 0:
                market_state["day_low"] = d_low

            # Determine trend from Pine Script payload
            bias_str = safe_get(data, "bias", "")
            if "Bull" in bias_str or "bull" in bias_str:
                trend = "صاعد 📈"
            elif "Bear" in bias_str or "bear" in bias_str:
                trend = "هابط 📉"
            else:
                trend = "جانبي ↔️"

            # Determine liquidity from volume
            vol_str = safe_get(data, "vol", "")
            if "Ultra" in vol_str or "Surge" in vol_str:
                liquidity = "عالية 💧💧"
            elif "Normal" in vol_str:
                liquidity = "طبيعية 💧"
            else:
                liquidity = "منخفضة ⚠️"

            if d_high > 0 and d_low > 0 and d_high > d_low:
                fib = calc_fibonacci(d_high, d_low)
                tg_msg = format_opening_map(live_price or float(price), d_high, d_low, fib, trend, liquidity)
            else:
                # Minimal message if no range data
                now_et = get_et_now()
                tg_msg = (
                    f"📊 <b>خريطة TSLA — {now_et.strftime('%d/%m/%Y')}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"💰 <b>السعر:</b> <code>${price}</code>\n"
                    f"📈 <b>الاتجاه:</b> {trend}\n\n"
                    f"⚠️ <i>بيانات Fibonacci غير متاحة بعد — ستُحدَّث لاحقاً</i>\n"
                    f"🕐 {now_et.strftime('%I:%M %p')} ET"
                )

            tg_ok = send_telegram(tg_msg)
            return jsonify({"status": "opening_map_sent", "telegram": "sent" if tg_ok else "failed"}), 200

        except Exception as e:
            logger.error(f"Opening map error: {e}")
            return jsonify({"status": "error", "error": str(e)}), 200

    # ── REVERSAL ALERT ───────────────────────────────────────────────────────
    if signal == "REVERSAL_ALERT":
        logger.info(f"Processing REVERSAL_ALERT: div_type={safe_get(data, 'div_type', '?')}")

        # Cooldown: max once per 30 minutes
        now = time.time()
        if now - last_reversal_time < REVERSAL_COOLDOWN_SECS:
            remaining = REVERSAL_COOLDOWN_SECS - (now - last_reversal_time)
            logger.info(f"REVERSAL blocked by cooldown — wait {remaining/60:.0f} min")
            return jsonify({"status": "blocked", "reason": f"reversal cooldown — {remaining/60:.0f} min remaining"}), 200

        # Update day high/low from payload if provided
        try:
            ph = float(safe_get(data, "day_high", "0") or 0)
            pl = float(safe_get(data, "day_low",  "0") or 0)
            if ph > 0:
                market_state["day_high"] = ph
            if pl > 0:
                market_state["day_low"] = pl
        except:
            pass

        d_high = market_state["day_high"]
        d_low  = market_state["day_low"]
        fib = calc_fibonacci(d_high, d_low) if (d_high > 0 and d_low > 0 and d_high > d_low) else None

        tg_msg = format_reversal_alert(data, fib)
        tg_ok  = send_telegram(tg_msg)

        if tg_ok:
            last_reversal_time = now

        return jsonify({"status": "reversal_sent", "telegram": "sent" if tg_ok else "failed"}), 200

    # ── VOL TRAP (chart only — no Telegram in V5.5) ──────────────────────────
    if signal == "VOL_INTEL":
        logger.info(f"VOL_INTEL received (chart-only in V5.5, no Telegram): type={msg_type}")
        return jsonify({"status": "vol_intel_chart_only", "note": "VOL TRAP is chart-only in V5.5"}), 200

    # ── LOSS COUNTER LOGIC ───────────────────────────────────────────────────
    if last_alert_price and price not in ("?", "--"):
        try:
            prev_p = float(last_alert_price)
            curr_p = float(price)
            move_pct = abs(curr_p - prev_p) / prev_p
            if move_pct < 0.003:
                consecutive_no_move += 1
                logger.info(f"Price didn't move enough ({move_pct*100:.2f}%). Counter: {consecutive_no_move}/{LOSS_COUNTER_MAX}")
                if consecutive_no_move >= LOSS_COUNTER_MAX:
                    loss_cooldown_until = time.time() + LOSS_COOLDOWN_SECONDS
                    consecutive_no_move = 0
                    msg = (
                        "🛑 <b>تفعيل وضع الراحة (صمت 30 دقيقة)</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        "السوق في مسار عرضي ضعيف (3 إشارات متتالية بدون حركة سعرية كافية).\n"
                        "النظام سيتوقف عن إرسال الإشارات لمدة 30 دقيقة لحمايتك من التذبذب."
                    )
                    send_telegram(msg)
                    logger.info("Activated 30-min loss cooldown.")
            else:
                consecutive_no_move = 0
        except:
            pass

    # ── TRADE SIGNAL (CALL / PUT) ────────────────────────────────────────────
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
    primary_opt, alt_opt = get_best_option("TSLA", signal, price)

    tg_msg = format_v5_5_trade_alert(data, primary_opt, alt_opt)
    tg_ok  = send_telegram(tg_msg)

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
    """Test 5-Star CALL signal."""
    test_data = {
        "signal": "CALL", "type": "TRADE_V5_5", "price": "348.50",
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
        "strike": 348.0, "expiry": "2026-04-25",
        "last_price": 3.20, "tp": 4.48, "sl": 1.60
    }
    tg_ok = send_telegram(format_v5_5_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "5 Stars CALL", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_3stars", methods=["GET"])
def test_3stars():
    """Test 3-Star PUT signal."""
    test_data = {
        "signal": "PUT", "type": "TRADE_V5_5", "price": "352.00",
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
        "strike": 352.0, "expiry": "2026-04-25",
        "last_price": 2.10, "tp": 2.94, "sl": 1.05
    }
    tg_ok = send_telegram(format_v5_5_trade_alert(test_data, primary_opt, alt_opt))
    return jsonify({"status": "test_sent", "level": "3 Stars PUT", "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_opening_map", methods=["GET"])
def test_opening_map():
    """Test Opening Map message (V5.5 new feature)."""
    # Simulate a typical TSLA opening scenario
    test_price = 347.50
    test_high  = 352.80
    test_low   = 343.20
    fib = calc_fibonacci(test_high, test_low)
    tg_msg = format_opening_map(test_price, test_high, test_low, fib, "صاعد 📈", "عالية 💧💧")
    tg_ok = send_telegram(tg_msg)
    return jsonify({
        "status": "test_sent",
        "level": "Opening Map",
        "fib": fib,
        "telegram": "sent" if tg_ok else "failed"
    }), 200


@app.route("/test_reversal_bear", methods=["GET"])
def test_reversal_bear():
    """Test Bearish Reversal Alert (V5.5 new feature)."""
    test_data = {
        "signal": "REVERSAL_ALERT",
        "price": "352.40",
        "div_type": "BEAR",
        "timeframe": "15m",
        "vol_confirm": "true",
        "day_high": "355.80",
        "day_low": "344.20"
    }
    # Bypass cooldown for test
    d_high = 355.80
    d_low  = 344.20
    fib = calc_fibonacci(d_high, d_low)
    tg_msg = format_reversal_alert(test_data, fib)
    tg_ok = send_telegram(tg_msg)
    return jsonify({"status": "test_sent", "level": "Reversal Bear", "fib": fib, "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/test_reversal_bull", methods=["GET"])
def test_reversal_bull():
    """Test Bullish Reversal Alert (V5.5 new feature)."""
    test_data = {
        "signal": "REVERSAL_ALERT",
        "price": "345.60",
        "div_type": "BULL",
        "timeframe": "5m",
        "vol_confirm": "false",
        "day_high": "355.80",
        "day_low": "344.20"
    }
    d_high = 355.80
    d_low  = 344.20
    fib = calc_fibonacci(d_high, d_low)
    tg_msg = format_reversal_alert(test_data, fib)
    tg_ok = send_telegram(tg_msg)
    return jsonify({"status": "test_sent", "level": "Reversal Bull", "fib": fib, "telegram": "sent" if tg_ok else "failed"}), 200


@app.route("/reset", methods=["GET"])
def reset():
    global daily_alerts, daily_date, blocked_today
    global last_call_time, last_put_time, last_alert_price, last_alert_signal, last_alert_time
    global last_reversal_time, consecutive_no_move, loss_cooldown_until
    daily_alerts        = []
    blocked_today       = []
    daily_date          = get_today()
    last_call_time      = 0
    last_put_time       = 0
    last_alert_price    = ""
    last_alert_signal   = ""
    last_alert_time     = 0
    last_reversal_time  = 0
    consecutive_no_move = 0
    loss_cooldown_until = 0
    return jsonify({"status": "reset", "message": "All counters cleared — V5.5"})


# ──────────────────────────────────────────────────────────────────────────────
# Keep-Alive Worker
# ──────────────────────────────────────────────────────────────────────────────

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
