"""
reversal_detector.py — Reversal Detection Module
=================================================
Standalone background module for TSLA Reversal Detection.
Runs as a background thread alongside the Flask server in app.py.

Specifications (agreed):
  - Adaptive Delta: % of 5-min rolling average (not fixed threshold)
  - Divergence condition: 90 seconds sustained + price consolidation < $0.20
  - S&R levels: TradingView Webhook first, then Previous Day High/Low + VWAP as backup
  - Check interval: every 3 seconds
  - Cooldown between alerts: 120 seconds
  - Trading window: 10:00 AM – 1:00 PM ET only
  - Alert only when position_open = True
  - Log every alert to reversal_log.csv
  - Two alert types: Early Warning + Actual Reversal
"""

import os
import csv
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from collections import deque

try:
    import requests as http_requests
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "requests"])
    import requests as http_requests

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "yfinance"])
    import yfinance as yf

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")

CHECK_INTERVAL_SECS    = 3       # فحص كل 3 ثوانٍ
COOLDOWN_SECS          = 120     # 120 ثانية بين كل تنبيه وآخر
DIVERGENCE_SECS        = 90      # 90 ثانية مستمرة للانعكاس الفعلي
CONSOLIDATION_RANGE    = 0.20    # نطاق السعر الضيق ($0.20) لتأكيد التجميع
PROXIMITY_ALERT_RANGE  = 0.30    # مسافة $0.30 من المستوى لتفعيل التنبيه المبكر
DELTA_WINDOW_SECS      = 60      # نافذة حساب الـ Delta (60 ثانية)
DELTA_AVG_WINDOW_SECS  = 300     # نافذة حساب متوسط الـ Delta (5 دقائق)
DELTA_THRESHOLD_PCT    = 0.40    # الـ Delta يجب أن يكون أقل من 40% من متوسط الـ 5 دقائق

# نافذة التداول (ET)
TRADING_START_HOUR   = 10
TRADING_START_MINUTE = 0
TRADING_END_HOUR     = 15
TRADING_END_MINUTE   = 30

LOG_FILE = "reversal_log.csv"

logger = logging.getLogger("reversal_detector")

# ──────────────────────────────────────────────────────────────────────────────
# Shared State (thread-safe)
# ──────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# حالة الصفقة المفتوحة — يتم تحديثها من /position_status route في app.py
position_open = False

# مستويات الدعم والمقاومة — يتم تحديثها من TradingView Webhook أو Backup
_levels = {
    "resistance": None,   # من TradingView أو Backup
    "support":    None,
    "source":     None,   # "tradingview" أو "backup"
    "updated_at": None,
}

# بيانات الـ Tape (تدفق الأوامر) — تُضاف من Cheddar Flow أو محاكاة
# كل عنصر: {"ts": timestamp, "side": "ask"/"bid", "volume": float}
_tape_buffer: deque = deque(maxlen=5000)

# بيانات السعر اللحظي
_price_buffer: deque = deque(maxlen=500)  # {"ts": timestamp, "price": float}

# بيانات الأوبشن اللحظية
_option_data = {
    "premium":       0.0,
    "delta":         0.0,
    "peak_premium":  0.0,
    "last_updated":  0.0,
    "option_symbol": None,
    "strike_price":  None,
    "expiration":    None,
    "option_type":   None,
}

# حالة الانعكاس
_divergence_start_ts = None   # وقت بدء الانحراف المستمر
_divergence_price_range = []  # أسعار خلال فترة الانحراف (لحساب الـ consolidation)

# آخر وقت تنبيه
_last_alert_ts = 0.0
_last_warning_ts = 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Helper: Telegram
# ──────────────────────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> bool:
    """إرسال رسالة تليجرام مباشرة عبر Requests."""
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = http_requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[RD] Telegram send failed: {e}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Helper: Time
# ──────────────────────────────────────────────────────────────────────────────

def _get_et_now() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=4)

def _is_trading_window() -> bool:
    """هل نحن داخل نافذة التداول 10:00 AM – 1:00 PM ET؟"""
    now = _get_et_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (TRADING_START_HOUR * 60 + TRADING_START_MINUTE) <= mins < (TRADING_END_HOUR * 60 + TRADING_END_MINUTE)

# ──────────────────────────────────────────────────────────────────────────────
# Helper: CSV Logging
# ──────────────────────────────────────────────────────────────────────────────

def _check_premium_collapse() -> bool:
    """التحقق من انهيار البريميوم (Premium Collapse)."""
    with _lock:
        peak_premium = _option_data["peak_premium"]
        current_premium = _option_data["premium"]
        if peak_premium > 0 and current_premium > 0:
            drop_percentage = (peak_premium - current_premium) / peak_premium
            if drop_percentage >= 0.70: # 70% drop
                logger.warning(f"[RD] Premium Collapse detected! Current: {current_premium:.2f}, Peak: {peak_premium:.2f}, Drop: {drop_percentage:.2%}")
                return True
    return False

def _check_delta_flip() -> bool:
    """التحقق من انقلاب الدلتا (Delta Flip) لتأكيد الانعكاس."""
    # Placeholder - a proper implementation requires historical delta data
    return False

def _log_to_csv(alert_type: str, price: float, delta: float, imbalance: float,
                divergence: bool, reason: str):
    """حفظ كل تنبيه في reversal_log.csv."""
    try:
        file_exists = os.path.isfile(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp_et", "alert_type", "price",
                                 "delta", "imbalance_ratio", "divergence", "reason"])
            writer.writerow([
                _get_et_now().strftime("%Y-%m-%d %H:%M:%S"),
                alert_type,
                f"{price:.2f}",
                f"{delta:.1f}",
                f"{imbalance:.3f}",
                str(divergence),
                reason
            ])
    except Exception as e:
        logger.error(f"[RD] CSV log error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Public: Update position status (called from /position_status Flask route)
# ──────────────────────────────────────────────────────────────────────────────

def set_position_open(is_open: bool):
    """تحديث حالة الصفقة المفتوحة من Flask route."""
    global position_open
    with _lock:
        position_open = is_open
    logger.info(f"[RD] position_open set to: {is_open}")

def get_position_open() -> bool:
    with _lock:
        return position_open

# ──────────────────────────────────────────────────────────────────────────────
# Public: Update S&R levels (called from TradingView Webhook in app.py)
# ──────────────────────────────────────────────────────────────────────────────

def update_levels_from_webhook(resistance: float, support: float):
    """تحديث مستويات الدعم والمقاومة من TradingView Webhook."""
    with _lock:
        _levels["resistance"] = resistance
        _levels["support"]    = support
        _levels["source"]     = "tradingview"
        _levels["updated_at"] = time.time()
    logger.info(f"[RD] Levels updated from TradingView — R: ${resistance} | S: ${support}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Public: Add tape data (called when Cheddar Flow data is received)
    # ──────────────────────────────────────────────────────────────────────────────

def add_tape_event(side: str, volume: float):
    """
    إضافة حدث تداول للـ Tape Buffer.
    side: "ask" (شراء عدواني) أو "bid" (بيع عدواني)
    volume: حجم الأمر
    """
    with _lock:
        _tape_buffer.append({
            "ts":     time.time(),
            "side":   side.lower(),
            "volume": abs(volume)
        })

def add_price_event(price: float):
    """إضافة سعر لحظي للـ Price Buffer."""
    with _lock:
        _price_buffer.append({
            "ts":    time.time(),
            "price": price
        })

    # ──────────────────────────────────────────────────────────────────────────────
    # Public: Get Option Data
    # ──────────────────────────────────────────────────────────────────────────────

    def get_option_data() -> dict:
        with _lock:
            return _option_data.copy()

    # ──────────────────────────────────────────────────────────────────────────────
    # Core: Get current TSLA price
    # ──────────────────────────────────────────────────────────────────────────────

def _check_premium_collapse() -> bool:
    """التحقق من انهيار البريميوم (Premium Collapse)."""
    with _lock:
        peak_premium = _option_data["peak_premium"]
        current_premium = _option_data["premium"]
        if peak_premium > 0 and current_premium > 0:
            drop_percentage = (peak_premium - current_premium) / peak_premium
            if drop_percentage >= 0.70: # 70% drop
                logger.warning(f"[RD] Premium Collapse detected! Current: {current_premium:.2f}, Peak: {peak_premium:.2f}, Drop: {drop_percentage:.2%}")
                return True
    return False

def _check_delta_flip() -> bool:
    """التحقق من انقلاب الدلتا (Delta Flip) لتأكيد الانعكاس."""
    # Placeholder - a proper implementation requires historical delta data
    return False

def _check_premium_collapse() -> bool:
    """التحقق من انهيار البريميوم (Premium Collapse)."""
    with _lock:
        peak_premium = _option_data["peak_premium"]
        current_premium = _option_data["premium"]
        if peak_premium > 0 and current_premium > 0:
            drop_percentage = (peak_premium - current_premium) / peak_premium
            if drop_percentage >= 0.70: # 70% drop
                logger.warning(f"[RD] Premium Collapse detected! Current: {current_premium:.2f}, Peak: {peak_premium:.2f}, Drop: {drop_percentage:.2%}")
                return True
    return False

def _check_delta_flip() -> bool:
    """التحقق من انقلاب الدلتا (Delta Flip) لتأكيد الانعكاس."""
    # Placeholder - a proper implementation requires historical delta data
    return False

def _alpaca_snapshot_price() -> float:
    """جلب سعر TSLA من Alpaca Snapshot — موثوق وسريع."""
    try:
        alpaca_key    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
        alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
        r = http_requests.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/snapshot",
            headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            timeout=6
        )
        if r.status_code == 200:
            snap = r.json()
            price = float(snap.get("latestTrade", {}).get("p", 0))
            if price > 0:
                return price
            price = float(snap.get("latestQuote", {}).get("ap", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.error(f"[RD] Alpaca snapshot error: {e}")
    return 0.0

def _get_current_price() -> float:
    """جلب السعر اللحظي لـ TSLA — Alpaca أولاً، yfinance احتياطي."""
    # أولاً: من الـ Price Buffer (إذا كانت البيانات حديثة)
    with _lock:
        if _price_buffer:
            latest = _price_buffer[-1]
            if time.time() - latest["ts"] < 10:
                return latest["price"]
    # ثانياً: Alpaca Snapshot
    price = _alpaca_snapshot_price()
    if price > 0:
        add_price_event(price)
        return price
    # ثالثاً: yfinance كاحتياطي
    try:
        tkr = yf.Ticker("TSLA")
        price = float(tkr.fast_info.last_price)
        if price > 0:
            add_price_event(price)
            return price
    except Exception as e:
        logger.error(f"[RD] yfinance price error: {e}")
    return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Core: Get S&R Levels (with Backup)
# ──────────────────────────────────────────────────────────────────────────────

def _get_levels() -> tuple:
    """
    جلب مستويات الدعم والمقاومة.
    الأولوية: TradingView Webhook → Previous Day High/Low + VWAP (Backup)
    Returns: (resistance, support)
    """
    with _lock:
        if (_levels["resistance"] is not None and
                _levels["support"] is not None and
                _levels["source"] == "tradingview"):
            return _levels["resistance"], _levels["support"]

    # Backup: Alpaca Snapshot للحصول على High/Low اليومي
    try:
        alpaca_key    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
        alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
        r = http_requests.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/snapshot",
            headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            timeout=8
        )
        if r.status_code == 200:
            snap = r.json()
            daily = snap.get("dailyBar", {})
            prev  = snap.get("prevDailyBar", {})
            # استخدام High/Low اليوم السابق كمستويات
            resistance_backup = round(float(prev.get("h", daily.get("h", 0))), 2)
            support_backup    = round(float(prev.get("l", daily.get("l", 0))), 2)
            # VWAP من شمعات 5 دقائق عبر Alpaca Bars
            from datetime import datetime as _dt, timedelta as _td
            end = _dt.utcnow()
            start = end - _td(hours=8)
            params = {
                "timeframe": "5Min",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10, "feed": "iex"
            }
            r2 = http_requests.get(
                "https://data.alpaca.markets/v2/stocks/TSLA/bars",
                headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
                params=params, timeout=10
            )
            vwap = None
            if r2.status_code == 200:
                bars = r2.json().get("bars", [])
                if len(bars) >= 4:
                    closed = bars[-4:-1]
                    total_vol = sum(b["v"] for b in closed)
                    if total_vol > 0:
                        vwap = round(sum(b["vw"] * b["v"] for b in closed) / total_vol, 2)
            if vwap and support_backup and vwap > support_backup:
                support_backup = vwap
            with _lock:
                if resistance_backup and support_backup:
                    _levels["resistance"] = resistance_backup
                    _levels["support"]    = support_backup
                    _levels["source"]     = "backup"
                    _levels["updated_at"] = time.time()
                    logger.info(f"[RD] Backup levels (Alpaca) — R: ${resistance_backup} | S: ${support_backup} | VWAP: ${vwap}")
            return resistance_backup, support_backup
    except Exception as e:
        logger.error(f"[RD] Alpaca backup levels error: {e}")
    # Fallback: yfinance
    try:
        tkr = yf.Ticker("TSLA")
        hist = tkr.history(period="5d", interval="1d")
        if hist is not None and len(hist) >= 2:
            prev_day = hist.iloc[-2]
            resistance_backup = round(float(prev_day["High"]), 2)
            support_backup    = round(float(prev_day["Low"]),  2)
            with _lock:
                _levels["resistance"] = resistance_backup
                _levels["support"]    = support_backup
                _levels["source"]     = "backup"
                _levels["updated_at"] = time.time()
            return resistance_backup, support_backup
    except Exception as e:
        logger.error(f"[RD] yfinance backup levels error: {e}")
    with _lock:
        return _levels["resistance"], _levels["support"]

# ──────────────────────────────────────────────────────────────────────────────
# Core: Calculate Delta & Imbalance (Adaptive)
# ──────────────────────────────────────────────────────────────────────────────

def _calc_delta_and_imbalance() -> tuple:
    """
    حساب:
    - Delta (60 ثانية): حجم Ask - حجم Bid
    - Imbalance Ratio: حجم Bid ÷ حجم Ask
    - Adaptive threshold: هل الـ Delta أقل من 40% من متوسط الـ 5 دقائق؟

    Returns: (delta_60s, imbalance_ratio, ask_vol_60s, bid_vol_60s,
              delta_avg_5min, is_delta_weak)
    """
    now = time.time()

    with _lock:
        tape_copy = list(_tape_buffer)

    # فلترة آخر 60 ثانية
    recent_60s = [e for e in tape_copy if now - e["ts"] <= DELTA_WINDOW_SECS]
    ask_vol_60s = sum(e["volume"] for e in recent_60s if e["side"] == "ask")
    bid_vol_60s = sum(e["volume"] for e in recent_60s if e["side"] == "bid")
    delta_60s   = ask_vol_60s - bid_vol_60s

    # متوسط الـ Delta لآخر 5 دقائق (نوافذ 60 ثانية متداخلة)
    delta_5min_values = []
    for offset in range(0, DELTA_AVG_WINDOW_SECS, DELTA_WINDOW_SECS):
        window_events = [
            e for e in tape_copy
            if (now - DELTA_AVG_WINDOW_SECS + offset) <= e["ts"] < (now - DELTA_AVG_WINDOW_SECS + offset + DELTA_WINDOW_SECS)
        ]
        if window_events:
            w_ask = sum(e["volume"] for e in window_events if e["side"] == "ask")
            w_bid = sum(e["volume"] for e in window_events if e["side"] == "bid")
            delta_5min_values.append(abs(w_ask - w_bid))

    delta_avg_5min = (sum(delta_5min_values) / len(delta_5min_values)) if delta_5min_values else 0.0

    # Imbalance Ratio
    imbalance_ratio = (bid_vol_60s / ask_vol_60s) if ask_vol_60s > 0 else 0.0

    # هل الـ Delta ضعيف؟ (أقل من 40% من المتوسط)
    is_delta_weak = (
        delta_avg_5min > 0 and
        abs(delta_60s) < (delta_avg_5min * DELTA_THRESHOLD_PCT)
    ) if delta_avg_5min > 0 else False

    return delta_60s, imbalance_ratio, ask_vol_60s, bid_vol_60s, delta_avg_5min, is_delta_weak

# ──────────────────────────────────────────────────────────────────────────────
# Core: Check Divergence (Price vs Delta)
# ──────────────────────────────────────────────────────────────────────────────

def _check_divergence(delta_60s: float) -> bool:
    """
    هل يوجد انحراف بين حركة السعر وتدفق السيولة؟
    - السعر يرتفع لكن الـ Delta سالب (بيع عدواني يسيطر) = انحراف هبوطي
    - السعر ينخفض لكن الـ Delta موجب (شراء عدواني يسيطر) = انحراف صعودي
    """
    now = time.time()
    with _lock:
        price_copy = list(_price_buffer)

    recent_prices = [e for e in price_copy if now - e["ts"] <= DELTA_WINDOW_SECS]
    if len(recent_prices) < 3:
        return False

    price_start = recent_prices[0]["price"]
    price_end   = recent_prices[-1]["price"]
    price_move  = price_end - price_start

    # انحراف هبوطي: السعر صاعد لكن الـ Delta سالب
    bearish_div = price_move > 0.10 and delta_60s < -50
    # انحراف صعودي: السعر هابط لكن الـ Delta موجب
    bullish_div = price_move < -0.10 and delta_60s > 50

    return bearish_div or bullish_div

# ──────────────────────────────────────────────────────────────────────────────
# Core: Check Consolidation (price in tight range)
# ──────────────────────────────────────────────────────────────────────────────

def _check_consolidation() -> tuple:
    """
    هل السعر في نطاق ضيق (أقل من $0.20) خلال آخر 90 ثانية؟
    Returns: (is_consolidating, price_range)
    """
    now = time.time()
    with _lock:
        price_copy = list(_price_buffer)

    recent = [e["price"] for e in price_copy if now - e["ts"] <= DIVERGENCE_SECS]
    if len(recent) < 3:
        return False, 0.0

    price_range = max(recent) - min(recent)
    return price_range <= CONSOLIDATION_RANGE, round(price_range, 3)

# ──────────────────────────────────────────────────────────────────────────────
# Core: Check proximity to S&R level
# ──────────────────────────────────────────────────────────────────────────────

def _near_level(price: float, resistance: float, support: float) -> tuple:
    """
    هل السعر قريب من مستوى الدعم أو المقاومة (±$0.30)؟
    Returns: (is_near, level_type, level_value, distance)
    """
    if resistance and abs(price - resistance) <= PROXIMITY_ALERT_RANGE:
        return True, "مقاومة", resistance, round(abs(price - resistance), 2)
    if support and abs(price - support) <= PROXIMITY_ALERT_RANGE:
        return True, "دعم", support, round(abs(price - support), 2)
    return False, None, None, None

# ──────────────────────────────────────────────────────────────────────────────
# Core: Send Early Warning Alert
# ──────────────────────────────────────────────────────────────────────────────

def _send_early_warning(price: float, delta: float, imbalance: float,
                         level_type: str, level_val: float, distance: float,
                         ask_vol: float, bid_vol: float):
    """إرسال تنبيه مبكر (تحذير) عند اقتراب السعر من مستوى مهم مع ضعف الزخم."""
    now_str = _get_et_now().strftime("%I:%M:%S %p")
    sweep_note = "نعم ⚠️" if imbalance > 1.5 else "لا"

    premium_collapse = _check_premium_collapse()
    delta_flip = _check_delta_flip()

    msg = (
        f"⚠️ <b>تحذير مبكر — راقب صفقتك</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>السهم:</b> TSLA\n"
        f"💰 <b>السعر:</b> <code>${price:.2f}</code>\n"
        f"🎯 <b>المستوى:</b> {level_type} @ ${level_val:.2f} (بُعد: ${distance:.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Delta (60s):</b> {delta:+.0f}\n"
        f"⚖️ <b>نسبة البيع/الشراء:</b> {imbalance:.2f}x\n"
        f"🔴 <b>حجم البيع:</b> {bid_vol:.0f}\n"
        f"🟢 <b>حجم الشراء:</b> {ask_vol:.0f}\n"
        f"🌊 <b>Sweep معاكس:</b> {sweep_note}\n"
    )

    if premium_collapse:
        msg += f"📉 <b>Premium Collapse:</b> ⚠️ تحذير (انهيار > 70%)\n"
    if delta_flip:
        msg += f"🔄 <b>Delta Flip:</b> ⚠️ تحذير (انقلاب الدلتا)\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"→ راقب صفقتك وجهز أمر الخروج\n"
        f"🕐 {now_str} ET"
    )

    reason = f"قرب {level_type} @ ${level_val:.2f}"
    if premium_collapse: reason += " + Premium Collapse"
    if delta_flip: reason += " + Delta Flip"

    sent = _send_telegram(msg)
    if sent:
        _log_to_csv("EARLY_WARNING", price, delta, imbalance, False, reason)
        logger.info(f"[RD] Early warning sent @ ${price:.2f} near {level_type} ${level_val:.2f}")

# ──────────────────────────────────────────────────────────────────────────────
# Core: Send Actual Reversal Alert
# ──────────────────────────────────────────────────────────────────────────────

def _send_reversal_alert(price: float, delta: float, imbalance: float):
    """إرسال تنبيه الانعكاس الفعلي — اخرج فوراً."""
    now_str = _get_et_now().strftime("%I:%M:%S %p")

    premium_collapse = _check_premium_collapse()
    delta_flip = _check_delta_flip()

    msg = (
        f"🚨 <b>انعكاس يحدث الحين — اخرج فوراً</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>السهم:</b> TSLA\n"
        f"💰 <b>السعر:</b> <code>${price:.2f}</code>\n"
        f"📊 <b>Delta:</b> {delta:+.0f}\n"
        f"⚖️ <b>نسبة البيع/الشراء:</b> {imbalance:.2f}x\n"
        f"🔀 <b>Divergence:</b> نعم ✅\n"
    )

    if premium_collapse:
        msg += f"📉 <b>Premium Collapse:</b> ⚠️ مؤكد (انهيار > 70%)\n"
    if delta_flip:
        msg += f"🔄 <b>Delta Flip:</b> ⚠️ مؤكد (انقلاب الدلتا)\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"→ اخرج من العقد الحالي فوراً\n"
        f"→ انتظر استقرار السوق\n"
        f"→ ادخل عقد ATM جديد بعد التأكد\n"
        f"🕐 {now_str} ET"
    )

    reason = "Divergence + Consolidation 90s"
    if premium_collapse: reason += " + Premium Collapse"
    if delta_flip: reason += " + Delta Flip"

    sent = _send_telegram(msg)
    if sent:
        _log_to_csv("ACTUAL_REVERSAL", price, delta, imbalance, True, reason)
        logger.info(f"[RD] Reversal alert sent @ ${price:.2f} | Delta: {delta:+.0f}")

# ──────────────────────────────────────────────────────────────────────────────
# Main Detection Loop
# ──────────────────────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────────────────────
# AUTO TAPE FEED: جلب صفقات TSLA الأخيرة من Alpaca وتغذية tape_buffer
# ──────────────────────────────────────────────────────────────────────────────

_last_trade_ts = ""  # تتبع آخر صفقة لتجنب التكرار

def _fetch_alpaca_trades_as_tape():
    """
    جلب آخر صفقات TSLA من Alpaca وتحويلها لبيانات Tape.
    يستخدم Tick Rule: إذا السعر أعلى من الصفقة السابقة = ask (شراء)
    إذا السعر أقل = bid (بيع)
    إذا متساوي = يستخدم NBBO midpoint للتصنيف
    """
    global _last_trade_ts
    try:
        alpaca_key    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
        alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
        headers = {"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret}
        
        # جلب آخر quote للحصول على bid/ask
        quote_r = http_requests.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/quotes/latest",
            headers=headers, timeout=6
        )
        if quote_r.status_code != 200:
            return
        quote = quote_r.json().get("quote", {})
        bid_price = float(quote.get("bp", 0))
        ask_price = float(quote.get("ap", 0))
        if bid_price <= 0 or ask_price <= 0:
            return
        mid_price = (bid_price + ask_price) / 2
        
        # جلب آخر 100 صفقة
        trades_r = http_requests.get(
            "https://data.alpaca.markets/v2/stocks/TSLA/trades?limit=100",
            headers=headers, timeout=6
        )
        if trades_r.status_code != 200:
            return
        trades = trades_r.json().get("trades", [])
        if not trades:
            return
        
        # تجنب تكرار نفس الصفقات
        latest_ts = trades[-1].get("t", "")
        if latest_ts == _last_trade_ts:
            return  # لا توجد صفقات جديدة
        _last_trade_ts = latest_ts
        
        # Tick Rule + NBBO midpoint
        prev_price = float(trades[0].get("p", 0))
        for trade in trades:
            trade_price = float(trade.get("p", 0))
            trade_size  = float(trade.get("s", 0))
            if trade_price <= 0 or trade_size <= 0:
                continue
            
            # Tick Rule: مقارنة بالصفقة السابقة
            if trade_price > prev_price:
                side = "ask"  # uptick = شراء عدواني
            elif trade_price < prev_price:
                side = "bid"  # downtick = بيع عدواني
            else:
                # نفس السعر = استخدم NBBO midpoint
                side = "ask" if trade_price >= mid_price else "bid"
            
            add_tape_event(side, trade_size)
            prev_price = trade_price
        
    except Exception as e:
        logger.error(f"[RD] Tape feed error: {e}")

def _detection_loop():
    """
    الحلقة الرئيسية للكشف عن الانعكاس.
    تعمل كـ background thread، تفحص كل 3 ثوانٍ.
    """
    global _divergence_start_ts, _last_alert_ts, _last_warning_ts

    logger.info("[RD] Reversal Detector started ✅")

    while True:
        time.sleep(CHECK_INTERVAL_SECS)

        try:
            # ── شرط 1: هل نافذة التداول مفتوحة؟ ──────────────────────────────
            if not _is_trading_window():
                _divergence_start_ts = None  # reset divergence outside window
                continue

            # ── مراقبة TSLA دائماً (CALL فقط) ─────────────────────────────

            # ── جلب البيانات ────────────────────────────────────────────────────
            current_price = _get_current_price()
            if current_price <= 0:
                continue

            # تغذية Tape Buffer من Alpaca Trades
            _fetch_alpaca_trades_as_tape()

            resistance, support = _get_levels()
            delta_60s, imbalance, ask_vol, bid_vol, delta_avg, is_delta_weak = _calc_delta_and_imbalance()
            has_divergence = _check_divergence(delta_60s)
            is_consolidating, price_range = _check_consolidation()
            near_lvl, lvl_type, lvl_val, distance = _near_level(current_price, resistance, support)

            now = time.time()

            # ── التنبيه المبكر (Early Warning) ──────────────────────────────────
            # الشروط: قرب مستوى + ضعف الـ Delta + Imbalance > 1.3
            # Cooldown مستقل للتحذيرات المبكرة (نصف الـ Cooldown الرئيسي)
            early_warning_cooldown = COOLDOWN_SECS / 2
            if (near_lvl and
                    is_delta_weak and
                    imbalance > 1.3 and
                    now - _last_warning_ts >= early_warning_cooldown):
                _send_early_warning(current_price, delta_60s, imbalance,
                                    lvl_type, lvl_val, distance, ask_vol, bid_vol)
                _last_warning_ts = now

            # ── الانعكاس الفعلي (Actual Reversal) ──────────────────────────────
            # الشروط: Divergence مستمر 90 ثانية + Consolidation + Delta سالب قوي
            if has_divergence and is_consolidating:
                if _divergence_start_ts is None:
                    _divergence_start_ts = now
                    logger.info(f"[RD] Divergence started @ ${current_price:.2f} | Delta: {delta_60s:+.0f}")
                else:
                    elapsed_div = now - _divergence_start_ts
                    if (elapsed_div >= DIVERGENCE_SECS and
                            now - _last_alert_ts >= COOLDOWN_SECS):
                        _send_reversal_alert(current_price, delta_60s, imbalance)
                        _last_alert_ts = now
                        _divergence_start_ts = None  # reset بعد الإرسال
            else:
                # إذا انتهى الانحراف قبل 90 ثانية، نعيد العداد
                if _divergence_start_ts is not None:
                    logger.info(f"[RD] Divergence reset (lasted {now - _divergence_start_ts:.0f}s)")
                _divergence_start_ts = None

        except Exception as e:
            logger.error(f"[RD] Detection loop error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Public: Start the detector as a background thread
# ──────────────────────────────────────────────────────────────────────────────

def start_reversal_detector():
    """
    تشغيل وحدة كشف الانعكاس كـ background thread.
    يُستدعى من app.py عند بدء تشغيل الـ Flask server.
    """
    import threading
    t = threading.Thread(target=_detection_loop, daemon=True, name="reversal_detector")
    t.start()
    logger.info("[RD] Reversal Detector thread launched ✅")
    return t


# ──────────────────────────────────────────────────────────────────────────────
# Standalone test (run directly: python reversal_detector.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    logger.info("=== Reversal Detector — Standalone Test ===")

    # محاكاة صفقة مفتوحة
    set_position_open(True)

    # محاكاة مستويات من TradingView
    update_levels_from_webhook(resistance=378.50, support=374.00)

    # محاكاة بيانات Tape (بيع عدواني مسيطر)
    import random
    for _ in range(100):
        side = "bid" if random.random() > 0.35 else "ask"
        add_tape_event(side, random.uniform(50, 300))

    # محاكاة حركة سعر صاعدة مع بيع عدواني (Divergence)
    base_price = 377.50
    for i in range(30):
        add_price_event(base_price + i * 0.05)

    # اختبار الحسابات
    delta, imbalance, ask_v, bid_v, avg, weak = _calc_delta_and_imbalance()
    div = _check_divergence(delta)
    consol, rng = _check_consolidation()
    r, s = _get_levels()
    price = _get_current_price()

    logger.info(f"Price: ${price:.2f}")
    logger.info(f"Resistance: ${r} | Support: ${s}")
    logger.info(f"Delta (60s): {delta:+.0f} | Avg (5min): {avg:.0f} | Weak: {weak}")
    logger.info(f"Imbalance: {imbalance:.3f} | Ask: {ask_v:.0f} | Bid: {bid_v:.0f}")
    logger.info(f"Divergence: {div} | Consolidating: {consol} | Range: ${rng}")

    logger.info("=== Test Complete ===")


def _check_premium_collapse() -> bool:
    """التحقق من انهيار البريميوم (Premium Collapse)."""
    with _lock:
        peak_premium = _option_data["peak_premium"]
        current_premium = _option_data["premium"]
        if peak_premium > 0 and current_premium > 0:
            drop_percentage = (peak_premium - current_premium) / peak_premium
            if drop_percentage >= 0.70: # 70% drop
                logger.warning(f"[RD] Premium Collapse detected! Current: {current_premium:.2f}, Peak: {peak_premium:.2f}, Drop: {drop_percentage:.2%}")
                return True
    return False

def _check_delta_flip() -> bool:
    """التحقق من انقلاب الدلتا (Delta Flip) لتأكيد الانعكاس."""
    # Placeholder - a proper implementation requires historical delta data
    return False
