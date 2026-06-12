"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 TSLA ORB Smart Assistant — مساعد تداول ذكي (تنبيه فقط — لا تنفيذ تلقائي)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

المراحل:
1. مراقبة السوق (TSLA عبر Alpaca)
2. حساب Opening Range (9:30 - 10:00 ET)
3. اكتشاف كسر ORB (شمعة 5M مغلقة + Volume + VWAP)
4. إرسال تنبيه تلغرام مع اقتراح عقد ATM + TP/SL
5. انتظار تحليل Cheddar Flow (يدوي عبر المستخدم)

القواعد:
- نافذة التنبيه: 10:10 AM - 11:30 AM ET فقط
- CALL: شمعة 5M تغلق فوق ORB High + السعر فوق VWAP
- PUT: شمعة 5M تغلق تحت ORB Low + السعر تحت VWAP
- حد أقصى: 2 تنبيه في نفس الاتجاه يومياً
- لا تنفيذ تلقائي — تنبيه فقط
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime, timedelta

logger = logging.getLogger("ORB_Assistant")

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

# Alpaca
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
ALPACA_DATA_URL   = "https://data.alpaca.markets"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8708530077:AAF16LsdHUNTW5G25UypCm8NiFTmCIranP8")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "975644160")

# ORB Parameters
ORB_WINDOW_START = 40    # 9:30 + 40 = 10:10 AM ET
ORB_WINDOW_END   = 120   # 9:30 + 120 = 11:30 AM ET
ORB_BUILD_AFTER  = 30    # 9:30 + 30 = 10:00 AM ET (بعد بناء OR)

# Trading Parameters
MAX_ALERTS_PER_DIRECTION = 2   # حد 2 تنبيه في نفس الاتجاه
LOOP_INTERVAL = 15             # فحص كل 15 ثانية
MIN_BREAKOUT_AMOUNT = 0.30     # كسر لا يقل عن $0.30
MIN_VOLUME_RATIO = 1.2         # Volume أعلى من المتوسط بـ 20%

# TP/SL defaults
DEFAULT_TP_PCT = 0.12   # +12%
DEFAULT_SL_PCT = -0.08  # -8%

# ══════════════════════════════════════════════════════════════════════════════
# State
# ══════════════════════════════════════════════════════════════════════════════

_orb_lock = threading.Lock()
_orb_state = {
    "running": False,
    "or_high": None,
    "or_low": None,
    "or_built": False,
    "vwap": 0,
    "price": 0,
    "alerts_today": [],       # [{direction, time, price}]
    "call_alerts": 0,
    "put_alerts": 0,
    "status_msg": "غير نشط",
    "last_check": "--",
}

_session = requests.Session()

# ══════════════════════════════════════════════════════════════════════════════
# Alpaca Data Functions
# ══════════════════════════════════════════════════════════════════════════════

def _headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def get_snapshot():
    """جلب snapshot لـ TSLA (سعر + VWAP + volume)."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/TSLA/snapshot",
            headers=_headers(),
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            trade = data.get("latestTrade", {})
            bar = data.get("dailyBar", {})
            minute_bar = data.get("minuteBar", {})
            return {
                "price": float(trade.get("p", 0)),
                "vwap": float(bar.get("vw", 0)),
                "volume": int(bar.get("v", 0)),
                "day_high": float(bar.get("h", 0)),
                "day_low": float(bar.get("l", 0)),
                "day_open": float(bar.get("o", 0)),
                "minute_volume": int(minute_bar.get("v", 0)),
            }
    except Exception as e:
        logger.error(f"[ORB] Snapshot error: {e}")
    return None


def get_bars(timeframe="5Min", limit=12):
    """جلب شموع TSLA."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/TSLA/bars",
            headers=_headers(),
            params={"timeframe": timeframe, "limit": limit, "feed": "iex"},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception as e:
        logger.error(f"[ORB] Bars error: {e}")
    return []


def get_options_atm(price, direction):
    """
    جلب أفضل عقد ATM بناءً على Volume + Open Interest.
    ATM = أقرب strike للسعر الحالي.
    """
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    
    # أقرب expiry (هذا الأسبوع أو الأسبوع القادم)
    days_to_friday = (4 - now.weekday()) % 7
    if days_to_friday == 0 and now.hour >= 15:
        days_to_friday = 7
    if days_to_friday < 1:
        days_to_friday = 7
    expiry = (now + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")
    
    option_type = "call" if direction == "CALL" else "put"
    
    # ATM range: ±5 من السعر الحالي
    strike_min = round(price - 5, 0)
    strike_max = round(price + 5, 0)
    
    try:
        params = {
            "underlying_symbols": "TSLA",
            "expiration_date": expiry,
            "type": option_type,
            "strike_price_gte": str(strike_min),
            "strike_price_lte": str(strike_max),
            "limit": 20
        }
        r = _session.get(
            f"{ALPACA_BASE_URL}/v2/options/contracts",
            headers=_headers(),
            params=params,
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"[ORB] Options chain error: {r.status_code}")
            return None
        
        contracts = r.json().get("option_contracts", [])
        if not contracts:
            return None
        
        # جلب quotes لكل العقود
        symbols = [c.get("symbol", "") for c in contracts if c.get("symbol")]
        quotes = _get_options_quotes(symbols)
        
        # اختيار أفضل عقد ATM بناءً على Volume + OI
        best = None
        best_score = -1
        
        for c in contracts:
            symbol = c.get("symbol", "")
            strike = float(c.get("strike_price", 0))
            oi = int(c.get("open_interest", 0))
            
            # جلب quote
            q = quotes.get(symbol, {})
            bid = q.get("bid", 0)
            ask = q.get("ask", 0)
            vol = q.get("volume", 0)
            mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0
            
            if mid < 0.50:
                continue
            
            # ATM score: أقرب للسعر + أعلى OI + أعلى Volume
            distance = abs(price - strike)
            if distance > 5:
                continue
            
            spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 99
            if spread_pct > 8:
                continue
            
            # Score: OI (40%) + Volume (40%) + Proximity (20%)
            proximity_score = max(0, (5 - distance) * 20)
            score = oi * 0.4 + vol * 0.4 + proximity_score
            
            if score > best_score:
                best_score = score
                best = {
                    "symbol": symbol,
                    "strike": strike,
                    "expiry": expiry,
                    "open_interest": oi,
                    "volume": vol,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_pct": round(spread_pct, 1),
                    "distance_from_atm": round(distance, 2),
                }
        
        return best
    except Exception as e:
        logger.error(f"[ORB] ATM selection error: {e}")
    return None


def _get_options_quotes(symbols):
    """جلب quotes لمجموعة عقود."""
    if not symbols:
        return {}
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v1beta1/options/quotes/latest",
            headers=_headers(),
            params={"symbols": ",".join(symbols[:10]), "feed": "indicative"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("quotes", {})
            result = {}
            for sym, q in data.items():
                result[sym] = {
                    "bid": float(q.get("bp", 0)),
                    "ask": float(q.get("ap", 0)),
                    "volume": int(q.get("v", 0)) if "v" in q else 0,
                }
            return result
    except Exception as e:
        logger.error(f"[ORB] Options quotes error: {e}")
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# ORB Logic
# ══════════════════════════════════════════════════════════════════════════════

def build_opening_range():
    """بناء Opening Range من أول 30 دقيقة (9:30-10:00 ET)."""
    bars = get_bars("5Min", 12)
    if not bars or len(bars) < 6:
        return None, None
    
    # أول 6 شموع 5M = 30 دقيقة
    or_bars = bars[:6]
    or_high = max(float(b["h"]) for b in or_bars)
    or_low = min(float(b["l"]) for b in or_bars)
    return or_high, or_low


def check_breakout():
    """
    فحص كسر ORB مع جميع الشروط:
    1. شمعة 5M أغلقت فوق/تحت ORB
    2. Volume أعلى من المتوسط
    3. VWAP alignment (CALL فوق VWAP, PUT تحت VWAP)
    """
    with _orb_lock:
        or_high = _orb_state["or_high"]
        or_low = _orb_state["or_low"]
    
    if not or_high or not or_low:
        return None, {"reason": "Opening Range لم يُبنَ بعد"}
    
    # جلب البيانات
    snap = get_snapshot()
    if not snap:
        return None, {"reason": "بيانات السعر غير متاحة"}
    
    price = snap["price"]
    vwap = snap["vwap"]
    
    # جلب شموع 5M
    bars = get_bars("5Min", 12)
    if not bars or len(bars) < 8:
        return None, {"reason": "بيانات الشموع غير كافية"}
    
    # حساب Volume المتوسط
    volumes = [float(b["v"]) for b in bars]
    avg_vol = sum(volumes[:-2]) / max(len(volumes) - 2, 1)
    recent_vol = (volumes[-1] + volumes[-2]) / 2
    vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
    
    last_bar_close = float(bars[-1]["c"])
    last_bar_high = float(bars[-1]["h"])
    last_bar_low = float(bars[-1]["l"])
    
    # تحديث الحالة
    with _orb_lock:
        _orb_state["price"] = price
        _orb_state["vwap"] = vwap
    
    # ═══ فحص كسر صاعد (CALL) ═══
    if last_bar_close > or_high + MIN_BREAKOUT_AMOUNT:
        # شرط 1: شمعة 5M أغلقت بالكامل فوق ORB High
        if last_bar_low <= or_high:
            return None, {"reason": f"شمعة لم تغلق بالكامل فوق ORB (Low={last_bar_low:.2f} ≤ {or_high:.2f})"}
        
        # شرط 2: السعر فوق VWAP
        if price < vwap:
            return None, {"reason": f"CALL لكن السعر تحت VWAP ({price:.2f} < {vwap:.2f})"}
        
        # شرط 3: Volume أعلى من المتوسط
        if vol_ratio < MIN_VOLUME_RATIO:
            return None, {"reason": f"Volume ضعيف ({vol_ratio:.1f}x < {MIN_VOLUME_RATIO}x)"}
        
        # شرط 4: حد التنبيهات
        with _orb_lock:
            if _orb_state["call_alerts"] >= MAX_ALERTS_PER_DIRECTION:
                return None, {"reason": "وصل حد CALL اليوم (2 تنبيهات)"}
        
        return "CALL", {
            "price": price,
            "vwap": vwap,
            "or_high": or_high,
            "or_low": or_low,
            "breakout_amount": round(last_bar_close - or_high, 2),
            "volume_ratio": round(vol_ratio, 2),
            "recent_volume": int(recent_vol),
            "avg_volume": int(avg_vol),
            "vwap_distance": round(price - vwap, 2),
        }
    
    # ═══ فحص كسر هابط (PUT) ═══
    if last_bar_close < or_low - MIN_BREAKOUT_AMOUNT:
        # شرط 1: شمعة 5M أغلقت بالكامل تحت ORB Low
        if last_bar_high >= or_low:
            return None, {"reason": f"شمعة لم تغلق بالكامل تحت ORB (High={last_bar_high:.2f} ≥ {or_low:.2f})"}
        
        # شرط 2: السعر تحت VWAP
        if price > vwap:
            return None, {"reason": f"PUT لكن السعر فوق VWAP ({price:.2f} > {vwap:.2f})"}
        
        # شرط 3: Volume أعلى من المتوسط
        if vol_ratio < MIN_VOLUME_RATIO:
            return None, {"reason": f"Volume ضعيف ({vol_ratio:.1f}x < {MIN_VOLUME_RATIO}x)"}
        
        # شرط 4: حد التنبيهات
        with _orb_lock:
            if _orb_state["put_alerts"] >= MAX_ALERTS_PER_DIRECTION:
                return None, {"reason": "وصل حد PUT اليوم (2 تنبيهات)"}
        
        return "PUT", {
            "price": price,
            "vwap": vwap,
            "or_high": or_high,
            "or_low": or_low,
            "breakout_amount": round(or_low - last_bar_close, 2),
            "volume_ratio": round(vol_ratio, 2),
            "recent_volume": int(recent_vol),
            "avg_volume": int(avg_vol),
            "vwap_distance": round(vwap - price, 2),
        }
    
    return None, {"reason": f"داخل النطاق (${or_low:.2f} — ${or_high:.2f}) | السعر: ${price:.2f}"}


# ══════════════════════════════════════════════════════════════════════════════
# Telegram Alert
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(message):
    """إرسال رسالة على تلغرام."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"[ORB] Telegram error: {e}")
        return False


def send_orb_alert(direction, data, contract):
    """إرسال تنبيه ORB كامل على تلغرام."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    
    # حساب الوقت المتبقي في النافذة
    window_end = now.replace(hour=11, minute=30, second=0)
    remaining = (window_end - now).total_seconds() / 60
    
    # TP/SL
    if contract:
        entry_price = contract["mid"]
        tp_price = round(entry_price * (1 + DEFAULT_TP_PCT), 2)
        sl_price = round(entry_price * (1 + DEFAULT_SL_PCT), 2)
    else:
        entry_price = tp_price = sl_price = 0
    
    emoji = "📈" if direction == "CALL" else "📉"
    
    msg = (
        f"{emoji} <b>🧠 ORB ALERT — {direction}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now.strftime('%I:%M %p')} ET\n"
        f"📍 TSLA: <b>${data['price']:.2f}</b>\n"
        f"\n"
        f"📊 <b>Opening Range:</b>\n"
        f"   🔼 High: ${data['or_high']:.2f}\n"
        f"   🔽 Low: ${data['or_low']:.2f}\n"
        f"   📐 Range: ${data['or_high'] - data['or_low']:.2f}\n"
        f"\n"
        f"💥 <b>كسر بـ ${data['breakout_amount']:.2f}</b>\n"
        f"📊 VWAP: ${data['vwap']:.2f} (بعد: ${data['vwap_distance']:.2f})\n"
        f"📈 Volume: {data['volume_ratio']:.1f}x المتوسط\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    if contract:
        msg += (
            f"🎯 <b>عقد ATM المقترح:</b>\n"
            f"   📋 {contract['symbol']}\n"
            f"   Strike: ${contract['strike']:.0f} | Expiry: {contract['expiry']}\n"
            f"   💰 سعر العقد: ${contract['mid']:.2f}\n"
            f"   📊 OI: {contract['open_interest']:,} | Vol: {contract['volume']:,}\n"
            f"   Spread: {contract['spread_pct']}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 TP: ${tp_price:.2f} (+{DEFAULT_TP_PCT*100:.0f}%)\n"
            f"🛑 SL: ${sl_price:.2f} ({DEFAULT_SL_PCT*100:.0f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
    
    msg += (
        f"⏰ الوقت المتبقي في النافذة: {remaining:.0f} دقيقة\n"
        f"\n"
        f"⚠️ <b>أرسل صورة Cheddar Flow للتحليل النهائي</b>"
    )
    
    send_telegram(msg)
    
    # تحديث الحالة
    with _orb_lock:
        _orb_state["alerts_today"].append({
            "direction": direction,
            "time": now.strftime("%I:%M %p"),
            "price": data["price"],
        })
        if direction == "CALL":
            _orb_state["call_alerts"] += 1
        else:
            _orb_state["put_alerts"] += 1


# ══════════════════════════════════════════════════════════════════════════════
# Main Loop
# ══════════════════════════════════════════════════════════════════════════════

def _in_window():
    """هل نحن في نافذة التنبيه (10:10 AM - 11:30 AM ET)."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return ORB_WINDOW_START <= elapsed <= ORB_WINDOW_END


def _is_market_open():
    """هل السوق مفتوح."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    # الاثنين-الجمعة فقط
    if now.weekday() >= 5:
        return False
    # 9:30 AM - 4:00 PM ET
    market_open = now.replace(hour=9, minute=30, second=0)
    market_close = now.replace(hour=16, minute=0, second=0)
    return market_open <= now <= market_close


def orb_assistant_loop():
    """Main loop — مراقبة ORB وإرسال تنبيهات."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    logger.info("[ORB] 🧠 Smart ORB Assistant started")
    
    while True:
        try:
            with _orb_lock:
                if not _orb_state["running"]:
                    break
            
            now = datetime.now(et_tz)
            now_str = now.strftime("%I:%M %p")
            
            with _orb_lock:
                _orb_state["last_check"] = now_str
            
            # هل السوق مفتوح؟
            if not _is_market_open():
                with _orb_lock:
                    _orb_state["status_msg"] = "السوق مغلق"
                time.sleep(60)
                continue
            
            # Reset يومي
            open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed = (now - open_time).total_seconds() / 60
            
            if elapsed < 2:
                with _orb_lock:
                    _orb_state["or_built"] = False
                    _orb_state["or_high"] = None
                    _orb_state["or_low"] = None
                    _orb_state["alerts_today"] = []
                    _orb_state["call_alerts"] = 0
                    _orb_state["put_alerts"] = 0
                    _orb_state["status_msg"] = "بداية يوم جديد..."
            
            # بناء Opening Range بعد 10:00 AM
            with _orb_lock:
                or_built = _orb_state["or_built"]
            
            if elapsed >= ORB_BUILD_AFTER and not or_built:
                or_high, or_low = build_opening_range()
                if or_high and or_low:
                    with _orb_lock:
                        _orb_state["or_high"] = or_high
                        _orb_state["or_low"] = or_low
                        _orb_state["or_built"] = True
                    
                    # إرسال OR على تلغرام
                    send_telegram(
                        f"📊 <b>Opening Range — TSLA</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔼 High: ${or_high:.2f}\n"
                        f"🔽 Low: ${or_low:.2f}\n"
                        f"📐 Range: ${or_high - or_low:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏰ نافذة التنبيه: 10:10 - 11:30 AM ET\n"
                        f"🔍 أراقب الكسر..."
                    )
                    logger.info(f"[ORB] OR built: ${or_low:.2f} - ${or_high:.2f}")
            
            # هل نحن في نافذة التنبيه؟
            if not _in_window():
                with _orb_lock:
                    if elapsed < ORB_WINDOW_START:
                        _orb_state["status_msg"] = f"انتظار نافذة 10:10 AM... ({now_str})"
                    else:
                        _orb_state["status_msg"] = f"انتهت النافذة ({now_str})"
                time.sleep(30)
                continue
            
            # فحص الكسر
            direction, data = check_breakout()
            
            if direction:
                # وجدنا كسر صحيح — جلب أفضل عقد ATM
                contract = get_options_atm(data["price"], direction)
                
                # إرسال التنبيه
                send_orb_alert(direction, data, contract)
                logger.info(f"[ORB] 🚨 ALERT: {direction} @ ${data['price']:.2f}")
                
                with _orb_lock:
                    _orb_state["status_msg"] = f"✅ تنبيه {direction} @ ${data['price']:.2f}"
                
                # انتظار 5 دقائق بعد التنبيه (لتجنب تنبيهات متكررة)
                time.sleep(300)
            else:
                with _orb_lock:
                    reason = data.get("reason", "لا إشارة")
                    _orb_state["status_msg"] = f"🔍 {reason} ({now_str})"
            
        except Exception as e:
            logger.error(f"[ORB] Loop error: {e}")
        
        time.sleep(LOOP_INTERVAL)
    
    logger.info("[ORB] 🧠 Smart ORB Assistant stopped")


# ══════════════════════════════════════════════════════════════════════════════
# Control Functions (called from app.py)
# ══════════════════════════════════════════════════════════════════════════════

def start_orb_assistant():
    """تشغيل مساعد ORB."""
    with _orb_lock:
        if _orb_state["running"]:
            return {"ok": True, "msg": "Already running"}
        _orb_state["running"] = True
    
    t = threading.Thread(target=orb_assistant_loop, daemon=True, name="ORB_Assistant")
    t.start()
    return {"ok": True, "msg": "ORB Assistant started"}


def stop_orb_assistant():
    """إيقاف مساعد ORB."""
    with _orb_lock:
        _orb_state["running"] = False
    return {"ok": True, "msg": "ORB Assistant stopped"}


def get_orb_status():
    """حالة مساعد ORB."""
    with _orb_lock:
        return {
            "ok": True,
            "running": _orb_state["running"],
            "or_high": _orb_state["or_high"],
            "or_low": _orb_state["or_low"],
            "or_built": _orb_state["or_built"],
            "price": _orb_state["price"],
            "vwap": _orb_state["vwap"],
            "alerts_today": _orb_state["alerts_today"],
            "call_alerts": _orb_state["call_alerts"],
            "put_alerts": _orb_state["put_alerts"],
            "status_msg": _orb_state["status_msg"],
            "last_check": _orb_state["last_check"],
        }
