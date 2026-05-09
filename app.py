"""
Smart Trading Alert Bot - V6.0
Webhook Server for TSLA Mosquito Swamp Pine Script

NEW IN V6.0:
  - فلتر ADX إجباري (لا تداول في سوق جانبي ADX < 20)
  - Stop Loss تلقائي -20% (محاكاة على السهم)
  - FlashAlpha GEX كفلتر إضافي (توافق مع مستويات Gamma)
  - نافذة تداول: 9:30 AM - 3:30 PM ET (البوت يعمل مستقل)
  - Alpaca Paper Trading Auto-Executor
  - Position tracker with real-time P&L

الهدف: تجربة 3-6 أشهر لإثبات الاستراتيجية قبل التداول الحقيقي.
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

## ── FlashAlpha GEX Integration (V6.0) ────────────────────────────────────
try:
    import flashalpha_gex as fa
    _FA_AVAILABLE = True
except ImportError:
    _FA_AVAILABLE = False
    logging.warning("flashalpha_gex.py not found — GEX filter disabled")

# ── Reversal Detector Integration ─────────────────────────────────────────
try:
    import reversal_detector as rd
    _RD_AVAILABLE = True
except ImportError:
    _RD_AVAILABLE = False
    logging.warning("reversal_detector.py not found — reversal detection disabled")

# # ── Reversal Tracker Integration ─────────────────────────────────────────
try:
    import reversal_tracker as rt
    _RT_AVAILABLE = True
except ImportError:
    _RT_AVAILABLE = False
    logging.warning("reversal_tracker.py not found — reversal tracking disabled")
# ── Alpaca Options Feed Integration ─────────────────────────────────────────
try:
    import alpaca_options as ao
    _AO_AVAILABLE = True
except ImportError:
    _AO_AVAILABLE = False
    logging.warning("alpaca_options.py not found — options feed disabled")

# ── حالة الصفقة الحالية لتحديد نوع الأوبشن ────────────────────────────────────
_current_signal_type = None  # "CALL" أو "PUT"

def _get_signal_type():
    return _current_signal_type

def _get_tsla_price_for_options():
    """جلب سعر TSLA من Alpaca (بديل yfinance الموثوق)."""
    return get_tsla_price_alpaca_snapshot()

def _on_option_data_received(opt: dict):
    """استقبال بيانات الأوبشن الحية وتمريرها لـ reversal_detector."""
    if _RD_AVAILABLE:
        rd.update_option_data(
            premium      = opt["premium"],
            delta        = opt["delta"],
            option_symbol= opt["symbol"],
            strike_price = opt["strike"],
            expiration   = opt["expiration"],
            option_type  = opt["option_type"]
        )

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8701854195:AAHVmtrdxwyPBjtXMC-bU1ZCOnUBNafmtzA")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "975644160")

SERVER_HOST    = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT    = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ── Alpaca Configuration ──────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")

# ── Trading Window (ET) ───────────────────────────────────────────────────────
# نافذة واسعة: 9:30 AM - 3:30 PM ET (البوت يعمل مستقل طوال الجلسة)
TRADING_START_HOUR   = 9    # 9:30 AM ET
TRADING_START_MINUTE = 30
TRADING_END_HOUR     = 15   # 3:30 PM ET
TRADING_END_MINUTE   = 30

# ── Position Sizing ───────────────────────────────────────────────────────────
MAX_CONTRACTS_PER_TRADE = 10     # عدد العقود لكل صفقة (تمت الزيادة بطلب المستخدم)
MAX_OPTION_PRICE        = 5.00   # أقصى سعر للعقد (لا تشتري عقود غالية جداً)
MIN_OPTION_PRICE        = 0.10   # أدنى سعر (لا تشتري عقود رخيصة جداً = خطر)
MIN_STARS_TO_EXECUTE    = 3      # أقل عدد نجوم لتنفيذ الصفقة تلقائياً

# ── Cooldowns ─────────────────────────────────────────────────────────────────
COOLDOWN_SECONDS_SIMILAR = 1500   # 25 min between same-direction trade signals
COOLDOWN_MIN_GAP         = 30     # minimum 30s between any two alerts
REVERSAL_COOLDOWN_SECS   = 1800   # 30 min between reversal alerts

MAX_DAILY_ALERTS    = int(os.environ.get("MAX_DAILY_TRADES", "11"))
KEEP_ALIVE_INTERVAL = 600

# V6.0: فلتر ADX إجباري (لا تداول في سوق جانبي)
MIN_ADX_TO_TRADE = 20  # ADX < 20 = لا دخول

# V6.0: Stop Loss تلقائي -20% (محاكاة على السهم)
AUTO_STOP_LOSS_PCT = 0.20  # -20% من سعر الأوبشن (يعادل ~1.5% على السهم)

LOSS_COUNTER_MAX      = 3
LOSS_COOLDOWN_SECONDS = 1800

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert_bot_v56.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Flask App & State
# ──────────────────────────────────────────────────────────────────────────────


# ── HTTP Session with Retry (لحل SSL timeouts) ──────────────────────────────
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _create_retry_session():
    """إنشاء session مع retry تلقائي لحل SSL timeouts."""
    session = http_requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

_retry_session = _create_retry_session()

app = Flask(__name__)

alert_history = []
MAX_HISTORY   = 100

last_alert_time   = 0
last_alert_price  = ""
last_alert_signal = ""
last_call_time    = 0
last_put_time     = 0
last_reversal_time = 0

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

# Alpaca position tracker
active_positions = {}   # {order_id: {signal, strike, contracts, entry_price, sl, tp, symbol}}

# ──────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def get_et_now():
    return datetime.now(timezone.utc) - timedelta(hours=4)

def get_today():
    return get_et_now().strftime("%Y-%m-%d")

def is_trading_window():
    """
    Returns True only if current ET time is within the allowed trading window:
    10:00 AM - 3:30 PM ET (30 min after open, stop at 3:30 PM)
    """
    now = get_et_now()
    current_minutes = now.hour * 60 + now.minute
    start_minutes   = TRADING_START_HOUR * 60 + TRADING_START_MINUTE   # 600 = 10:00 AM
    end_minutes     = TRADING_END_HOUR   * 60 + TRADING_END_MINUTE     # 930 = 3:30 PM

    # Also check it's a weekday (Mon-Fri)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, "عطلة نهاية الأسبوع"

    if current_minutes < start_minutes:
        remaining = start_minutes - current_minutes
        return False, f"قبل نافذة التداول — يبدأ 9:30 AM ET (بعد {remaining} دقيقة)"

    if current_minutes >= end_minutes:
        return False, "انتهت نافذة التداول — 3:30 PM ET"

    return True, "نافذة التداول مفتوحة (9:30-3:30) ✅"

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
        logger.info(f"--- V6.0 Reset daily limits for {today} ---")

def safe_get(data, key, default=""):
    val = data.get(key, default)
    return str(val).strip() if val is not None else default

# ──────────────────────────────────────────────────────────────────────────────
# Alpaca API Functions
# ──────────────────────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json"
    }

def get_alpaca_account():
    """Get current account info from Alpaca."""
    try:
        r = _retry_session.get(
            f"{ALPACA_BASE_URL}/v2/account",
            headers=alpaca_headers(),
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
        logger.error(f"Alpaca account error: {r.status_code} {r.text}")
        return None
    except Exception as e:
        logger.error(f"Alpaca account exception: {e}")
        return None

def get_alpaca_positions():
    """Get open positions from Alpaca."""
    try:
        r = _retry_session.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=alpaca_headers(),
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        logger.error(f"Alpaca positions error: {e}")
        return []

def place_alpaca_stock_order(symbol, qty, side, order_type="market",
                              limit_price=None, stop_price=None,
                              time_in_force="day"):
    """
    Place a stock order on Alpaca Paper Trading.
    side: 'buy' or 'sell'
    """
    payload = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": time_in_force,
    }
    if order_type == "limit" and limit_price:
        payload["limit_price"] = str(round(limit_price, 2))
    if order_type == "stop" and stop_price:
        payload["stop_price"] = str(round(stop_price, 2))

    try:
        r = _retry_session.post(
            f"{ALPACA_BASE_URL}/v2/orders",
            headers=alpaca_headers(),
            json=payload,
            timeout=15
        )
        if r.status_code in (200, 201):
            order = r.json()
            logger.info(f"Alpaca order placed: {order.get('id')} | {side} {qty} {symbol}")
            return order
        else:
            logger.error(f"Alpaca order error: {r.status_code} {r.text}")
            return None
    except Exception as e:
        logger.error(f"Alpaca order exception: {e}")
        return None

def close_alpaca_position(symbol):
    """Close an open position by symbol."""
    try:
        r = _retry_session.delete(
            f"{ALPACA_BASE_URL}/v2/positions/{symbol}",
            headers=alpaca_headers(),
            timeout=10
        )
        if r.status_code in (200, 204):
            logger.info(f"Alpaca position closed: {symbol}")
            return True
        logger.error(f"Close position error: {r.status_code} {r.text}")
        return False
    except Exception as e:
        logger.error(f"Close position exception: {e}")
        return False

def execute_trade(signal, price, stars, opt_data):
    """
    Execute a trade on Alpaca Paper Trading.
    Since Alpaca Paper doesn't support options, we trade TSLA stock as proxy.
    - CALL signal → BUY TSLA shares
    - PUT signal  → SELL SHORT TSLA shares (if allowed) or skip
    
    Returns: (success, order_id, message)
    """
    try:
        # Check account
        account = get_alpaca_account()
        if not account:
            return False, None, "فشل الاتصال بـ Alpaca"

        buying_power = float(account.get("buying_power", 0))
        cash = float(account.get("cash", 0))

        # Determine trade parameters
        # We use TSLA stock as proxy for options
        # For CALL: buy MAX_CONTRACTS_PER_TRADE shares of TSLA
        # For PUT: we'll simulate by tracking the signal (paper only)
        
        tsla_price = float(price)
        qty = MAX_CONTRACTS_PER_TRADE  # 10 أسهم لكل صفقة (تم التحديث بطلب المستخدم)

        if signal == "CALL":
            # Check we have enough buying power
            if buying_power < tsla_price * qty:
                return False, None, f"رصيد غير كافٍ (${buying_power:.0f} < ${tsla_price:.0f})"

            order = place_alpaca_stock_order("TSLA", qty, "buy")
            if not order:
                return False, None, "فشل تنفيذ أمر الشراء"

            order_id = order.get("id", "")
            
            # Calculate SL and TP based on option data if available
            # V6.0: SL ثابت -20% من الأوبشن، TP +40% (محاكاة على السهم)
            # للسهم كبروكسي: SL -1.5% و TP +2.5% (يعادل تقريباً -20%/+40% على الأوبشن)
            tp_stock = tsla_price * 1.025  # +2.5% (محاكاة +40% أوبشن)
            sl_stock = tsla_price * (1 - AUTO_STOP_LOSS_PCT * 0.075)  # -1.5% (محاكاة -20% أوبشن)

            # Store position info
            active_positions[order_id] = {
                "signal":     signal,
                "tsla_price": tsla_price,
                "qty":        qty,
                "tp":         round(tp_stock, 2),
                "sl":         round(sl_stock, 2),
                "entry_time": get_et_now().strftime("%I:%M %p"),
                "stars":      stars,
                "opt_data":   opt_data
            }

            msg = (
                f"✅ <b>تم تنفيذ الصفقة — Alpaca Paper</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>شراء TSLA</b> × {qty} سهم\n"
                f"💰 <b>سعر الدخول:</b> ~${tsla_price:.2f}\n"
                f"🎯 <b>الهدف:</b> ${tp_stock:.2f} (+{((tp_stock/tsla_price)-1)*100:.1f}%)\n"
                f"🛑 <b>وقف الخسارة:</b> ${sl_stock:.2f} (-{(1-(sl_stock/tsla_price))*100:.1f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Order: <code>{order_id[:8]}...</code>\n"
                f"⭐ النجوم: {'⭐'*int(stars)}\n"
                f"🕐 {get_et_now().strftime('%I:%M %p')} ET"
            )
            return True, order_id, msg

        elif signal == "PUT":
            # For PUT: try short selling (paper trading allows it)
            # Check if shorting is enabled
            if account.get("shorting_enabled", False):
                order = place_alpaca_stock_order("TSLA", qty, "sell")
                if not order:
                    return False, None, "فشل تنفيذ أمر البيع"

                order_id = order.get("id", "")
                tp_stock = tsla_price * 0.985  # -1.5%
                sl_stock = tsla_price * 1.010  # +1.0%

                active_positions[order_id] = {
                    "signal":     signal,
                    "tsla_price": tsla_price,
                    "qty":        qty,
                    "tp":         round(tp_stock, 2),
                    "sl":         round(sl_stock, 2),
                    "entry_time": get_et_now().strftime("%I:%M %p"),
                    "stars":      stars,
                    "opt_data":   opt_data
                }

                msg = (
                    f"✅ <b>تم تنفيذ الصفقة — Alpaca Paper</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔴 <b>بيع (Short) TSLA</b> × {qty} سهم\n"
                    f"💰 <b>سعر الدخول:</b> ~${tsla_price:.2f}\n"
                    f"🎯 <b>الهدف:</b> ${tp_stock:.2f} (-{(1-(tp_stock/tsla_price))*100:.1f}%)\n"
                    f"🛑 <b>وقف الخسارة:</b> ${sl_stock:.2f} (+{((sl_stock/tsla_price)-1)*100:.1f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 Order: <code>{order_id[:8]}...</code>\n"
                    f"⭐ النجوم: {'⭐'*int(stars)}\n"
                    f"🕐 {get_et_now().strftime('%I:%M %p')} ET"
                )
                return True, order_id, msg
            else:
                # Shorting not enabled — log as paper trade only
                fake_id = f"PAPER_{int(time.time())}"
                tp_stock = tsla_price * 0.985
                sl_stock = tsla_price * 1.010

                active_positions[fake_id] = {
                    "signal":     signal,
                    "tsla_price": tsla_price,
                    "qty":        qty,
                    "tp":         round(tp_stock, 2),
                    "sl":         round(sl_stock, 2),
                    "entry_time": get_et_now().strftime("%I:%M %p"),
                    "stars":      stars,
                    "opt_data":   opt_data,
                    "paper_only": True
                }

                msg = (
                    f"📝 <b>تسجيل صفقة PUT — Paper Only</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔴 <b>PUT TSLA</b> × {qty} (محاكاة)\n"
                    f"💰 <b>سعر الدخول:</b> ${tsla_price:.2f}\n"
                    f"🎯 <b>الهدف:</b> ${tp_stock:.2f}\n"
                    f"🛑 <b>وقف الخسارة:</b> ${sl_stock:.2f}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⭐ النجوم: {'⭐'*int(stars)}\n"
                    f"ℹ️ البيع القصير غير مفعل — تم التسجيل فقط\n"
                    f"🕐 {get_et_now().strftime('%I:%M %p')} ET"
                )
                return True, fake_id, msg

    except Exception as e:
        logger.error(f"execute_trade error: {e}")
        return False, None, f"خطأ في التنفيذ: {e}"

# ──────────────────────────────────────────────────────────────────────────────
# Position Monitor (background thread)
# ──────────────────────────────────────────────────────────────────────────────

def monitor_positions():
    """Background thread: check open positions against SL/TP every 60 seconds."""
    while True:
        time.sleep(60)
        if not active_positions:
            continue

        try:
            # Get current TSLA price — Alpaca (موثوق بدلاً من yfinance)
            current_price = get_tsla_price_alpaca_snapshot()
            if current_price <= 0:
                continue

            now = get_et_now()
            positions_to_close = []

            for order_id, pos in list(active_positions.items()):
                signal     = pos["signal"]
                entry      = pos["tsla_price"]
                tp         = pos["tp"]
                sl         = pos["sl"]
                entry_time = pos["entry_time"]
                stars      = pos["stars"]
                paper_only = pos.get("paper_only", False)

                hit_tp = False
                hit_sl = False
                close_reason = ""
                pnl = 0.0

                if signal == "CALL":
                    pnl = (current_price - entry) * pos["qty"]
                    if current_price >= tp:
                        hit_tp = True
                        close_reason = "🎯 وصل الهدف"
                    elif current_price <= sl:
                        hit_sl = True
                        close_reason = "🛑 وصل وقف الخسارة"
                elif signal == "PUT":
                    pnl = (entry - current_price) * pos["qty"]
                    if current_price <= tp:
                        hit_tp = True
                        close_reason = "🎯 وصل الهدف"
                    elif current_price >= sl:
                        hit_sl = True
                        close_reason = "🛑 وصل وقف الخسارة"

                # Force close at 3:45 PM ET (15 min before market close)
                force_close = (now.hour == 15 and now.minute >= 45)

                if hit_tp or hit_sl or force_close:
                    if force_close:
                        close_reason = "⏰ إغلاق إجباري (قبل إغلاق السوق)"

                    # Close position if not paper_only
                    if not paper_only:
                        close_alpaca_position("TSLA")

                    pnl_icon = "💚" if pnl >= 0 else "🔴"
                    msg = (
                        f"{pnl_icon} <b>إغلاق صفقة — Alpaca Paper</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{'🟢 CALL' if signal == 'CALL' else '🔴 PUT'} TSLA\n"
                        f"📥 <b>دخول:</b> ${entry:.2f} ({entry_time})\n"
                        f"📤 <b>خروج:</b> ${current_price:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{close_reason}\n"
                        f"{'💰' if pnl >= 0 else '💸'} <b>P&L:</b> {'+' if pnl >= 0 else ''}{pnl:.2f}$\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⭐ النجوم: {'⭐'*int(stars)}\n"
                        f"🕐 {now.strftime('%I:%M %p')} ET"
                    )
                    send_telegram(msg)
                    positions_to_close.append(order_id)
                    logger.info(f"Position closed: {order_id} | P&L: {pnl:.2f} | Reason: {close_reason}")

            for oid in positions_to_close:
                active_positions.pop(oid, None)

        except Exception as e:
            logger.error(f"monitor_positions error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Fibonacci Calculator
# ──────────────────────────────────────────────────────────────────────────────

def calc_fibonacci(high, low):
    diff = high - low
    return {
        "high":    round(high, 2),
        "low":     round(low, 2),
        "fib_236": round(high - diff * 0.236, 2),
        "fib_382": round(high - diff * 0.382, 2),
        "fib_500": round(high - diff * 0.500, 2),
        "fib_618": round(high - diff * 0.618, 2),
        "fib_786": round(high - diff * 0.786, 2),
    }

def get_tsla_price_alpaca():
    """جلب سعر TSLA من Alpaca (quote)."""
    try:
        r = _retry_session.get(
            f"{ALPACA_BASE_URL}/v2/stocks/TSLA/quotes/latest",
            headers=alpaca_headers(),
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            price = float(data.get("quote", {}).get("ap", 0) or data.get("quote", {}).get("bp", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.error(f"Alpaca price error: {e}")
    return 0.0

def get_tsla_price_alpaca_snapshot():
    """المصدر الرئيسي للسعر — Alpaca Snapshot (أسرع وأكثر موثوقية)."""
    try:
        r = _retry_session.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/snapshot",
            headers=alpaca_headers(),
            timeout=8
        )
        if r.status_code == 200:
            snap = r.json()
            # أولاً: آخر صفقة
            price = float(snap.get("latestTrade", {}).get("p", 0))
            if price > 0:
                return price
            # ثانياً: آخر سعر طلب
            price = float(snap.get("latestQuote", {}).get("ap", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.error(f"Alpaca snapshot error: {e}")
    # Fallback: quote endpoint
    return get_tsla_price_alpaca()

def get_tsla_day_data_alpaca():
    """جلب بيانات اليوم (High/Low/Price) من Alpaca Snapshot — بديل yfinance."""
    try:
        r = _retry_session.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/snapshot",
            headers=alpaca_headers(),
            timeout=8
        )
        if r.status_code == 200:
            snap = r.json()
            daily = snap.get("dailyBar", {})
            latest = snap.get("latestTrade", {})
            last_price = float(latest.get("p", 0))
            day_high   = float(daily.get("h", 0))
            day_low    = float(daily.get("l", 0))
            if last_price > 0:
                logger.info(f"[Alpaca] Price: ${last_price} | H: ${day_high} | L: ${day_low}")
                return last_price, day_high, day_low
    except Exception as e:
        logger.error(f"Alpaca day data error: {e}")
    return None, None, None

def get_tsla_5min_bars_alpaca():
    """جلب شمعات 5 دقائق من Alpaca — بديل yfinance للـ S&R."""
    try:
        from datetime import datetime, timedelta
        end = datetime.utcnow()
        start = end - timedelta(hours=8)
        params = {
            "timeframe": "5Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10,
            "feed": "iex"
        }
        r = http_requests.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/bars",
            headers=alpaca_headers(),
            params=params,
            timeout=10
        )
        if r.status_code == 200:
            bars = r.json().get("bars", [])
            if len(bars) >= 4:
                return bars  # قائمة من dicts: {o, h, l, c, v, t}
    except Exception as e:
        logger.error(f"Alpaca 5min bars error: {e}")
    return []

def get_tsla_day_data():
    """جلب بيانات اليوم — Alpaca أولاً (موثوق)، yfinance كاحتياطي."""
    # المصدر الأساسي: Alpaca
    price, high, low = get_tsla_day_data_alpaca()
    if price and price > 0:
        return price, high or 0.0, low or 0.0
    # Fallback: yfinance
    try:
        tkr = yf.Ticker("TSLA")
        info = tkr.fast_info
        day_high   = float(info.day_high)   if info.day_high   else 0.0
        day_low    = float(info.day_low)    if info.day_low    else 0.0
        last_price = float(info.last_price) if info.last_price else 0.0
        if last_price > 0:
            return last_price, day_high, day_low
    except Exception as e:
        logger.error(f"yfinance day data error: {e}")
    return None, None, None

# ──────────────────────────────────────────────────────────────────────────────
# Support & Resistance — 5-Minute Candles (Last 3 Candles)
# ──────────────────────────────────────────────────────────────────────────────

# Cache: store last computed S/R levels and the candle timestamp they were computed on
_sr_cache = {
    "resistance":    None,   # Highest high of last 3 closed 5-min candles
    "support":       None,   # Lowest low of last 3 closed 5-min candles
    "last_candle_ts": None,  # Timestamp of the last candle used for calculation
}

def get_tsla_sr_levels():
    """
    Calculate Support & Resistance from the last 3 CLOSED 5-minute candles.
    Uses Alpaca as primary source, yfinance as fallback.
    """
    global _sr_cache
    # المصدر الأساسي: Alpaca 5-min bars
    try:
        bars = get_tsla_5min_bars_alpaca()
        if bars and len(bars) >= 4:
            # آخر 3 شمعات مغلقة (نتجاهل الأخيرة لأنها قد تكون مفتوحة)
            closed = bars[-4:-1]
            latest_ts = closed[-1].get("t", "")
            if _sr_cache["last_candle_ts"] is not None and latest_ts == _sr_cache["last_candle_ts"]:
                return _sr_cache["resistance"], _sr_cache["support"]
            resistance = round(max(b["h"] for b in closed), 2)
            support    = round(min(b["l"] for b in closed), 2)
            _sr_cache["resistance"]     = resistance
            _sr_cache["support"]        = support
            _sr_cache["last_candle_ts"] = latest_ts
            logger.info(f"[S&R][Alpaca] R: ${resistance} | S: ${support}")
            return resistance, support
    except Exception as e:
        logger.error(f"[S&R] Alpaca bars error: {e}")
    # Fallback: yfinance
    try:
        tkr = yf.Ticker("TSLA")
        df = tkr.history(period="1d", interval="5m")
        if df is not None and len(df) >= 4:
            closed_candles = df.iloc[-4:-1]
            latest_candle_ts = closed_candles.index[-1]
            if _sr_cache["last_candle_ts"] is not None and latest_candle_ts == _sr_cache["last_candle_ts"]:
                return _sr_cache["resistance"], _sr_cache["support"]
            resistance = round(float(closed_candles["High"].max()), 2)
            support    = round(float(closed_candles["Low"].min()),  2)
            _sr_cache["resistance"]     = resistance
            _sr_cache["support"]        = support
            _sr_cache["last_candle_ts"] = latest_candle_ts
            logger.info(f"[S&R][yfinance] R: ${resistance} | S: ${support}")
            return resistance, support
    except Exception as e:
        logger.error(f"[S&R] yfinance error: {e}")
    return _sr_cache["resistance"], _sr_cache["support"]

# ──────────────────────────────────────────────────────────────────────────────
# Options Data (Yahoo Finance)
# ──────────────────────────────────────────────────────────────────────────────

def get_best_option(ticker_symbol, signal_type, current_price):
    try:
        current_price = float(current_price)
        try:
            tkr = yf.Ticker(ticker_symbol)
            exp_dates = tkr.options
        except Exception as e:
            logger.error(f"yfinance options error: {e}")
            return None, None
        if not exp_dates:
            return None, None

        today_str = get_today()
        valid_dates = [d for d in exp_dates if d >= today_str]
        if not valid_dates:
            return None, None

        primary_expiry = valid_dates[0]
        alt_expiry     = valid_dates[1] if len(valid_dates) > 1 else None

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

        best_row   = valid_strikes.loc[valid_strikes['strike_diff'].idxmin()]
        last_price = float(best_row.get('lastPrice', 0))
        ask        = float(best_row.get('ask', 0))
        bid        = float(best_row.get('bid', 0))

        if last_price <= 0 and ask > 0:
            last_price = ask

        tp = last_price * 1.40
        sl = last_price * 0.50

        is_0dte = (expiry == get_today())

        return {
            "strike":             float(best_row['strike']),
            "expiry":             expiry,
            "symbol":             best_row['contractSymbol'],
            "last_price":         last_price,
            "ask":                ask,
            "bid":                bid,
            "volume":             int(best_row.get('volume', 0)),
            "open_interest":      int(best_row.get('openInterest', 0)),
            "implied_volatility": float(best_row.get('impliedVolatility', 0)),
            "tp":                 tp,
            "sl":                 sl,
            "is_0dte":            is_0dte
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

def format_v5_6_trade_alert(data, primary_opt=None, alt_opt=None, alpaca_msg=None):
    """Format V5.6 trade signal with Stars + Alpaca execution status."""
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
        decision      = "إشارة قوية"
        decision_icon = "🔥"
    elif stars_int == 3:
        decision      = "إشارة جيدة"
        decision_icon = "🟢"
    else:
        decision      = "إشارة مبدئية / خطرة"
        decision_icon = "⚠️"

    sig_icon  = "🟢" if signal == "CALL" else "🔴"
    direction = "CALL شراء" if signal == "CALL" else "PUT بيع"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    # Determine session label
    hour = now_et.hour
    if hour < 10:
        session_label = "Opening Power"
    elif hour < 12:
        session_label = "Morning Momentum"
    elif hour < 14:
        session_label = "Midday"
    else:
        session_label = "Afternoon"

    # 1m/5m/15m alignment
    align_1m  = "✅"
    align_5m  = "✅ Bull" if "Bull" in bias_5m  else ("✅ Bear" if "Bear" in bias_5m  else "➖ Neutral")
    align_15m = "✅ Bull" if "Bull" in bias_15m else ("✅ Bear" if "Bear" in bias_15m else "➖ Neutral")

    # Vol label
    if "Ultra" in vol or "Surge" in vol:
        vol_label = "قوي 💪"
    elif "Normal" in vol:
        vol_label = "طبيعي"
    elif "Weak" in vol or "Low" in vol:
        vol_label = "Weak"
    else:
        vol_label = vol

    msg = (
        f"{decision_icon} <b>{decision} — Mosquito Swamp V5.6</b>\n"
        f"{sig_icon} <b>{direction}</b> | TSLA @ <code>${price}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌟 <b>التقييم:</b> {stars_display} ({stars_int}/5)\n"
        f"📈 <b>الاتجاه:</b> {bias}\n"
        f"📝 <b>ملاحظات:</b> {cond} | Vol: {vol_label}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟣 <b>توافق الفريمات:</b>\n"
        f"   1m: {align_1m} | 5m: {align_5m} | 15m: {align_15m}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if primary_opt and primary_opt.get("last_price", 0) > 0:
        p_strike = primary_opt["strike"]
        p_price  = primary_opt["last_price"]
        p_tp     = primary_opt["tp"]
        p_sl     = primary_opt["sl"]
        p_exp    = format_expiry_ar(primary_opt["expiry"])
        is_0dte  = primary_opt.get("is_0dte", False)
        dte_label = "0DTE" if is_0dte else p_exp

        msg += (
            f"🎯 <b>الأساسي:</b> {signal} ${p_strike:.0f} {dte_label} | "
            f"<code>${p_price:.2f}</code> → TP <code>${p_tp:.2f}</code> | SL <code>${p_sl:.2f}</code>\n\n"
        )

        if alt_opt and alt_opt.get("last_price", 0) > 0:
            a_strike = alt_opt["strike"]
            a_price  = alt_opt["last_price"]
            a_tp     = alt_opt["tp"]
            a_sl     = alt_opt["sl"]
            a_exp    = format_expiry_ar(alt_opt["expiry"])
            msg += (
                f"🔄 <b>البديل:</b> {signal} ${a_strike:.0f} ({a_exp}) | "
                f"<code>${a_price:.2f}</code> → TP <code>${a_tp:.2f}</code> | SL <code>${a_sl:.2f}</code>\n"
                f"✅ أكثر أماناً — وقت أطول\n\n"
            )
    else:
        msg += f"⚠️ <i>بيانات الـ Options غير متاحة حالياً</i>\n\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {timestamp} ET | {session_label}\n"
        f"⏱ <b>الوقف الزمني:</b> 10 دقائق — اطلع إذا ما تحرك السعر\n"
    )

    # Alpaca execution status
    if alpaca_msg:
        msg += f"\n🤖 <b>Alpaca:</b> {alpaca_msg}"

    return msg

def format_opening_map(price, d_high, d_low, fib, trend, liquidity):
    now_et = get_et_now()
    date_str = f"{now_et.day} {MONTHS_AR[now_et.month - 1]} {now_et.year}"
    timestamp = now_et.strftime("%I:%M %p")

    msg = (
        f"📊 <b>خريطة TSLA — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>السعر الحالي:</b> <code>${price:.2f}</code>\n"
        f"📈 <b>الاتجاه:</b> {trend}\n"
        f"💧 <b>السيولة:</b> {liquidity}\n\n"
        f"📐 <b>مستويات Fibonacci اليوم:</b>\n"
        f"   🔴 أعلى: <code>${fib['high']:.2f}</code>\n"
        f"   ├─ 23.6%: <code>${fib['fib_236']:.2f}</code>\n"
        f"   ├─ 38.2%: <code>${fib['fib_382']:.2f}</code>\n"
        f"   ├─ 50.0%: <code>${fib['fib_500']:.2f}</code>  ← <b>المحور</b>\n"
        f"   ├─ 61.8%: <code>${fib['fib_618']:.2f}</code>  ← <b>ذهبي</b>\n"
        f"   ├─ 78.6%: <code>${fib['fib_786']:.2f}</code>\n"
        f"   🟢 أدنى: <code>${fib['low']:.2f}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <b>نافذة التداول:</b> 10:00 AM – 3:30 PM ET\n"
        f"🤖 <b>Alpaca:</b> جاهز للتنفيذ التلقائي ✅\n"
        f"🕐 {timestamp} ET"
    )
    return msg

def format_reversal_alert(data, fib=None):
    price    = safe_get(data, "price", "?")
    div_type = safe_get(data, "div_type", "?")
    tf       = safe_get(data, "timeframe", "5m")
    vol_ok   = safe_get(data, "vol_confirm", "false").lower() == "true"

    now_et    = get_et_now()
    timestamp = now_et.strftime("%I:%M %p")

    if div_type == "BULL":
        rev_icon = "📈"
        rev_text = f"صعود محتمل (Divergence إيجابي {tf})"
    else:
        rev_icon = "📉"
        rev_text = f"هبوط محتمل (Divergence سلبي {tf})"

    vol_text = "✅ مؤكد بالحجم" if vol_ok else "⚠️ ضعيف — تأكد قبل الدخول"

    msg = (
        f"⚠️ <b>قرب انعكاس</b> — TSLA @ <code>${price}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{rev_icon} <b>النوع:</b> {rev_text}\n"
        f"📦 <b>الحجم:</b> {vol_text}\n"
    )

    if fib:
        if div_type == "BULL":
            nearest_key = "fib_618"
            nearest_label = "أقرب مستوى مقاومة"
            levels = [
                ("61.8%", fib["fib_618"]),
                ("50.0%", fib["fib_500"]),
                ("38.2%", fib["fib_382"]),
            ]
        else:
            nearest_key = "fib_382"
            nearest_label = "أقرب مستوى دعم"
            levels = [
                ("38.2%", fib["fib_382"]),
                ("50.0%", fib["fib_500"]),
                ("61.8%", fib["fib_618"]),
            ]

        msg += f"\n📐 <b>مستويات Fibonacci (دعم/مقاومة):</b>\n"
        for label, val in levels:
            msg += f"   {label} → <code>${val:.2f}</code>\n"
        msg += f"\n   🎯 <b>{nearest_label}:</b> <code>${fib[nearest_key]:.2f}</code>\n"

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

def check_adx_filter(data):
    """V6.0: فلتر ADX إجباري — لا تداول في سوق جانبي."""
    cond = safe_get(data, "cond", "")
    # TradingView يرسل cond = "Trending (Clear)" أو "Choppy" أو "Ranging"
    if "Choppy" in cond or "Ranging" in cond or "choppy" in cond:
        return False, f"⛔ ADX منخفض (سوق جانبي) — لا دخول | cond={cond}"
    return True, ""

def check_gex_alignment(data):
    """V6.0: فلتر GEX — تحقق من توافق الإشارة مع مستويات Gamma."""
    try:
        if not _FA_AVAILABLE:
            return True, ""  # لا فلتر إذا FlashAlpha غير متاح
        signal = safe_get(data, "signal", "")
        price = float(safe_get(data, "price", "0"))
        if not signal or price <= 0:
            return True, ""
        aligned, reason = fa.check_gex_alignment(signal, price)
        if not aligned:
            return False, f"📊 GEX غير متوافق: {reason}"
        return True, reason
    except Exception:
        return True, ""  # في حالة خطأ، لا نمنع الصفقة

def apply_filters(data):
    # V6.0: فلاتر معززة — ADX + GEX
    for check in [check_data_quality, check_adx_filter, check_gex_alignment, check_cooldown, check_daily_limit]:
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
    in_window, window_msg = is_trading_window()
    account = get_alpaca_account()
    return jsonify({
        "status":          "running",
        "service":         "Smart Trading Alert Bot — Mosquito Swamp V6.0 (Discipline Edition)",
        "version":         "6.0",
        "trading_window":  window_msg,
        "in_window":       in_window,
        "alpaca_balance":  f"${float(account.get('cash', 0)):,.2f}" if account else "N/A",
        "alpaca_status":   account.get("status", "N/A") if account else "disconnected",
        "active_positions": len(active_positions),
        "alerts_today":    len(daily_alerts),
        "remaining":       MAX_DAILY_ALERTS - len(daily_alerts),
        "gex_available":   _FA_AVAILABLE and fa.get_gex_levels() is not None if _FA_AVAILABLE else False,
        "adx_filter":      "active",
        "timestamp":       datetime.now(timezone.utc).isoformat()
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

    # ── تحديث مستويات S/R في reversal_detector من TradingView Webhook ──────────
    try:
        wh_resistance = float(safe_get(data, "resistance", "0") or 0)
        wh_support    = float(safe_get(data, "support",    "0") or 0)
        if wh_resistance > 0 and wh_support > 0 and _RD_AVAILABLE:
            rd.update_levels_from_webhook(wh_resistance, wh_support)
            logger.info(f"[S&R] Passed to reversal_detector: R=${wh_resistance} | S=${wh_support}")
    except Exception as _e:
        logger.warning(f"[S&R] Could not update reversal_detector levels: {_e}")

    if price not in ("?", "--"):
        market_state["last_price"]   = price
        market_state["last_updated"] = datetime.now(timezone.utc).isoformat()

    # ── OPENING MAP ──────────────────────────────────────────────────────────
    if signal == "OPENING_MAP":
        try:
            live_price, d_high, d_low = get_tsla_day_data()
            if not live_price:
                live_price = float(safe_get(data, "price", "0") or 0)
                d_high = float(safe_get(data, "day_high", "0") or 0)
                d_low  = float(safe_get(data, "day_low",  "0") or 0)

            if d_high > 0: market_state["day_high"] = d_high
            if d_low  > 0: market_state["day_low"]  = d_low

            bias_str = safe_get(data, "bias", "")
            if "Bull" in bias_str or "bull" in bias_str:
                trend = "صاعد 📈"
            elif "Bear" in bias_str or "bear" in bias_str:
                trend = "هابط 📉"
            else:
                trend = "جانبي ↔️"

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
                now_et = get_et_now()
                tg_msg = (
                    f"📊 <b>خريطة TSLA — {now_et.strftime('%d/%m/%Y')}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"💰 <b>السعر:</b> <code>${price}</code>\n"
                    f"📈 <b>الاتجاه:</b> {trend}\n\n"
                    f"⏰ <b>نافذة التداول:</b> 10:00 AM – 3:30 PM ET\n"
                    f"🤖 <b>Alpaca:</b> جاهز ✅\n"
                    f"🕐 {now_et.strftime('%I:%M %p')} ET"
                )

            tg_ok = send_telegram(tg_msg)
            return jsonify({"status": "opening_map_sent", "telegram": "sent" if tg_ok else "failed"}), 200

        except Exception as e:
            logger.error(f"Opening map error: {e}")
            return jsonify({"status": "error", "error": str(e)}), 200

    # ── REVERSAL ALERT ───────────────────────────────────────────────────────
    if signal == "REVERSAL_ALERT":
        now = time.time()
        if now - last_reversal_time < REVERSAL_COOLDOWN_SECS:
            remaining = REVERSAL_COOLDOWN_SECS - (now - last_reversal_time)
            return jsonify({"status": "blocked", "reason": f"reversal cooldown — {remaining/60:.0f} min"}), 200

        try:
            ph = float(safe_get(data, "day_high", "0") or 0)
            pl = float(safe_get(data, "day_low",  "0") or 0)
            if ph > 0: market_state["day_high"] = ph
            if pl > 0: market_state["day_low"]  = pl
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

    # ── VOL TRAP (chart only) ─────────────────────────────────────────────────
    if signal == "VOL_INTEL":
        return jsonify({"status": "vol_intel_chart_only"}), 200

    # ── LOSS COUNTER ─────────────────────────────────────────────────────────
    if last_alert_price and price not in ("?", "--"):
        try:
            prev_p = float(last_alert_price)
            curr_p = float(price)
            move_pct = abs(curr_p - prev_p) / prev_p
            if move_pct < 0.003:
                consecutive_no_move += 1
                if consecutive_no_move >= LOSS_COUNTER_MAX:
                    loss_cooldown_until = time.time() + LOSS_COOLDOWN_SECONDS
                    consecutive_no_move = 0
                    send_telegram(
                        "🛑 <b>تفعيل وضع الراحة (صمت 30 دقيقة)</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        "السوق في مسار عرضي ضعيف.\n"
                        "النظام سيتوقف 30 دقيقة لحمايتك."
                    )
            else:
                consecutive_no_move = 0
        except:
            pass

    # ── TRADE SIGNAL (CALL / PUT) ────────────────────────────────────────────
    try:
        passed, rejection_reason = apply_filters(data)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 200

    if not passed:
        blocked_today.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal":    signal,
            "price":     price,
            "reason":    rejection_reason
        })
        return jsonify({"status": "blocked", "reason": rejection_reason}), 200

    # Fetch Option Data
    primary_opt, alt_opt = get_best_option("TSLA", signal, price)

    # ── ALPACA AUTO-EXECUTE ───────────────────────────────────────────────────
    alpaca_status_msg = None
    stars = safe_get(data, "stars", "1")
    in_window, window_msg = is_trading_window()

    if in_window:
        try:
            stars_int = int(stars)
        except:
            stars_int = 1

        if stars_int >= MIN_STARS_TO_EXECUTE:
            exec_ok, order_id, exec_msg = execute_trade(signal, price, stars, primary_opt)
            if exec_ok:
                alpaca_status_msg = "تم التنفيذ ✅"
                # Send separate Alpaca notification
                send_telegram(exec_msg)
                logger.info(f"Alpaca trade executed: {order_id}")
            else:
                alpaca_status_msg = f"فشل التنفيذ ❌ ({exec_msg})"
                logger.error(f"Alpaca execution failed: {exec_msg}")
        else:
            alpaca_status_msg = f"لم يُنفَّذ — النجوم أقل من {MIN_STARS_TO_EXECUTE} ({stars_int}⭐)"
    else:
        alpaca_status_msg = f"خارج نافذة التداول ({window_msg})"

    # Format and send Telegram alert
    tg_msg = format_v5_6_trade_alert(data, primary_opt, alt_opt, alpaca_status_msg)
    tg_ok  = send_telegram(tg_msg)

    # Update state
    global _current_signal_type
    now = time.time()
    last_alert_time   = now
    last_alert_price  = price
    last_alert_signal = signal
    _current_signal_type = signal  # تحديث نوع الأوبشن لـ Options Feed
    if signal == "CALL":
        last_call_time = now
    elif signal == "PUT":
        last_put_time = now

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal":    signal,
        "price":     price,
        "stars":     stars,
        "alpaca":    alpaca_status_msg
    }
    alert_history.insert(0, entry)
    if len(alert_history) > MAX_HISTORY:
        alert_history.pop()
    daily_alerts.append(entry)

    logger.info(f"SENT: {signal} @ ${price} | Stars: {stars} | Alpaca: {alpaca_status_msg}")

    return jsonify({
        "status":   "processed",
        "telegram": "sent" if tg_ok else "failed",
        "alpaca":   alpaca_status_msg
    }), 200


# ──────────────────────────────────────────────────────────────────────────────
## Test & Utility Endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/reversal_report", methods=["GET"])
def reversal_report():
    """تقرير أداء إشارات الانعكاس — نسبة الصدق والتاريخ."""
    if not _RT_AVAILABLE:
        return jsonify({"error": "Reversal Tracker not available"}), 503
    report_text = rt.generate_performance_report()
    # قراءة الملف مباشرة لإرجاع JSON مفصل
    import csv, os
    rows = []
    tracker_file = "/home/ubuntu/reversal_tracker_log.csv"
    if os.path.isfile(tracker_file):
        with open(tracker_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    return jsonify({
        "summary": report_text,
        "total_evaluated": len(rows),
        "signals": rows[-20:] if len(rows) > 20 else rows  # آخر 20 إشارة
    })

@app.route("/alpaca_status", methods=["GET"])
def alpaca_status():
    """Check Alpaca account status."""
    account = get_alpaca_account()
    in_window, window_msg = is_trading_window()
    if account:
        return jsonify({
            "connected":       True,
            "account_number":  account.get("account_number"),
            "cash":            f"${float(account.get('cash', 0)):,.2f}",
            "portfolio_value": f"${float(account.get('portfolio_value', 0)):,.2f}",
            "buying_power":    f"${float(account.get('buying_power', 0)):,.2f}",
            "status":          account.get("status"),
            "trading_window":  window_msg,
            "in_window":       in_window,
            "active_positions": len(active_positions)
        })
    return jsonify({"connected": False}), 500


@app.route("/positions", methods=["GET"])
def positions():
    """Get current tracked positions."""
    return jsonify({
        "active_positions": len(active_positions),
        "positions": list(active_positions.values())
    })


@app.route("/test_alpaca", methods=["GET"])
def test_alpaca_endpoint():
    """Test Alpaca connection and send Telegram notification."""
    account = get_alpaca_account()
    if account:
        in_window, window_msg = is_trading_window()
        msg = (
            f"🤖 <b>Alpaca Paper Trading — متصل ✅</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>الرصيد:</b> ${float(account.get('cash', 0)):,.2f}\n"
            f"📊 <b>Portfolio:</b> ${float(account.get('portfolio_value', 0)):,.2f}\n"
            f"⚡ <b>Buying Power:</b> ${float(account.get('buying_power', 0)):,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>نافذة التداول:</b> {window_msg}\n"
            f"🕐 {get_et_now().strftime('%I:%M %p')} ET"
        )
        send_telegram(msg)
        return jsonify({"status": "ok", "cash": account.get("cash"), "window": window_msg})
    return jsonify({"status": "error", "message": "Alpaca connection failed"}), 500


@app.route("/test_5stars", methods=["GET"])
def test_5stars():
    test_data = {
        "signal": "CALL", "type": "TRADE_V5_6", "price": "392.00",
        "stars": "5", "bias": "Bullish", "vol": "Surge",
        "cond": "Trending (Clear)", "session": "Morning Momentum",
        "bias_5m": "Bull", "bias_15m": "Bull"
    }
    primary_opt = {
        "strike": 392.0, "expiry": get_today(),
        "last_price": 2.50, "tp": 3.50, "sl": 1.25, "is_0dte": True
    }
    in_window, window_msg = is_trading_window()
    tg_ok = send_telegram(format_v5_6_trade_alert(test_data, primary_opt, None,
                          f"اختبار — {window_msg}"))
    return jsonify({"status": "test_sent", "window": window_msg, "telegram": "sent" if tg_ok else "failed"})


@app.route("/test_opening_map", methods=["GET"])
def test_opening_map():
    test_price = 392.00
    test_high  = 406.80
    test_low   = 388.33
    fib = calc_fibonacci(test_high, test_low)
    tg_msg = format_opening_map(test_price, test_high, test_low, fib, "هابط 📉", "منخفضة ⚠️")
    tg_ok = send_telegram(tg_msg)
    return jsonify({"status": "test_sent", "fib": fib, "telegram": "sent" if tg_ok else "failed"})


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
    return jsonify({"status": "reset", "message": "All counters cleared — V6.0"})


# ──────────────────────────────────────────────────────────────────────────────
# Position Status Route (for reversal_detector integration)
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/position_status", methods=["POST"])
def position_status():
    """
    تحديث حالة الصفقة المفتوحة لوحدة reversal_detector.
    Body JSON: {"open": true/false}
    """
    if not _RD_AVAILABLE:
        return jsonify({"status": "error", "message": "reversal_detector not available"}), 503

    data = request.get_json(force=True, silent=True) or {}
    is_open = bool(data.get("open", False))
    rd.set_position_open(is_open)

    logger.info(f"[position_status] position_open set to: {is_open}")
    return jsonify({
        "status":        "ok",
        "position_open": is_open,
        "message":       f"Position {'opened' if is_open else 'closed'} — reversal detector {'active' if is_open else 'paused'}"
    })


# ──────────────────────────────────────────────────────────────────────────────
# Background Workers
# ──────────────────────────────────────────────────────────────────────────────

def keep_alive_worker():
    """Self-ping to prevent Render free tier from sleeping."""
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tsla-scalper-bot.onrender.com")
            http_requests.get(f"{render_url}/", timeout=15)
            logger.debug(f"Keep-alive ping sent to {render_url}")
        except Exception as e:
            logger.debug(f"Keep-alive ping failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# V6.0: GEX Morning Worker
# ──────────────────────────────────────────────────────────────────────────────
def gex_morning_worker():
    """يسحب بيانات GEX مرة واحدة صباحاً (9:15 AM ET) ويرسل الخريطة على Telegram."""
    while True:
        try:
            now = get_et_now()
            # انتظر حتى 9:15 AM ET في أيام العمل
            if now.hour == 9 and 15 <= now.minute <= 20 and now.weekday() < 5:
                if _FA_AVAILABLE:
                    gex_data = fa.fetch_gex()
                    if gex_data:
                        msg = fa.format_gex_telegram()
                        if msg:
                            send_telegram(msg)
                            logger.info("[GEX] Morning map sent to Telegram")
                # انتظر ساعة لتجنب التكرار
                time.sleep(3600)
            else:
                time.sleep(60)
        except Exception as e:
            logger.error(f"[GEX] Morning worker error: {e}")
            time.sleep(300)


# ──────────────────────────────────────────────────────────────────────────────
# Startup: launch background threads (works with both gunicorn and direct run)
# ──────────────────────────────────────────────────────────────────────────────
_threads_started = False

def _start_background_threads():
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    threading.Thread(target=keep_alive_worker, daemon=True).start()
    threading.Thread(target=monitor_positions, daemon=True).start()
    logger.info("Background threads started (keep_alive + monitor_positions) ✅")
    # Start Reversal Detector (if available)
    if _RD_AVAILABLE:
        rd.start_reversal_detector()
        logger.info("Reversal Detector started as background thread ✅")
    else:
        logger.warning("Reversal Detector not available — skipping")
    # Start Reversal Tracker (if available)
    if _RT_AVAILABLE:
        rt.start_reversal_tracker()
        logger.info("Reversal Tracker started — تتبع وتقييم إشارات الانعكاس ✅")
    else:
        logger.warning("Reversal Tracker not available — skipping")
    # Start Alpaca Options Feed (if available)
    if _AO_AVAILABLE:
        ao.start_options_feed(
            signal_type_fn   = _get_signal_type,
            current_price_fn = _get_tsla_price_for_options,
            update_callback  = _on_option_data_received
        )
        logger.info("Alpaca Options Feed started ✅")
    else:
        logger.warning("Alpaca Options Feed not available — skipping")
    # Start GEX Morning Worker (V6.0)
    if _FA_AVAILABLE:
        threading.Thread(target=gex_morning_worker, daemon=True).start()
        logger.info("FlashAlpha GEX morning worker started ✅")
    else:
        logger.warning("FlashAlpha GEX not available — skipping")

# Auto-start threads when module loads (works with gunicorn)
_start_background_threads()

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
