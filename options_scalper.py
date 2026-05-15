"""
ثاقب V8 — Options Scalper Engine
=================================
محرك تداول أوبشن تلقائي بالكامل — طبقتين:
  الطبقة 1: سكالبينج ATM (0DTE) — صفقات سريعة متكررة
  الطبقة 2: ITM Pullback — عقد عميق عند البولباك

يعمل كـ background thread مستقل — لا يحتاج webhook للدخول.
يعتمد على: Alpaca Options API + GEX Map + Trend Detection.

Author: ثاقب V8
Date: May 2026
"""

import os
import time
import json
import math
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests as http_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "PKW3OHVLGGWGYCFMTCKDB435WA")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_URL   = "https://data.alpaca.markets"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Trading Window ───────────────────────────────────────────────────────────
SCALP_START_HOUR   = 10
SCALP_START_MINUTE = 10   # بعد 40 دقيقة من فتح السوق
SCALP_END_HOUR     = 12
SCALP_END_MINUTE   = 40   # ساعتين ونص فقط
FORCE_CLOSE_HOUR   = 12
FORCE_CLOSE_MINUTE = 45   # بيع كل شي المتبقي

# ── Layer 1: ATM Scalp ───────────────────────────────────────────────────────
ATM_CONTRACTS      = 2      # عقدين ATM
ATM_TP1_PCT        = 0.05   # +5% بيع العقد الأول
ATM_TP2_PCT        = 0.10   # +10% بيع العقد الثاني
ATM_SL_PCT         = 0.25   # -25% ستوب
ATM_REINFORCE_PCT  = 0.15   # -15% = تعزيز (شراء 2 إضافية)
ATM_REINFORCE_TP   = 0.05   # +5% من المتوسط الجديد = بيع الكل
ATM_MAX_REINFORCE  = 1      # تعزيز مرة وحدة بس

# ── Layer 2: ITM Pullback ────────────────────────────────────────────────────
ITM_CONTRACTS      = 1      # عقد واحد ITM
ITM_DELTA_MIN      = 0.70   # أقل دلتا
ITM_DELTA_MAX      = 0.88   # أعلى دلتا
ITM_ENTRY_BELOW    = 0.015  # يحط الأمر تحت البولباك بـ 1-2%
ITM_TP_PCT         = 0.40   # +40% جني أرباح
ITM_SL_PCT         = 0.16   # -16% ستوب
ITM_MIN_VOLUME     = 100    # حد أدنى حجم العقد

# ── Risk Management ──────────────────────────────────────────────────────────
PORTFOLIO_START    = 99408.71  # رصيد البداية (يُحدَّث تلقائياً)
MAX_PORTFOLIO_LOSS = 7000.0    # حد خسارة المحفظة الكلي
PAUSE_AFTER_CONSECUTIVE_LOSSES = 2  # خسارتين متتالية = وقف 30 دقيقة
PAUSE_DURATION_SECONDS = 1800       # 30 دقيقة

# ── Trend Detection ──────────────────────────────────────────────────────────
TREND_CHECK_INTERVAL = 30    # يشيك كل 30 ثانية
TREND_BARS_COUNT     = 20    # آخر 20 شمعة 1-دقيقة
TREND_MIN_ADX        = 20    # أقل ADX لاعتبار ترند
TREND_VWAP_CONFIRM   = True  # لازم فوق/تحت VWAP

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("V8_Scalper")

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Session with Retry
# ──────────────────────────────────────────────────────────────────────────────

def _create_session():
    s = http_requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_session = _create_session()

# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────

_state = {
    "running": False,
    "today": "",
    "trend": None,           # "BULL" / "BEAR" / None
    "trend_strength": 0,     # 0-100
    "vwap": 0.0,
    "current_price": 0.0,
    "day_high": 0.0,
    "day_low": 0.0,
    
    # Layer 1 (ATM Scalp)
    "atm_positions": [],     # list of active ATM positions
    "atm_trade_count": 0,
    "atm_reinforced": False,
    
    # Layer 2 (ITM Pullback)
    "itm_position": None,    # single ITM position
    "itm_pending_order": None,  # pending limit buy
    "itm_trade_count": 0,
    
    # Risk
    "daily_pnl": 0.0,
    "total_pnl": 0.0,
    "consecutive_losses": 0,
    "pause_until": 0,
    "portfolio_start_value": 0.0,
    "portfolio_current_value": 0.0,
    "stopped_for_day": False,
    "stopped_permanent": False,
    
    # Stats
    "trades_today": [],
    "wins": 0,
    "losses": 0,
}

# Reference to reversal map from main app
_reversal_map_ref = None
_gex_levels_fn = None

def set_reversal_map_ref(ref):
    """Called from app.py to share reversal map."""
    global _reversal_map_ref
    _reversal_map_ref = ref

def set_gex_fn(fn):
    """Called from app.py to share GEX fetch function."""
    global _gex_levels_fn
    _gex_levels_fn = fn

# ──────────────────────────────────────────────────────────────────────────────
# Alpaca Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json"
    }

def get_account():
    """Get Alpaca account info."""
    try:
        r = _session.get(f"{ALPACA_BASE_URL}/v2/account", headers=_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[V8] Account error: {e}")
    return None

def get_tsla_snapshot():
    """Get TSLA current price via snapshot."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/TSLA/snapshot",
            headers=_headers(),
            params={"feed": "iex"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            trade = data.get("latestTrade", {})
            price = float(trade.get("p", 0))
            bar = data.get("dailyBar", {})
            return {
                "price": price,
                "high": float(bar.get("h", 0)),
                "low": float(bar.get("l", 0)),
                "vwap": float(bar.get("vw", 0)),
                "volume": int(bar.get("v", 0))
            }
    except Exception as e:
        logger.error(f"[V8] Snapshot error: {e}")
    return None

def get_tsla_bars(timeframe="1Min", limit=20):
    """Get recent TSLA bars for trend detection."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/TSLA/bars",
            headers=_headers(),
            params={
                "timeframe": timeframe,
                "limit": limit,
                "feed": "iex",
                "sort": "asc"
            },
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("bars", [])
    except Exception as e:
        logger.error(f"[V8] Bars error: {e}")
    return []

def get_options_chain(expiry_date, option_type="call", strike_min=None, strike_max=None):
    """Get TSLA options chain from Alpaca."""
    try:
        params = {
            "underlying_symbols": "TSLA",
            "expiration_date": expiry_date,
            "type": option_type,
            "limit": 50
        }
        if strike_min:
            params["strike_price_gte"] = str(strike_min)
        if strike_max:
            params["strike_price_lte"] = str(strike_max)
        
        r = _session.get(
            f"{ALPACA_BASE_URL}/v2/options/contracts",
            headers=_headers(),
            params=params,
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("option_contracts", [])
    except Exception as e:
        logger.error(f"[V8] Options chain error: {e}")
    return []

def get_option_quote(symbol):
    """Get latest quote for an option contract."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v1beta1/options/quotes/latest",
            headers=_headers(),
            params={"symbols": symbol, "feed": "indicative"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            quotes = data.get("quotes", {})
            if symbol in quotes:
                q = quotes[symbol]
                bid = float(q.get("bp", 0))
                ask = float(q.get("ap", 0))
                mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0
                return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        logger.error(f"[V8] Option quote error: {e}")
    return None

def place_option_order(symbol, qty, side, order_type="market",
                       limit_price=None, time_in_force="day",
                       position_intent=None):
    """Place an options order on Alpaca."""
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if order_type == "limit" and limit_price is not None:
        payload["limit_price"] = str(round(limit_price, 2))
    if position_intent:
        payload["position_intent"] = position_intent
    
    try:
        r = _session.post(
            f"{ALPACA_BASE_URL}/v2/orders",
            headers=_headers(),
            json=payload,
            timeout=15
        )
        if r.status_code in (200, 201):
            order = r.json()
            logger.info(f"[V8] Order placed: {order.get('id')} | {side} {qty} {symbol} @ {limit_price or 'market'}")
            return order
        else:
            logger.error(f"[V8] Order error: {r.status_code} {r.text}")
    except Exception as e:
        logger.error(f"[V8] Order exception: {e}")
    return None

def cancel_order(order_id):
    """Cancel a pending order."""
    try:
        r = _session.delete(
            f"{ALPACA_BASE_URL}/v2/orders/{order_id}",
            headers=_headers(),
            timeout=10
        )
        return r.status_code in (200, 204)
    except Exception as e:
        logger.error(f"[V8] Cancel error: {e}")
    return False

def get_order_status(order_id):
    """Check order status."""
    try:
        r = _session.get(
            f"{ALPACA_BASE_URL}/v2/orders/{order_id}",
            headers=_headers(),
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[V8] Order status error: {e}")
    return None

def get_positions():
    """Get all open positions."""
    try:
        r = _session.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=_headers(),
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[V8] Positions error: {e}")
    return []

def close_position(symbol):
    """Close a specific position."""
    try:
        r = _session.delete(
            f"{ALPACA_BASE_URL}/v2/positions/{symbol}",
            headers=_headers(),
            timeout=10
        )
        return r.status_code in (200, 204)
    except Exception as e:
        logger.error(f"[V8] Close position error: {e}")
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    """Send message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Time Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _et_now():
    """Current time in ET."""
    return datetime.now(timezone.utc) - timedelta(hours=4)

def _today_str():
    return _et_now().strftime("%Y-%m-%d")

def _today_expiry():
    """Get today's date in YYYY-MM-DD for 0DTE."""
    return _today_str()

def _is_scalp_window():
    """Check if we're in the scalping window (10:10 - 12:40 ET)."""
    now = _et_now()
    mins = now.hour * 60 + now.minute
    start = SCALP_START_HOUR * 60 + SCALP_START_MINUTE
    end = SCALP_END_HOUR * 60 + SCALP_END_MINUTE
    return start <= mins < end

def _is_force_close_time():
    """Check if it's time to force close all positions."""
    now = _et_now()
    mins = now.hour * 60 + now.minute
    close_mins = FORCE_CLOSE_HOUR * 60 + FORCE_CLOSE_MINUTE
    return mins >= close_mins

def _is_weekday():
    """Check if today is a weekday (Mon-Fri)."""
    return _et_now().weekday() < 5

def _is_0dte_day():
    """Check if today has 0DTE options for TSLA.
    TSLA has options expiring Mon, Wed, Fri.
    But user wants Mon-Thu only (4 days).
    """
    day = _et_now().weekday()
    return day < 4  # Mon=0, Tue=1, Wed=2, Thu=3

# ──────────────────────────────────────────────────────────────────────────────
# Option Symbol Builder
# ──────────────────────────────────────────────────────────────────────────────

def build_option_symbol(ticker, expiry_date, option_type, strike):
    """
    Build OCC option symbol.
    Example: TSLA260515C00450000
    """
    dt = datetime.strptime(expiry_date, "%Y-%m-%d")
    date_part = dt.strftime("%y%m%d")
    type_char = "C" if option_type.upper() in ("CALL", "C") else "P"
    strike_int = int(round(strike * 1000))
    strike_part = f"{strike_int:08d}"
    return f"{ticker}{date_part}{type_char}{strike_part}"

# ──────────────────────────────────────────────────────────────────────────────
# Trend Detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_trend():
    """
    Detect current trend using 1-min bars.
    Returns: ("BULL", strength) / ("BEAR", strength) / (None, 0)
    
    Uses:
    1. EMA 9 vs EMA 21 crossover
    2. Price vs VWAP
    3. Momentum (last 5 bars direction)
    4. ADX approximation
    """
    bars = get_tsla_bars("1Min", 30)
    if not bars or len(bars) < 21:
        logger.warning("[V8] Not enough bars for trend detection")
        return None, 0
    
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    volumes = [int(b["v"]) for b in bars]
    
    # ── EMA Calculation ──
    def ema(data, period):
        if len(data) < period:
            return data[-1]
        k = 2 / (period + 1)
        result = sum(data[:period]) / period
        for val in data[period:]:
            result = val * k + result * (1 - k)
        return result
    
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    current = closes[-1]
    
    # ── VWAP from snapshot ──
    snap = get_tsla_snapshot()
    vwap = snap["vwap"] if snap and snap.get("vwap", 0) > 0 else 0
    _state["vwap"] = vwap
    _state["current_price"] = snap["price"] if snap else current
    if snap:
        _state["day_high"] = snap.get("high", 0)
        _state["day_low"] = snap.get("low", 0)
    
    # ── Momentum: last 5 bars ──
    last5 = closes[-5:]
    up_bars = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
    down_bars = sum(1 for i in range(1, len(last5)) if last5[i] < last5[i-1])
    
    # ── ADX Approximation (using ATR-based directional movement) ──
    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(bars)):
        h = highs[i]
        l = lows[i]
        pc = closes[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
        
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(max(up_move, 0) if up_move > down_move else 0)
        minus_dm.append(max(down_move, 0) if down_move > up_move else 0)
    
    period = 14
    if len(tr_list) >= period:
        atr = sum(tr_list[-period:]) / period
        if atr > 0:
            plus_di = (sum(plus_dm[-period:]) / period) / atr * 100
            minus_di = (sum(minus_dm[-period:]) / period) / atr * 100
            di_sum = plus_di + minus_di
            dx = abs(plus_di - minus_di) / di_sum * 100 if di_sum > 0 else 0
            adx = dx  # simplified
        else:
            adx = 0
            plus_di = 0
            minus_di = 0
    else:
        adx = 0
        plus_di = 0
        minus_di = 0
    
    # ── Score Calculation ──
    bull_score = 0
    bear_score = 0
    
    # EMA crossover (weight: 30)
    if ema9 > ema21:
        bull_score += 30
    elif ema9 < ema21:
        bear_score += 30
    
    # Price vs VWAP (weight: 25)
    if vwap > 0:
        if current > vwap:
            bull_score += 25
        elif current < vwap:
            bear_score += 25
    
    # Momentum (weight: 25)
    if up_bars >= 4:
        bull_score += 25
    elif up_bars >= 3:
        bull_score += 15
    elif down_bars >= 4:
        bear_score += 25
    elif down_bars >= 3:
        bear_score += 15
    
    # ADX direction (weight: 20)
    if adx >= TREND_MIN_ADX:
        if plus_di > minus_di:
            bull_score += 20
        else:
            bear_score += 20
    
    # ── Determine Trend ──
    strength = max(bull_score, bear_score)
    
    if bull_score >= 60 and bull_score > bear_score + 15:
        trend = "BULL"
    elif bear_score >= 60 and bear_score > bull_score + 15:
        trend = "BEAR"
    else:
        trend = None  # Choppy / No clear trend
    
    logger.info(f"[V8 Trend] bull={bull_score} bear={bear_score} adx={adx:.0f} "
                f"ema9={ema9:.2f} ema21={ema21:.2f} vwap={vwap:.2f} "
                f"price={current:.2f} → {trend or 'CHOP'} ({strength})")
    
    return trend, strength

def detect_pullback(trend):
    """
    Detect if current price is in a pullback within the trend.
    Returns: (is_pullback: bool, pullback_depth_pct: float)
    """
    bars = get_tsla_bars("1Min", 10)
    if not bars or len(bars) < 5:
        return False, 0
    
    closes = [float(b["c"]) for b in bars]
    current = closes[-1]
    
    if trend == "BULL":
        # In uptrend, pullback = price dipped from recent high
        recent_high = max(closes[-8:])
        depth = (recent_high - current) / recent_high
        # Pullback if price dipped 0.2-0.8% from recent high
        is_pullback = 0.002 <= depth <= 0.008
        # Also check: last 2 bars going down but overall trend still up
        last2_down = closes[-1] < closes[-2] or closes[-2] < closes[-3]
        return is_pullback and last2_down, depth
    
    elif trend == "BEAR":
        recent_low = min(closes[-8:])
        depth = (current - recent_low) / recent_low
        is_pullback = 0.002 <= depth <= 0.008
        last2_up = closes[-1] > closes[-2] or closes[-2] > closes[-3]
        return is_pullback and last2_up, depth
    
    return False, 0

# ──────────────────────────────────────────────────────────────────────────────
# Zone Safety Check (using reversal map)
# ──────────────────────────────────────────────────────────────────────────────

def is_safe_zone(price, trend):
    """
    Check if current price is NOT near a dangerous support/resistance level.
    Returns: (is_safe: bool, reason: str)
    """
    if not _reversal_map_ref or not _reversal_map_ref.get("built"):
        return True, "No map available — proceeding"
    
    levels = _reversal_map_ref.get("levels", [])
    if not levels:
        return True, "No levels — proceeding"
    
    for lvl in levels:
        lvl_price = float(lvl["price"])
        dist_pct = abs(price - lvl_price) / lvl_price
        
        if dist_pct <= 0.003:  # within 0.3% of a level
            lvl_type = lvl["type"]
            lvl_name = lvl["name"]
            
            # BULL trend near resistance = dangerous
            if trend == "BULL" and lvl_type == "resistance":
                return False, f"قريب من مقاومة {lvl_name} (${lvl_price:.2f})"
            
            # BEAR trend near support = dangerous
            if trend == "BEAR" and lvl_type == "support":
                return False, f"قريب من دعم {lvl_name} (${lvl_price:.2f})"
            
            # Near Gamma Flip = always dangerous
            if "Gamma" in lvl_name or "Flip" in lvl_name:
                return False, f"قريب من {lvl_name} (${lvl_price:.2f})"
    
    return True, "Safe zone"

# ──────────────────────────────────────────────────────────────────────────────
# Contract Finder
# ──────────────────────────────────────────────────────────────────────────────

def find_atm_contract(price, trend, expiry):
    """Find the best ATM 0DTE contract."""
    option_type = "call" if trend == "BULL" else "put"
    
    # Search near the money
    strike_min = round(price - 3, 0)
    strike_max = round(price + 3, 0)
    
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        logger.warning(f"[V8] No ATM contracts found for {expiry} {option_type}")
        return None
    
    # Find closest to current price
    best = None
    min_diff = float('inf')
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        diff = abs(strike - price)
        if diff < min_diff:
            min_diff = diff
            best = c
    
    if best:
        symbol = best.get("symbol", "")
        strike = float(best.get("strike_price", 0))
        
        # Get quote for mid price
        quote = get_option_quote(symbol)
        if quote and quote["mid"] > 0:
            return {
                "symbol": symbol,
                "strike": strike,
                "type": option_type,
                "expiry": expiry,
                "bid": quote["bid"],
                "ask": quote["ask"],
                "mid": quote["mid"],
                "is_atm": True
            }
        else:
            # Use contract data if no quote
            return {
                "symbol": symbol,
                "strike": strike,
                "type": option_type,
                "expiry": expiry,
                "bid": 0,
                "ask": 0,
                "mid": 0,
                "is_atm": True
            }
    
    return None

def find_itm_contract(price, trend, expiry):
    """
    Find the best ITM contract with delta 0.70-0.88.
    ITM Call: strike < price (deeper = higher delta)
    ITM Put: strike > price (deeper = higher delta)
    """
    option_type = "call" if trend == "BULL" else "put"
    
    if trend == "BULL":
        # ITM Call: strike below price. Delta ~0.70-0.88 ≈ $3-8 ITM
        strike_min = round(price - 10, 0)
        strike_max = round(price - 3, 0)
    else:
        # ITM Put: strike above price
        strike_min = round(price + 3, 0)
        strike_max = round(price + 10, 0)
    
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        logger.warning(f"[V8] No ITM contracts found for {expiry} {option_type}")
        return None
    
    # For 0DTE ITM, delta is approximately:
    # $3 ITM ≈ 0.70 delta
    # $5 ITM ≈ 0.80 delta
    # $8 ITM ≈ 0.88 delta
    # We want $4-7 ITM for delta 0.70-0.88
    
    best = None
    best_score = -1
    
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        
        if trend == "BULL":
            itm_amount = price - strike
        else:
            itm_amount = strike - price
        
        if itm_amount < 3 or itm_amount > 10:
            continue
        
        # Approximate delta
        approx_delta = min(0.95, 0.50 + itm_amount * 0.05)
        
        if ITM_DELTA_MIN <= approx_delta <= ITM_DELTA_MAX:
            # Get quote
            symbol = c.get("symbol", "")
            quote = get_option_quote(symbol)
            
            if quote and quote["mid"] > 0:
                # Prefer contracts with good volume (check via spread)
                spread_pct = (quote["ask"] - quote["bid"]) / quote["mid"] if quote["mid"] > 0 else 1
                
                # Score: prefer tighter spread and delta closer to 0.80
                delta_score = 1 - abs(approx_delta - 0.80) * 5
                spread_score = max(0, 1 - spread_pct * 5)
                score = delta_score + spread_score
                
                if score > best_score:
                    best_score = score
                    best = {
                        "symbol": symbol,
                        "strike": strike,
                        "type": option_type,
                        "expiry": expiry,
                        "bid": quote["bid"],
                        "ask": quote["ask"],
                        "mid": quote["mid"],
                        "approx_delta": round(approx_delta, 2),
                        "itm_amount": round(itm_amount, 2),
                        "is_itm": True
                    }
    
    return best

# ──────────────────────────────────────────────────────────────────────────────
# Layer 1: ATM Scalp Engine
# ──────────────────────────────────────────────────────────────────────────────

def execute_atm_scalp(trend, price, expiry):
    """
    Execute Layer 1: Buy 2 ATM contracts.
    Set TP1 +5%, TP2 +10%, SL -25%.
    """
    contract = find_atm_contract(price, trend, expiry)
    if not contract:
        return False, "No ATM contract found"
    
    symbol = contract["symbol"]
    mid = contract["mid"]
    
    if mid <= 0:
        return False, f"No valid price for {symbol}"
    
    # Use limit order at mid price
    order = place_option_order(
        symbol=symbol,
        qty=ATM_CONTRACTS,
        side="buy",
        order_type="limit",
        limit_price=mid,
        position_intent="buy_to_open"
    )
    
    if not order:
        # Retry with ask price
        order = place_option_order(
            symbol=symbol,
            qty=ATM_CONTRACTS,
            side="buy",
            order_type="limit",
            limit_price=contract["ask"],
            position_intent="buy_to_open"
        )
    
    if not order:
        return False, "Order failed"
    
    position = {
        "order_id": order.get("id"),
        "symbol": symbol,
        "strike": contract["strike"],
        "type": contract["type"],
        "qty": ATM_CONTRACTS,
        "entry_price": mid,
        "tp1_price": round(mid * (1 + ATM_TP1_PCT), 2),
        "tp2_price": round(mid * (1 + ATM_TP2_PCT), 2),
        "sl_price": round(mid * (1 - ATM_SL_PCT), 2),
        "reinforce_price": round(mid * (1 - ATM_REINFORCE_PCT), 2),
        "entry_time": _et_now().strftime("%I:%M:%S %p"),
        "trend": trend,
        "status": "open",
        "qty_sold": 0,
        "reinforced": False,
        "avg_price": mid,
        "total_qty": ATM_CONTRACTS,
        "pnl": 0.0
    }
    
    _state["atm_positions"].append(position)
    _state["atm_trade_count"] += 1
    _state["atm_reinforced"] = False
    
    direction = "CALL" if trend == "BULL" else "PUT"
    msg = (
        f"🤖 <b>V8 سكالب — {direction}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📥 شراء {ATM_CONTRACTS} عقود ATM\n"
        f"📋 {symbol}\n"
        f"💵 السعر: ${mid:.2f}\n"
        f"🎯 TP1: ${position['tp1_price']:.2f} (+5%)\n"
        f"🎯 TP2: ${position['tp2_price']:.2f} (+10%)\n"
        f"🛑 SL: ${position['sl_price']:.2f} (-25%)\n"
        f"🔄 تعزيز: ${position['reinforce_price']:.2f} (-15%)\n"
        f"📊 TSLA: ${price:.2f} | Trend: {trend}\n"
        f"🕐 {position['entry_time']} ET"
    )
    send_telegram(msg)
    logger.info(f"[V8 ATM] Opened: {symbol} x{ATM_CONTRACTS} @ ${mid:.2f}")
    
    return True, position

def monitor_atm_positions():
    """Monitor ATM positions for TP/SL/Reinforce."""
    if not _state["atm_positions"]:
        return
    
    for pos in list(_state["atm_positions"]):
        if pos["status"] != "open":
            continue
        
        symbol = pos["symbol"]
        quote = get_option_quote(symbol)
        if not quote or quote["mid"] <= 0:
            continue
        
        current_mid = quote["mid"]
        avg_price = pos["avg_price"]
        remaining_qty = pos["total_qty"] - pos["qty_sold"]
        
        if remaining_qty <= 0:
            pos["status"] = "closed"
            continue
        
        # ── Check Stop Loss ──
        if current_mid <= pos["sl_price"]:
            # Sell everything
            sell_order = place_option_order(
                symbol=symbol,
                qty=remaining_qty,
                side="sell",
                order_type="market",
                position_intent="sell_to_close"
            )
            
            pnl = (current_mid - avg_price) * remaining_qty * 100
            pos["pnl"] = pnl
            pos["status"] = "closed_sl"
            _state["daily_pnl"] += pnl
            _state["total_pnl"] += pnl
            _state["consecutive_losses"] += 1
            _state["losses"] += 1
            
            msg = (
                f"🔴 <b>V8 ستوب لوس — ATM</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📋 {symbol}\n"
                f"📥 دخول: ${avg_price:.2f}\n"
                f"📤 خروج: ${current_mid:.2f}\n"
                f"💸 P&L: <b>${pnl:+.2f}</b>\n"
                f"🕐 {_et_now().strftime('%I:%M %p')} ET"
            )
            send_telegram(msg)
            _record_trade(pos, "SL", pnl)
            
            # Check pause
            if _state["consecutive_losses"] >= PAUSE_AFTER_CONSECUTIVE_LOSSES:
                _state["pause_until"] = time.time() + PAUSE_DURATION_SECONDS
                send_telegram(
                    f"⏸️ <b>V8 وقف مؤقت — {PAUSE_AFTER_CONSECUTIVE_LOSSES} خسارات متتالية</b>\n"
                    f"يستأنف بعد 30 دقيقة"
                )
            continue
        
        # ── Check Reinforce (-15%) ──
        if (not pos["reinforced"] and 
            current_mid <= pos["reinforce_price"] and
            _state["trend"] == pos["trend"]):  # Trend still valid
            
            # Check if zone is safe
            safe, reason = is_safe_zone(_state["current_price"], pos["trend"])
            if safe:
                reinforce_order = place_option_order(
                    symbol=symbol,
                    qty=ATM_CONTRACTS,
                    side="buy",
                    order_type="limit",
                    limit_price=current_mid,
                    position_intent="buy_to_open"
                )
                
                if reinforce_order:
                    # Update average price
                    old_total = avg_price * pos["total_qty"]
                    new_total = old_total + current_mid * ATM_CONTRACTS
                    pos["total_qty"] += ATM_CONTRACTS
                    pos["avg_price"] = round(new_total / pos["total_qty"], 2)
                    pos["reinforced"] = True
                    pos["sl_price"] = round(pos["avg_price"] * (1 - ATM_SL_PCT), 2)
                    pos["tp1_price"] = round(pos["avg_price"] * (1 + ATM_REINFORCE_TP), 2)
                    pos["tp2_price"] = pos["tp1_price"]  # After reinforce, sell all at +5%
                    _state["atm_reinforced"] = True
                    
                    msg = (
                        f"🔄 <b>V8 تعزيز — ATM</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"📋 {symbol}\n"
                        f"📥 تعزيز: +{ATM_CONTRACTS} عقود @ ${current_mid:.2f}\n"
                        f"📊 متوسط جديد: ${pos['avg_price']:.2f}\n"
                        f"🎯 هدف جديد: ${pos['tp1_price']:.2f} (+5%)\n"
                        f"🛑 ستوب جديد: ${pos['sl_price']:.2f}\n"
                        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                    )
                    send_telegram(msg)
            continue
        
        # ── Check TP1 (+5%) — sell first contract ──
        if pos["qty_sold"] == 0 and current_mid >= pos["tp1_price"]:
            sell_qty = 1
            sell_order = place_option_order(
                symbol=symbol,
                qty=sell_qty,
                side="sell",
                order_type="market",
                position_intent="sell_to_close"
            )
            
            if sell_order:
                pnl1 = (current_mid - avg_price) * sell_qty * 100
                pos["qty_sold"] += sell_qty
                pos["pnl"] += pnl1
                _state["daily_pnl"] += pnl1
                _state["total_pnl"] += pnl1
                
                msg = (
                    f"💰 <b>V8 TP1 — ATM</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {symbol}\n"
                    f"📤 بيع 1 عقد @ ${current_mid:.2f} (+5%)\n"
                    f"💵 ربح: ${pnl1:+.2f}\n"
                    f"📊 باقي: {pos['total_qty'] - pos['qty_sold']} عقود\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
        
        # ── Check TP2 (+10%) — sell remaining ──
        remaining = pos["total_qty"] - pos["qty_sold"]
        if remaining > 0 and current_mid >= pos["tp2_price"]:
            sell_order = place_option_order(
                symbol=symbol,
                qty=remaining,
                side="sell",
                order_type="market",
                position_intent="sell_to_close"
            )
            
            if sell_order:
                pnl2 = (current_mid - avg_price) * remaining * 100
                pos["qty_sold"] += remaining
                pos["pnl"] += pnl2
                pos["status"] = "closed_tp"
                _state["daily_pnl"] += pnl2
                _state["total_pnl"] += pnl2
                _state["consecutive_losses"] = 0
                _state["wins"] += 1
                
                msg = (
                    f"🎯 <b>V8 TP2 — ATM هدف كامل!</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {symbol}\n"
                    f"📤 بيع {remaining} عقود @ ${current_mid:.2f} (+10%)\n"
                    f"💵 إجمالي الربح: ${pos['pnl']:+.2f}\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
                _record_trade(pos, "TP", pos["pnl"])

# ──────────────────────────────────────────────────────────────────────────────
# Layer 2: ITM Pullback Engine
# ──────────────────────────────────────────────────────────────────────────────

def execute_itm_pullback(trend, price, expiry):
    """
    Execute Layer 2: Place limit buy for ITM contract below pullback.
    """
    contract = find_itm_contract(price, trend, expiry)
    if not contract:
        return False, "No ITM contract found"
    
    symbol = contract["symbol"]
    mid = contract["mid"]
    
    if mid <= 0:
        return False, f"No valid price for {symbol}"
    
    # Place limit order BELOW current mid by 1-2%
    limit_price = round(mid * (1 - ITM_ENTRY_BELOW), 2)
    
    order = place_option_order(
        symbol=symbol,
        qty=ITM_CONTRACTS,
        side="buy",
        order_type="limit",
        limit_price=limit_price,
        position_intent="buy_to_open"
    )
    
    if not order:
        return False, "ITM order failed"
    
    pending = {
        "order_id": order.get("id"),
        "symbol": symbol,
        "strike": contract["strike"],
        "type": contract["type"],
        "qty": ITM_CONTRACTS,
        "limit_price": limit_price,
        "current_mid": mid,
        "tp_price": round(limit_price * (1 + ITM_TP_PCT), 2),
        "sl_price": round(limit_price * (1 - ITM_SL_PCT), 2),
        "entry_time": _et_now().strftime("%I:%M:%S %p"),
        "trend": trend,
        "approx_delta": contract.get("approx_delta", 0),
        "status": "pending",
        "pnl": 0.0
    }
    
    _state["itm_pending_order"] = pending
    
    direction = "CALL" if trend == "BULL" else "PUT"
    msg = (
        f"🎯 <b>V8 ITM أمر معلّق — {direction}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 {symbol}\n"
        f"💵 أمر شراء: ${limit_price:.2f} (تحت السوق {ITM_ENTRY_BELOW*100:.0f}%)\n"
        f"📊 Delta: ~{contract.get('approx_delta', 0):.2f}\n"
        f"🎯 TP: ${pending['tp_price']:.2f} (+40%)\n"
        f"🛑 SL: ${pending['sl_price']:.2f} (-16%)\n"
        f"📊 TSLA: ${price:.2f}\n"
        f"🕐 {pending['entry_time']} ET"
    )
    send_telegram(msg)
    logger.info(f"[V8 ITM] Pending: {symbol} limit ${limit_price:.2f}")
    
    return True, pending

def monitor_itm_position():
    """Monitor ITM pending order and open position."""
    
    # ── Check pending order ──
    pending = _state["itm_pending_order"]
    if pending and pending["status"] == "pending":
        order = get_order_status(pending["order_id"])
        if order:
            status = order.get("status", "")
            if status == "filled":
                # Order filled! Convert to active position
                fill_price = float(order.get("filled_avg_price", pending["limit_price"]))
                pending["status"] = "filled"
                pending["entry_price"] = fill_price
                pending["tp_price"] = round(fill_price * (1 + ITM_TP_PCT), 2)
                pending["sl_price"] = round(fill_price * (1 - ITM_SL_PCT), 2)
                
                _state["itm_position"] = pending
                _state["itm_pending_order"] = None
                _state["itm_trade_count"] += 1
                
                msg = (
                    f"✅ <b>V8 ITM — أمر نُفِّذ!</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pending['symbol']}\n"
                    f"📥 دخول: ${fill_price:.2f}\n"
                    f"🎯 TP: ${pending['tp_price']:.2f} (+40%)\n"
                    f"🛑 SL: ${pending['sl_price']:.2f} (-16%)\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            elif status in ("cancelled", "expired", "rejected"):
                _state["itm_pending_order"] = None
                logger.info(f"[V8 ITM] Pending order {status}")
        
        # Cancel if trend reversed
        if pending and _state["trend"] != pending["trend"]:
            cancel_order(pending["order_id"])
            _state["itm_pending_order"] = None
            logger.info("[V8 ITM] Cancelled pending — trend reversed")
    
    # ── Monitor active ITM position ──
    pos = _state["itm_position"]
    if not pos or pos["status"] != "filled":
        return
    
    quote = get_option_quote(pos["symbol"])
    if not quote or quote["mid"] <= 0:
        return
    
    current_mid = quote["mid"]
    entry_price = pos["entry_price"]
    
    # ── Stop Loss ──
    if current_mid <= pos["sl_price"]:
        sell_order = place_option_order(
            symbol=pos["symbol"],
            qty=ITM_CONTRACTS,
            side="sell",
            order_type="market",
            position_intent="sell_to_close"
        )
        
        pnl = (current_mid - entry_price) * ITM_CONTRACTS * 100
        pos["pnl"] = pnl
        pos["status"] = "closed_sl"
        _state["daily_pnl"] += pnl
        _state["total_pnl"] += pnl
        _state["consecutive_losses"] += 1
        _state["losses"] += 1
        _state["itm_position"] = None
        
        msg = (
            f"🔴 <b>V8 ستوب — ITM</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📋 {pos['symbol']}\n"
            f"📥 دخول: ${entry_price:.2f}\n"
            f"📤 خروج: ${current_mid:.2f}\n"
            f"💸 P&L: <b>${pnl:+.2f}</b>\n"
            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
        )
        send_telegram(msg)
        _record_trade(pos, "SL", pnl)
        return
    
    # ── Take Profit ──
    if current_mid >= pos["tp_price"]:
        sell_order = place_option_order(
            symbol=pos["symbol"],
            qty=ITM_CONTRACTS,
            side="sell",
            order_type="market",
            position_intent="sell_to_close"
        )
        
        pnl = (current_mid - entry_price) * ITM_CONTRACTS * 100
        pos["pnl"] = pnl
        pos["status"] = "closed_tp"
        _state["daily_pnl"] += pnl
        _state["total_pnl"] += pnl
        _state["consecutive_losses"] = 0
        _state["wins"] += 1
        _state["itm_position"] = None
        
        msg = (
            f"🎯 <b>V8 هدف — ITM +40%!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📋 {pos['symbol']}\n"
            f"📥 دخول: ${entry_price:.2f}\n"
            f"📤 خروج: ${current_mid:.2f}\n"
            f"💰 P&L: <b>${pnl:+.2f}</b>\n"
            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
        )
        send_telegram(msg)
        _record_trade(pos, "TP", pnl)

# ──────────────────────────────────────────────────────────────────────────────
# Risk Manager
# ──────────────────────────────────────────────────────────────────────────────

def check_risk():
    """
    Check all risk conditions.
    Returns: (can_trade: bool, reason: str)
    """
    # Portfolio loss limit
    account = get_account()
    if account:
        portfolio_value = float(account.get("portfolio_value", 0))
        if _state["portfolio_start_value"] > 0:
            loss = _state["portfolio_start_value"] - portfolio_value
            if loss >= MAX_PORTFOLIO_LOSS:
                _state["stopped_permanent"] = True
                send_telegram(
                    f"🚨 <b>V8 إيقاف نهائي!</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"خسارة المحفظة وصلت ${loss:,.0f}\n"
                    f"الحد: ${MAX_PORTFOLIO_LOSS:,.0f}\n"
                    f"البوت متوقف حتى إشعار آخر"
                )
                return False, f"Portfolio loss ${loss:,.0f} >= ${MAX_PORTFOLIO_LOSS:,.0f}"
    
    # Permanent stop
    if _state["stopped_permanent"]:
        return False, "Stopped permanently"
    
    # Daily stop
    if _state["stopped_for_day"]:
        return False, "Stopped for today"
    
    # Pause after consecutive losses
    if time.time() < _state["pause_until"]:
        remaining = (_state["pause_until"] - time.time()) / 60
        return False, f"Paused — {remaining:.0f} min remaining"
    
    return True, "OK"

# ──────────────────────────────────────────────────────────────────────────────
# Trade Recorder
# ──────────────────────────────────────────────────────────────────────────────

def _record_trade(pos, exit_type, pnl):
    """Record trade for daily summary."""
    _state["trades_today"].append({
        "symbol": pos.get("symbol", ""),
        "type": pos.get("type", ""),
        "entry_price": pos.get("entry_price", pos.get("avg_price", 0)),
        "exit_type": exit_type,
        "pnl": pnl,
        "time": _et_now().strftime("%I:%M %p"),
        "layer": "ATM" if pos.get("is_atm") or not pos.get("approx_delta") else "ITM"
    })

# ──────────────────────────────────────────────────────────────────────────────
# Force Close All
# ──────────────────────────────────────────────────────────────────────────────

def force_close_all():
    """Force close all open positions at end of window."""
    closed_count = 0
    total_pnl = 0
    
    # Close ATM positions
    for pos in _state["atm_positions"]:
        if pos["status"] == "open":
            remaining = pos["total_qty"] - pos["qty_sold"]
            if remaining > 0:
                quote = get_option_quote(pos["symbol"])
                mid = quote["mid"] if quote else 0
                
                sell_order = place_option_order(
                    symbol=pos["symbol"],
                    qty=remaining,
                    side="sell",
                    order_type="market",
                    position_intent="sell_to_close"
                )
                
                pnl = (mid - pos["avg_price"]) * remaining * 100 if mid > 0 else 0
                pos["pnl"] += pnl
                pos["status"] = "closed_eod"
                _state["daily_pnl"] += pnl
                _state["total_pnl"] += pnl
                total_pnl += pnl
                closed_count += 1
                _record_trade(pos, "EOD", pnl)
    
    # Close ITM position
    pos = _state["itm_position"]
    if pos and pos["status"] == "filled":
        quote = get_option_quote(pos["symbol"])
        mid = quote["mid"] if quote else 0
        
        sell_order = place_option_order(
            symbol=pos["symbol"],
            qty=ITM_CONTRACTS,
            side="sell",
            order_type="market",
            position_intent="sell_to_close"
        )
        
        pnl = (mid - pos["entry_price"]) * ITM_CONTRACTS * 100 if mid > 0 else 0
        pos["pnl"] = pnl
        pos["status"] = "closed_eod"
        _state["daily_pnl"] += pnl
        _state["total_pnl"] += pnl
        total_pnl += pnl
        closed_count += 1
        _state["itm_position"] = None
        _record_trade(pos, "EOD", pnl)
    
    # Cancel pending ITM order
    if _state["itm_pending_order"]:
        cancel_order(_state["itm_pending_order"]["order_id"])
        _state["itm_pending_order"] = None
    
    if closed_count > 0:
        msg = (
            f"⏰ <b>V8 إغلاق نهاية النافذة</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📤 أغلق {closed_count} صفقات\n"
            f"💵 P&L: <b>${total_pnl:+.2f}</b>\n"
            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
        )
        send_telegram(msg)
    
    return closed_count

# ──────────────────────────────────────────────────────────────────────────────
# Daily Summary
# ──────────────────────────────────────────────────────────────────────────────

def send_daily_summary():
    """Send end-of-day summary to Telegram."""
    trades = _state["trades_today"]
    wins = _state["wins"]
    losses = _state["losses"]
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    
    atm_trades = [t for t in trades if t["layer"] == "ATM"]
    itm_trades = [t for t in trades if t["layer"] == "ITM"]
    
    atm_pnl = sum(t["pnl"] for t in atm_trades)
    itm_pnl = sum(t["pnl"] for t in itm_trades)
    
    pnl_icon = "💚" if _state["daily_pnl"] >= 0 else "🔴"
    
    msg = (
        f"📊 <b>V8 ملخص اليوم</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{pnl_icon} <b>P&L: ${_state['daily_pnl']:+.2f}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 صفقات: {total} (✅ {wins} | ❌ {losses})\n"
        f"📊 Win Rate: {win_rate:.0f}%\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔵 ATM: {len(atm_trades)} صفقات | ${atm_pnl:+.2f}\n"
        f"🟡 ITM: {len(itm_trades)} صفقات | ${itm_pnl:+.2f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 إجمالي P&L (كلي): ${_state['total_pnl']:+.2f}\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)

# ──────────────────────────────────────────────────────────────────────────────
# Main Engine Loop
# ──────────────────────────────────────────────────────────────────────────────

def _reset_daily():
    """Reset daily state."""
    today = _today_str()
    if _state["today"] != today:
        _state["today"] = today
        _state["atm_positions"] = []
        _state["atm_trade_count"] = 0
        _state["atm_reinforced"] = False
        _state["itm_position"] = None
        _state["itm_pending_order"] = None
        _state["itm_trade_count"] = 0
        _state["daily_pnl"] = 0.0
        _state["consecutive_losses"] = 0
        _state["pause_until"] = 0
        _state["stopped_for_day"] = False
        _state["trades_today"] = []
        _state["wins"] = 0
        _state["losses"] = 0
        
        # Get starting portfolio value
        account = get_account()
        if account:
            _state["portfolio_start_value"] = float(account.get("portfolio_value", 0))
            _state["portfolio_current_value"] = _state["portfolio_start_value"]
        
        logger.info(f"[V8] Daily reset for {today} | Portfolio: ${_state['portfolio_start_value']:,.2f}")

def _has_open_atm():
    """Check if there's an active ATM position."""
    return any(p["status"] == "open" for p in _state["atm_positions"])

def engine_loop():
    """
    Main engine loop — runs every 30 seconds.
    """
    logger.info("[V8] Options Scalper Engine started! 🚀")
    _state["running"] = True
    _summary_sent = False
    _summary_date = ""
    
    # Initialize portfolio value
    account = get_account()
    if account:
        _state["portfolio_start_value"] = float(account.get("portfolio_value", 0))
        logger.info(f"[V8] Initial portfolio: ${_state['portfolio_start_value']:,.2f}")
    
    while _state["running"]:
        try:
            _reset_daily()
            
            now = _et_now()
            today = _today_str()
            
            # Reset summary flag for new day
            if _summary_date != today:
                _summary_sent = False
                _summary_date = today
            
            # Skip weekends
            if now.weekday() >= 5:
                time.sleep(60)
                continue
            
            # Skip non-0DTE days (user wants Mon-Thu only)
            if not _is_0dte_day():
                time.sleep(60)
                continue
            
            # ── Force Close Time ──
            if _is_force_close_time():
                if _has_open_atm() or _state["itm_position"]:
                    force_close_all()
                
                # Send daily summary once
                if not _summary_sent and _state["trades_today"]:
                    send_daily_summary()
                    _summary_sent = True
                
                time.sleep(60)
                continue
            
            # ── Outside Scalp Window ──
            if not _is_scalp_window():
                # Still monitor existing positions even outside window
                if _has_open_atm():
                    monitor_atm_positions()
                if _state["itm_position"] or _state["itm_pending_order"]:
                    monitor_itm_position()
                
                time.sleep(30)
                continue
            
            # ══════════════════════════════════════════════════════════════════
            # INSIDE SCALP WINDOW (10:10 - 12:40 ET)
            # ══════════════════════════════════════════════════════════════════
            
            # ── Risk Check ──
            can_trade, risk_reason = check_risk()
            if not can_trade:
                # Still monitor existing positions
                monitor_atm_positions()
                monitor_itm_position()
                logger.info(f"[V8] Can't trade: {risk_reason}")
                time.sleep(30)
                continue
            
            # ── Detect Trend ──
            trend, strength = detect_trend()
            _state["trend"] = trend
            _state["trend_strength"] = strength
            
            if not trend:
                # No clear trend — monitor existing only
                monitor_atm_positions()
                monitor_itm_position()
                logger.info("[V8] No clear trend — waiting")
                time.sleep(TREND_CHECK_INTERVAL)
                continue
            
            # ── Check Zone Safety ──
            price = _state["current_price"]
            safe, zone_reason = is_safe_zone(price, trend)
            
            if not safe:
                monitor_atm_positions()
                monitor_itm_position()
                logger.info(f"[V8] Unsafe zone: {zone_reason}")
                time.sleep(TREND_CHECK_INTERVAL)
                continue
            
            expiry = _today_expiry()
            
            # ── Layer 1: ATM Scalp ──
            if not _has_open_atm():
                logger.info(f"[V8] Trend={trend} ({strength}) | Price=${price:.2f} | Entering ATM scalp")
                success, result = execute_atm_scalp(trend, price, expiry)
                if not success:
                    logger.warning(f"[V8] ATM entry failed: {result}")
            else:
                monitor_atm_positions()
                
                # Check if ATM position closed — can re-enter if trend still valid
                if not _has_open_atm() and trend == _state["trend"]:
                    logger.info(f"[V8] ATM closed, trend still {trend} — re-entering")
                    # Will re-enter next cycle
            
            # ── Layer 2: ITM Pullback ──
            if not _state["itm_position"] and not _state["itm_pending_order"]:
                is_pb, pb_depth = detect_pullback(trend)
                if is_pb:
                    logger.info(f"[V8] Pullback detected ({pb_depth*100:.1f}%) — placing ITM order")
                    success, result = execute_itm_pullback(trend, price, expiry)
                    if not success:
                        logger.warning(f"[V8] ITM entry failed: {result}")
            else:
                monitor_itm_position()
            
            # ── Sleep ──
            time.sleep(TREND_CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"[V8] Engine error: {e}", exc_info=True)
            time.sleep(30)

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

_engine_thread = None

def start_scalper():
    """Start the V8 Options Scalper engine in a background thread."""
    global _engine_thread
    if _engine_thread and _engine_thread.is_alive():
        logger.info("[V8] Engine already running")
        return
    
    _engine_thread = threading.Thread(target=engine_loop, daemon=True, name="V8_Scalper")
    _engine_thread.start()
    logger.info("[V8] Options Scalper Engine thread started! 🚀")

def stop_scalper():
    """Stop the engine."""
    _state["running"] = False
    logger.info("[V8] Engine stopping...")

def get_scalper_status():
    """Get current engine status for API."""
    return {
        "running": _state["running"],
        "trend": _state["trend"],
        "trend_strength": _state["trend_strength"],
        "current_price": _state["current_price"],
        "vwap": _state["vwap"],
        "atm_positions": len([p for p in _state["atm_positions"] if p["status"] == "open"]),
        "atm_trades_today": _state["atm_trade_count"],
        "itm_position": bool(_state["itm_position"]),
        "itm_pending": bool(_state["itm_pending_order"]),
        "itm_trades_today": _state["itm_trade_count"],
        "daily_pnl": _state["daily_pnl"],
        "total_pnl": _state["total_pnl"],
        "wins": _state["wins"],
        "losses": _state["losses"],
        "consecutive_losses": _state["consecutive_losses"],
        "paused_until": _state["pause_until"],
        "stopped_for_day": _state["stopped_for_day"],
        "stopped_permanent": _state["stopped_permanent"],
        "portfolio_start": _state["portfolio_start_value"],
        "trades_today": len(_state["trades_today"])
    }
