"""
ثاقب V8 — Options Scalper Engine (Enhanced)
=============================================
محرك تداول أوبشن تلقائي بالكامل — طبقتين:
  الطبقة 1: سكالبينج ATM (0DTE) — صفقات سريعة متكررة
  الطبقة 2: ITM Pullback — عقد عميق عند البولباك

تحسينات V8.1:
  - Multi-Timeframe: 15m (اتجاه عام) → 5m (ترند) → 1m (دخول)
  - Micro S/R: دعم/مقاومة صغيرة من شموع 5 دقائق
  - VWAP Danger Zone: لا تداول ±0.2% من VWAP
  - PDH/PDL: هاي/لو أمس كمستويات خطيرة
  - Psychological Levels: أرقام نفسية ($440, $445, $450...)
  - Opening Range: هاي/لو أول 40 دقيقة

Author: ثاقب V8.1
Date: May 2026
"""

import os
import time
import gc
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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8708530077:AAF16LsdHUNTW5G25UypCm8NiFTmCIranP8")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "975644160")

# ── Trading Window ───────────────────────────────────────────────────────────
SCALP_START_HOUR   = 9
SCALP_START_MINUTE = 35   # بعد 5 دقائق من فتح السوق
SCALP_END_HOUR     = 15
SCALP_END_MINUTE   = 45   # قبل الإغلاق بـ 15 دقيقة
FORCE_CLOSE_HOUR   = 15
FORCE_CLOSE_MINUTE = 50   # بيع كل شي المتبقي

# ── Layer 1: ATM Scalp (V9 — 3 Contracts Strategy) ─────────────────────────
ATM_CONTRACTS      = 3      # 3 عقود ATM
ATM_TP1_PCT        = 0.05   # +5% بيع C1 + C2 (عقدين)
ATM_TP2_PCT        = 0.10   # +10% بيع C3 (الـ Runner)
ATM_SL_PCT         = 0.10   # -10% ستوب للكل
ATM_REINFORCE_PCT  = 0.15   # غير مستخدم (احتياطي)
ATM_REINFORCE_TP   = 0.05   # غير مستخدم (احتياطي)
ATM_MAX_REINFORCE  = 0      # لا تعزيز في الاستراتيجية الجديدة

# ── Layer 2: ITM Manual Scalp (V9 — Web Interface) ──────────────────────────
ITM_CONTRACTS      = 1      # عقد واحد ITM
ITM_DELTA_MIN      = 0.60
ITM_DELTA_MAX      = 0.92
ITM_ENTRY_BELOW    = 0.015  # احتياطي
ITM_TP_DOLLARS     = 0.80   # +$0.80 جني أرباح (Market Order)
ITM_SL_DOLLARS     = 0.65   # -$0.65 بيع تلقائي (قبل التعزيز)
ITM_REINFORCE_TRIGGER = 0.35  # عند -$0.35 اشتري 2 عقد إضافي
ITM_REINFORCE_QTY  = 2      # عدد عقود التعزيز
ITM_REINFORCE_TP   = 0.80   # الكل يبيع عند +$0.80 من سعر التعزيز
ITM_REINFORCE_SL   = 0.40   # بعد التعزيز: إذا نزل -$0.40 من سعر التعزيز → أغلق الكل
ITM_ALERT1_DOLLARS = 0.20   # تنبيه 1 عند -$0.20
ITM_ALERT2_DOLLARS = 0.35   # تنبيه 2 عند -$0.35 (عند التعزيز)
ITM_TP_PCT         = 0.40   # احتياطي
ITM_SL_PCT         = 0.16   # احتياطي
ITM_MIN_VOLUME     = 100

# ── Risk Management ──────────────────────────────────────────────────────────
PORTFOLIO_START    = 99408.71
MAX_PORTFOLIO_LOSS = 7000.0
PAUSE_AFTER_CONSECUTIVE_LOSSES = 2
PAUSE_DURATION_SECONDS = 1800

# ── Trend Detection (Multi-Timeframe) ────────────────────────────────────────
TREND_CHECK_INTERVAL = 30
TREND_MIN_ADX        = 20

# ── Micro S/R ────────────────────────────────────────────────────────────────
MICRO_SR_LOOKBACK    = 10   # آخر 10 شموع 5 دقائق
MICRO_SR_PROXIMITY   = 0.003  # 0.3% = قريب جداً

# ── VWAP Danger Zone ─────────────────────────────────────────────────────────
VWAP_DANGER_PCT      = 0.002  # ±0.2% من VWAP = خطر

# ── Psychological Levels ─────────────────────────────────────────────────────
PSYCH_LEVEL_INTERVAL = 5.0    # كل $5 ($440, $445, $450...)
PSYCH_PROXIMITY      = 0.002  # 0.2% = قريب

# ── Opening Range ────────────────────────────────────────────────────────────
OR_END_MINUTE        = 10     # Opening Range ينتهي 10:10 AM (أول 40 دقيقة)

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
    "trend": None,
    "trend_strength": 0,
    "trend_15m": None,       # NEW: 15m trend direction
    "trend_5m": None,        # NEW: 5m trend direction
    "vwap": 0.0,
    "current_price": 0.0,
    "day_high": 0.0,
    "day_low": 0.0,
    
    # NEW: Enhanced Levels
    "pdh": 0.0,              # Previous Day High
    "pdl": 0.0,              # Previous Day Low
    "opening_range_high": 0.0,
    "opening_range_low": 0.0,
    "opening_range_set": False,
    "micro_resistances": [],  # list of prices
    "micro_supports": [],     # list of prices
    
    # Layer 1 (ATM Scalp)
    "atm_positions": [],
    "atm_trade_count": 0,
    "atm_reinforced": False,
    
    # Layer 2 (ITM Pullback)
    "itm_position": None,
    "itm_pending_order": None,
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
    global _reversal_map_ref
    _reversal_map_ref = ref

def set_gex_fn(fn):
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
    try:
        r = _session.get(f"{ALPACA_BASE_URL}/v2/account", headers=_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[V8] Account error: {e}")
    return None

def get_tsla_snapshot():
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

def get_spy_direction():
    """جلب اتجاه SPY الحالي — صاعد/هابط/محايد."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/SPY/bars",
            headers=_headers(),
            params={"timeframe": "5Min", "limit": 3, "feed": "iex"},
            timeout=10
        )
        if r.status_code == 200:
            bars = r.json().get("bars", [])
            if len(bars) >= 2:
                prev_close = float(bars[-2]["c"])
                curr_close = float(bars[-1]["c"])
                change_pct = (curr_close - prev_close) / prev_close * 100
                if change_pct > 0.05:
                    return "BULL", round(change_pct, 3)
                elif change_pct < -0.05:
                    return "BEAR", round(change_pct, 3)
                else:
                    return "FLAT", round(change_pct, 3)
    except Exception as e:
        logger.error(f"[SPY] Direction error: {e}")
    return "N/A", 0.0

def get_tsla_bars(timeframe="1Min", limit=20):
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
        logger.error(f"[V8] Bars error ({timeframe}): {e}")
    return []

def get_previous_day_bars():
    """Get previous day's daily bar for PDH/PDL."""
    try:
        now = _et_now()
        end = now.strftime("%Y-%m-%d")
        start = (now - timedelta(days=5)).strftime("%Y-%m-%d")
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/TSLA/bars",
            headers=_headers(),
            params={
                "timeframe": "1Day",
                "start": start,
                "end": end,
                "limit": 5,
                "feed": "iex",
                "sort": "desc"
            },
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            bars = data.get("bars", [])
            # First bar in desc order is most recent completed day
            if bars:
                return bars[0]
    except Exception as e:
        logger.error(f"[V8] Previous day bars error: {e}")
    return None

def get_options_chain(expiry_date, option_type="call", strike_min=None, strike_max=None):
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
    return datetime.now(timezone.utc) - timedelta(hours=4)

def _today_str():
    return _et_now().strftime("%Y-%m-%d")

def _today_expiry():
    return _today_str()

def _is_scalp_window():
    now = _et_now()
    mins = now.hour * 60 + now.minute
    start = SCALP_START_HOUR * 60 + SCALP_START_MINUTE
    end = SCALP_END_HOUR * 60 + SCALP_END_MINUTE
    return start <= mins < end

def _is_force_close_time():
    now = _et_now()
    mins = now.hour * 60 + now.minute
    close_mins = FORCE_CLOSE_HOUR * 60 + FORCE_CLOSE_MINUTE
    return mins >= close_mins

def _is_0dte_day():
    day = _et_now().weekday()
    return day < 5  # Mon-Fri (TSLA has daily 0DTE options)

# ──────────────────────────────────────────────────────────────────────────────
# EMA Calculator (shared utility)
# ──────────────────────────────────────────────────────────────────────────────

def _ema(data, period):
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    result = sum(data[:period]) / period
    for val in data[period:]:
        result = val * k + result * (1 - k)
    return result

def _adx_calc(highs, lows, closes, period=14):
    """Calculate ADX, +DI, -DI from price data."""
    if len(closes) < period + 1:
        return 0, 0, 0
    
    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(max(up_move, 0) if up_move > down_move else 0)
        minus_dm.append(max(down_move, 0) if down_move > up_move else 0)
    
    if len(tr_list) < period:
        return 0, 0, 0
    
    atr = sum(tr_list[-period:]) / period
    if atr <= 0:
        return 0, 0, 0
    
    plus_di = (sum(plus_dm[-period:]) / period) / atr * 100
    minus_di = (sum(minus_dm[-period:]) / period) / atr * 100
    di_sum = plus_di + minus_di
    dx = abs(plus_di - minus_di) / di_sum * 100 if di_sum > 0 else 0
    return dx, plus_di, minus_di

# ──────────────────────────────────────────────────────────────────────────────
# Option Symbol Builder
# ──────────────────────────────────────────────────────────────────────────────

def build_option_symbol(ticker, expiry_date, option_type, strike):
    dt = datetime.strptime(expiry_date, "%Y-%m-%d")
    date_part = dt.strftime("%y%m%d")
    type_char = "C" if option_type.upper() in ("CALL", "C") else "P"
    strike_int = int(round(strike * 1000))
    strike_part = f"{strike_int:08d}"
    return f"{ticker}{date_part}{type_char}{strike_part}"

# ══════════════════════════════════════════════════════════════════════════════
# NEW: Enhanced Level Detection
# ══════════════════════════════════════════════════════════════════════════════

def load_pdh_pdl():
    """Load Previous Day High/Low."""
    prev_bar = get_previous_day_bars()
    if prev_bar:
        _state["pdh"] = float(prev_bar.get("h", 0))
        _state["pdl"] = float(prev_bar.get("l", 0))
        logger.info(f"[V8 Levels] PDH=${_state['pdh']:.2f} | PDL=${_state['pdl']:.2f}")
    else:
        logger.warning("[V8 Levels] Could not load PDH/PDL")

def compute_opening_range():
    """
    Compute Opening Range from first 40 minutes (9:30-10:10).
    Uses 5-min bars from market open.
    """
    if _state["opening_range_set"]:
        return
    
    now = _et_now()
    if now.hour * 60 + now.minute < SCALP_START_HOUR * 60 + SCALP_START_MINUTE:
        return  # Not yet 10:10
    
    # Get 5-min bars covering 9:30-10:10 (8 bars)
    bars = get_tsla_bars("5Min", 12)
    if not bars:
        return
    
    # Filter bars from today's open (9:30 AM) to 10:10 AM
    or_high = 0
    or_low = float('inf')
    for b in bars:
        h = float(b.get("h", 0))
        l = float(b.get("l", 0))
        if h > or_high:
            or_high = h
        if l < or_low:
            or_low = l
    
    if or_high > 0 and or_low < float('inf'):
        _state["opening_range_high"] = or_high
        _state["opening_range_low"] = or_low
        _state["opening_range_set"] = True
        logger.info(f"[V8 Levels] Opening Range: High=${or_high:.2f} | Low=${or_low:.2f}")

def compute_micro_sr():
    """
    Compute Micro Support/Resistance from last 10 candles of 5-min chart.
    Looks for swing highs/lows and high-volume nodes.
    """
    bars = get_tsla_bars("5Min", MICRO_SR_LOOKBACK + 2)
    if not bars or len(bars) < 5:
        return
    
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    volumes = [int(b.get("v", 0)) for b in bars]
    closes = [float(b["c"]) for b in bars]
    
    resistances = []
    supports = []
    
    # Find swing highs (local maxima) and swing lows (local minima)
    for i in range(1, len(highs) - 1):
        # Swing high: higher than both neighbors
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            resistances.append(highs[i])
        # Swing low: lower than both neighbors
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            supports.append(lows[i])
    
    # Add high-volume candle levels (top 3 by volume)
    if volumes:
        avg_vol = sum(volumes) / len(volumes)
        for i, v in enumerate(volumes):
            if v > avg_vol * 1.5:  # 50% above average
                resistances.append(highs[i])
                supports.append(lows[i])
    
    # Add recent high/low as micro levels
    recent_high = max(highs[-5:]) if len(highs) >= 5 else max(highs)
    recent_low = min(lows[-5:]) if len(lows) >= 5 else min(lows)
    resistances.append(recent_high)
    supports.append(recent_low)
    
    # Deduplicate (merge levels within 0.2% of each other)
    def dedupe(levels):
        if not levels:
            return []
        levels = sorted(set(levels))
        result = [levels[0]]
        for lvl in levels[1:]:
            if abs(lvl - result[-1]) / result[-1] > 0.002:
                result.append(lvl)
            else:
                # Keep the average
                result[-1] = (result[-1] + lvl) / 2
        return result
    
    _state["micro_resistances"] = dedupe(resistances)
    _state["micro_supports"] = dedupe(supports)
    
    logger.info(f"[V8 Micro S/R] Resistances: {[f'${r:.2f}' for r in _state['micro_resistances']]} | "
                f"Supports: {[f'${s:.2f}' for s in _state['micro_supports']]}")

def get_nearest_psych_level(price):
    """Get nearest psychological level ($5 intervals)."""
    lower = math.floor(price / PSYCH_LEVEL_INTERVAL) * PSYCH_LEVEL_INTERVAL
    upper = lower + PSYCH_LEVEL_INTERVAL
    dist_lower = abs(price - lower) / price
    dist_upper = abs(price - upper) / price
    nearest = lower if dist_lower < dist_upper else upper
    nearest_dist = min(dist_lower, dist_upper)
    return nearest, nearest_dist

# ══════════════════════════════════════════════════════════════════════════════
# NEW: Multi-Timeframe Trend Detection
# ══════════════════════════════════════════════════════════════════════════════

def _analyze_timeframe(timeframe, bar_count):
    """
    Analyze a single timeframe for trend direction.
    Returns: ("BULL", score) / ("BEAR", score) / (None, 0)
    """
    bars = get_tsla_bars(timeframe, bar_count)
    if not bars or len(bars) < 10:
        return None, 0
    
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, min(21, len(closes)))
    current = closes[-1]
    
    # Momentum: last 5 bars
    last5 = closes[-5:]
    up_bars = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
    down_bars = sum(1 for i in range(1, len(last5)) if last5[i] < last5[i-1])
    
    # ADX
    adx, plus_di, minus_di = _adx_calc(highs, lows, closes)
    
    bull_score = 0
    bear_score = 0
    
    # EMA crossover (30 pts)
    if ema9 > ema21:
        bull_score += 30
    elif ema9 < ema21:
        bear_score += 30
    
    # Momentum (25 pts)
    if up_bars >= 4:
        bull_score += 25
    elif up_bars >= 3:
        bull_score += 15
    elif down_bars >= 4:
        bear_score += 25
    elif down_bars >= 3:
        bear_score += 15
    
    # ADX direction (25 pts)
    if adx >= TREND_MIN_ADX:
        if plus_di > minus_di:
            bull_score += 25
        else:
            bear_score += 25
    
    # Price vs EMA21 (20 pts)
    if current > ema21 * 1.001:
        bull_score += 20
    elif current < ema21 * 0.999:
        bear_score += 20
    
    strength = max(bull_score, bear_score)
    if bull_score >= 55 and bull_score > bear_score + 10:
        return "BULL", strength
    elif bear_score >= 55 and bear_score > bull_score + 10:
        return "BEAR", strength
    return None, strength

def detect_trend_multi():
    """
    Multi-Timeframe Trend Detection:
      15m → الاتجاه العام (must agree)
      5m  → الترند المتوسط (primary signal)
      1m  → توقيت الدخول (confirmation)
    
    Returns: (trend, strength, details)
    """
    # ── 15-minute: Big picture ──
    trend_15m, str_15m = _analyze_timeframe("15Min", 20)
    _state["trend_15m"] = trend_15m
    
    # ── 5-minute: Primary trend ──
    trend_5m, str_5m = _analyze_timeframe("5Min", 20)
    _state["trend_5m"] = trend_5m
    
    # ── 1-minute: Entry timing ──
    trend_1m, str_1m = _analyze_timeframe("1Min", 30)
    
    # ── VWAP confirmation ──
    snap = get_tsla_snapshot()
    vwap = 0
    if snap:
        vwap = snap.get("vwap", 0)
        _state["vwap"] = vwap
        _state["current_price"] = snap["price"]
        _state["day_high"] = snap.get("high", 0)
        _state["day_low"] = snap.get("low", 0)
    
    price = _state["current_price"]
    vwap_bull = price > vwap if vwap > 0 else True
    vwap_bear = price < vwap if vwap > 0 else True
    
    # ── Multi-TF Agreement Logic ──
    # Rule: 15m and 5m MUST agree. 1m is bonus.
    
    final_trend = None
    total_strength = 0
    
    if trend_15m == "BULL" and trend_5m == "BULL" and vwap_bull:
        final_trend = "BULL"
        total_strength = str_15m + str_5m + (str_1m if trend_1m == "BULL" else 0)
    elif trend_15m == "BEAR" and trend_5m == "BEAR" and vwap_bear:
        final_trend = "BEAR"
        total_strength = str_15m + str_5m + (str_1m if trend_1m == "BEAR" else 0)
    elif trend_5m and trend_5m == trend_1m:
        # 5m + 1m agree but 15m neutral — weaker signal, still tradeable
        if (trend_5m == "BULL" and vwap_bull) or (trend_5m == "BEAR" and vwap_bear):
            final_trend = trend_5m
            total_strength = str_5m + str_1m  # Lower strength (no 15m bonus)
    
    details = {
        "15m": trend_15m or "CHOP",
        "5m": trend_5m or "CHOP",
        "1m": trend_1m or "CHOP",
        "vwap": f"${vwap:.2f}",
        "price": f"${price:.2f}",
        "vwap_side": "above" if vwap_bull else "below"
    }
    
    logger.info(f"[V8 MTF] 15m={details['15m']}({str_15m}) | 5m={details['5m']}({str_5m}) | "
                f"1m={details['1m']}({str_1m}) | VWAP={details['vwap']} ({details['vwap_side']}) | "
                f"→ {final_trend or 'NO TREND'} ({total_strength})")
    
    return final_trend, total_strength, details

# ══════════════════════════════════════════════════════════════════════════════
# NEW: Enhanced Pullback Detection (5-min based)
# ══════════════════════════════════════════════════════════════════════════════

def detect_pullback(trend):
    """
    Detect pullback on 5-minute chart (more reliable than 1-min).
    Also checks RSI for oversold/overbought confirmation.
    Returns: (is_pullback: bool, pullback_depth_pct: float)
    """
    bars = get_tsla_bars("5Min", 12)
    if not bars or len(bars) < 5:
        return False, 0
    
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    current = closes[-1]
    
    # ── RSI (14-period on 5min) ──
    gains = []
    losses_list = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses_list.append(abs(min(change, 0)))
    
    period = min(14, len(gains))
    if period > 0:
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses_list[-period:]) / period
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        else:
            rsi = 100
    else:
        rsi = 50
    
    # ── EMA 9 on 5min ──
    ema9 = _ema(closes, 9)
    
    if trend == "BULL":
        recent_high = max(highs[-6:])
        depth = (recent_high - current) / recent_high if recent_high > 0 else 0
        
        # Pullback conditions on 5min:
        # 1. Price dipped 0.3-1.0% from recent high
        # 2. RSI dropped below 45 (from overbought)
        # 3. Price near or touching EMA9
        near_ema9 = abs(current - ema9) / current < 0.003  # within 0.3% of EMA9
        
        is_pullback = (
            0.003 <= depth <= 0.010 and
            (rsi < 45 or near_ema9) and
            current > _ema(closes, min(21, len(closes)))  # Still above EMA21
        )
        
        logger.info(f"[V8 PB] BULL pullback check: depth={depth*100:.2f}% rsi={rsi:.1f} "
                    f"near_ema9={near_ema9} → {is_pullback}")
        return is_pullback, depth
    
    elif trend == "BEAR":
        recent_low = min(lows[-6:])
        depth = (current - recent_low) / recent_low if recent_low > 0 else 0
        
        near_ema9 = abs(current - ema9) / current < 0.003
        
        is_pullback = (
            0.003 <= depth <= 0.010 and
            (rsi > 55 or near_ema9) and
            current < _ema(closes, min(21, len(closes)))
        )
        
        logger.info(f"[V8 PB] BEAR pullback check: depth={depth*100:.2f}% rsi={rsi:.1f} "
                    f"near_ema9={near_ema9} → {is_pullback}")
        return is_pullback, depth
    
    return False, 0

# ══════════════════════════════════════════════════════════════════════════════
# NEW: Enhanced Zone Safety (All Levels)
# ══════════════════════════════════════════════════════════════════════════════

def is_safe_zone(price, trend):
    """
    Comprehensive safety check against ALL levels:
    1. GEX levels (reversal map)
    2. Micro S/R (5-min swing points)
    3. VWAP danger zone
    4. PDH/PDL
    5. Psychological levels
    6. Opening Range boundaries
    
    Returns: (is_safe: bool, reason: str)
    """
    dangers = []
    
    # ── 1. GEX / Reversal Map Levels ──
    if _reversal_map_ref and _reversal_map_ref.get("built"):
        levels = _reversal_map_ref.get("levels", [])
        for lvl in levels:
            lvl_price = float(lvl["price"])
            dist_pct = abs(price - lvl_price) / lvl_price
            if dist_pct <= 0.003:
                lvl_type = lvl["type"]
                lvl_name = lvl["name"]
                if trend == "BULL" and lvl_type == "resistance":
                    dangers.append(f"GEX مقاومة {lvl_name} ${lvl_price:.2f}")
                elif trend == "BEAR" and lvl_type == "support":
                    dangers.append(f"GEX دعم {lvl_name} ${lvl_price:.2f}")
                elif "Gamma" in lvl_name or "Flip" in lvl_name:
                    dangers.append(f"Gamma Flip ${lvl_price:.2f}")
    
    # ── 2. Micro S/R (5-min) ──
    if trend == "BULL":
        for r in _state["micro_resistances"]:
            dist = abs(price - r) / price
            if dist <= MICRO_SR_PROXIMITY and price < r:
                dangers.append(f"Micro مقاومة ${r:.2f}")
                break
    elif trend == "BEAR":
        for s in _state["micro_supports"]:
            dist = abs(price - s) / price
            if dist <= MICRO_SR_PROXIMITY and price > s:
                dangers.append(f"Micro دعم ${s:.2f}")
                break
    
    # ── 3. VWAP Danger Zone ──
    vwap = _state["vwap"]
    if vwap > 0:
        vwap_dist = abs(price - vwap) / price
        if vwap_dist <= VWAP_DANGER_PCT:
            dangers.append(f"VWAP Zone ${vwap:.2f} (±0.2%)")
    
    # ── 4. PDH / PDL ──
    pdh = _state["pdh"]
    pdl = _state["pdl"]
    if pdh > 0:
        dist = abs(price - pdh) / price
        if dist <= 0.003:
            dangers.append(f"PDH ${pdh:.2f}")
    if pdl > 0:
        dist = abs(price - pdl) / price
        if dist <= 0.003:
            dangers.append(f"PDL ${pdl:.2f}")
    
    # ── 5. Psychological Levels ──
    psych, psych_dist = get_nearest_psych_level(price)
    if psych_dist <= PSYCH_PROXIMITY:
        dangers.append(f"رقم نفسي ${psych:.0f}")
    
    # ── 6. Opening Range Boundaries ──
    or_high = _state["opening_range_high"]
    or_low = _state["opening_range_low"]
    if or_high > 0:
        dist_h = abs(price - or_high) / price
        if dist_h <= 0.002:
            dangers.append(f"OR High ${or_high:.2f}")
    if or_low > 0:
        dist_l = abs(price - or_low) / price
        if dist_l <= 0.002:
            dangers.append(f"OR Low ${or_low:.2f}")
    
    # ── Decision ──
    if dangers:
        reason = " | ".join(dangers)
        logger.info(f"[V8 Zone] UNSAFE @ ${price:.2f}: {reason}")
        return False, reason
    
    return True, "Safe zone"

# ══════════════════════════════════════════════════════════════════════════════
# Contract Finders (unchanged logic)
# ══════════════════════════════════════════════════════════════════════════════

def find_atm_contract(price, trend, expiry):
    option_type = "call" if trend == "BULL" else "put"
    strike_min = round(price - 3, 0)
    strike_max = round(price + 3, 0)
    
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        logger.warning(f"[V8] No ATM contracts for {expiry} {option_type}")
        return None
    
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
        quote = get_option_quote(symbol)
        if quote and quote["mid"] > 0:
            return {
                "symbol": symbol, "strike": strike, "type": option_type,
                "expiry": expiry, "bid": quote["bid"], "ask": quote["ask"],
                "mid": quote["mid"], "is_atm": True
            }
        else:
            return {
                "symbol": symbol, "strike": strike, "type": option_type,
                "expiry": expiry, "bid": 0, "ask": 0, "mid": 0, "is_atm": True
            }
    return None

def find_itm_contract(price, trend, expiry):
    option_type = "call" if trend == "BULL" else "put"
    
    if trend == "BULL":
        strike_min = round(price - 10, 0)
        strike_max = round(price - 3, 0)
    else:
        strike_min = round(price + 3, 0)
        strike_max = round(price + 10, 0)
    
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        logger.warning(f"[V8] No ITM contracts for {expiry} {option_type}")
        return None
    
    best = None
    best_score = -1
    
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        itm_amount = (price - strike) if trend == "BULL" else (strike - price)
        
        if itm_amount < 3 or itm_amount > 10:
            continue
        
        approx_delta = min(0.95, 0.50 + itm_amount * 0.05)
        
        if ITM_DELTA_MIN <= approx_delta <= ITM_DELTA_MAX:
            symbol = c.get("symbol", "")
            quote = get_option_quote(symbol)
            
            if quote and quote["mid"] > 0:
                spread_pct = (quote["ask"] - quote["bid"]) / quote["mid"] if quote["mid"] > 0 else 1
                delta_score = 1 - abs(approx_delta - 0.80) * 5
                spread_score = max(0, 1 - spread_pct * 5)
                score = delta_score + spread_score
                
                if score > best_score:
                    best_score = score
                    best = {
                        "symbol": symbol, "strike": strike, "type": option_type,
                        "expiry": expiry, "bid": quote["bid"], "ask": quote["ask"],
                        "mid": quote["mid"], "approx_delta": round(approx_delta, 2),
                        "itm_amount": round(itm_amount, 2), "is_itm": True
                    }
    
    return best

# ══════════════════════════════════════════════════════════════════════════════
# Layer 1: ATM Scalp Engine
# ══════════════════════════════════════════════════════════════════════════════

def execute_atm_scalp(trend, price, expiry):
    contract = find_atm_contract(price, trend, expiry)
    if not contract:
        return False, "No ATM contract found"
    
    symbol = contract["symbol"]
    mid = contract["mid"]
    
    if mid <= 0:
        return False, f"No valid price for {symbol}"
    
    order = place_option_order(
        symbol=symbol, qty=ATM_CONTRACTS, side="buy",
        order_type="limit", limit_price=mid,
        position_intent="buy_to_open"
    )
    
    if not order:
        order = place_option_order(
            symbol=symbol, qty=ATM_CONTRACTS, side="buy",
            order_type="limit", limit_price=contract["ask"],
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
        "tp1_price": round(mid * (1 + ATM_TP1_PCT), 2),   # +5% بيع C1+C2
        "tp2_price": round(mid * (1 + ATM_TP2_PCT), 2),   # +10% بيع C3
        "sl_price": round(mid * (1 - ATM_SL_PCT), 2),     # -10% ستوب
        "reinforce_price": round(mid * (1 - ATM_REINFORCE_PCT), 2),
        "entry_time": _et_now().strftime("%I:%M:%S %p"),
        "trend": trend,
        "status": "open",
        "qty_sold": 0,
        "reinforced": False,
        "avg_price": mid,
        "total_qty": ATM_CONTRACTS,
        "pnl": 0.0,
        "c3_be_stop": mid,   # BE Stop لـ C3 بعد بيع C1+C2
        "c3_be_active": False
    }
    
    _state["atm_positions"].append(position)
    _state["atm_trade_count"] += 1
    _state["atm_reinforced"] = False
    
    direction = "CALL" if trend == "BULL" else "PUT"
    tf_info = f"15m={_state['trend_15m'] or 'N/A'} | 5m={_state['trend_5m'] or 'N/A'}"
    msg = (
        f"🤖 <b>V9 ATM — {direction} (3 عقود)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📥 شراء 3 عقود ATM\n"
        f"📋 {symbol}\n"
        f"💵 السعر: ${mid:.2f}\n"
        f"🎯 TP1 (+5%): ${position['tp1_price']:.2f} → بيع C1+C2\n"
        f"🎯 TP2 (+10%): ${position['tp2_price']:.2f} → بيع C3 (Runner)\n"
        f"🛑 SL (-10%): ${position['sl_price']:.2f}\n"
        f"📊 TSLA: ${price:.2f} | VWAP: ${_state['vwap']:.2f}\n"
        f"📈 {tf_info}\n"
        f"🕐 {position['entry_time']} ET"
    )
    send_telegram(msg)
    logger.info(f"[V9 ATM] Opened: {symbol} x3 @ ${mid:.2f} | TP1={position['tp1_price']} TP2={position['tp2_price']} SL={position['sl_price']}")
    
    return True, position

def monitor_atm_positions():
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
            sell_order = place_option_order(
                symbol=symbol, qty=remaining_qty, side="sell",
                order_type="market", position_intent="sell_to_close"
            )
            pnl = (current_mid - avg_price) * remaining_qty * 100
            pos["pnl"] = pnl
            pos["status"] = "closed_sl"
            _state["daily_pnl"] += pnl
            _state["total_pnl"] += pnl
            _state["consecutive_losses"] += 1
            _state["losses"] += 1
            
            if _state["consecutive_losses"] >= PAUSE_AFTER_CONSECUTIVE_LOSSES:
                _state["pause_until"] = time.time() + PAUSE_DURATION_SECONDS
                send_telegram(f"⏸️ <b>V8 وقف مؤقت</b> — {PAUSE_AFTER_CONSECUTIVE_LOSSES} خسارات متتالية. استراحة 30 دقيقة.")
            
            msg = (
                f"🔴 <b>V8 ستوب لوس — ATM</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📋 {symbol}\n"
                f"📥 دخول: ${avg_price:.2f}\n"
                f"📤 خروج: ${current_mid:.2f}\n"
                f"💸 P&L: <b>${pnl:+.2f}</b>\n"
                f"📊 خسارات متتالية: {_state['consecutive_losses']}\n"
                f"🕐 {_et_now().strftime('%I:%M %p')} ET"
            )
            send_telegram(msg)
            _record_trade(pos, "SL", pnl)
            continue
        
        # ── Check TP1 (+5%) — بيع C1+C2 (عقدين) ──
        if pos["qty_sold"] == 0 and current_mid >= pos["tp1_price"]:
            sell_qty = 2  # بيع C1 + C2
            sell_order = place_option_order(
                symbol=symbol, qty=sell_qty, side="sell",
                order_type="market", position_intent="sell_to_close"
            )
            if sell_order:
                pnl = (current_mid - avg_price) * sell_qty * 100
                pos["qty_sold"] += sell_qty
                pos["pnl"] += pnl
                _state["daily_pnl"] += pnl
                _state["total_pnl"] += pnl
                _state["consecutive_losses"] = 0
                _state["wins"] += 1
                # تفعيل BE Stop لـ C3
                pos["c3_be_active"] = True
                pos["c3_be_stop"] = avg_price  # بيع C3 لو رجع للدخول
                
                msg = (
                    f"💰 <b>V9 TP1 — ATM +5%</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {symbol}\n"
                    f"📤 بيع C1+C2 (2 عقود) @ ${current_mid:.2f}\n"
                    f"💵 P&L: <b>${pnl:+.2f}</b>\n"
                    f"🏃 C3 Runner متبقي — هدف ${pos['tp2_price']:.2f} (+10%)\n"
                    f"🛑 BE Stop C3: ${pos['c3_be_stop']:.2f}\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
                _record_trade(pos, "TP1", pnl)
            continue
        
        # ── Check C3 BE Stop (بعد بيع C1+C2) ──
        if pos["c3_be_active"] and pos["qty_sold"] == 2:
            remaining = pos["total_qty"] - pos["qty_sold"]
            if remaining > 0 and current_mid <= pos["c3_be_stop"]:
                sell_order = place_option_order(
                    symbol=symbol, qty=remaining, side="sell",
                    order_type="market", position_intent="sell_to_close"
                )
                if sell_order:
                    pnl = (current_mid - avg_price) * remaining * 100
                    pos["qty_sold"] += remaining
                    pos["pnl"] += pnl
                    pos["status"] = "closed_be"
                    _state["daily_pnl"] += pnl
                    _state["total_pnl"] += pnl
                    
                    msg = (
                        f"🔄 <b>V9 BE Stop — C3 Runner</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"📋 {symbol}\n"
                        f"📤 بيع C3 @ ${current_mid:.2f} (BE Stop)\n"
                        f"💵 P&L C3: <b>${pnl:+.2f}</b>\n"
                        f"✅ الصفقة مكتملة\n"
                        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                    )
                    send_telegram(msg)
                    _record_trade(pos, "BE_STOP", pnl)
            continue
        
        # ── Check TP2 (+10%) — بيع C3 Runner ──
        if pos["qty_sold"] >= 2 and current_mid >= pos["tp2_price"]:
            remaining = pos["total_qty"] - pos["qty_sold"]
            if remaining > 0:
                sell_order = place_option_order(
                    symbol=symbol, qty=remaining, side="sell",
                    order_type="market", position_intent="sell_to_close"
                )
                if sell_order:
                    pnl = (current_mid - avg_price) * remaining * 100
                    pos["qty_sold"] += remaining
                    pos["pnl"] += pnl
                    pos["status"] = "closed_tp"
                    _state["daily_pnl"] += pnl
                    _state["total_pnl"] += pnl
                    _state["wins"] += 1
                    pos["c3_be_active"] = False
                    
                    msg = (
                        f"🎯 <b>V9 TP2 — C3 Runner +10%! 🚀</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"📋 {symbol}\n"
                        f"📤 بيع C3 Runner @ ${current_mid:.2f}\n"
                        f"💵 P&L C3: <b>${pnl:+.2f}</b>\n"
                        f"✅ الصفقة مكتملة بالكامل\n"
                        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                    )
                    send_telegram(msg)
                    _record_trade(pos, "TP2", pnl)
        
        # ── Check if trend reversed — exit remaining ──
        if _state["trend"] and _state["trend"] != pos["trend"]:
            remaining = pos["total_qty"] - pos["qty_sold"]
            if remaining > 0:
                sell_order = place_option_order(
                    symbol=symbol, qty=remaining, side="sell",
                    order_type="market", position_intent="sell_to_close"
                )
                pnl = (current_mid - avg_price) * remaining * 100
                pos["qty_sold"] += remaining
                pos["pnl"] += pnl
                pos["status"] = "closed_reversal"
                _state["daily_pnl"] += pnl
                _state["total_pnl"] += pnl
                
                if pnl >= 0:
                    _state["wins"] += 1
                    _state["consecutive_losses"] = 0
                else:
                    _state["losses"] += 1
                    _state["consecutive_losses"] += 1
                
                msg = (
                    f"🔄 <b>V8 خروج — انعكاس الترند</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {symbol}\n"
                    f"📤 بيع {remaining} عقود @ ${current_mid:.2f}\n"
                    f"💵 P&L: <b>${pnl:+.2f}</b>\n"
                    f"📊 الترند انعكس: {pos['trend']} → {_state['trend']}\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
                _record_trade(pos, "REVERSAL", pnl)

# ══════════════════════════════════════════════════════════════════════════════
# Layer 2: ITM Pullback Engine
# ══════════════════════════════════════════════════════════════════════════════

def execute_itm_pullback(trend, price, expiry):
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
        symbol=symbol, qty=ITM_CONTRACTS, side="buy",
        order_type="limit", limit_price=limit_price,
        position_intent="buy_to_open"
    )
    
    if not order:
        return False, "ITM order failed"
    
    pending = {
        "order_id": order.get("id"),
        "symbol": symbol,
        "strike": contract["strike"],
        "type": contract["type"],
        "approx_delta": contract.get("approx_delta", 0),
        "itm_amount": contract.get("itm_amount", 0),
        "limit_price": limit_price,
        "current_mid": mid,
        "trend": trend,
        "status": "pending",
        "entry_price": 0,
        "tp_price": 0,
        "sl_price": 0,
        "pnl": 0.0
    }
    
    _state["itm_pending_order"] = pending
    
    direction = "CALL" if trend == "BULL" else "PUT"
    msg = (
        f"🎯 <b>V8 ITM — أمر معلّق</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 {symbol} ({direction})\n"
        f"💵 أمر شراء: ${limit_price:.2f} (تحت السوق {ITM_ENTRY_BELOW*100:.1f}%)\n"
        f"📊 Delta: ~{contract.get('approx_delta', 0):.2f}\n"
        f"📏 ITM: ${contract.get('itm_amount', 0):.2f}\n"
        f"🎯 TP: +40% | 🛑 SL: -16%\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)
    logger.info(f"[V8 ITM] Pending: {symbol} limit=${limit_price:.2f}")
    
    return True, pending

def monitor_itm_position():
    # ── Check pending order ──
    pending = _state["itm_pending_order"]
    if pending and pending["status"] == "pending":
        order = get_order_status(pending["order_id"])
        if order:
            status = order.get("status", "")
            if status == "filled":
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
        if pending and _state["trend"] and _state["trend"] != pending.get("trend"):
            cancel_order(pending["order_id"])
            _state["itm_pending_order"] = None
            logger.info("[V8 ITM] Cancelled pending — trend reversed")
    
    # ── Monitor active ITM position ──
    pos = _state["itm_position"]
    if not pos or pos.get("status") != "filled":
        return
    
    quote = get_option_quote(pos["symbol"])
    if not quote or quote["mid"] <= 0:
        return
    
    current_mid = quote["mid"]
    entry_price = pos["entry_price"]
    
    # ── Stop Loss ──
    if current_mid <= pos["sl_price"]:
        sell_order = place_option_order(
            symbol=pos["symbol"], qty=ITM_CONTRACTS, side="sell",
            order_type="market", position_intent="sell_to_close"
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
            symbol=pos["symbol"], qty=ITM_CONTRACTS, side="sell",
            order_type="market", position_intent="sell_to_close"
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
    
    # ── Trend Reversal — exit ITM too ──
    if _state["trend"] and _state["trend"] != pos.get("trend"):
        sell_order = place_option_order(
            symbol=pos["symbol"], qty=ITM_CONTRACTS, side="sell",
            order_type="market", position_intent="sell_to_close"
        )
        pnl = (current_mid - entry_price) * ITM_CONTRACTS * 100
        pos["pnl"] = pnl
        pos["status"] = "closed_reversal"
        _state["daily_pnl"] += pnl
        _state["total_pnl"] += pnl
        _state["itm_position"] = None
        
        if pnl >= 0:
            _state["wins"] += 1
            _state["consecutive_losses"] = 0
        else:
            _state["losses"] += 1
            _state["consecutive_losses"] += 1
        
        msg = (
            f"🔄 <b>V8 خروج ITM — انعكاس</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📋 {pos['symbol']}\n"
            f"📤 خروج: ${current_mid:.2f}\n"
            f"💵 P&L: <b>${pnl:+.2f}</b>\n"
            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
        )
        send_telegram(msg)
        _record_trade(pos, "REVERSAL", pnl)

# ══════════════════════════════════════════════════════════════════════════════
# Risk Manager
# ══════════════════════════════════════════════════════════════════════════════

def check_risk():
    account = get_account()
    if account:
        portfolio_value = float(account.get("portfolio_value", 0))
        _state["portfolio_current_value"] = portfolio_value
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
    
    if _state["stopped_permanent"]:
        return False, "Stopped permanently"
    
    if _state["stopped_for_day"]:
        return False, "Stopped for today"
    
    if time.time() < _state["pause_until"]:
        remaining = (_state["pause_until"] - time.time()) / 60
        return False, f"Paused — {remaining:.0f} min remaining"
    
    return True, "OK"

# ══════════════════════════════════════════════════════════════════════════════
# Trade Recorder
# ══════════════════════════════════════════════════════════════════════════════

def _record_trade(pos, exit_type, pnl):
    _state["trades_today"].append({
        "symbol": pos.get("symbol", ""),
        "type": pos.get("type", ""),
        "entry_price": pos.get("entry_price", pos.get("avg_price", 0)),
        "exit_type": exit_type,
        "pnl": pnl,
        "time": _et_now().strftime("%I:%M %p"),
        "layer": "ITM" if pos.get("is_itm") or pos.get("approx_delta") else "ATM"
    })

# ══════════════════════════════════════════════════════════════════════════════
# Force Close All
# ══════════════════════════════════════════════════════════════════════════════

def force_close_all():
    closed_count = 0
    total_pnl = 0
    
    for pos in _state["atm_positions"]:
        if pos["status"] == "open":
            remaining = pos["total_qty"] - pos["qty_sold"]
            if remaining > 0:
                quote = get_option_quote(pos["symbol"])
                mid = quote["mid"] if quote else 0
                sell_order = place_option_order(
                    symbol=pos["symbol"], qty=remaining, side="sell",
                    order_type="market", position_intent="sell_to_close"
                )
                pnl = (mid - pos["avg_price"]) * remaining * 100 if mid > 0 else 0
                pos["pnl"] += pnl
                pos["status"] = "closed_eod"
                _state["daily_pnl"] += pnl
                _state["total_pnl"] += pnl
                total_pnl += pnl
                closed_count += 1
                _record_trade(pos, "EOD", pnl)
    
    pos = _state["itm_position"]
    if pos and pos.get("status") == "filled":
        quote = get_option_quote(pos["symbol"])
        mid = quote["mid"] if quote else 0
        sell_order = place_option_order(
            symbol=pos["symbol"], qty=ITM_CONTRACTS, side="sell",
            order_type="market", position_intent="sell_to_close"
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

# ══════════════════════════════════════════════════════════════════════════════
# Daily Summary
# ══════════════════════════════════════════════════════════════════════════════

def send_daily_summary():
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
    
    # Portfolio info
    port_start = _state["portfolio_start_value"]
    port_now = _state["portfolio_current_value"]
    port_change = port_now - port_start if port_start > 0 else 0
    
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
        f"💼 المحفظة: ${port_now:,.2f} ({'+' if port_change >= 0 else ''}{port_change:,.2f})\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)

# ══════════════════════════════════════════════════════════════════════════════
# Main Engine Loop
# ══════════════════════════════════════════════════════════════════════════════

def _reset_daily():
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
        _state["opening_range_set"] = False
        _state["micro_resistances"] = []
        _state["micro_supports"] = []
        _state["trend_15m"] = None
        _state["trend_5m"] = None
        
        # Get starting portfolio value
        account = get_account()
        if account:
            _state["portfolio_start_value"] = float(account.get("portfolio_value", 0))
            _state["portfolio_current_value"] = _state["portfolio_start_value"]
        
        # Load previous day levels
        load_pdh_pdl()
        
        logger.info(f"[V8] Daily reset for {today} | Portfolio: ${_state['portfolio_start_value']:,.2f}")

def _has_open_atm():
    return any(p["status"] == "open" for p in _state["atm_positions"])

def engine_loop():
    """
    Main engine loop — runs every 30 seconds.
    Enhanced with Multi-Timeframe and all level checks.
    """
    logger.info("[V8] Options Scalper Engine V8.1 started! 🚀")
    _state["running"] = True
    _summary_sent = False
    _summary_date = ""
    _levels_updated = 0
    
    # Initialize
    account = get_account()
    if account:
        _state["portfolio_start_value"] = float(account.get("portfolio_value", 0))
        logger.info(f"[V8] Initial portfolio: ${_state['portfolio_start_value']:,.2f}")
    
    load_pdh_pdl()
    
    while _state["running"]:
        try:
            _reset_daily()
            
            now = _et_now()
            today = _today_str()
            
            if _summary_date != today:
                _summary_sent = False
                _summary_date = today
            
            # Skip weekends
            if now.weekday() >= 5:
                time.sleep(60)
                continue
            
            # Skip non-0DTE days
            if not _is_0dte_day():
                time.sleep(60)
                continue

            # ── Always update price snapshot (even outside scalp window) ──
            try:
                snap = get_tsla_snapshot()
                if snap and snap.get("price", 0) > 0:
                    _state["current_price"] = snap["price"]
                    _state["vwap"]          = snap.get("vwap", 0)
                    _state["day_high"]      = snap.get("high", 0)
                    _state["day_low"]       = snap.get("low", 0)
            except Exception:
                pass

            # ── Compute Opening Range (before scalp window) ──
            if not _state["opening_range_set"]:
                compute_opening_range()
            
            # ── Force Close Time ──
            if _is_force_close_time():
                if _has_open_atm() or _state["itm_position"]:
                    force_close_all()
                
                if not _summary_sent and _state["trades_today"]:
                    send_daily_summary()
                    _summary_sent = True
                
                time.sleep(60)
                continue
            
            # ── Outside Scalp Window ──
            if not _is_scalp_window():
                if _has_open_atm():
                    monitor_atm_positions()
                if _state["itm_position"] or _state["itm_pending_order"]:
                    monitor_itm_position()
                time.sleep(30)
                continue
            
            # ══════════════════════════════════════════════════════════════
            # INSIDE SCALP WINDOW (10:10 - 12:40 ET)
            # ══════════════════════════════════════════════════════════════
            
            # ── Update Micro S/R every 5 minutes ──
            now_ts = time.time()
            if now_ts - _levels_updated > 300:  # every 5 min
                compute_micro_sr()
                _levels_updated = now_ts
            
            # ── Risk Check ──
            can_trade, risk_reason = check_risk()
            if not can_trade:
                monitor_atm_positions()
                monitor_itm_position()
                logger.info(f"[V8] Can't trade: {risk_reason}")
                time.sleep(30)
                continue
            
            # ── Multi-Timeframe Trend Detection ──
            trend, strength, details = detect_trend_multi()
            _state["trend"] = trend
            _state["trend_strength"] = strength
            
            if not trend:
                monitor_atm_positions()
                monitor_itm_position()
                logger.info("[V8] No clear trend (MTF disagree) — waiting")
                time.sleep(TREND_CHECK_INTERVAL)
                continue
            
            # ── Zone Safety Check (ALL levels) ──
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
                logger.info(f"[V8] Trend={trend} ({strength}) | Price=${price:.2f} | "
                           f"15m={details['15m']} 5m={details['5m']} | Entering ATM")
                success, result = execute_atm_scalp(trend, price, expiry)
                if not success:
                    logger.warning(f"[V8] ATM entry failed: {result}")
            else:
                monitor_atm_positions()
            
            # ── Layer 2: ITM Pullback ──
            if not _state["itm_position"] and not _state["itm_pending_order"]:
                is_pb, pb_depth = detect_pullback(trend)
                if is_pb:
                    logger.info(f"[V8] Pullback on 5min ({pb_depth*100:.1f}%) — placing ITM order")
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

# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

_engine_thread = None

def start_scalper():
    global _engine_thread
    if _engine_thread and _engine_thread.is_alive():
        logger.info("[V8] Engine already running")
        return
    _engine_thread = threading.Thread(target=engine_loop, daemon=True, name="V8_Scalper")
    _engine_thread.start()
    logger.info("[V8] Options Scalper Engine V8.1 thread started! 🚀")

def stop_scalper():
    _state["running"] = False
    logger.info("[V8] Engine stopping...")

def get_scalper_status():
    return {
        "running": _state["running"],
        "version": "V8.1",
        "trend": _state["trend"],
        "trend_15m": _state["trend_15m"],
        "trend_5m": _state["trend_5m"],
        "trend_strength": _state["trend_strength"],
        "current_price": _state["current_price"],
        "vwap": _state["vwap"],
        "pdh": _state["pdh"],
        "pdl": _state["pdl"],
        "opening_range_high": _state["opening_range_high"],
        "opening_range_low": _state["opening_range_low"],
        "micro_resistances": _state["micro_resistances"],
        "micro_supports": _state["micro_supports"],
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
        "portfolio_current": _state["portfolio_current_value"],
        "trades_today": len(_state["trades_today"])
    }

# ══════════════════════════════════════════════════════════════════════════════
# Layer 2: ITM Manual Scalp — Web Interface Engine (V9)
# ══════════════════════════════════════════════════════════════════════════════

# حالة الخط 2 اليدوي
_manual_state = {
    "position": None,           # الصفقة المفتوحة
    "alert1_sent": False,       # تنبيه -$0.20
    "alert2_sent": False,       # تنبيه -$0.35 (عند التعزيز)
    "reinforce_done": False,    # تم التعزيز
    "reinforce_price": None,    # سعر التعزيز
    "reinforce_qty": 0,         # عدد عقود التعزيز
    "monitor_active": False,
    "last_price": 0.0,
    "pnl_dollar": 0.0,
}

_manual_monitor_thread = None

def find_itm_contract_for_manual(price, option_type):
    """
    يجد أفضل عقد ITM بـ Delta 0.70-0.88 لأمر يدوي.
    option_type: "call" أو "put"
    """
    # جرب اليوم أولاً ثم الأيام القادمة (fallback)
    from datetime import datetime, timedelta, timezone
    et_now = datetime.now(timezone.utc) - timedelta(hours=4)
    expiry_candidates = []
    for i in range(0, 8):  # جرب 8 أيام قادمة
        d = et_now + timedelta(days=i)
        if d.weekday() < 5:  # Mon-Fri فقط
            expiry_candidates.append(d.strftime('%Y-%m-%d'))
        if len(expiry_candidates) >= 4:
            break
    
    if option_type == "call":
        strike_min = round(price - 20, 0)
        strike_max = round(price - 1, 0)
    else:
        strike_min = round(price + 1, 0)
        strike_max = round(price + 20, 0)
    
    contracts = []
    expiry = expiry_candidates[0]
    for exp in expiry_candidates:
        c = get_options_chain(exp, option_type, strike_min, strike_max)
        if c:
            contracts = c
            expiry = exp
            logger.info(f"[V9 Manual] Found {len(c)} contracts for {exp}")
            break
    
    if not contracts:
        logger.warning(f"[V9 Manual] No ITM contracts found for {option_type} in next 4 trading days")
        return None
    
    best = None
    best_score = -1
    
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        
        if option_type == "call":
            itm_amount = price - strike
        else:
            itm_amount = strike - price
        
        if itm_amount <= 0:
            continue
        
        # تقدير Delta بناءً على ITM amount
        approx_delta = min(0.97, 0.50 + itm_amount * 0.045)
        
        if ITM_DELTA_MIN <= approx_delta <= ITM_DELTA_MAX:
            symbol = c.get("symbol", "")
            quote = get_option_quote(symbol)
            
            if quote and quote["mid"] > 0:
                spread_pct = (quote["ask"] - quote["bid"]) / quote["mid"] if quote["mid"] > 0 else 1
                delta_score = 1 - abs(approx_delta - 0.79) * 5  # أفضل delta = 0.79
                spread_score = max(0, 1 - spread_pct * 3)
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
                        "spread_pct": round(spread_pct * 100, 1)
                    }
    
    return best


def execute_manual_itm(option_type):
    """
    تنفيذ شراء ITM يدوي (من زر CALL أو PUT في الواجهة).
    Returns: (success: bool, data: dict)
    """
    global _manual_state
    
    # لا تفتح صفقة جديدة لو في صفقة مفتوحة
    if _manual_state["position"]:
        return False, {"error": "يوجد صفقة مفتوحة — أغلقها أولاً"}
    
    # جلب سعر TSLA
    snap = get_tsla_snapshot()
    if not snap or snap["price"] <= 0:
        return False, {"error": "لا يمكن جلب سعر TSLA"}
    
    price = snap["price"]
    
    # إيجاد أفضل عقد ITM
    contract = find_itm_contract_for_manual(price, option_type)
    if not contract:
        return False, {"error": f"لا يوجد عقد ITM مناسب (Delta 0.60-0.92) لـ {option_type.upper()}"}
    
    symbol = contract["symbol"]
    mid = contract["mid"]
    
    if mid <= 0:
        return False, {"error": f"سعر غير صالح للعقد {symbol}"}
    
    # شراء Market Order
    order = place_option_order(
        symbol=symbol, qty=1, side="buy",
        order_type="market", position_intent="buy_to_open"
    )
    
    if not order:
        return False, {"error": "فشل تنفيذ أمر الشراء"}
    
    # حساب مستويات TP و SL
    tp_price = round(mid + ITM_TP_DOLLARS, 2)
    sl_price = round(mid - ITM_SL_DOLLARS, 2)
    alert1_price = round(mid - ITM_ALERT1_DOLLARS, 2)
    alert2_price = round(mid - ITM_ALERT2_DOLLARS, 2)
    
    position = {
        "order_id": order.get("id"),
        "symbol": symbol,
        "strike": contract["strike"],
        "type": option_type,
        "approx_delta": contract["approx_delta"],
        "itm_amount": contract["itm_amount"],
        "entry_price": mid,
        "current_price": mid,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "alert1_price": alert1_price,
        "alert2_price": alert2_price,
        "entry_time": _et_now().strftime("%I:%M:%S %p"),
        "entry_tsla": price,
        "status": "open",
        "pnl": 0.0
    }
    
    _manual_state["position"] = position
    _manual_state["alert1_sent"] = False
    _manual_state["alert2_sent"] = False
    _manual_state["reinforce_done"] = False
    _manual_state["reinforce_price"] = None
    _manual_state["reinforce_qty"] = 0
    _manual_state["last_price"] = mid
    _manual_state["pnl_dollar"] = 0.0
    
    # ── تسجيل تلقائي في السجل عند الفتح ──
    journal_entry = {
        "direction": option_type.upper(),
        "symbol": symbol,
        "strike": contract["strike"],
        "expiry": symbol[4:10] if len(symbol) > 10 else "",
        "entry_price": mid,
        "entry_tsla": price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "delta": contract["approx_delta"],
        "entry_time": _et_now().strftime("%I:%M:%S %p ET"),
        "status": "open",
        "pnl_dollar": 0.0,
        "close_reason": "",
        "exit_price": None,
        "exit_time": "",
        "notes": "",
        "images": []
    }
    journal_id = add_journal_entry(journal_entry)
    _manual_state["journal_id"] = journal_id
    logger.info(f"[V9 Manual] Journal entry #{journal_id} created")
    
    direction = "CALL 🟢" if option_type == "call" else "PUT 🔴"
    logger.info(f"[V9 Manual] Opened {direction}: {symbol} @ ${mid:.2f} | "
                f"TP=${tp_price} SL=${sl_price} Delta={contract['approx_delta']}")
    
    # بدء مراقبة الصفقة
    _start_manual_monitor()
    
    return True, {
        "symbol": symbol,
        "strike": contract["strike"],
        "type": option_type,
        "entry_price": mid,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "alert1_price": alert1_price,
        "alert2_price": alert2_price,
        "approx_delta": contract["approx_delta"],
        "entry_time": position["entry_time"],
        "entry_tsla": price,
        "journal_id": journal_id  # V10 FIX: لمنع إنشاء record مكرر في app.py
    }


def close_manual_itm(reason="manual"):
    """
    بيع ITM يدوي (زر STOP LOSS أو بيع تلقائي).
    Returns: (success: bool, data: dict)
    """
    global _manual_state
    
    pos = _manual_state["position"]
    if not pos:
        return False, {"error": "لا توجد صفقة مفتوحة"}
    
    symbol = pos["symbol"]
    
    # جلب السعر الحالي
    quote = get_option_quote(symbol)
    current_price = quote["mid"] if quote and quote["mid"] > 0 else pos["entry_price"]
    
    # بيع Market Order
    order = place_option_order(
        symbol=symbol, qty=1, side="sell",
        order_type="market", position_intent="sell_to_close"
    )
    
    pnl = round((current_price - pos["entry_price"]) * 100, 2)
    
    result = {
        "symbol": symbol,
        "entry_price": pos["entry_price"],
        "exit_price": current_price,
        "pnl": pnl,
        "reason": reason,
        "exit_time": _et_now().strftime("%I:%M:%S %p")
    }
    
    pos["status"] = "closed"
    pos["pnl"] = pnl
    _manual_state["position"] = None
    _manual_state["monitor_active"] = False
    _manual_state["last_reason"] = reason  # TP / SL / MANUAL / ERROR
    _manual_state["last_pnl"] = pnl
    
    # ── تحديث تلقائي للسجل عند الإغلاق ──
    journal_id = _manual_state.get("journal_id")
    if journal_id:
        reason_ar = {"TP": "جني أرباح تلقائي", "SL": "ستوب لوس تلقائي", "MANUAL": "إغلاق يدوي", "ERROR": "خطأ تقني"}.get(reason, reason)
        update_journal_entry(journal_id, {
            "status": "closed",
            "exit_price": current_price,
            "exit_time": _et_now().strftime("%I:%M:%S %p ET"),
            "pnl_dollar": pnl,
            "close_reason": reason_ar
        })
        _manual_state["journal_id"] = None
        logger.info(f"[V9 Manual] Journal entry #{journal_id} updated with close data")
    
    # إرسال تنبيه Telegram بسبب الإغلاق
    direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
    emoji = "✅" if pnl >= 0 else "🔴"
    reason_ar = {"TP": "جني أرباح تلقائي ✅", "SL": "ستوب لوس تلقائي 🛑", "MANUAL": "إغلاق يدوي 👆", "ERROR": "خطأ تقني ⚠️"}.get(reason, reason)
    msg = (
        f"{emoji} <b>V9 إغلاق — {direction}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 {symbol}\n"
        f"📥 دخول: ${pos['entry_price']:.2f}\n"
        f"📤 خروج: ${current_price:.2f}\n"
        f"💰 P&L: ${pnl:+.2f}\n"
        f"📌 السبب: {reason_ar}\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)
    
    logger.info(f"[V9 Manual] Closed {symbol} @ ${current_price:.2f} | PnL=${pnl:+.2f} | Reason={reason}")
    
    return True, result


def close_manual_itm_all(reason="manual"):
    """
    بيع كل العقود (1 أصلي + 2 تعزيز) بعد التعزيز.
    """
    global _manual_state
    
    pos = _manual_state["position"]
    if not pos:
        return False, {"error": "لا توجد صفقة مفتوحة"}
    
    symbol = pos["symbol"]
    total_qty = 1 + _manual_state["reinforce_qty"]  # 1 + 2 = 3
    
    quote = get_option_quote(symbol)
    current_price = quote["mid"] if quote and quote["mid"] > 0 else pos["entry_price"]
    
    # بيع كل العقود
    order = place_option_order(
        symbol=symbol, qty=total_qty, side="sell",
        order_type="market", position_intent="sell_to_close"
    )
    
    # حساب P&L الكلي
    pnl_original = round((current_price - pos["entry_price"]) * 100, 2)
    reinforce_price = _manual_state["reinforce_price"]
    pnl_reinforce = round((current_price - reinforce_price) * 100 * _manual_state["reinforce_qty"], 2) if reinforce_price else 0
    pnl_total = round(pnl_original + pnl_reinforce, 2)
    
    result = {
        "symbol": symbol,
        "entry_price": pos["entry_price"],
        "reinforce_price": reinforce_price,
        "exit_price": current_price,
        "total_qty": total_qty,
        "pnl_original": pnl_original,
        "pnl_reinforce": pnl_reinforce,
        "pnl_total": pnl_total,
        "reason": reason,
        "exit_time": _et_now().strftime("%I:%M:%S %p")
    }
    
    pos["status"] = "closed"
    pos["pnl"] = pnl_total
    _manual_state["position"] = None
    _manual_state["monitor_active"] = False
    _manual_state["last_reason"] = reason
    _manual_state["last_pnl"] = pnl_total
    
    # تحديث السجل
    journal_id = _manual_state.get("journal_id")
    if journal_id:
        reason_ar = {"TP": "جني أرباح تلقائي", "SL": "ستوب لوس تلقائي", "MANUAL": "إغلاق يدوي"}.get(reason, reason)
        update_journal_entry(journal_id, {
            "status": "closed",
            "exit_price": current_price,
            "exit_time": _et_now().strftime("%I:%M:%S %p ET"),
            "pnl_dollar": pnl_total,
            "close_reason": reason_ar + f" (تعزيز {_manual_state['reinforce_qty']} عقود)"
        })
        _manual_state["journal_id"] = None
    
    direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
    emoji = "✅" if pnl_total >= 0 else "🔴"
    reason_ar = {"TP": "جني أرباح تلقائي ✅", "SL": "ستوب لوس تلقائي 🛑", "MANUAL": "إغلاق يدوي 👆"}.get(reason, reason)
    msg = (
        f"{emoji} <b>V9 إغلاق بعد التعزيز — {direction}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 {symbol}\n"
        f"📥 دخول أصلي: ${pos['entry_price']:.2f}\n"
        f"🔄 تعزيز عند: ${reinforce_price:.2f}\n"
        f"📤 خروج: ${current_price:.2f}\n"
        f"📊 عدد العقود: {total_qty}\n"
        f"💰 P&L الكلي: ${pnl_total:+.2f}\n"
        f"📌 السبب: {reason_ar}\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)
    
    logger.info(f"[V9 Manual] Closed ALL {total_qty} contracts @ ${current_price:.2f} | PnL=${pnl_total:+.2f} | Reason={reason}")
    return True, result


def _manual_monitor_loop():
    """
    حلقة مراقبة صفقة ITM اليدوي.
    - TP: +$0.80 → بيع الكل تلقائياً
    - SL قبل التعزيز: -$0.65 → بيع تلقائي
    - تعزيز: عند -$0.35 → شراء 2 عقد إضافي
    - TP بعد التعزيز: +$0.80 من سعر التعزيز → بيع الكل
    - SL بعد التعزيز: -$0.40 من سعر التعزيز → بيع الكل
    """
    global _manual_state
    
    logger.info("[V9 Manual] Monitor started (TP=$0.80, Reinforce@-$0.35)")
    
    while _manual_state["monitor_active"]:
        try:
            pos = _manual_state["position"]
            if not pos or pos.get("status") != "open":
                _manual_state["monitor_active"] = False
                break
            
            symbol = pos["symbol"]
            quote = get_option_quote(symbol)
            
            if not quote or quote["mid"] <= 0:
                time.sleep(5)
                continue
            
            current_price = quote["mid"]
            entry_price = pos["entry_price"]
            pnl_dollar = round(current_price - entry_price, 2)
            
            # تحديث الحالة
            pos["current_price"] = current_price
            total_qty = 1 + _manual_state["reinforce_qty"]
            pos["pnl"] = round(pnl_dollar * 100 * total_qty, 2)
            _manual_state["last_price"] = current_price
            _manual_state["pnl_dollar"] = pnl_dollar
            
            direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
            reinforce_done = _manual_state["reinforce_done"]
            reinforce_price = _manual_state["reinforce_price"]
            
            # ── مرحلة ما بعد التعزيز ──
            if reinforce_done and reinforce_price:
                tp_after = round(reinforce_price + ITM_REINFORCE_TP, 2)
                sl_after = round(reinforce_price - ITM_REINFORCE_SL, 2)
                
                # TP بعد التعزيز
                if current_price >= tp_after:
                    logger.info(f"[V9 Manual] TP (after reinforce) hit! ${current_price:.2f} >= ${tp_after:.2f}")
                    close_manual_itm_all(reason="TP")
                    break
                
                # SL بعد التعزيز
                if current_price <= sl_after:
                    logger.info(f"[V9 Manual] SL (after reinforce) hit! ${current_price:.2f} <= ${sl_after:.2f}")
                    close_manual_itm_all(reason="SL")
                    break
            
            # ── مرحلة ما قبل التعزيز ──
            else:
                # TP تلقائي: +$0.80 من سعر الدخول
                if current_price >= pos["tp_price"]:
                    logger.info(f"[V9 Manual] TP hit! ${current_price:.2f} >= ${pos['tp_price']:.2f}")
                    close_manual_itm(reason="TP")
                    break
                
                # SL تلقائي: -$0.65 من سعر الدخول (قبل التعزيز)
                if current_price <= pos["sl_price"]:
                    logger.info(f"[V9 Manual] SL hit! ${current_price:.2f} <= ${pos['sl_price']:.2f}")
                    close_manual_itm(reason="SL")
                    break
                
                # تعزيز: عند -$0.35
                if not reinforce_done and pnl_dollar <= -ITM_REINFORCE_TRIGGER:
                    logger.info(f"[V9 Manual] Reinforce triggered at ${current_price:.2f} (pnl={pnl_dollar:+.2f})")
                    reinforce_order = place_option_order(
                        symbol=symbol, qty=ITM_REINFORCE_QTY, side="buy",
                        order_type="market", position_intent="buy_to_open"
                    )
                    if reinforce_order:
                        _manual_state["reinforce_done"] = True
                        _manual_state["reinforce_price"] = current_price
                        _manual_state["reinforce_qty"] = ITM_REINFORCE_QTY
                        tp_after = round(current_price + ITM_REINFORCE_TP, 2)
                        sl_after = round(current_price - ITM_REINFORCE_SL, 2)
                        msg = (
                            f"🔄 <b>V9 تعزيز — {direction}</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"📋 {symbol}\n"
                            f"📥 دخول أصلي: ${entry_price:.2f}\n"
                            f"🔄 سعر التعزيز: ${current_price:.2f}\n"
                            f"📊 +{ITM_REINFORCE_QTY} عقود إضافية (المجموع: 3 عقود)\n"
                            f"🎯 TP جديد: ${tp_after:.2f} (+$0.80 من سعر التعزيز)\n"
                            f"🛑 SL جديد: ${sl_after:.2f} (-$0.40 من سعر التعزيز)\n"
                            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                        )
                        send_telegram(msg)
                        logger.info(f"[V9 Manual] Reinforce done: {ITM_REINFORCE_QTY} contracts @ ${current_price:.2f}")
                    else:
                        logger.error("[V9 Manual] Reinforce order FAILED")
            
            # ── تنبيه 1: -$0.20 ──
            if not _manual_state["alert1_sent"] and pnl_dollar <= -ITM_ALERT1_DOLLARS:
                _manual_state["alert1_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 1: -$0.20 | ${current_price:.2f}")
                msg = (
                    f"⚠️ <b>V9 تنبيه — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر: ${current_price:.2f} | دخول: ${entry_price:.2f}\n"
                    f"📉 خسارة: <b>-$0.20</b> (-$20)\n"
                    f"🔄 التعزيز سيبدأ عند -$0.35\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            # ── تنبيه 2: -$0.35 (عند التعزيز) ──
            if not _manual_state["alert2_sent"] and pnl_dollar <= -ITM_ALERT2_DOLLARS:
                _manual_state["alert2_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 2: -$0.35 (reinforce zone) | ${current_price:.2f}")
                msg = (
                    f"🚨 <b>V9 منطقة التعزيز — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر: ${current_price:.2f} | دخول: ${entry_price:.2f}\n"
                    f"📉 خسارة: <b>-$0.35</b>\n"
                    f"🔄 جاري التعزيز بـ {ITM_REINFORCE_QTY} عقود...\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            time.sleep(10)
            
        except Exception as e:
            logger.error(f"[V9 Manual Monitor] Error: {e}")
            time.sleep(10)
    
    logger.info("[V9 Manual] Monitor stopped")


def _start_manual_monitor():
    """تشغيل thread مراقبة الصفقة اليدوية."""
    global _manual_monitor_thread, _manual_state
    
    _manual_state["monitor_active"] = True
    
    if _manual_monitor_thread and _manual_monitor_thread.is_alive():
        return
    
    _manual_monitor_thread = threading.Thread(
        target=_manual_monitor_loop, daemon=True, name="V9_Manual_Monitor"
    )
    _manual_monitor_thread.start()


def get_manual_status():
    """إرجاع حالة الخط 2 اليدوي للواجهة."""
    pos = _manual_state["position"]
    
    # جلب سعر TSLA الحالي
    snap = get_tsla_snapshot()
    tsla_price = snap["price"] if snap else 0
    
    # اقتراح عقد ITM للعرض (قبل الضغط)
    suggested_call = None
    suggested_put = None
    
    if tsla_price > 0 and not pos:
        try:
            c = find_itm_contract_for_manual(tsla_price, "call")
            if c:
                suggested_call = {
                    "symbol": c["symbol"],
                    "strike": c["strike"],
                    "mid": c["mid"],
                    "delta": c["approx_delta"],
                    "itm": c["itm_amount"]
                }
        except:
            pass
        
        try:
            p = find_itm_contract_for_manual(tsla_price, "put")
            if p:
                suggested_put = {
                    "symbol": p["symbol"],
                    "strike": p["strike"],
                    "mid": p["mid"],
                    "delta": p["approx_delta"],
                    "itm": p["itm_amount"]
                }
        except:
            pass
    
    return {
        "tsla_price": tsla_price,
        "has_position": bool(pos),
        "position": pos,
        "alert1_sent": _manual_state["alert1_sent"],
        "alert2_sent": _manual_state["alert2_sent"],
        "pnl_dollar": _manual_state["pnl_dollar"],
        "suggested_call": suggested_call,
        "suggested_put": suggested_put,
        "is_trading_hours": _is_scalp_window(),
        "et_time": _et_now().strftime("%I:%M:%S %p"),
        "last_reason": _manual_state.get("last_reason", ""),
        "last_pnl": _manual_state.get("last_pnl", 0)
    }


# ═══════════════════════════════════════════════════════════════
# V9.3 — TRADE JOURNAL (سجل الصفقات) — V10.3: SQLite Persistent Storage
# ═══════════════════════════════════════════════════════════════

import json
import os
import sqlite3
import threading as _journal_th

# ── مسار قاعدة البيانات الدائمة ─────────────────────────────────────────────
# الأولوية: JOURNAL_DB_PATH → /data/journal.db (Render Disk) → /tmp/journal.db
_JOURNAL_DB = os.environ.get(
    "JOURNAL_DB_PATH",
    "/data/journal.db" if os.path.isdir("/data") else "/tmp/journal.db"
)
_IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "journal_images")
os.makedirs(_IMAGES_DIR, exist_ok=True)
try:
    os.makedirs(os.path.dirname(_JOURNAL_DB), exist_ok=True)
except Exception:
    pass

_journal_db_lock = _journal_th.Lock()


def _get_db_conn():
    """فتح اتصال SQLite مع WAL للأداء."""
    conn = sqlite3.connect(_JOURNAL_DB, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_journal_db():
    """إنشاء جدول الصفقات إذا لم يكن موجوداً."""
    with _journal_db_lock:
        conn = _get_db_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   TEXT,
                status       TEXT DEFAULT 'open',
                direction    TEXT,
                symbol       TEXT,
                strike       REAL,
                qty          INTEGER,
                entry_price  REAL,
                exit_price   REAL,
                pnl_dollar   REAL,
                pnl_pct      REAL,
                exit_reason  TEXT,
                closed_at    TEXT,
                tsla_price   REAL,
                pre_trade    TEXT,
                notes        TEXT,
                image_file   TEXT,
                images_json  TEXT,
                extra_json   TEXT
            )
        """)
        # ── Migration: إضافة عمود images_json إذا لم يكن موجوداً ──
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN images_json TEXT")
            conn.commit()
            logger.info("[Journal] ✅ Migration: images_json column added")
        except Exception:
            pass  # العمود موجود مسبقاً
        conn.commit()
        conn.close()
    logger.info(f"[Journal] ✅ SQLite DB ready: {_JOURNAL_DB}")


# تهيئة قاعدة البيانات عند الاستيراد
_init_journal_db()


def _row_to_dict(row) -> dict:
    """تحويل صف SQLite إلى dict متوافق مع الكود القديم."""
    d = dict(row)
    if d.get("extra_json"):
        try:
            d.update(json.loads(d["extra_json"]))
        except Exception:
            pass
    d.pop("extra_json", None)
    if d.get("pre_trade") and isinstance(d["pre_trade"], str):
        try:
            d["pre_trade"] = json.loads(d["pre_trade"])
        except Exception:
            pass
    # تحويل images_json إلى قائمة
    if d.get("images_json") and isinstance(d["images_json"], str):
        try:
            d["images"] = json.loads(d["images_json"])
        except Exception:
            d["images"] = []
    else:
        d["images"] = []
    d.pop("images_json", None)
    return d


def _load_journal() -> list:
    """تحميل جميع الصفقات من SQLite (للتوافق مع الكود القديم)."""
    try:
        with _journal_db_lock:
            conn = _get_db_conn()
            rows = conn.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[Journal] Load error: {e}")
        return []


def _save_journal(entries: list):
    """للتوافق فقط — SQLite يحفظ مباشرة في add/update."""
    pass


def add_journal_entry(entry: dict):
    """إضافة صفقة جديدة للسجل الدائم (SQLite)."""
    try:
        created_at = _et_now().strftime("%Y-%m-%d %H:%M:%S ET")
        known = ["status", "direction", "symbol", "strike", "qty",
                 "entry_price", "exit_price", "pnl_dollar", "pnl_pct",
                 "exit_reason", "closed_at", "tsla_price", "notes", "image_file"]
        pre_trade = json.dumps(entry.get("pre_trade", {}), ensure_ascii=False)
        extra = {k: v for k, v in entry.items()
                 if k not in known + ["pre_trade", "id", "created_at"]}
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        with _journal_db_lock:
            conn = _get_db_conn()
            cur = conn.execute("""
                INSERT INTO trades
                (created_at, status, direction, symbol, strike, qty,
                 entry_price, exit_price, pnl_dollar, pnl_pct,
                 exit_reason, closed_at, tsla_price, pre_trade, notes, image_file, extra_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                created_at,
                entry.get("status", "open"),
                entry.get("direction"),
                entry.get("symbol"),
                entry.get("strike"),
                entry.get("qty"),
                entry.get("entry_price"),
                entry.get("exit_price"),
                entry.get("pnl_dollar"),
                entry.get("pnl_pct"),
                entry.get("exit_reason"),
                entry.get("closed_at"),
                entry.get("tsla_price"),
                pre_trade,
                entry.get("notes"),
                entry.get("image_file"),
                extra_json,
            ))
            new_id = cur.lastrowid
            conn.commit()
            conn.close()
        logger.info(f"[Journal] ✅ Entry #{new_id} saved permanently to SQLite")
        return new_id
    except Exception as e:
        logger.error(f"[Journal] add_journal_entry error: {e}")
        return -1


def update_journal_entry(entry_id: int, updates: dict):
    """تحديث صفقة موجودة في SQLite (إضافة نتيجة الخروج)."""
    try:
        known_cols = ["status", "direction", "symbol", "strike", "qty",
                      "entry_price", "exit_price", "pnl_dollar", "pnl_pct",
                      "exit_reason", "closed_at", "tsla_price", "notes", "image_file"]
        set_parts = []
        vals = []
        extra_updates = {}
        for k, v in updates.items():
            if k in known_cols:
                set_parts.append(f"{k} = ?")
                vals.append(v)
            elif k not in ("id", "created_at", "pre_trade"):
                extra_updates[k] = v
        if extra_updates:
            # دمج extra_json الحالي مع التحديثات الجديدة
            with _journal_db_lock:
                conn = _get_db_conn()
                row = conn.execute("SELECT extra_json FROM trades WHERE id=?", (entry_id,)).fetchone()
                conn.close()
            existing = {}
            if row and row["extra_json"]:
                try:
                    existing = json.loads(row["extra_json"])
                except Exception:
                    pass
            existing.update(extra_updates)
            set_parts.append("extra_json = ?")
            vals.append(json.dumps(existing, ensure_ascii=False))
        if not set_parts:
            return
        vals.append(entry_id)
        with _journal_db_lock:
            conn = _get_db_conn()
            conn.execute(f"UPDATE trades SET {', '.join(set_parts)} WHERE id=?", vals)
            conn.commit()
            conn.close()
        logger.info(f"[Journal] ✅ Entry #{entry_id} updated in SQLite")
    except Exception as e:
        logger.error(f"[Journal] update_journal_entry error: {e}")


def get_journal_entries(limit: int = 200) -> list:
    """إرجاع آخر N صفقة من SQLite."""
    try:
        with _journal_db_lock:
            conn = _get_db_conn()
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[Journal] get_journal_entries error: {e}")
        return []


def save_journal_image(entry_id: int, image_data: bytes, ext: str = "jpg", label: str = "") -> str:
    """
    حفظ صورة مرتبطة بصفقة في SQLite كـ Base64 (دائمة حتى بعد Restart).
    يُرجع data-URI للاستخدام المباشر في HTML.
    """
    import base64
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{mime};base64,{b64}"
    # تحميل قائمة الصور الحالية من DB
    try:
        with _journal_db_lock:
            conn = _get_db_conn()
            row = conn.execute(
                "SELECT images_json FROM trades WHERE id=?", (entry_id,)
            ).fetchone()
            conn.close()
        images = json.loads(row["images_json"]) if row and row["images_json"] else []
    except Exception:
        images = []
    # إضافة الصورة الجديدة
    images.append({"label": label or f"img_{len(images)+1}", "data": data_uri})
    # حفظ القائمة المحدّثة في DB
    try:
        with _journal_db_lock:
            conn = _get_db_conn()
            conn.execute(
                "UPDATE trades SET images_json=? WHERE id=?",
                (json.dumps(images, ensure_ascii=False), entry_id)
            )
            conn.commit()
            conn.close()
        logger.info(f"[Journal] ✅ Image saved to DB for trade #{entry_id} ({len(image_data)//1024}KB)")
    except Exception as e:
        logger.error(f"[Journal] save_journal_image error: {e}")
    return data_uri


def get_journal_stats() -> dict:
    """إحصائيات السجل الكاملة — V10."""
    entries = _load_journal()
    closed  = [e for e in entries if e.get("status") == "closed"]
    wins    = [e for e in closed if (e.get("pnl_dollar") or 0) > 0]
    losses  = [e for e in closed if (e.get("pnl_dollar") or 0) <= 0]

    total_pnl = sum(e.get("pnl_dollar", 0) for e in closed)
    win_rate  = (len(wins) / len(closed) * 100) if closed else 0
    avg_win   = round(sum(e.get("pnl_dollar", 0) for e in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(e.get("pnl_dollar", 0) for e in losses) / len(losses), 2) if losses else 0
    rr_ratio  = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0

    # ── نسبة الفوز CALL vs PUT ──────────────────────────────────────────────
    call_closed = [e for e in closed if e.get("direction") == "CALL"]
    put_closed  = [e for e in closed if e.get("direction") == "PUT"]
    call_wins   = [e for e in call_closed if (e.get("pnl_dollar") or 0) > 0]
    put_wins    = [e for e in put_closed  if (e.get("pnl_dollar") or 0) > 0]
    call_wr = round(len(call_wins) / len(call_closed) * 100, 1) if call_closed else None
    put_wr  = round(len(put_wins)  / len(put_closed)  * 100, 1) if put_closed  else None

    # ── أفضل وقت دخول (ساعة ET) ─────────────────────────────────────────────
    hour_stats: dict = {}  # {"10": {"wins": 0, "total": 0}, ...}
    for e in closed:
        ts = e.get("created_at", "")
        try:
            # صيغة: "2026-05-19 10:35:22 ET"
            hour = ts.split(" ")[1].split(":")[0]  # "10"
            if hour not in hour_stats:
                hour_stats[hour] = {"wins": 0, "total": 0}
            hour_stats[hour]["total"] += 1
            if (e.get("pnl_dollar") or 0) > 0:
                hour_stats[hour]["wins"] += 1
        except:
            pass
    best_hour = None
    best_hour_wr = 0
    for h, s in hour_stats.items():
        if s["total"] >= 2:  # على الأقل صفقتان
            wr = s["wins"] / s["total"] * 100
            if wr > best_hour_wr:
                best_hour_wr = wr
                best_hour = h

    # ── Streak (الصفقات الرابحة المتتالية) ──────────────────────────────────
    # الترتيب: الأقدم أولاً لحساب الـ streak الصحيح
    sorted_closed = sorted(closed, key=lambda x: x.get("created_at", ""))
    current_streak = 0
    longest_streak = 0
    temp_streak    = 0
    for e in sorted_closed:
        if (e.get("pnl_dollar") or 0) > 0:
            temp_streak += 1
            longest_streak = max(longest_streak, temp_streak)
        else:
            temp_streak = 0
    # الـ streak الحالي = من آخر صفقة للخلف
    for e in reversed(sorted_closed):
        if (e.get("pnl_dollar") or 0) > 0:
            current_streak += 1
        else:
            break

    return {
        "total":          len(entries),
        "closed":         len(closed),
        "open":           len(entries) - len(closed),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(win_rate, 1),
        "total_pnl":      round(total_pnl, 2),
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "rr_ratio":       rr_ratio,          # Risk/Reward الفعلي
        "call_win_rate":  call_wr,            # نسبة فوز CALL
        "put_win_rate":   put_wr,             # نسبة فوز PUT
        "call_count":     len(call_closed),
        "put_count":      len(put_closed),
        "best_hour":      best_hour,          # أفضل ساعة دخول
        "best_hour_wr":   round(best_hour_wr, 1) if best_hour else None,
        "current_streak": current_streak,     # الـ streak الحالي
        "longest_streak": longest_streak,     # أطول streak
    }


# ═══════════════════════════════════════════════════════════════════════════════
# V9.4 — ITM PRECISION ENTRY ENGINE (محرك الدخول الدقيق)
# ═══════════════════════════════════════════════════════════════════════════════
#
# المنطق:
#   - يشتغل بعد 40 دقيقة من الافتتاح (10:10 AM ET)
#   - يفحص شروط الدخول كل 30 ثانية
#   - إذا اكتملت الشروط → يرسل تنبيه Telegram
#   - لا يدخل تلقائياً — المتداول يقرر من /manual
#
# شروط CALL:
#   ✅ GEX: فوق Gamma Flip
#   ✅ VWAP: السعر فوق VWAP
#   ✅ CheddarFlow: CALL% > 60%
#   ✅ الموقع: قريب من مستوى دعم (Put Wall / PDL / OR Low / مستوى نفسي)
#   ❌ لا تدخل: السعر قريب من Call Wall (أقل من $5)
#
# شروط PUT:
#   ✅ GEX: تحت Gamma Flip
#   ✅ VWAP: السعر تحت VWAP
#   ✅ CheddarFlow: CALL% < 45% (PUT يسيطر)
#   ✅ الموقع: قريب من مستوى مقاومة (Call Wall / PDH / OR High)
#   ❌ لا تدخل: CheddarFlow CALL% > 75%
# ═══════════════════════════════════════════════════════════════════════════════

import threading as _threading_pe
import time as _time_pe

# ── إعدادات محرك الدخول الدقيق ──────────────────────────────────────────────
PE_CHECK_INTERVAL   = 30     # فحص كل 30 ثانية
PE_CALL_FLOW_MIN    = 60     # CheddarFlow CALL% الحد الأدنى للـ CALL
PE_PUT_FLOW_MAX     = 45     # CheddarFlow CALL% الحد الأقصى للـ PUT
PE_FLOW_BLOCK_CALL  = 75     # إذا CALL% > 75% لا تدخل PUT
PE_SUPPORT_PROX     = 0.008  # 0.8% = قريب من مستوى دعم
PE_RESIST_PROX      = 0.008  # 0.8% = قريب من مستوى مقاومة
PE_CALL_WALL_BLOCK  = 5.0    # $5 من Call Wall = لا تدخل CALL
PE_ALERT_COOLDOWN   = 300    # 5 دقائق بين كل تنبيهين

# ── حالة المحرك ──────────────────────────────────────────────────────────────
_pe_state = {
    "running":           False,
    "last_alert_time":   0,
    "last_alert_dir":    "",   # "CALL" أو "PUT"
    "cheddar_call_pct":  None, # آخر قراءة CheddarFlow
    "cheddar_updated":   0,    # وقت آخر تحديث
    "gex_levels":        {},   # GEX levels من FlashAlpha
    "signal_active":     False,
    "signal_direction":  "",
    "signal_reasons":    [],
    "signal_time":       "",
    "signal_price":      0.0,
}

_pe_lock = _threading_pe.Lock()


def update_cheddar_flow(call_pct: float):
    """
    يُستدعى من الخارج (app.py أو يدوياً) لتحديث نسبة CheddarFlow.
    call_pct: نسبة CALL من 0 إلى 100
    """
    with _pe_lock:
        _pe_state["cheddar_call_pct"] = float(call_pct)
        _pe_state["cheddar_updated"] = _time_pe.time()
    logger.info(f"[PE] CheddarFlow updated: CALL={call_pct:.1f}% | PUT={100-call_pct:.1f}%")


def update_gex_levels(levels: dict):
    """
    يُستدعى من FlashAlpha worker لتحديث مستويات GEX.
    levels: {
      "gamma_flip": float,
      "call_wall": float,
      "put_wall": float,
      "max_gamma": float,
    }
    """
    with _pe_lock:
        _pe_state["gex_levels"] = levels
    logger.info(f"[PE] GEX levels updated: {levels}")


def get_pe_status() -> dict:
    """إرجاع حالة محرك الدخول الدقيق للواجهة."""
    with _pe_lock:
        return dict(_pe_state)


def _check_entry_conditions(price: float) -> tuple:
    """
    فحص شروط الدخول الدقيقة.
    Returns: (direction: str|None, reasons: list, warnings: list)
      direction = "CALL" / "PUT" / None
    """
    reasons_call = []
    reasons_put  = []
    warnings     = []
    blocks_call  = []
    blocks_put   = []

    cheddar_pct  = _pe_state.get("cheddar_call_pct")
    gex          = _pe_state.get("gex_levels", {})
    vwap         = _state.get("vwap", 0)
    pdh          = _state.get("pdh", 0)
    pdl          = _state.get("pdl", 0)
    or_high      = _state.get("opening_range_high", 0)
    or_low       = _state.get("opening_range_low", 0)
    micro_res    = _state.get("micro_resistances", [])
    micro_sup    = _state.get("micro_supports", [])

    gamma_flip   = gex.get("gamma_flip", 0)
    call_wall    = gex.get("call_wall", 0)
    put_wall     = gex.get("put_wall", 0)
    max_gamma    = gex.get("max_gamma", 0)

    # ── 1. GEX — Gamma Flip ──────────────────────────────────────────────────
    if gamma_flip > 0:
        if price > gamma_flip:
            reasons_call.append(f"✅ فوق Gamma Flip ${gamma_flip:.2f}")
        else:
            reasons_put.append(f"✅ تحت Gamma Flip ${gamma_flip:.2f}")
            blocks_call.append(f"❌ تحت Gamma Flip — CALL خطر")
    else:
        warnings.append("⚠️ GEX غير متاح — تحقق من FlashAlpha")

    # ── 2. VWAP ──────────────────────────────────────────────────────────────
    if vwap > 0:
        if price > vwap * 1.001:
            reasons_call.append(f"✅ فوق VWAP ${vwap:.2f}")
        elif price < vwap * 0.999:
            reasons_put.append(f"✅ تحت VWAP ${vwap:.2f}")
        else:
            warnings.append(f"⚠️ قريب جداً من VWAP ${vwap:.2f} — انتظر")
            blocks_call.append("❌ VWAP Danger Zone")
            blocks_put.append("❌ VWAP Danger Zone")

    # ── 3. CheddarFlow ───────────────────────────────────────────────────────
    if cheddar_pct is not None:
        put_pct = 100 - cheddar_pct
        if cheddar_pct >= PE_CALL_FLOW_MIN:
            reasons_call.append(f"✅ CheddarFlow CALL {cheddar_pct:.0f}%")
        else:
            blocks_call.append(f"❌ CheddarFlow CALL ضعيف ({cheddar_pct:.0f}%)")

        if cheddar_pct <= PE_PUT_FLOW_MAX:
            reasons_put.append(f"✅ CheddarFlow PUT {put_pct:.0f}%")
        else:
            blocks_put.append(f"❌ CheddarFlow PUT ضعيف (CALL={cheddar_pct:.0f}%)")

        # حجب PUT إذا CALL قوي جداً
        if cheddar_pct > PE_FLOW_BLOCK_CALL:
            blocks_put.append(f"🚫 CheddarFlow CALL {cheddar_pct:.0f}% — لا تدخل PUT")
    else:
        warnings.append("⚠️ CheddarFlow غير محدّث — أدخل النسبة يدوياً")

    # ── 4. موقع السعر من المستويات ───────────────────────────────────────────
    # دعم قريب (للـ CALL)
    support_near = False
    support_name = ""

    # Put Wall
    if put_wall > 0 and price > put_wall:
        dist = (price - put_wall) / price
        if dist <= PE_SUPPORT_PROX:
            support_near = True
            support_name = f"Put Wall ${put_wall:.2f}"

    # PDL
    if pdl > 0 and price > pdl:
        dist = (price - pdl) / price
        if dist <= PE_SUPPORT_PROX:
            support_near = True
            support_name = f"PDL ${pdl:.2f}"

    # OR Low
    if or_low > 0 and price > or_low:
        dist = (price - or_low) / price
        if dist <= PE_SUPPORT_PROX:
            support_near = True
            support_name = f"OR Low ${or_low:.2f}"

    # Micro Support
    for s in micro_sup:
        if price > s:
            dist = (price - s) / price
            if dist <= PE_SUPPORT_PROX:
                support_near = True
                support_name = f"Micro Support ${s:.2f}"
                break

    # Psychological level (دعم)
    psych, psych_dist = get_nearest_psych_level(price)
    if psych_dist <= 0.005 and price >= psych:
        support_near = True
        support_name = f"رقم نفسي ${psych:.0f}"

    if support_near:
        reasons_call.append(f"✅ قريب من دعم: {support_name}")
    else:
        warnings.append("⚠️ لا يوجد دعم قريب — انتظر بولباك")

    # مقاومة قريبة (للـ PUT)
    resist_near = False
    resist_name = ""

    # Call Wall
    if call_wall > 0 and price < call_wall:
        dist = (call_wall - price) / price
        if dist <= PE_RESIST_PROX:
            resist_near = True
            resist_name = f"Call Wall ${call_wall:.2f}"

    # PDH
    if pdh > 0 and price < pdh:
        dist = (pdh - price) / price
        if dist <= PE_RESIST_PROX:
            resist_near = True
            resist_name = f"PDH ${pdh:.2f}"

    # OR High
    if or_high > 0 and price < or_high:
        dist = (or_high - price) / price
        if dist <= PE_RESIST_PROX:
            resist_near = True
            resist_name = f"OR High ${or_high:.2f}"

    # Micro Resistance
    for r in micro_res:
        if price < r:
            dist = (r - price) / price
            if dist <= PE_RESIST_PROX:
                resist_near = True
                resist_name = f"Micro Resistance ${r:.2f}"
                break

    if resist_near:
        reasons_put.append(f"✅ قريب من مقاومة: {resist_name}")
    else:
        warnings.append("⚠️ لا توجد مقاومة قريبة للـ PUT")

    # ── 5. حجب CALL إذا قريب من Call Wall ───────────────────────────────────
    if call_wall > 0:
        dist_to_wall = call_wall - price
        if 0 < dist_to_wall < PE_CALL_WALL_BLOCK:
            blocks_call.append(f"🚫 قريب من Call Wall ${call_wall:.2f} (فرق ${dist_to_wall:.2f})")

    # ── 6. Max Gamma كمعلومة إضافية ──────────────────────────────────────────
    if max_gamma > 0:
        dist_mg = abs(price - max_gamma) / price
        if dist_mg <= 0.01:
            warnings.append(f"🧲 قريب من Max Gamma ${max_gamma:.2f} (مغناطيس)")

    # ── القرار النهائي ────────────────────────────────────────────────────────
    # CALL: يحتاج 3 شروط أساسية (GEX + VWAP + CheddarFlow) + لا حجب
    call_score = len(reasons_call)
    put_score  = len(reasons_put)

    if call_score >= 3 and not blocks_call:
        return "CALL", reasons_call, warnings
    elif put_score >= 3 and not blocks_put:
        return "PUT", reasons_put, warnings
    else:
        return None, [], warnings + blocks_call + blocks_put


def _build_alert_message(direction: str, price: float, reasons: list, warnings: list) -> str:
    """بناء رسالة تنبيه Telegram منظمة."""
    now_str = _et_now().strftime("%I:%M %p ET")
    arrow   = "📈" if direction == "CALL" else "📉"
    color   = "🟢" if direction == "CALL" else "🔴"

    gex    = _pe_state.get("gex_levels", {})
    vwap   = _state.get("vwap", 0)
    cheddar = _pe_state.get("cheddar_call_pct")

    lines = [
        f"{color} {arrow} *إشارة دخول {direction}*",
        f"━━━━━━━━━━━━━━━━━",
        f"💵 TSLA: *${price:.2f}*",
        f"🕐 {now_str}",
        "",
        "*الشروط المكتملة:*",
    ]
    for r in reasons:
        lines.append(f"  {r}")

    if warnings:
        lines.append("")
        lines.append("*ملاحظات:*")
        for w in warnings:
            lines.append(f"  {w}")

    lines.append("")
    lines.append("*المستويات الحالية:*")
    if gex.get("gamma_flip"):
        lines.append(f"  Gamma Flip: ${gex['gamma_flip']:.2f}")
    if gex.get("call_wall"):
        lines.append(f"  Call Wall: ${gex['call_wall']:.2f}")
    if gex.get("put_wall"):
        lines.append(f"  Put Wall: ${gex['put_wall']:.2f}")
    if vwap > 0:
        lines.append(f"  VWAP: ${vwap:.2f}")
    if cheddar is not None:
        lines.append(f"  CheddarFlow: CALL {cheddar:.0f}% | PUT {100-cheddar:.0f}%")

    lines.append("")
    lines.append("👆 *افتح /manual للدخول*")

    return "\n".join(lines)


def _pe_engine_loop():
    """حلقة محرك الدخول الدقيق — تعمل كـ background thread."""
    logger.info("[PE] ITM Precision Entry Engine started ✅")

    while _pe_state["running"]:
        try:
            now = _et_now()
            now_minutes = now.hour * 60 + now.minute

            # نافذة التداول: 10:10 AM - 12:40 PM ET
            window_start = 10 * 60 + 10
            window_end   = 12 * 60 + 40

            if not (window_start <= now_minutes <= window_end):
                # خارج النافذة — انتظر
                with _pe_lock:
                    _pe_state["signal_active"]    = False
                    _pe_state["signal_direction"] = ""
                    _pe_state["signal_reasons"]   = []
                _time_pe.sleep(PE_CHECK_INTERVAL)
                continue

            # جلب سعر TSLA
            snap = get_tsla_snapshot()
            if not snap or snap["price"] <= 0:
                _time_pe.sleep(PE_CHECK_INTERVAL)
                continue

            price = snap["price"]

            # تحديث VWAP
            bars_1m = get_tsla_bars("1Min", 60)
            if bars_1m:
                cum_pv = sum(float(b["c"]) * int(b.get("v", 0)) for b in bars_1m)
                cum_v  = sum(int(b.get("v", 0)) for b in bars_1m)
                if cum_v > 0:
                    _state["vwap"] = cum_pv / cum_v

            # تحديث Micro S/R
            compute_micro_sr()

            # فحص الشروط
            with _pe_lock:
                direction, reasons, warnings = _check_entry_conditions(price)

            if direction:
                now_ts = _time_pe.time()
                last_alert = _pe_state["last_alert_time"]
                last_dir   = _pe_state["last_alert_dir"]

                # إرسال تنبيه إذا:
                # 1. مضت 5 دقائق من آخر تنبيه
                # 2. أو تغير الاتجاه
                should_alert = (
                    (now_ts - last_alert) >= PE_ALERT_COOLDOWN
                    or last_dir != direction
                )

                with _pe_lock:
                    _pe_state["signal_active"]    = True
                    _pe_state["signal_direction"] = direction
                    _pe_state["signal_reasons"]   = reasons
                    _pe_state["signal_time"]      = now.strftime("%I:%M %p")
                    _pe_state["signal_price"]     = price

                if should_alert:
                    msg = _build_alert_message(direction, price, reasons, warnings)
                    send_telegram(msg)
                    with _pe_lock:
                        _pe_state["last_alert_time"] = now_ts
                        _pe_state["last_alert_dir"]  = direction
                    logger.info(f"[PE] Alert sent: {direction} @ ${price:.2f}")
            else:
                with _pe_lock:
                    _pe_state["signal_active"]    = False
                    _pe_state["signal_direction"] = ""
                    _pe_state["signal_reasons"]   = []

        except Exception as e:
            logger.error(f"[PE] Engine error: {e}")

        _time_pe.sleep(PE_CHECK_INTERVAL)

    logger.info("[PE] ITM Precision Entry Engine stopped.")


def start_pe_engine():
    """تشغيل محرك الدخول الدقيق."""
    if _pe_state["running"]:
        return
    _pe_state["running"] = True
    t = _threading_pe.Thread(target=_pe_engine_loop, daemon=True)
    t.start()
    logger.info("[PE] Precision Entry Engine thread started ✅")


def stop_pe_engine():
    """إيقاف محرك الدخول الدقيق."""
    _pe_state["running"] = False
    logger.info("[PE] Precision Entry Engine stopping...")


# ══════════════════════════════════════════════════════════════════════════════
# PAIR TRADE ENGINE — XOM CALL (×2) + XLE PUT (×4)
# Beta-Weighted Relative Value Options Pair Trade
# Target: +30% combined profit on total premium paid
# ══════════════════════════════════════════════════════════════════════════════

PAIR_XOM_CONTRACTS = 2
PAIR_XLE_CONTRACTS = 4
PAIR_TP_PCT        = 0.10   # تم تعديله من 30% إلى 10% بناءً على طلب المستخدم
PAIR_DTE_MIN       = 14
PAIR_DTE_MAX       = 21

PAIR_STATE_FILE = "/tmp/pair_state.json"

def _save_pair_state():
    """حفظ حالة Pair Trade في ملف مؤقت."""
    try:
        import json
        with open(PAIR_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_pair_state, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"[Pair] Save state error: {e}")

def _load_pair_state():
    """تحميل حالة Pair Trade من الملف عند بدء التشغيل."""
    try:
        import json, os
        if os.path.exists(PAIR_STATE_FILE):
            with open(PAIR_STATE_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            if saved.get("position") and saved["position"].get("status") == "open":
                _pair_state.update(saved)
                _pair_state["monitor_active"] = True
                logger.info("[Pair] Restored open position from file — restarting monitor")
                _start_pair_monitor()
            else:
                logger.info("[Pair] No open position in saved state")
    except Exception as e:
        logger.error(f"[Pair] Load state error: {e}")

_pair_state = {
    "position": None,
    "monitor_active": False,
    "total_cost": 0.0,
    "target_value": 0.0,
    "current_value": 0.0,
    "pnl_dollar": 0.0,
    "pnl_pct": 0.0,
}
_pair_monitor_thread = None
_pair_lock = threading.Lock()


def get_stock_price(ticker):
    """جلب سعر سهم من Alpaca snapshot."""
    try:
        r = _session.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/snapshot",
            headers=_headers(),
            params={"feed": "iex"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            price = float(data.get("latestTrade", {}).get("p", 0))
            if price <= 0:
                price = float(data.get("dailyBar", {}).get("c", 0))
            if price <= 0:
                price = float(data.get("prevDailyBar", {}).get("c", 0))
            return price
    except Exception as e:
        logger.error(f"[Pair] Stock price error {ticker}: {e}")
    return 0.0


def get_options_chain_generic(ticker, expiry_date, option_type="call",
                               strike_min=None, strike_max=None):
    """جلب سلسلة الخيارات لأي سهم."""
    try:
        params = {
            "underlying_symbols": ticker,
            "expiration_date": expiry_date,
            "type": option_type,
            "limit": 50
        }
        if strike_min is not None:
            params["strike_price_gte"] = str(strike_min)
        if strike_max is not None:
            params["strike_price_lte"] = str(strike_max)
        r = _session.get(
            f"{ALPACA_BASE_URL}/v2/options/contracts",
            headers=_headers(),
            params=params,
            timeout=15
        )
        if r.status_code == 200:
            return r.json().get("option_contracts", [])
        else:
            logger.warning(f"[Pair] Chain {ticker}/{expiry_date}: {r.status_code}")
    except Exception as e:
        logger.error(f"[Pair] Chain error {ticker}: {e}")
    return []


def find_atm_contract_pair(ticker, price, option_type):
    """
    يجد أقرب عقد ATM لـ ticker في نافذة DTE 14-21 يوماً.
    Returns: (contract_dict, expiry_date) أو (None, None)
    """
    et_now = datetime.now(timezone.utc) - timedelta(hours=4)
    expiry_candidates = []
    for i in range(PAIR_DTE_MIN, PAIR_DTE_MAX + 1):
        d = et_now + timedelta(days=i)
        if d.weekday() < 5:
            expiry_candidates.append(d.strftime('%Y-%m-%d'))

    if not expiry_candidates:
        return None, None

    strike_min = round(price * 0.92, 0)
    strike_max = round(price * 1.08, 0)

    for expiry in expiry_candidates:
        contracts = get_options_chain_generic(ticker, expiry, option_type, strike_min, strike_max)
        if not contracts:
            continue
        best = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - price))
        symbol = best.get("symbol", "")
        quote = get_option_quote(symbol)
        if quote and quote["mid"] > 0:
            logger.info(f"[Pair] ATM {ticker} {option_type.upper()} Strike=${float(best['strike_price']):.2f} Mid=${quote['mid']:.2f} Expiry={expiry}")
            return {
                "symbol": symbol,
                "strike": float(best.get("strike_price", 0)),
                "type": option_type,
                "expiry": expiry,
                "bid": quote["bid"],
                "ask": quote["ask"],
                "mid": quote["mid"],
            }, expiry

    logger.warning(f"[Pair] No ATM contract for {ticker} {option_type} in DTE {PAIR_DTE_MIN}-{PAIR_DTE_MAX}")
    return None, None


def scan_pair_contracts():
    """
    مسح وإيجاد عقود XOM CALL و XLE PUT بنفس تاريخ الانتهاء.
    Returns: (xom_contract, xle_contract, shared_expiry) أو (None, None, None)
    """
    xom_price = get_stock_price("XOM")
    xle_price = get_stock_price("XLE")

    if xom_price <= 0 or xle_price <= 0:
        logger.error(f"[Pair] Cannot get prices: XOM=${xom_price} XLE=${xle_price}")
        return None, None, None

    logger.info(f"[Pair] Scanning: XOM=${xom_price:.2f} XLE=${xle_price:.2f}")

    xom_contract, xom_expiry = find_atm_contract_pair("XOM", xom_price, "call")
    if not xom_contract:
        return None, None, None

    # حاول نفس expiry لـ XLE أولاً
    xle_contract = None
    contracts = get_options_chain_generic(
        "XLE", xom_expiry, "put",
        round(xle_price * 0.92, 0), round(xle_price * 1.08, 0)
    )
    if contracts:
        best = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - xle_price))
        symbol = best.get("symbol", "")
        quote = get_option_quote(symbol)
        if quote and quote["mid"] > 0:
            xle_contract = {
                "symbol": symbol,
                "strike": float(best.get("strike_price", 0)),
                "type": "put",
                "expiry": xom_expiry,
                "bid": quote["bid"],
                "ask": quote["ask"],
                "mid": quote["mid"],
            }

    if not xle_contract:
        xle_contract, _ = find_atm_contract_pair("XLE", xle_price, "put")
        if not xle_contract:
            return None, None, None

    return xom_contract, xle_contract, xom_expiry


def execute_pair_trade():
    """تنفيذ Pair Trade: شراء 2 XOM CALL + 4 XLE PUT."""
    global _pair_state

    with _pair_lock:
        if _pair_state["position"]:
            return False, {"error": "يوجد Pair Trade مفتوح — أغلقه أولاً"}

    xom_c, xle_c, expiry = scan_pair_contracts()
    if not xom_c or not xle_c:
        return False, {"error": "لم يُعثر على عقود ATM مناسبة في نافذة DTE 14-21"}

    xom_cost   = xom_c["mid"] * PAIR_XOM_CONTRACTS * 100
    xle_cost   = xle_c["mid"] * PAIR_XLE_CONTRACTS * 100
    total_cost = round(xom_cost + xle_cost, 2)
    target_value = round(total_cost * (1 + PAIR_TP_PCT), 2)

    logger.info(f"[Pair] Executing: XOM CALL {PAIR_XOM_CONTRACTS}x@${xom_c['mid']:.2f} + XLE PUT {PAIR_XLE_CONTRACTS}x@${xle_c['mid']:.2f} | Total=${total_cost:.2f} | Target=${target_value:.2f}")

    xom_order = place_option_order(symbol=xom_c["symbol"], qty=PAIR_XOM_CONTRACTS,
                                   side="buy", order_type="market", position_intent="buy_to_open")
    if not xom_order:
        return False, {"error": "فشل تنفيذ أمر XOM CALL"}

    xle_order = place_option_order(symbol=xle_c["symbol"], qty=PAIR_XLE_CONTRACTS,
                                   side="buy", order_type="market", position_intent="buy_to_open")
    if not xle_order:
        try:
            cancel_order(xom_order.get("id"))
        except Exception:
            pass
        return False, {"error": "فشل تنفيذ أمر XLE PUT (تم إلغاء XOM)"}

    position = {
        "xom": {
            "symbol": xom_c["symbol"], "strike": xom_c["strike"],
            "expiry": xom_c["expiry"], "qty": PAIR_XOM_CONTRACTS,
            "entry_price": xom_c["mid"], "current_price": xom_c["mid"],
            "order_id": xom_order.get("id"),
        },
        "xle": {
            "symbol": xle_c["symbol"], "strike": xle_c["strike"],
            "expiry": xle_c["expiry"], "qty": PAIR_XLE_CONTRACTS,
            "entry_price": xle_c["mid"], "current_price": xle_c["mid"],
            "order_id": xle_order.get("id"),
        },
        "total_cost": total_cost,
        "target_value": target_value,
        "shared_expiry": expiry,
        "entry_time": _et_now().strftime("%I:%M:%S %p ET"),
        "status": "open",
    }

    with _pair_lock:
        _pair_state["position"]      = position
        _pair_state["total_cost"]    = total_cost
        _pair_state["target_value"]  = target_value
        _pair_state["current_value"] = total_cost
        _pair_state["pnl_dollar"]    = 0.0
        _pair_state["pnl_pct"]       = 0.0
        _pair_state["monitor_active"] = True

    msg = (
        f"🔀 <b>Pair Trade مفتوح</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 XOM CALL ×{PAIR_XOM_CONTRACTS}: {xom_c['symbol']}\n"
        f"   Strike: ${xom_c['strike']:.2f} | Mid: ${xom_c['mid']:.2f}\n"
        f"📉 XLE PUT ×{PAIR_XLE_CONTRACTS}: {xle_c['symbol']}\n"
        f"   Strike: ${xle_c['strike']:.2f} | Mid: ${xle_c['mid']:.2f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 إجمالي القسط: ${total_cost:.2f}\n"
        f"🎯 هدف الربح (+30%): ${target_value:.2f}\n"
        f"📅 Expiry: {expiry}\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)
    _save_pair_state()  # حفظ الحالة لمنع الاختفاء عند restart
    _start_pair_monitor()

    return True, {
        "xom_symbol": xom_c["symbol"], "xom_strike": xom_c["strike"],
        "xom_mid": xom_c["mid"], "xom_qty": PAIR_XOM_CONTRACTS,
        "xle_symbol": xle_c["symbol"], "xle_strike": xle_c["strike"],
        "xle_mid": xle_c["mid"], "xle_qty": PAIR_XLE_CONTRACTS,
        "total_cost": total_cost, "target_value": target_value,
        "shared_expiry": expiry, "entry_time": position["entry_time"],
    }


def close_pair_trade(reason="manual"):
    """إغلاق Pair Trade: بيع جميع العقود الـ 6."""
    global _pair_state

    with _pair_lock:
        pos = _pair_state.get("position")

    if not pos or pos.get("status") != "open":
        return False, {"error": "لا يوجد Pair Trade مفتوح"}

    xom = pos["xom"]
    xle = pos["xle"]

    xom_quote = get_option_quote(xom["symbol"])
    xle_quote = get_option_quote(xle["symbol"])
    xom_exit  = xom_quote["mid"] if xom_quote and xom_quote["mid"] > 0 else xom["entry_price"]
    xle_exit  = xle_quote["mid"] if xle_quote and xle_quote["mid"] > 0 else xle["entry_price"]

    place_option_order(symbol=xom["symbol"], qty=xom["qty"], side="sell",
                       order_type="market", position_intent="sell_to_close")
    place_option_order(symbol=xle["symbol"], qty=xle["qty"], side="sell",
                       order_type="market", position_intent="sell_to_close")

    current_value = round(xom_exit * xom["qty"] * 100 + xle_exit * xle["qty"] * 100, 2)
    total_cost    = pos["total_cost"]
    pnl_dollar    = round(current_value - total_cost, 2)
    pnl_pct       = round((pnl_dollar / total_cost) * 100, 1) if total_cost > 0 else 0

    with _pair_lock:
        _pair_state["position"]["status"] = "closed"
        _pair_state["monitor_active"]     = False
        _pair_state["pnl_dollar"]         = pnl_dollar
        _pair_state["pnl_pct"]            = pnl_pct
        _pair_state["current_value"]      = current_value

    reason_ar = {"TP": "جني أرباح تلقائي +30% ✅", "manual": "إغلاق يدوي 👆"}.get(reason, reason)
    emoji = "✅" if pnl_dollar >= 0 else "❌"
    msg = (
        f"{emoji} <b>Pair Trade مغلق</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 XOM CALL: ${xom['entry_price']:.2f} → ${xom_exit:.2f}\n"
        f"📉 XLE PUT: ${xle['entry_price']:.2f} → ${xle_exit:.2f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 دخول: ${total_cost:.2f} | خروج: ${current_value:.2f}\n"
        f"{'💚' if pnl_dollar >= 0 else '🔴'} P&L: ${pnl_dollar:+.2f} ({pnl_pct:+.1f}%)\n"
        f"📌 السبب: {reason_ar}\n"
        f"🕐 {_et_now().strftime('%I:%M %p')} ET"
    )
    send_telegram(msg)
    _save_pair_state()  # تحديث الملف بعد الإغلاق
    logger.info(f"[Pair] Closed | PnL=${pnl_dollar:+.2f} ({pnl_pct:+.1f}%) | Reason={reason}")

    return True, {
        "pnl_dollar": pnl_dollar, "pnl_pct": pnl_pct,
        "total_cost": total_cost, "current_value": current_value,
        "xom_exit": xom_exit, "xle_exit": xle_exit, "reason": reason,
    }


def _pair_monitor_loop():
    """حلقة مراقبة Pair Trade — تُغلق تلقائياً عند +30%."""
    global _pair_state
    logger.info("[Pair] Monitor started")

    while True:
        with _pair_lock:
            active = _pair_state["monitor_active"]
        if not active:
            break

        try:
            with _pair_lock:
                pos = _pair_state.get("position")

            if not pos or pos.get("status") != "open":
                with _pair_lock:
                    _pair_state["monitor_active"] = False
                break

            xom = pos["xom"]
            xle = pos["xle"]

            xom_q = get_option_quote(xom["symbol"])
            xle_q = get_option_quote(xle["symbol"])

            if not xom_q or not xle_q:
                time.sleep(10)
                continue

            xom_now = xom_q["mid"] if xom_q["mid"] > 0 else xom["entry_price"]
            xle_now = xle_q["mid"] if xle_q["mid"] > 0 else xle["entry_price"]

            current_value = round(xom_now * xom["qty"] * 100 + xle_now * xle["qty"] * 100, 2)
            total_cost    = pos["total_cost"]
            target_value  = pos["target_value"]
            pnl_dollar    = round(current_value - total_cost, 2)
            pnl_pct       = round((pnl_dollar / total_cost) * 100, 1) if total_cost > 0 else 0

            with _pair_lock:
                _pair_state["current_value"]     = current_value
                _pair_state["pnl_dollar"]        = pnl_dollar
                _pair_state["pnl_pct"]           = pnl_pct
                pos["xom"]["current_price"]      = xom_now
                pos["xle"]["current_price"]      = xle_now

            logger.debug(f"[Pair] Value=${current_value:.2f} PnL=${pnl_dollar:+.2f} ({pnl_pct:+.1f}%) Target=${target_value:.2f}")

            if current_value >= target_value:
                logger.info(f"[Pair] TP hit! ${current_value:.2f} >= ${target_value:.2f} (+30%)")
                close_pair_trade(reason="TP")
                break

        except Exception as e:
            logger.error(f"[Pair Monitor] Error: {e}")

        time.sleep(10)

    logger.info("[Pair] Monitor stopped")


def _start_pair_monitor():
    """تشغيل thread مراقبة Pair Trade."""
    global _pair_monitor_thread
    if _pair_monitor_thread and _pair_monitor_thread.is_alive():
        return
    _pair_monitor_thread = threading.Thread(
        target=_pair_monitor_loop, daemon=True, name="Pair_Monitor"
    )
    _pair_monitor_thread.start()


def get_pair_status():
    """إرجاع حالة Pair Trade للواجهة."""
    with _pair_lock:
        pos           = _pair_state.get("position")
        total_cost    = _pair_state["total_cost"]
        target_value  = _pair_state["target_value"]
        current_value = _pair_state["current_value"]
        pnl_dollar    = _pair_state["pnl_dollar"]
        pnl_pct       = _pair_state["pnl_pct"]

    if not pos:
        return {"has_position": False, "total_cost": 0, "target_value": 0,
                "current_value": 0, "pnl_dollar": 0, "pnl_pct": 0}

    return {
        "has_position":   pos.get("status") == "open",
        "status":         pos.get("status", "closed"),
        "xom_symbol":     pos["xom"]["symbol"],
        "xom_strike":     pos["xom"]["strike"],
        "xom_qty":        pos["xom"]["qty"],
        "xom_entry":      pos["xom"]["entry_price"],
        "xom_current":    pos["xom"].get("current_price", pos["xom"]["entry_price"]),
        "xle_symbol":     pos["xle"]["symbol"],
        "xle_strike":     pos["xle"]["strike"],
        "xle_qty":        pos["xle"]["qty"],
        "xle_entry":      pos["xle"]["entry_price"],
        "xle_current":    pos["xle"].get("current_price", pos["xle"]["entry_price"]),
        "total_cost":     total_cost,
        "target_value":   target_value,
        "current_value":  current_value,
        "pnl_dollar":     pnl_dollar,
        "pnl_pct":        pnl_pct,
        "entry_time":     pos.get("entry_time", ""),
        "shared_expiry":  pos.get("shared_expiry", ""),
        "tp_pct":         PAIR_TP_PCT * 100,
    }

# ══════════════════════════════════════════════════════════════════════════════
# TSLA 5M REVERSAL WARNING SYSTEM — V10.2
# 4 Conditions: Bearish Divergence | Trend Break | Price Rejection | Volume Exhaustion
# ══════════════════════════════════════════════════════════════════════════════

_reversal_warn_state = {
    "last_alert_time":   {},
    "last_check":        None,
    "last_candle_time":  None,
    "alerts_today":      [],
    "active":            False,
    "current_conditions": {
        "BEARISH_DIVERGENCE": False,
        "TREND_BREAKDOWN":    False,
        "PRICE_REJECTION":    False,
        "VOLUME_EXHAUSTION":  False,
    },
}
_reversal_warn_lock = threading.Lock()

def _rw_ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def _rw_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def _rw_macd_hist(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal + 2:
        return None, None
    macd_series = []
    for i in range(slow, len(closes) + 1):
        ef = _rw_ema(closes[:i], fast)
        es = _rw_ema(closes[:i], slow)
        if ef and es:
            macd_series.append(ef - es)
    if len(macd_series) < signal + 2:
        return None, None
    sig_line = _rw_ema(macd_series, signal)
    if sig_line is None:
        return None, None
    curr_hist = macd_series[-1] - sig_line
    prev_hist = macd_series[-2] - _rw_ema(macd_series[:-1], signal) if len(macd_series) > signal else None
    return curr_hist, prev_hist

def _rw_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return round(mid + std_dev * std, 4), round(mid, 4), round(mid - std_dev * std, 4)

def _rw_obv(closes, volumes):
    if len(closes) < 2:
        return []
    obv = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv

def _rw_check_bearish_divergence(bars):
    if len(bars) < 22:
        return None
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    curr_high = highs[-1]
    prev_high = max(highs[-7:-1])
    if curr_high <= prev_high:
        return None
    curr_rsi = _rw_rsi(closes[-17:], 14)
    prev_rsi = _rw_rsi(closes[-22:-5], 14)
    curr_hist, prev_hist = _rw_macd_hist(closes[-50:])
    rsi_div  = (curr_rsi is not None and prev_rsi is not None and curr_rsi < prev_rsi - 1)
    macd_div = (curr_hist is not None and prev_hist is not None and curr_hist < prev_hist)
    if rsi_div or macd_div:
        indicator = "RSI" if rsi_div else "MACD Histogram"
        return {
            "condition": "BEARISH_DIVERGENCE",
            "name": "Bearish Divergence",
            "detail": f"Bearish Divergence Detected - Momentum is Weakening! ({indicator} Lower High while Price Higher High)",
            "price": closes[-1],
        }
    return None

def _rw_check_trend_breakdown(bars):
    if len(bars) < 12:
        return None
    closes = [b["c"] for b in bars]
    ema9_curr = _rw_ema(closes, 9)
    ema9_prev = _rw_ema(closes[:-1], 9)
    if ema9_curr is None or ema9_prev is None:
        return None
    if closes[-2] >= ema9_prev and closes[-1] < ema9_curr:
        return {
            "condition": "TREND_BREAKDOWN",
            "name": "Trend Break",
            "detail": f"Trend Break - Candle Closed Below 9 EMA. Exit Calls! (Close: ${closes[-1]:.2f} | EMA9: ${ema9_curr:.2f})",
            "price": closes[-1],
        }
    return None

def _rw_check_price_rejection(bars):
    if len(bars) < 22:
        return None
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    opens  = [b["o"] for b in bars]
    upper_bb, _, _ = _rw_bollinger(closes, 20, 2)
    if upper_bb is None:
        return None
    curr_high  = highs[-1]
    curr_close = closes[-1]
    curr_open  = opens[-1]
    body = abs(curr_close - curr_open)
    upper_wick = curr_high - max(curr_close, curr_open)
    if body < 0.01:
        return None
    wick_ratio = upper_wick / body
    near_upper_bb = curr_high >= upper_bb * 0.997
    round_lvl = round(curr_high / 0.5) * 0.5
    near_round = abs(curr_high - round_lvl) <= 0.35
    if wick_ratio >= 2.0 and (near_upper_bb or near_round):
        reason = "Upper Bollinger Band" if near_upper_bb else f"Round Number ${round_lvl:.2f}"
        return {
            "condition": "PRICE_REJECTION",
            "name": "Price Rejection",
            "detail": f"Institutional Selling / Price Rejection at {reason}! (Wick: {wick_ratio:.1f}x body)",
            "price": curr_close,
        }
    return None

def _rw_check_volume_exhaustion(bars):
    if len(bars) < 25:
        return None
    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    if not (closes[-1] > closes[-2] > closes[-3]):
        return None
    vol_declining = volumes[-1] < volumes[-2] < volumes[-3]
    avg_vol_20 = sum(volumes[-21:-1]) / 20
    vol_below_avg = volumes[-1] < avg_vol_20 * 0.85 if avg_vol_20 > 0 else False
    obv = _rw_obv(closes[-10:], volumes[-10:])
    obv_flat = len(obv) >= 4 and (obv[-1] <= obv[-3])
    if (vol_declining or vol_below_avg) and obv_flat:
        reason = "Volume declining + OBV flattening" if vol_declining else "Volume below 20-avg + OBV flattening"
        return {
            "condition": "VOLUME_EXHAUSTION",
            "name": "Volume Exhaustion",
            "detail": f"Fake Move - Price Rising on Dying Volume/OBV! ({reason})",
            "price": closes[-1],
        }
    return None

def _rw_send_telegram(alert):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    msg = (
        f"🚨 *[TSLA 5M WARNING]: {alert['name']}* triggered at price *${alert['price']:.2f}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ {alert['detail']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🛑 *Consider stopping Scalping/Exiting Positions immediately!* 🛑"
    )
    try:
        r = http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"[ReversalWarn] Telegram error: {e}")
        return False

def _reversal_warning_loop():
    logger.info("[ReversalWarn] 5M Reversal Warning Monitor started ✅")
    with _reversal_warn_lock:
        _reversal_warn_state["active"] = True
    while True:
        try:
            now_et = _et_now()
            market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
            market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
            if not (market_open <= now_et <= market_close):
                time.sleep(60)
                continue
            bars = get_tsla_bars(timeframe="5Min", limit=55)
            if not bars or len(bars) < 25:
                time.sleep(60)
                continue
            latest_t = bars[-1].get("t", "")
            with _reversal_warn_lock:
                if latest_t == _reversal_warn_state["last_candle_time"]:
                    time.sleep(30)
                    continue
                _reversal_warn_state["last_candle_time"] = latest_t
                _reversal_warn_state["last_check"] = now_et.strftime("%H:%M:%S ET")
            check_results = [
                ("BEARISH_DIVERGENCE", _rw_check_bearish_divergence(bars)),
                ("TREND_BREAKDOWN",    _rw_check_trend_breakdown(bars)),
                ("PRICE_REJECTION",    _rw_check_price_rejection(bars)),
                ("VOLUME_EXHAUSTION",  _rw_check_volume_exhaustion(bars)),
            ]
            # Update live condition states
            with _reversal_warn_lock:
                for cond_key, alert_result in check_results:
                    _reversal_warn_state["current_conditions"][cond_key] = (alert_result is not None)
            checks = [r for _, r in check_results]
            for alert in checks:
                if alert is None:
                    continue
                cond = alert["condition"]
                now_ts = time.time()
                with _reversal_warn_lock:
                    last_sent = _reversal_warn_state["last_alert_time"].get(cond, 0)
                    if now_ts - last_sent < 300:
                        continue
                    _reversal_warn_state["last_alert_time"][cond] = now_ts
                    _reversal_warn_state["alerts_today"].append({
                        "time":      now_et.strftime("%H:%M ET"),
                        "condition": cond,
                        "name":      alert["name"],
                        "price":     alert["price"],
                        "detail":    alert["detail"],
                    })
                    _reversal_warn_state["alerts_today"] = _reversal_warn_state["alerts_today"][-20:]
                logger.warning(f"[ReversalWarn] 🚨 {alert['name']} @ ${alert['price']:.2f}")
                _rw_send_telegram(alert)
        except Exception as e:
            logger.error(f"[ReversalWarn] Loop error: {e}")
        time.sleep(60)

def start_reversal_warning():
    t = threading.Thread(target=_reversal_warning_loop, daemon=True)
    t.start()
    return t

def get_reversal_warning_status():
    with _reversal_warn_lock:
        return {
            "active":             _reversal_warn_state["active"],
            "last_check":         _reversal_warn_state["last_check"],
            "alerts_today":       list(_reversal_warn_state["alerts_today"]),
            "alert_count":        len(_reversal_warn_state["alerts_today"]),
            "current_conditions": dict(_reversal_warn_state["current_conditions"]),
        }
# ══════════════════════════════════════════════════════════════════════════════
# V12.0: Strategy A — True Pyramid (Pyramiding on Profit) — Paper Trading
# الفكرة: دخول بعقد واحد مع تأكيد MTF (15M+5M+1M)، تعزيز على الربح فقط
# نافذة العمل: 10:05 AM → 12:10 PM ET
# الملعب: ITM Options (Delta 0.65-0.85) | Alpaca Paper
# الهدف: جمع بيانات حقيقية + Pyramiding صحيح + حماية الرأسمال
# ══════════════════════════════════════════════════════════════════════════════
PYR_START_MINUTES   = 35        # 9:30 + 35 = 10:05 AM
PYR_END_MINUTES     = 160       # 9:30 + 160 = 12:10 PM
PYR_TP1_PCT         = 0.12      # +12% → تعزيز (Pyramid on profit)
PYR_TP_FINAL_PCT    = 0.20      # +20% من المتوسط → إغلاق نهائي
PYR_SL_INIT_PCT     = -0.15     # -15% قبل التعزيز → إغلاق
PYR_SL_AFTER_PCT    = 0.00      # Breakeven بعد التعزيز → إغلاق (حماية الربح)
PYR_DELTA_MIN       = 0.65
PYR_DELTA_MAX       = 0.85
PYR_LOOP_SLEEP      = 30
PYR_MTF_MIN_AGREE   = 2         # على الأقل 2 من 3 إطارات تتفق

_pyr_lock  = threading.Lock()
_pyr_state = {
    "running":      False,
    "active_trade": None,
    "last_check":   "--",
    "trades_today": [],
    "status_msg":   "غير نشط",
}

# ─── أدوات مساعدة ────────────────────────────────────────────────────────────

def _pyr_in_window():
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return PYR_START_MINUTES <= elapsed <= PYR_END_MINUTES

def _pyr_is_force_close():
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return elapsed >= PYR_END_MINUTES

def _pyr_macd(timeframe, bar_count=50):
    """إرجاع (curr_hist, prev_hist, macd_line_curr, macd_line_prev)"""
    bars = get_tsla_bars(timeframe, bar_count)
    if not bars or len(bars) < 35:
        return None, None, None, None
    closes = [float(b["c"]) for b in bars]
    # حساب MACD Line و Signal Line
    macd_series = []
    for i in range(26, len(closes) + 1):
        ef = _rw_ema(closes[:i], 12)
        es = _rw_ema(closes[:i], 26)
        if ef and es:
            macd_series.append(ef - es)
    if len(macd_series) < 11:
        return None, None, None, None
    sig_line_curr = _rw_ema(macd_series, 9)
    sig_line_prev = _rw_ema(macd_series[:-1], 9) if len(macd_series) > 9 else None
    if sig_line_curr is None:
        return None, None, None, None
    curr_hist = macd_series[-1] - sig_line_curr
    prev_hist = (macd_series[-2] - sig_line_prev) if sig_line_prev else None
    macd_line_curr = macd_series[-1]
    macd_line_prev = macd_series[-2] if len(macd_series) >= 2 else None
    return curr_hist, prev_hist, macd_line_curr, macd_line_prev

def _pyr_ema9(timeframe, bar_count=30):
    """حساب EMA9 من آخر شمعة"""
    bars = get_tsla_bars(timeframe, bar_count)
    if not bars or len(bars) < 10:
        return None
    closes = [float(b["c"]) for b in bars]
    return _rw_ema(closes, 9)

# ─── فلتر MTF (Multi-Timeframe Trend Confirmation) ───────────────────────────

def _pyr_mtf_score(direction):
    """
    يفحص 3 إطارات زمنية ويعطي نقطة لكل واحد يتفق مع الاتجاه.
    يُرجع (score, details_dict)
    """
    score = 0
    details = {}

    # 1) إطار 15M — MACD Histogram في نفس الاتجاه
    h15_curr, h15_prev, _, _ = _pyr_macd("15Min", 80)
    if h15_curr is not None:
        if direction == "CALL" and h15_curr > 0:
            score += 1
            details["15M_macd"] = f"✅ {h15_curr:+.4f}"
        elif direction == "PUT" and h15_curr < 0:
            score += 1
            details["15M_macd"] = f"✅ {h15_curr:+.4f}"
        else:
            details["15M_macd"] = f"❌ {h15_curr:+.4f}"
    else:
        details["15M_macd"] = "⚠️ N/A"

    # 2) إطار 5M — السعر فوق/تحت VWAP + EMA9
    snap = get_tsla_snapshot()
    if snap:
        price = snap.get("price", 0)
        vwap  = snap.get("vwap", 0)
        ema9_5m = _pyr_ema9("5Min", 30)
        if price and vwap and ema9_5m:
            if direction == "CALL" and price > vwap and price > ema9_5m:
                score += 1
                details["5M_price"] = f"✅ ${price:.2f} > VWAP ${vwap:.2f} > EMA9 ${ema9_5m:.2f}"
            elif direction == "PUT" and price < vwap and price < ema9_5m:
                score += 1
                details["5M_price"] = f"✅ ${price:.2f} < VWAP ${vwap:.2f} < EMA9 ${ema9_5m:.2f}"
            else:
                details["5M_price"] = f"❌ price=${price:.2f} vwap=${vwap:.2f} ema9=${ema9_5m:.2f}"
        else:
            details["5M_price"] = "⚠️ N/A"
    else:
        details["5M_price"] = "⚠️ N/A"

    # 3) إطار 3M — MACD Line يعبر الصفر (Zero Cross)
    _, _, ml3_curr, ml3_prev = _pyr_macd("3Min", 60)
    if ml3_curr is not None and ml3_prev is not None:
        if direction == "CALL" and ml3_prev < 0 and ml3_curr > 0:
            score += 1
            details["3M_zero_cross"] = f"✅ {ml3_prev:+.4f}→{ml3_curr:+.4f}"
        elif direction == "PUT" and ml3_prev > 0 and ml3_curr < 0:
            score += 1
            details["3M_zero_cross"] = f"✅ {ml3_prev:+.4f}→{ml3_curr:+.4f}"
        else:
            details["3M_zero_cross"] = f"❌ {ml3_prev:+.4f}→{ml3_curr:+.4f}"
    else:
        details["3M_zero_cross"] = "⚠️ N/A"

    return score, details

# ─── شروط الدخول ─────────────────────────────────────────────────────────────

def _pyr_check_entry():
    """
    شروط الدخول V12.0:
    1. MACD 1M Zero Cross (سالب→موجب = CALL، موجب→سالب = PUT)
    2. السعر فوق VWAP للـ CALL / تحت VWAP للـ PUT
    3. MTF Score >= 2 من 3 (15M + 5M + 3M)
    """
    snap = get_tsla_snapshot()
    if not snap or snap["price"] <= 0:
        return None, 0, 0, {}
    price = snap["price"]
    vwap  = snap["vwap"]

    # شرط 1: MACD 1M Zero Cross
    _, _, ml1_curr, ml1_prev = _pyr_macd("1Min", 50)
    if ml1_curr is None or ml1_prev is None:
        return None, price, vwap, {"reason": "MACD 1M غير متاح"}

    bull_cross = (ml1_prev < 0 and ml1_curr > 0)
    bear_cross = (ml1_prev > 0 and ml1_curr < 0)
    if not bull_cross and not bear_cross:
        return None, price, vwap, {"reason": f"لا يوجد Zero Cross 1M ({ml1_prev:+.4f}→{ml1_curr:+.4f})"}

    direction = "CALL" if bull_cross else "PUT"

    # شرط 2: VWAP
    if direction == "CALL" and price <= vwap:
        return None, price, vwap, {"reason": f"CALL مرفوض: تحت VWAP (${price:.2f} ≤ ${vwap:.2f})"}
    if direction == "PUT" and price >= vwap:
        return None, price, vwap, {"reason": f"PUT مرفوض: فوق VWAP (${price:.2f} ≥ ${vwap:.2f})"}

    # شرط 3: MTF Score
    mtf_score, mtf_details = _pyr_mtf_score(direction)
    if mtf_score < PYR_MTF_MIN_AGREE:
        return None, price, vwap, {
            "reason": f"MTF ضعيف ({mtf_score}/3) — {mtf_details}"
        }

    # جلب MACD Histogram للتسجيل
    h1_curr, _, _, _ = _pyr_macd("1Min", 50)

    reasons = {
        "direction": direction, "price": price, "vwap": vwap,
        "macd1_line_curr": round(ml1_curr, 4),
        "macd1_line_prev": round(ml1_prev, 4),
        "macd1_hist": round(h1_curr, 4) if h1_curr else 0,
        "mtf_score": mtf_score,
        "mtf_details": mtf_details,
    }
    return direction, price, vwap, reasons

# ─── اختيار العقد ────────────────────────────────────────────────────────────

def _pyr_find_best_contract(price, direction, expiry):
    option_type = "call" if direction == "CALL" else "put"
    if direction == "CALL":
        strike_min = round(price - 15, 0)
        strike_max = round(price - 2, 0)
    else:
        strike_min = round(price + 2, 0)
        strike_max = round(price + 15, 0)
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        return None
    best = None
    best_score = -1
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        itm_amount = (price - strike) if direction == "CALL" else (strike - price)
        if itm_amount < 2:
            continue
        approx_delta = min(0.95, 0.50 + itm_amount * 0.045)
        if not (PYR_DELTA_MIN <= approx_delta <= PYR_DELTA_MAX):
            continue
        symbol = c.get("symbol", "")
        oi     = int(c.get("open_interest", 0) or 0)
        vol    = int(c.get("volume", 0) or 0)
        quote  = get_option_quote(symbol)
        if not quote or quote["mid"] <= 0:
            continue
        spread_pct = (quote["ask"] - quote["bid"]) / quote["mid"] if quote["mid"] > 0 else 1.0
        if spread_pct > 0.25:
            continue
        delta_score  = 1 - abs(approx_delta - 0.75) * 3
        oi_score     = min(1.0, oi / 5000)
        vol_score    = min(1.0, vol / 1000)
        spread_score = max(0, 1 - spread_pct * 4)
        score = delta_score * 0.35 + oi_score * 0.30 + vol_score * 0.20 + spread_score * 0.15
        if score > best_score:
            best_score = score
            best = {
                "symbol": symbol, "strike": strike, "type": option_type,
                "expiry": expiry, "bid": quote["bid"], "ask": quote["ask"],
                "mid": quote["mid"], "approx_delta": round(approx_delta, 2),
                "itm_amount": round(itm_amount, 2),
                "open_interest": oi, "volume": vol,
                "spread_pct": round(spread_pct * 100, 1),
            }
    return best

# ─── رسائل Telegram ──────────────────────────────────────────────────────────

def _pyr_send_entry_msg(trade):
    mtf = trade.get("mtf_details", {})
    mtf_str = (
        f"   15M MACD: {mtf.get('15M_macd','N/A')}\n"
        f"   5M Price: {mtf.get('5M_price','N/A')}\n"
        f"   3M ZeroCross: {mtf.get('3M_zero_cross','N/A')}"
    )
    msg = (
        f"🦋 *PYRAMID V12 | {trade['direction']}*\n"
        f"🕐 دخول: {trade['entry_time']} ET\n"
        f"📍 TSLA: ${trade['entry_stock_price']:.2f} | "
        f"{'فوق' if trade['direction']=='CALL' else 'تحت'} VWAP ${trade['vwap']:.2f}\n"
        f"📈 MACD 1M Line: {trade['macd1_line_curr']:+.4f} (عبر الصفر)\n"
        f"─────────────────────\n"
        f"🎯 MTF Score: {trade['mtf_score']}/3\n"
        f"{mtf_str}\n"
        f"─────────────────────\n"
        f"🎯 العقد: `{trade['symbol']}`\n"
        f"   Strike: ${trade['strike']:.0f} | Delta≈{trade['approx_delta']}\n"
        f"   سعر الدخول: ${trade['entry_price']:.2f}\n"
        f"   OI: {trade['open_interest']:,} | Volume: {trade['volume']:,}\n"
        f"   Spread: {trade['spread_pct']}%\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"─────────────────────\n"
        f"🎯 TP1 (تعزيز): +{PYR_TP1_PCT*100:.0f}% → إضافة عقد\n"
        f"🏆 TP Final: +{PYR_TP_FINAL_PCT*100:.0f}% من المتوسط → إغلاق\n"
        f"🛡️ SL: {PYR_SL_INIT_PCT*100:.0f}% (قبل تعزيز) | Breakeven (بعد تعزيز)\n"
        f"⏰ إغلاق إجباري: 12:10 PM ET"
    )
    send_telegram(msg)

def _pyr_send_add_msg(trade, current_price, new_avg):
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    msg = (
        f"📈 *PYRAMID — تعزيز على الربح ✅*\n"
        f"🕐 {now_str} ET\n"
        f"💰 وصل +{PYR_TP1_PCT*100:.0f}% → ${current_price:.2f}\n"
        f"🔄 إضافة عقد (إجمالي {trade['qty']} عقود)\n"
        f"📊 متوسط التكلفة الجديد: ${new_avg:.2f}\n"
        f"🛡️ SL الجديد: Breakeven ${new_avg:.2f} (لا خسارة)\n"
        f"🏆 TP Final: ${new_avg * (1 + PYR_TP_FINAL_PCT):.2f} (+{PYR_TP_FINAL_PCT*100:.0f}%)"
    )
    send_telegram(msg)

def _pyr_send_close_msg(trade, exit_price, exit_type, pnl_pct):
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    emoji = "✅" if pnl_pct > 0 else ("🟡" if pnl_pct == 0 else "❌")
    mtf = trade.get("mtf_details", {})
    msg = (
        f"{emoji} *PYRAMID V12 — إغلاق ({exit_type})*\n"
        f"🕐 {now_str} ET\n"
        f"💰 سعر الخروج: ${exit_price:.2f}\n"
        f"📊 النتيجة: {pnl_pct:+.1f}%\n"
        f"─────────────────────\n"
        f"📋 *ملخص الصفقة:*\n"
        f"   الاتجاه: {trade['direction']}\n"
        f"   العقد: `{trade['symbol']}`\n"
        f"   دخول: ${trade['entry_price']:.2f} @ {trade['entry_time']}\n"
        f"   VWAP: ${trade['vwap']:.2f} | Delta≈{trade['approx_delta']}\n"
        f"   OI: {trade['open_interest']:,} | Volume: {trade['volume']:,}\n"
        f"   تعزيز: {'✅ نعم' if trade.get('reinforced') else '❌ لا'}\n"
        f"   عدد العقود: {trade.get('qty', 1)}\n"
        f"   سبب الإغلاق: {exit_type}\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"📉 MAE: {trade.get('mae',0)*100:.1f}% | MFE: {trade.get('mfe',0)*100:.1f}%\n"
        f"🎯 MTF Score: {trade.get('mtf_score',0)}/3\n"
        f"   15M: {mtf.get('15M_macd','N/A')}\n"
        f"   5M: {mtf.get('5M_price','N/A')}\n"
        f"   3M: {mtf.get('3M_zero_cross','N/A')}"
    )
    send_telegram(msg)

# ─── تنفيذ الدخول ────────────────────────────────────────────────────────────

def _pyr_execute_entry(direction, price, vwap, reasons):
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    expiry  = _today_expiry()
    contract = _pyr_find_best_contract(price, direction, expiry)
    if not contract:
        send_telegram(f"⚠️ Pyramid V12: لم يُعثر على عقد ITM مناسب لـ {direction} @ ${price:.2f}")
        return
    order = place_option_order(contract["symbol"], 1, "buy", order_type="market", position_intent="open")
    if not order:
        send_telegram(f"❌ Pyramid V12: فشل تنفيذ أمر الشراء لـ {contract['symbol']}")
        return
    spy_dir, spy_chg = get_spy_direction()
    trade = {
        "symbol":             contract["symbol"],
        "direction":          direction,
        "strike":             contract["strike"],
        "expiry":             contract["expiry"],
        "approx_delta":       contract["approx_delta"],
        "open_interest":      contract["open_interest"],
        "volume":             contract["volume"],
        "spread_pct":         contract["spread_pct"],
        "entry_price":        contract["mid"],
        "entry_stock_price":  price,
        "entry_time":         now_str,
        "vwap":               vwap,
        "macd1_line_curr":    reasons.get("macd1_line_curr", 0),
        "macd1_line_prev":    reasons.get("macd1_line_prev", 0),
        "macd1_hist":         reasons.get("macd1_hist", 0),
        "mtf_score":          reasons.get("mtf_score", 0),
        "mtf_details":        reasons.get("mtf_details", {}),
        "qty":                1,
        "reinforced":         False,
        "avg_cost":           contract["mid"],
        "breakeven_price":    contract["mid"],   # يُحدَّث بعد التعزيز
        "order_id":           order.get("id", ""),
        "mae":                0.0,
        "mfe":                0.0,
        "spy_direction":      spy_dir,
        "spy_change":         spy_chg,
    }
    with _pyr_lock:
        _pyr_state["active_trade"] = trade
        _pyr_state["status_msg"] = f"صفقة مفتوحة: {direction} @ ${contract['mid']:.2f}"
    _pyr_send_entry_msg(trade)
    logger.info(f"[Pyramid V12] ✅ دخول {direction} | {contract['symbol']} @ ${contract['mid']:.2f} | MTF={reasons.get('mtf_score',0)}/3")

# ─── التعزيز على الربح ───────────────────────────────────────────────────────

def _pyr_reinforce(trade, current_price):
    """تعزيز على الربح: إضافة عقد واحد عند +12%"""
    order = place_option_order(trade["symbol"], 1, "buy", order_type="market", position_intent="open")
    if not order:
        send_telegram(f"❌ Pyramid V12: فشل التعزيز لـ {trade['symbol']}")
        return
    # حساب المتوسط الجديد
    total_cost = (trade["entry_price"] * trade["qty"]) + (current_price * 1)
    new_qty    = trade["qty"] + 1
    new_avg    = total_cost / new_qty
    trade["reinforced"]      = True
    trade["qty"]             = new_qty
    trade["avg_cost"]        = round(new_avg, 2)
    trade["breakeven_price"] = round(new_avg, 2)  # SL الجديد = Breakeven
    with _pyr_lock:
        _pyr_state["active_trade"] = trade
        _pyr_state["status_msg"] = f"مُعزَّز @ ${current_price:.2f} | متوسط ${new_avg:.2f} | SL=Breakeven"
    _pyr_send_add_msg(trade, current_price, new_avg)
    logger.info(f"[Pyramid V12] 📈 تعزيز على الربح | {trade['symbol']} @ ${current_price:.2f} | متوسط=${new_avg:.2f}")

# ─── إغلاق الصفقة ────────────────────────────────────────────────────────────

def _pyr_close_trade(trade, exit_price, exit_type, pnl_pct):
    close_position(trade["symbol"])
    _pyr_send_close_msg(trade, exit_price, exit_type, pnl_pct)
    trade["exit_price"] = exit_price
    trade["exit_type"]  = exit_type
    trade["pnl_pct"]    = round(pnl_pct, 2)
    with _pyr_lock:
        _pyr_state["trades_today"].append(dict(trade))
        _pyr_state["active_trade"] = None
        _pyr_state["status_msg"]   = f"آخر صفقة: {exit_type} {pnl_pct:+.1f}%"
    logger.info(f"[Pyramid V12] 🔒 إغلاق {trade['direction']} | {exit_type} | {pnl_pct:+.1f}%")

# ─── مراقبة الصفقة ───────────────────────────────────────────────────────────

def _pyr_monitor_trade(trade):
    """
    منطق الإدارة V12.0:
    قبل التعزيز:
      - TP1 +12% → تعزيز (إضافة عقد) + SL ينتقل للـ Breakeven
      - SL -15% → إغلاق
    بعد التعزيز:
      - TP Final +20% من المتوسط → إغلاق
      - SL = Breakeven (المتوسط) → إغلاق (حماية الربح)
    """
    quote = get_option_quote(trade["symbol"])
    if not quote or quote["mid"] <= 0:
        return
    current_price = quote["mid"]
    avg_cost      = trade["avg_cost"]
    pnl_pct       = (current_price - avg_cost) / avg_cost

    # MAE/MFE tracking
    if pnl_pct < trade.get("mae", 0):
        trade["mae"] = round(pnl_pct, 4)
    if pnl_pct > trade.get("mfe", 0):
        trade["mfe"] = round(pnl_pct, 4)

    # تحديث الحالة
    with _pyr_lock:
        _pyr_state["status_msg"] = (
            f"{'🔄' if trade['reinforced'] else '📊'} {trade['direction']} | "
            f"${current_price:.2f} | {pnl_pct*100:+.1f}% | "
            f"{'مُعزَّز' if trade['reinforced'] else 'أولي'}"
        )

    if not trade["reinforced"]:
        # TP1 → تعزيز على الربح
        if pnl_pct >= PYR_TP1_PCT:
            _pyr_reinforce(trade, current_price)
            return
        # SL أولي
        if pnl_pct <= PYR_SL_INIT_PCT:
            _pyr_close_trade(trade, current_price, f"SL {PYR_SL_INIT_PCT*100:.0f}%", pnl_pct * 100)
            return
    else:
        # TP Final
        if pnl_pct >= PYR_TP_FINAL_PCT:
            _pyr_close_trade(trade, current_price, f"TP Final +{PYR_TP_FINAL_PCT*100:.0f}%", pnl_pct * 100)
            return
        # SL = Breakeven (حماية الربح)
        if current_price <= trade["breakeven_price"]:
            _pyr_close_trade(trade, current_price, "SL Breakeven (حماية الربح)", pnl_pct * 100)
            return

# ─── Main Loop ───────────────────────────────────────────────────────────────

def _pyramid_auto_loop():
    import pytz
    et_tz = pytz.timezone("America/New_York")
    logger.info("[Pyramid V12] 🦋 thread started")
    while True:
        try:
            with _pyr_lock:
                if not _pyr_state["running"]:
                    break
            now = datetime.now(et_tz)
            now_str = now.strftime("%I:%M %p")
            with _pyr_lock:
                _pyr_state["last_check"] = now_str

            if not _is_0dte_day():
                with _pyr_lock:
                    _pyr_state["status_msg"] = "عطلة — لا يوجد 0DTE"
                time.sleep(300)
                continue

            with _pyr_lock:
                active_trade = _pyr_state.get("active_trade")

            # إغلاق إجباري
            if active_trade and _pyr_is_force_close():
                quote = get_option_quote(active_trade["symbol"])
                exit_price = quote["mid"] if quote and quote["mid"] > 0 else active_trade["avg_cost"]
                pnl_pct = (exit_price - active_trade["avg_cost"]) / active_trade["avg_cost"]
                _pyr_close_trade(active_trade, exit_price, "إغلاق إجباري 12:10 PM", pnl_pct * 100)
                time.sleep(PYR_LOOP_SLEEP)
                continue

            if not _pyr_in_window():
                with _pyr_lock:
                    _pyr_state["status_msg"] = f"خارج النافذة — {now_str}"
                time.sleep(PYR_LOOP_SLEEP)
                continue

            if active_trade:
                _pyr_monitor_trade(active_trade)
            else:
                with _pyr_lock:
                    _pyr_state["status_msg"] = f"يراقب السوق... {now_str}"
                direction, price, vwap, reasons = _pyr_check_entry()
                if direction:
                    _pyr_execute_entry(direction, price, vwap, reasons)
                else:
                    with _pyr_lock:
                        _pyr_state["status_msg"] = f"انتظار: {reasons.get('reason','لا إشارة')}"
        except Exception as e:
            logger.error(f"[Pyramid V12] Loop error: {e}", exc_info=True)
        time.sleep(PYR_LOOP_SLEEP)

# ─── التقرير الأسبوعي ────────────────────────────────────────────────────────

def _pyr_weekly_report_loop():
    import pytz
    et_tz = pytz.timezone("America/New_York")
    while True:
        try:
            now = datetime.now(et_tz)
            if now.weekday() == 4 and now.hour == 16 and now.minute < 2:
                with _pyr_lock:
                    trades = list(_pyr_state["trades_today"])
                if not trades:
                    send_telegram("📊 *تقرير Pyramid V12 الأسبوعي*\nلا توجد صفقات مسجلة.")
                else:
                    total   = len(trades)
                    tp_fin  = sum(1 for t in trades if "TP Final" in t.get("exit_type",""))
                    tp1_rein= sum(1 for t in trades if t.get("reinforced"))
                    sl_init = sum(1 for t in trades if f"SL {int(abs(PYR_SL_INIT_PCT)*100)}%" in t.get("exit_type",""))
                    sl_be   = sum(1 for t in trades if "Breakeven" in t.get("exit_type",""))
                    forced  = sum(1 for t in trades if "إجباري" in t.get("exit_type",""))
                    wins    = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
                    avg_pnl = sum(t.get("pnl_pct", 0) for t in trades) / total
                    avg_mae = sum(abs(t.get("mae", 0)) for t in trades) / total * 100
                    avg_mfe = sum(t.get("mfe", 0) for t in trades) / total * 100
                    avg_mtf = sum(t.get("mtf_score", 0) for t in trades) / total
                    # تحليل SPY
                    bull_trades = [t for t in trades if t.get("spy_direction") == "BULL"]
                    bear_trades = [t for t in trades if t.get("spy_direction") == "BEAR"]
                    bull_wins = sum(1 for t in bull_trades if t.get("pnl_pct", 0) > 0)
                    bear_wins = sum(1 for t in bear_trades if t.get("pnl_pct", 0) > 0)
                    msg = (
                        f"📊 *تقرير Pyramid V12 الأسبوعي*\n"
                        f"─────────────────────\n"
                        f"📈 إجمالي الصفقات: {total}\n"
                        f"✅ Win Rate: {wins}/{total} ({wins/total*100:.0f}%)\n"
                        f"💰 متوسط P&L: {avg_pnl:+.1f}%\n"
                        f"─────────────────────\n"
                        f"🏆 TP Final (+{PYR_TP_FINAL_PCT*100:.0f}%): {tp_fin}\n"
                        f"📈 وصل للتعزيز: {tp1_rein}\n"
                        f"❌ SL أولي ({PYR_SL_INIT_PCT*100:.0f}%): {sl_init}\n"
                        f"🛡️ SL Breakeven: {sl_be}\n"
                        f"⏰ إغلاق إجباري: {forced}\n"
                        f"─────────────────────\n"
                        f"📉 متوسط MAE: {avg_mae:.1f}%\n"
                        f"📈 متوسط MFE: {avg_mfe:.1f}%\n"
                        f"🎯 متوسط MTF Score: {avg_mtf:.1f}/3\n"
                        f"─────────────────────\n"
                        f"🌐 SPY BULL → Win: {bull_wins}/{len(bull_trades)} ({bull_wins/len(bull_trades)*100:.0f}%)\n" if bull_trades else ""
                        f"🌐 SPY BEAR → Win: {bear_wins}/{len(bear_trades)} ({bear_wins/len(bear_trades)*100:.0f}%)\n" if bear_trades else ""
                        f"─────────────────────\n"
                        f"💡 {'MTF يحسّن الجودة ✅' if avg_mtf >= 2.5 else 'راجع شروط MTF ⚠️'}"
                    )
                    send_telegram(msg)
        except Exception as e:
            logger.error(f"[Pyramid V12] Weekly report error: {e}")
        time.sleep(60)

# ─── Start / Stop / Status ───────────────────────────────────────────────────

def start_pyramid_auto():
    with _pyr_lock:
        if _pyr_state["running"]:
            logger.warning("[Pyramid V12] already running")
            return False
        _pyr_state["running"] = True
    threading.Thread(target=_pyramid_auto_loop, daemon=True).start()
    threading.Thread(target=_pyr_weekly_report_loop, daemon=True).start()
    logger.info("[Pyramid V12] ✅ started — True Pyramiding + MTF Filter")
    return True

def stop_pyramid_auto():
    with _pyr_lock:
        _pyr_state["running"] = False
        _pyr_state["status_msg"] = "موقوف"

def get_pyramid_status():
    with _pyr_lock:
        trade = _pyr_state.get("active_trade")
        return {
            "ok":           True,
            "running":      _pyr_state["running"],
            "status":       _pyr_state["status_msg"],
            "last_check":   _pyr_state["last_check"],
            "active_trade": {
                "symbol":      trade["symbol"],
                "direction":   trade["direction"],
                "entry_price": trade["entry_price"],
                "avg_cost":    trade["avg_cost"],
                "qty":         trade["qty"],
                "reinforced":  trade["reinforced"],
                "entry_time":  trade["entry_time"],
                "mtf_score":   trade.get("mtf_score", 0),
                "mae":         round(trade.get("mae", 0) * 100, 1),
                "mfe":         round(trade.get("mfe", 0) * 100, 1),
            } if trade else None,
            "trades_today": len(_pyr_state["trades_today"]),
        }

# ══════════════════════════════════════════════════════════════════════════════
# V11.1: Strategy B — VWAP Bounce 15M (Paper Trading)
# الفكرة: السعر يلمس VWAP على 15M ثم يرتد مع MACD 15M انعكاس + حجم عالٍ
# نافذة العمل: 10:05 AM → 2:30 PM ET
# TP: +12% | SL: -15% | Delta: 0.70+
# ══════════════════════════════════════════════════════════════════════════════
STB_START_MINUTES   = 35       # 9:30 + 35 = 10:05 AM
STB_END_MINUTES     = 300      # 9:30 + 300 = 2:30 PM
STB_TP_PCT          = 0.12     # +12%
STB_SL_PCT          = -0.15    # -15%
STB_DELTA_MIN       = 0.70
STB_DELTA_MAX       = 0.90
STB_VWAP_PROXIMITY  = 0.50     # ±$0.50 من VWAP
STB_LOOP_SLEEP      = 45       # كل 45 ثانية (15M فريم — لا نحتاج سرعة)

_stb_lock  = threading.Lock()
_stb_state = {
    "running":      False,
    "active_trade": None,
    "last_check":   "--",
    "trades_today": [],
    "status_msg":   "غير نشط",
}

def _stb_in_window():
    """هل نحن في نافذة Strategy B (10:05 AM - 2:30 PM ET)."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return STB_START_MINUTES <= elapsed <= STB_END_MINUTES

def _stb_is_force_close():
    """هل حان وقت الإغلاق الإجباري."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return elapsed >= STB_END_MINUTES

def _stb_check_vwap_bounce():
    """فحص ارتداد السعر من VWAP على 15M."""
    # جلب بيانات 15M
    bars_15m = get_tsla_bars("15Min", 20)
    if not bars_15m or len(bars_15m) < 12:
        return None, 0, 0, {"reason": "بيانات 15M غير كافية"}

    closes = [float(b["c"]) for b in bars_15m]
    volumes = [float(b["v"]) for b in bars_15m]
    highs = [float(b["h"]) for b in bars_15m]
    lows = [float(b["l"]) for b in bars_15m]

    price = closes[-1]
    prev_close = closes[-2]

    # حساب VWAP تقريبي من الشموع
    snap = get_tsla_snapshot()
    vwap = snap.get("vwap", 0) if snap else 0
    if not vwap:
        return None, price, 0, {"reason": "VWAP غير متاح"}

    # شرط 1: السعر قريب من VWAP (±$0.50) في الشمعة السابقة أو الحالية
    prev_near_vwap = abs(prev_close - vwap) <= STB_VWAP_PROXIMITY or \
                     min(abs(lows[-2] - vwap), abs(lows[-1] - vwap)) <= STB_VWAP_PROXIMITY or \
                     min(abs(highs[-2] - vwap), abs(highs[-1] - vwap)) <= STB_VWAP_PROXIMITY
    if not prev_near_vwap:
        return None, price, vwap, {"reason": f"السعر بعيد عن VWAP (${abs(price-vwap):.2f})"}

    # شرط 2: ارتداد — الشمعة الحالية ابتعدت عن VWAP
    bounce_up = price > vwap + 0.20 and lows[-1] >= vwap - 0.30
    bounce_down = price < vwap - 0.20 and highs[-1] <= vwap + 0.30
    if not bounce_up and not bounce_down:
        return None, price, vwap, {"reason": "لا يوجد ارتداد واضح"}

    # شرط 3: MACD 15M يبدأ بالانعكاس
    macd_curr, macd_prev = _rw_macd_hist(closes)
    if macd_curr is None:
        return None, price, vwap, {"reason": "MACD 15M غير كافٍ"}

    if bounce_up and not (macd_curr > macd_prev):
        return None, price, vwap, {"reason": "MACD 15M لا يدعم الارتداد الصاعد"}
    if bounce_down and not (macd_curr < macd_prev):
        return None, price, vwap, {"reason": "MACD 15M لا يدعم الارتداد الهابط"}

    # شرط 4: حجم الشمعة أكبر من متوسط آخر 10 شموع
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else sum(volumes[:-1]) / max(len(volumes)-1, 1)
    current_vol = volumes[-1]
    if current_vol < avg_vol * 0.8:
        return None, price, vwap, {"reason": f"حجم ضعيف ({current_vol:.0f} < {avg_vol:.0f})"}

    direction = "CALL" if bounce_up else "PUT"
    reasons = {
        "direction": direction, "price": price, "vwap": vwap,
        "macd_curr": round(macd_curr, 4), "macd_prev": round(macd_prev, 4),
        "volume": current_vol, "avg_volume": avg_vol,
        "distance_from_vwap": round(abs(price - vwap), 2),
    }
    return direction, price, vwap, reasons

def _stb_find_contract(price, direction, expiry):
    """اختيار أفضل عقد ITM لـ Strategy B (Delta 0.70+)."""
    option_type = "call" if direction == "CALL" else "put"
    if direction == "CALL":
        strike_min = round(price - 15, 0)
        strike_max = round(price - 3, 0)
    else:
        strike_min = round(price + 3, 0)
        strike_max = round(price + 15, 0)

    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        return None

    best = None
    best_score = -1
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        itm_amount = (price - strike) if direction == "CALL" else (strike - price)
        if itm_amount < 3:
            continue
        approx_delta = min(0.95, 0.50 + itm_amount * 0.045)
        if not (STB_DELTA_MIN <= approx_delta <= STB_DELTA_MAX):
            continue
        oi = int(c.get("open_interest", 0))
        vol = int(c.get("volume", 0))
        bid = float(c.get("bid", 0))
        ask = float(c.get("ask", 0))
        mid = (bid + ask) / 2 if bid and ask else float(c.get("last_price", 0))
        if mid < 1.0:
            continue
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 99
        if spread_pct > 5:
            continue
        score = oi * 0.4 + vol * 0.4 + (10 - spread_pct) * 20
        if score > best_score:
            best_score = score
            best = {
                "symbol": c.get("symbol", ""),
                "strike": strike,
                "expiry": expiry,
                "approx_delta": round(approx_delta, 2),
                "open_interest": oi,
                "volume": vol,
                "mid": round(mid, 2),
                "spread_pct": round(spread_pct, 1),
            }
    return best

def _stb_send_entry_msg(trade):
    """إرسال رسالة دخول Strategy B على Telegram."""
    msg = (
        f"🎯 *STRATEGY B — VWAP Bounce 15M | {trade['direction']}*\n"
        f"🕐 دخول: {trade['entry_time']} ET\n"
        f"📍 TSLA: ${trade['entry_stock_price']:.2f} | "
        f"{'ارتداد فوق' if trade['direction']=='CALL' else 'ارتداد تحت'} VWAP ${trade['vwap']:.2f}\n"
        f"📊 MACD 15M: {trade['macd_curr']:+.4f}\n"
        f"📈 Volume: {trade['trade_volume']:,.0f} (متوسط: {trade['avg_volume']:,.0f})\n"
        f"─────────────────────\n"
        f"🎯 العقد: `{trade['symbol']}`\n"
        f"   Strike: ${trade['strike']:.0f} | Delta≈{trade['approx_delta']}\n"
        f"   سعر الدخول: ${trade['entry_price']:.2f}\n"
        f"   OI: {trade['open_interest']:,} | Volume: {trade['volume']:,}\n"
        f"   Spread: {trade['spread_pct']}%\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"─────────────────────\n"
        f"🎯 TP: ${trade['entry_price']*(1+STB_TP_PCT):.2f} (+{STB_TP_PCT*100:.0f}%)\n"
        f"🛑 SL: ${trade['entry_price']*(1+STB_SL_PCT):.2f} ({STB_SL_PCT*100:.0f}%)\n"
        f"⏰ إغلاق إجباري: 2:30 PM ET"
    )
    send_telegram(msg)

def _stb_send_close_msg(trade, exit_price, exit_type, pnl_pct):
    """إرسال رسالة إغلاق Strategy B."""
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    emoji = "✅" if pnl_pct > 0 else "❌"
    msg = (
        f"{emoji} *STRATEGY B — إغلاق ({exit_type})*\n"
        f"🕐 {now_str} ET\n"
        f"💰 سعر الخروج: ${exit_price:.2f}\n"
        f"📊 النتيجة: {pnl_pct:+.1f}%\n"
        f"─────────────────────\n"
        f"📋 *ملخص الصفقة:*\n"
        f"   الاتجاه: {trade['direction']}\n"
        f"   العقد: `{trade['symbol']}`\n"
        f"   دخول: ${trade['entry_price']:.2f} @ {trade['entry_time']}\n"
        f"   VWAP: ${trade['vwap']:.2f} | Delta≈{trade['approx_delta']}\n"
        f"   OI: {trade['open_interest']:,} | Volume: {trade['volume']:,}\n"
        f"   المسافة من VWAP: ${trade['distance_from_vwap']:.2f}\n"
        f"   سبب الإغلاق: {exit_type}\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"📉 MAE: {trade.get('mae',0)*100:.1f}% | MFE: {trade.get('mfe',0)*100:.1f}%"
    )
    send_telegram(msg)

def _stb_execute_entry(direction, price, vwap, reasons):
    """تنفيذ دخول Strategy B."""
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    expiry = _today_expiry()
    contract = _stb_find_contract(price, direction, expiry)
    if not contract:
        return

    order = place_option_order(contract["symbol"], 1, "buy", order_type="market", position_intent="open")
    if not order:
        send_telegram(f"❌ Strategy B: فشل تنفيذ أمر الشراء لـ {contract['symbol']}")
        return

    spy_dir, spy_chg = get_spy_direction()
    trade = {
        "strategy": "B", "symbol": contract["symbol"], "direction": direction,
        "strike": contract["strike"], "expiry": contract["expiry"],
        "approx_delta": contract["approx_delta"],
        "open_interest": contract["open_interest"], "volume": contract["volume"],
        "spread_pct": contract["spread_pct"],
        "entry_price": contract["mid"], "entry_stock_price": price,
        "entry_time": now_str, "vwap": vwap,
        "macd_curr": reasons.get("macd_curr", 0),
        "trade_volume": reasons.get("volume", 0),
        "avg_volume": reasons.get("avg_volume", 0),
        "distance_from_vwap": reasons.get("distance_from_vwap", 0),
        "order_id": order.get("id", ""),
        "mae": 0.0, "mfe": 0.0,
        "spy_direction": spy_dir, "spy_change": spy_chg,
    }
    with _stb_lock:
        _stb_state["active_trade"] = trade
        _stb_state["status_msg"] = f"صفقة مفتوحة: {direction} @ ${contract['mid']:.2f}"
    _stb_send_entry_msg(trade)
    logger.info(f"[StratB] ✅ دخول {direction} | {contract['symbol']} @ ${contract['mid']:.2f}")

def _stb_close_trade(trade, exit_price, exit_type, pnl_pct):
    """إغلاق صفقة Strategy B."""
    order = place_option_order(trade["symbol"], 1, "sell", order_type="market", position_intent="close")
    trade["exit_price"] = exit_price
    trade["exit_type"] = exit_type
    trade["pnl_pct"] = pnl_pct
    with _stb_lock:
        _stb_state["active_trade"] = None
        _stb_state["trades_today"].append(trade)
        _stb_state["status_msg"] = f"آخر صفقة: {exit_type} ({pnl_pct:+.1f}%)"
    _stb_send_close_msg(trade, exit_price, exit_type, pnl_pct)
    logger.info(f"[StratB] إغلاق: {exit_type} | P&L: {pnl_pct:+.1f}%")

def _stb_monitor_trade(trade):
    """مراقبة صفقة مفتوحة لـ Strategy B."""
    try:
        snap = get_tsla_snapshot()
        if not snap:
            return
        price = snap.get("price", 0)
        if not price:
            return
        # تقدير سعر الخيار بناءً على حركة السهم
        delta = trade["approx_delta"]
        price_change = price - trade["entry_stock_price"]
        if trade["direction"] == "PUT":
            price_change = -price_change
        estimated_option_price = trade["entry_price"] + (price_change * delta)
        estimated_option_price = max(0.01, estimated_option_price)

        pnl_pct = (estimated_option_price - trade["entry_price"]) / trade["entry_price"]

        # MAE/MFE tracking
        if pnl_pct < trade.get("mae", 0):
            trade["mae"] = pnl_pct
        if pnl_pct > trade.get("mfe", 0):
            trade["mfe"] = pnl_pct

        # TP +12%
        if pnl_pct >= STB_TP_PCT:
            _stb_close_trade(trade, estimated_option_price, f"TP +{STB_TP_PCT*100:.0f}%", pnl_pct * 100)
            return
        # SL -15%
        if pnl_pct <= STB_SL_PCT:
            _stb_close_trade(trade, estimated_option_price, f"SL {STB_SL_PCT*100:.0f}%", pnl_pct * 100)
            return
    except Exception as e:
        logger.error(f"[StratB] Monitor error: {e}")

def _strategy_b_loop():
    """Main loop لـ Strategy B — VWAP Bounce 15M."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    logger.info("[StratB] 🎯 V11.1 thread started")
    while True:
        try:
            with _stb_lock:
                if not _stb_state["running"]:
                    break
            now = datetime.now(et_tz)
            now_str = now.strftime("%I:%M %p")
            with _stb_lock:
                _stb_state["last_check"] = now_str

            # إغلاق إجباري
            if _stb_is_force_close():
                with _stb_lock:
                    trade = _stb_state.get("active_trade")
                if trade:
                    snap = get_tsla_snapshot()
                    ep = snap.get("price", trade["entry_stock_price"]) if snap else trade["entry_stock_price"]
                    delta = trade["approx_delta"]
                    pc = ep - trade["entry_stock_price"]
                    if trade["direction"] == "PUT":
                        pc = -pc
                    est_price = max(0.01, trade["entry_price"] + pc * delta)
                    pnl = (est_price - trade["entry_price"]) / trade["entry_price"] * 100
                    _stb_close_trade(trade, est_price, "إغلاق إجباري 2:30 PM", pnl)
                time.sleep(60)
                continue

            if not _stb_in_window():
                with _stb_lock:
                    _stb_state["status_msg"] = "انتظار نافذة 10:05 AM"
                time.sleep(30)
                continue

            with _stb_lock:
                active_trade = _stb_state.get("active_trade")

            if active_trade:
                _stb_monitor_trade(active_trade)
            else:
                with _stb_lock:
                    _stb_state["status_msg"] = f"يراقب VWAP... {now_str}"
                direction, price, vwap, reasons = _stb_check_vwap_bounce()
                if direction:
                    _stb_execute_entry(direction, price, vwap, reasons)
                else:
                    with _stb_lock:
                        _stb_state["status_msg"] = f"انتظار: {reasons.get('reason','لا إشارة')}"
        except Exception as e:
            logger.error(f"[StratB] Loop error: {e}")
        time.sleep(STB_LOOP_SLEEP)

def start_strategy_b():
    """تشغيل Strategy B."""
    with _stb_lock:
        if _stb_state["running"]:
            return False
        _stb_state["running"] = True
    threading.Thread(target=_strategy_b_loop, daemon=True).start()
    logger.info("[StratB] ✅ V11.1 VWAP Bounce 15M started")
    return True

def get_strategy_b_status():
    """حالة Strategy B."""
    with _stb_lock:
        trade = _stb_state.get("active_trade")
        return {
            "ok":           True,
            "strategy":     "B — VWAP Bounce 15M",
            "running":      _stb_state["running"],
            "status":       _stb_state["status_msg"],
            "last_check":   _stb_state["last_check"],
            "active_trade": {
                "symbol":      trade["symbol"],
                "direction":   trade["direction"],
                "entry_price": trade["entry_price"],
                "entry_time":  trade["entry_time"],
                "vwap":        trade["vwap"],
            } if trade else None,
            "trades_today": len(_stb_state["trades_today"]),
        }

# ══════════════════════════════════════════════════════════════════════════════
# V11.2: Strategy C — Opening Range Breakout (ORB) (Paper Trading)
# الفكرة: كسر أعلى/أدنى أول 30 دقيقة مع حجم عالٍ
# نافذة العمل: 10:05 AM → 12:30 PM ET
# TP: +10% | SL: -12% | Delta: 0.65+
# ══════════════════════════════════════════════════════════════════════════════
STC_START_MINUTES   = 35       # 9:30 + 35 = 10:05 AM
STC_END_MINUTES     = 180      # 9:30 + 180 = 12:30 PM
STC_TP_PCT          = 0.10     # +10%
STC_SL_PCT          = -0.12    # -12%
STC_DELTA_MIN       = 0.65
STC_DELTA_MAX       = 0.90
STC_LOOP_SLEEP      = 30

_stc_lock  = threading.Lock()
_stc_state = {
    "running":      False,
    "active_trade": None,
    "last_check":   "--",
    "trades_today": [],
    "status_msg":   "غير نشط",
    "or_high":      None,    # Opening Range High
    "or_low":       None,    # Opening Range Low
    "or_built":     False,   # هل تم بناء النطاق؟
}

def _stc_in_window():
    """هل نحن في نافذة Strategy C (10:05 AM - 12:30 PM ET)."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return STC_START_MINUTES <= elapsed <= STC_END_MINUTES

def _stc_is_force_close():
    """هل حان وقت الإغلاق الإجباري."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return elapsed >= STC_END_MINUTES

def _stc_build_opening_range():
    """بناء Opening Range من أول 30 دقيقة (9:30-10:00)."""
    bars = get_tsla_bars("5Min", 10)
    if not bars or len(bars) < 6:
        return None, None

    # أول 6 شموع 5M = 30 دقيقة
    or_bars = bars[:6]
    or_high = max(float(b["h"]) for b in or_bars)
    or_low = min(float(b["l"]) for b in or_bars)
    return or_high, or_low

def _stc_check_breakout():
    """فحص كسر Opening Range."""
    with _stc_lock:
        or_high = _stc_state.get("or_high")
        or_low = _stc_state.get("or_low")

    if not or_high or not or_low:
        return None, 0, 0, {"reason": "Opening Range لم يُبنَ بعد"}

    snap = get_tsla_snapshot()
    if not snap:
        return None, 0, 0, {"reason": "بيانات السعر غير متاحة"}

    price = snap.get("price", 0)
    vwap = snap.get("vwap", 0)
    if not price:
        return None, 0, 0, {"reason": "السعر غير متاح"}

    # فحص Volume — شموع 5M الأخيرة
    bars_5m = get_tsla_bars("5Min", 12)
    if not bars_5m or len(bars_5m) < 8:
        return None, price, vwap, {"reason": "بيانات Volume غير كافية"}

    volumes = [float(b["v"]) for b in bars_5m]
    avg_vol = sum(volumes[:-2]) / max(len(volumes)-2, 1)
    recent_vol = (volumes[-1] + volumes[-2]) / 2

    # كسر الأعلى
    if price > or_high + 0.20:
        if recent_vol < avg_vol * 0.9:
            return None, price, vwap, {"reason": f"كسر صاعد لكن Volume ضعيف"}
        # Fakeout filter: شرط إغلاق شمعة 5M فوق النطاق
        last_bar_close = float(bars_5m[-1]["c"])
        if last_bar_close <= or_high:
            return None, price, vwap, {"reason": "Fakeout — شمعة 5M لم تغلق فوق النطاق"}
        direction = "CALL"
        reasons = {
            "direction": direction, "price": price, "vwap": vwap,
            "or_high": or_high, "or_low": or_low,
            "breakout_amount": round(price - or_high, 2),
            "volume": recent_vol, "avg_volume": avg_vol,
        }
        return direction, price, vwap, reasons

    # كسر الأدنى
    if price < or_low - 0.20:
        if recent_vol < avg_vol * 0.9:
            return None, price, vwap, {"reason": f"كسر هابط لكن Volume ضعيف"}
        # Fakeout filter: شرط إغلاق شمعة 5M تحت النطاق
        last_bar_close = float(bars_5m[-1]["c"])
        if last_bar_close >= or_low:
            return None, price, vwap, {"reason": "Fakeout — شمعة 5M لم تغلق تحت النطاق"}
        direction = "PUT"
        reasons = {
            "direction": direction, "price": price, "vwap": vwap,
            "or_high": or_high, "or_low": or_low,
            "breakout_amount": round(or_low - price, 2),
            "volume": recent_vol, "avg_volume": avg_vol,
        }
        return direction, price, vwap, reasons

    return None, price, vwap, {"reason": f"داخل النطاق (${or_low:.2f} - ${or_high:.2f})"}

def _stc_find_contract(price, direction, expiry):
    """اختيار أفضل عقد ITM لـ Strategy C (Delta 0.65+)."""
    option_type = "call" if direction == "CALL" else "put"
    if direction == "CALL":
        strike_min = round(price - 15, 0)
        strike_max = round(price - 2, 0)
    else:
        strike_min = round(price + 2, 0)
        strike_max = round(price + 15, 0)

    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        return None

    best = None
    best_score = -1
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        itm_amount = (price - strike) if direction == "CALL" else (strike - price)
        if itm_amount < 2:
            continue
        approx_delta = min(0.95, 0.50 + itm_amount * 0.045)
        if not (STC_DELTA_MIN <= approx_delta <= STC_DELTA_MAX):
            continue
        oi = int(c.get("open_interest", 0))
        vol = int(c.get("volume", 0))
        bid = float(c.get("bid", 0))
        ask = float(c.get("ask", 0))
        mid = (bid + ask) / 2 if bid and ask else float(c.get("last_price", 0))
        if mid < 1.0:
            continue
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 99
        if spread_pct > 5:
            continue
        score = oi * 0.4 + vol * 0.4 + (10 - spread_pct) * 20
        if score > best_score:
            best_score = score
            best = {
                "symbol": c.get("symbol", ""),
                "strike": strike,
                "expiry": expiry,
                "approx_delta": round(approx_delta, 2),
                "open_interest": oi,
                "volume": vol,
                "mid": round(mid, 2),
                "spread_pct": round(spread_pct, 1),
            }
    return best

def _stc_send_entry_msg(trade):
    """إرسال رسالة دخول Strategy C على Telegram."""
    msg = (
        f"📈 *STRATEGY C — ORB Breakout | {trade['direction']}*\n"
        f"🕐 دخول: {trade['entry_time']} ET\n"
        f"📍 TSLA: ${trade['entry_stock_price']:.2f}\n"
        f"📊 Opening Range: ${trade['or_low']:.2f} - ${trade['or_high']:.2f}\n"
        f"💥 كسر بـ ${trade['breakout_amount']:.2f}\n"
        f"📈 Volume: {trade['trade_volume']:,.0f} (متوسط: {trade['avg_volume']:,.0f})\n"
        f"─────────────────────\n"
        f"🎯 العقد: `{trade['symbol']}`\n"
        f"   Strike: ${trade['strike']:.0f} | Delta≈{trade['approx_delta']}\n"
        f"   سعر الدخول: ${trade['entry_price']:.2f}\n"
        f"   OI: {trade['open_interest']:,} | Volume: {trade['volume']:,}\n"
        f"   Spread: {trade['spread_pct']}%\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"─────────────────────\n"
        f"🎯 TP: ${trade['entry_price']*(1+STC_TP_PCT):.2f} (+{STC_TP_PCT*100:.0f}%)\n"
        f"🛑 SL: ${trade['entry_price']*(1+STC_SL_PCT):.2f} ({STC_SL_PCT*100:.0f}%)\n"
        f"⏰ إغلاق إجباري: 12:30 PM ET"
    )
    send_telegram(msg)

def _stc_send_close_msg(trade, exit_price, exit_type, pnl_pct):
    """إرسال رسالة إغلاق Strategy C."""
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    emoji = "✅" if pnl_pct > 0 else "❌"
    msg = (
        f"{emoji} *STRATEGY C — إغلاق ({exit_type})*\n"
        f"🕐 {now_str} ET\n"
        f"💰 سعر الخروج: ${exit_price:.2f}\n"
        f"📊 النتيجة: {pnl_pct:+.1f}%\n"
        f"─────────────────────\n"
        f"📋 *ملخص الصفقة:*\n"
        f"   الاتجاه: {trade['direction']}\n"
        f"   العقد: `{trade['symbol']}`\n"
        f"   دخول: ${trade['entry_price']:.2f} @ {trade['entry_time']}\n"
        f"   OR: ${trade['or_low']:.2f} - ${trade['or_high']:.2f}\n"
        f"   Delta≈{trade['approx_delta']} | OI: {trade['open_interest']:,}\n"
        f"   كسر: ${trade['breakout_amount']:.2f}\n"
        f"   سبب الإغلاق: {exit_type}\n"
        f"─────────────────────\n"
        f"📊 SPY: {trade.get('spy_direction','N/A')} ({trade.get('spy_change',0):+.3f}%)\n"
        f"📉 MAE: {trade.get('mae',0)*100:.1f}% | MFE: {trade.get('mfe',0)*100:.1f}%"
    )
    send_telegram(msg)

def _stc_execute_entry(direction, price, vwap, reasons):
    """تنفيذ دخول Strategy C."""
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    expiry = _today_expiry()
    contract = _stc_find_contract(price, direction, expiry)
    if not contract:
        return

    order = place_option_order(contract["symbol"], 1, "buy", order_type="market", position_intent="open")
    if not order:
        send_telegram(f"❌ Strategy C: فشل تنفيذ أمر الشراء لـ {contract['symbol']}")
        return

    spy_dir, spy_chg = get_spy_direction()
    trade = {
        "strategy": "C", "symbol": contract["symbol"], "direction": direction,
        "strike": contract["strike"], "expiry": contract["expiry"],
        "approx_delta": contract["approx_delta"],
        "open_interest": contract["open_interest"], "volume": contract["volume"],
        "spread_pct": contract["spread_pct"],
        "entry_price": contract["mid"], "entry_stock_price": price,
        "entry_time": now_str, "vwap": vwap,
        "or_high": reasons.get("or_high", 0),
        "or_low": reasons.get("or_low", 0),
        "breakout_amount": reasons.get("breakout_amount", 0),
        "trade_volume": reasons.get("volume", 0),
        "avg_volume": reasons.get("avg_volume", 0),
        "order_id": order.get("id", ""),
        "mae": 0.0, "mfe": 0.0,
        "spy_direction": spy_dir, "spy_change": spy_chg,
    }
    with _stc_lock:
        _stc_state["active_trade"] = trade
        _stc_state["status_msg"] = f"صفقة مفتوحة: {direction} @ ${contract['mid']:.2f}"
    _stc_send_entry_msg(trade)
    logger.info(f"[StratC] ✅ دخول {direction} | {contract['symbol']} @ ${contract['mid']:.2f}")

def _stc_close_trade(trade, exit_price, exit_type, pnl_pct):
    """إغلاق صفقة Strategy C."""
    order = place_option_order(trade["symbol"], 1, "sell", order_type="market", position_intent="close")
    trade["exit_price"] = exit_price
    trade["exit_type"] = exit_type
    trade["pnl_pct"] = pnl_pct
    with _stc_lock:
        _stc_state["active_trade"] = None
        _stc_state["trades_today"].append(trade)
        _stc_state["status_msg"] = f"آخر صفقة: {exit_type} ({pnl_pct:+.1f}%)"
    _stc_send_close_msg(trade, exit_price, exit_type, pnl_pct)
    logger.info(f"[StratC] إغلاق: {exit_type} | P&L: {pnl_pct:+.1f}%")

def _stc_monitor_trade(trade):
    """مراقبة صفقة مفتوحة لـ Strategy C."""
    try:
        snap = get_tsla_snapshot()
        if not snap:
            return
        price = snap.get("price", 0)
        if not price:
            return
        delta = trade["approx_delta"]
        price_change = price - trade["entry_stock_price"]
        if trade["direction"] == "PUT":
            price_change = -price_change
        estimated_option_price = trade["entry_price"] + (price_change * delta)
        estimated_option_price = max(0.01, estimated_option_price)

        pnl_pct = (estimated_option_price - trade["entry_price"]) / trade["entry_price"]

        # MAE/MFE tracking
        if pnl_pct < trade.get("mae", 0):
            trade["mae"] = pnl_pct
        if pnl_pct > trade.get("mfe", 0):
            trade["mfe"] = pnl_pct

        # TP +10%
        if pnl_pct >= STC_TP_PCT:
            _stc_close_trade(trade, estimated_option_price, f"TP +{STC_TP_PCT*100:.0f}%", pnl_pct * 100)
            return
        # SL -12%
        if pnl_pct <= STC_SL_PCT:
            _stc_close_trade(trade, estimated_option_price, f"SL {STC_SL_PCT*100:.0f}%", pnl_pct * 100)
            return
    except Exception as e:
        logger.error(f"[StratC] Monitor error: {e}")

def _strategy_c_loop():
    """Main loop لـ Strategy C — Opening Range Breakout."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    logger.info("[StratC] 📈 V11.2 thread started")

    while True:
        try:
            with _stc_lock:
                if not _stc_state["running"]:
                    break
            now = datetime.now(et_tz)
            now_str = now.strftime("%I:%M %p")
            with _stc_lock:
                _stc_state["last_check"] = now_str

            # بناء Opening Range (مرة واحدة بعد 10:00 AM)
            open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed = (now - open_time).total_seconds() / 60
            with _stc_lock:
                or_built = _stc_state["or_built"]

            if elapsed >= 30 and not or_built:
                or_high, or_low = _stc_build_opening_range()
                if or_high and or_low:
                    with _stc_lock:
                        _stc_state["or_high"] = or_high
                        _stc_state["or_low"] = or_low
                        _stc_state["or_built"] = True
                    send_telegram(
                        f"📊 *Strategy C — Opening Range*\n"
                        f"🔼 High: ${or_high:.2f}\n"
                        f"🔽 Low: ${or_low:.2f}\n"
                        f"📐 Range: ${or_high - or_low:.2f}"
                    )
                    logger.info(f"[StratC] OR built: ${or_low:.2f} - ${or_high:.2f}")

            # Reset OR في بداية يوم جديد
            if elapsed < 5:
                with _stc_lock:
                    _stc_state["or_built"] = False
                    _stc_state["or_high"] = None
                    _stc_state["or_low"] = None
                    _stc_state["trades_today"] = []

            # إغلاق إجباري
            if _stc_is_force_close():
                with _stc_lock:
                    trade = _stc_state.get("active_trade")
                if trade:
                    snap = get_tsla_snapshot()
                    ep = snap.get("price", trade["entry_stock_price"]) if snap else trade["entry_stock_price"]
                    delta = trade["approx_delta"]
                    pc = ep - trade["entry_stock_price"]
                    if trade["direction"] == "PUT":
                        pc = -pc
                    est_price = max(0.01, trade["entry_price"] + pc * delta)
                    pnl = (est_price - trade["entry_price"]) / trade["entry_price"] * 100
                    _stc_close_trade(trade, est_price, "إغلاق إجباري 12:30 PM", pnl)
                time.sleep(60)
                continue

            if not _stc_in_window():
                with _stc_lock:
                    _stc_state["status_msg"] = "انتظار نافذة 10:05 AM"
                time.sleep(30)
                continue

            with _stc_lock:
                active_trade = _stc_state.get("active_trade")

            if active_trade:
                _stc_monitor_trade(active_trade)
            else:
                with _stc_lock:
                    _stc_state["status_msg"] = f"يراقب OR... {now_str}"
                direction, price, vwap, reasons = _stc_check_breakout()
                if direction:
                    _stc_execute_entry(direction, price, vwap, reasons)
                else:
                    with _stc_lock:
                        _stc_state["status_msg"] = f"انتظار: {reasons.get('reason','لا إشارة')}"
        except Exception as e:
            logger.error(f"[StratC] Loop error: {e}")
        time.sleep(STC_LOOP_SLEEP)

def _stc_weekly_report_combined():
    """تقرير أسبوعي مقارن لجميع الاستراتيجيات (يعمل مع Pyramid report)."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    while True:
        try:
            now = datetime.now(et_tz)
            # كل جمعة 4:05 PM (بعد تقرير Pyramid بـ 5 دقائق)
            if now.weekday() == 4 and now.hour == 16 and 5 <= now.minute < 7:
                with _stb_lock:
                    trades_b = list(_stb_state["trades_today"])
                with _stc_lock:
                    trades_c = list(_stc_state["trades_today"])
                with _pyr_lock:
                    trades_a = list(_pyr_state["trades_today"])

                def _calc_stats(trades, name):
                    total = len(trades)
                    if total == 0:
                        return f"   {name}: لا صفقات"
                    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
                    wr = wins / total * 100
                    avg_pnl = sum(t.get("pnl_pct", 0) for t in trades) / total
                    return f"   {name}: {total} صفقات | Win {wr:.0f}% | Avg P&L: {avg_pnl:+.1f}%"

                msg = (
                    f"📊 *STRATEGY LAB — تقرير أسبوعي مقارن*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_calc_stats(trades_a, 'A (Pyramid ITM)')}\n"
                    f"{_calc_stats(trades_b, 'B (VWAP Bounce)')}\n"
                    f"{_calc_stats(trades_c, 'C (ORB)')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                )
                # أفضل استراتيجية
                all_stats = []
                for name, trades in [("A", trades_a), ("B", trades_b), ("C", trades_c)]:
                    if trades:
                        wr = sum(1 for t in trades if t.get("pnl_pct",0)>0) / len(trades) * 100
                        all_stats.append((name, wr))
                if all_stats:
                    best = max(all_stats, key=lambda x: x[1])
                    msg += f"🏆 أفضل استراتيجية: {best[0]} (Win Rate: {best[1]:.0f}%)\n"

                send_telegram(msg)
        except Exception as e:
            logger.error(f"[StratC] Weekly combined report error: {e}")
        time.sleep(60)

def start_strategy_c():
    """تشغيل Strategy C."""
    with _stc_lock:
        if _stc_state["running"]:
            return False
        _stc_state["running"] = True
    threading.Thread(target=_strategy_c_loop, daemon=True).start()
    threading.Thread(target=_stc_weekly_report_combined, daemon=True).start()
    logger.info("[StratC] ✅ V11.2 ORB started")
    return True

def get_strategy_c_status():
    """حالة Strategy C."""
    with _stc_lock:
        trade = _stc_state.get("active_trade")
        return {
            "ok":           True,
            "strategy":     "C — Opening Range Breakout",
            "running":      _stc_state["running"],
            "status":       _stc_state["status_msg"],
            "last_check":   _stc_state["last_check"],
            "or_high":      _stc_state.get("or_high"),
            "or_low":       _stc_state.get("or_low"),
            "active_trade": {
                "symbol":      trade["symbol"],
                "direction":   trade["direction"],
                "entry_price": trade["entry_price"],
                "entry_time":  trade["entry_time"],
            } if trade else None,
            "trades_today": len(_stc_state["trades_today"]),
        }


# ══════════════════════════════════════════════════════════════════════════════
# V13.0: Strategy D — Mosquito Trend (ATM Options + Reinforcement)
# الفكرة: MACD Zero Cross (3M) + VWAP + RSI + OBV → دخول 1 عقد ATM
#         تعزيز عند +$0.45 → بيع العقدين عند TP أو SL
# نافذة العمل: 9:35 AM → 2:00 PM ET
# الملعب: ATM Options (Delta 0.45-0.60) | 0DTE أو أقرب Expiry
# الهدف: جمع بيانات شاملة (RSI, MACD, OBV, Mom, ATR, SPY, MAE, MFE)
# ══════════════════════════════════════════════════════════════════════════════

# ─── Configuration ───────────────────────────────────────────────────────────
STD_START_MINUTES   = 5         # 9:30 + 5 = 9:35 AM
STD_END_MINUTES     = 270       # 9:30 + 270 = 2:00 PM
STD_TP_DOLLARS      = 0.95      # +$0.95 TP (بيع العقدين)
STD_SL_DOLLARS      = -0.70     # -$0.70 SL (بيع الكل)
STD_REINFORCE_AT    = 0.45      # +$0.45 → شراء عقد ثاني
STD_SL_AFTER_REINFORCE = 0.0    # Breakeven بعد التعزيز (سعر الدخول الأول)
STD_DELTA_MIN       = 0.45
STD_DELTA_MAX       = 0.60
STD_LOOP_SLEEP      = 30        # رُفع من 15 إلى 30 ثانية لتخفيف RAM
STD_RSI_CALL_MIN    = 45
STD_RSI_CALL_MAX    = 70
STD_RSI_PUT_MIN     = 30
STD_RSI_PUT_MAX     = 55
STD_MAX_TRADES_DAY  = 4         # حد أقصى 4 صفقات باليوم

_std_lock  = threading.Lock()
_std_state = {
    "running":      False,
    "active_trade": None,
    "last_check":   "--",
    "trades_today": [],
    "status_msg":   "غير نشط",
    "daily_date":   None,
    "last_failed_entry": 0,   # timestamp آخر فشل تنفيذ
    "failed_entry_count": 0,  # عدد محاولات الفشل المتتالية
}

# ─── Time Window ─────────────────────────────────────────────────────────────

def _std_in_window():
    """هل نحن في نافذة Strategy D (9:35 AM - 2:00 PM ET)."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return STD_START_MINUTES <= elapsed <= STD_END_MINUTES

def _std_is_force_close():
    """هل حان وقت الإغلاق الإجباري (2:00 PM ET)."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - open_time).total_seconds() / 60
    return elapsed >= STD_END_MINUTES

# ─── Indicator Calculations ──────────────────────────────────────────────────

def _std_calc_rsi(bars, period=14):
    """حساب RSI من bars."""
    closes = [float(b["c"]) for b in bars]
    return _rw_rsi(closes, period)

def _std_calc_macd_zero_cross(bars):
    """
    فحص MACD Zero Cross على 3M.
    يُرجع: (direction, macd_line_curr, macd_line_prev) أو (None, ...)
    """
    closes = [float(b["c"]) for b in bars]
    if len(closes) < 38:
        return None, None, None

    macd_series = []
    for i in range(26, len(closes) + 1):
        ef = _rw_ema(closes[:i], 12)
        es = _rw_ema(closes[:i], 26)
        if ef and es:
            macd_series.append(ef - es)

    if len(macd_series) < 3:
        return None, None, None

    curr_macd = macd_series[-1]
    prev_macd = macd_series[-2]

    # Zero Cross: سالب → موجب = CALL | موجب → سالب = PUT
    if prev_macd < 0 and curr_macd > 0:
        return "CALL", curr_macd, prev_macd
    elif prev_macd > 0 and curr_macd < 0:
        return "PUT", curr_macd, prev_macd

    return None, curr_macd, prev_macd

def _std_calc_obv_slope(bars, lookback=5):
    """حساب OBV slope — هل صاعد أو هابط."""
    closes = [float(b["c"]) for b in bars]
    volumes = [int(b["v"]) for b in bars]
    obv = _rw_obv(closes, volumes)
    if len(obv) < lookback + 1:
        return None, None
    slope = obv[-1] - obv[-lookback]
    obv_ma = sum(obv[-lookback:]) / lookback if len(obv) >= lookback else obv[-1]
    return slope, obv[-1] - obv_ma

def _std_calc_momentum(bars, period=10):
    """حساب Momentum (Rate of Change)."""
    closes = [float(b["c"]) for b in bars]
    if len(closes) < period + 1:
        return None
    return closes[-1] - closes[-period - 1]

def _std_calc_atr(bars, period=14):
    """حساب ATR."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        pc = float(bars[i-1]["c"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def _std_calc_volume_ratio(bars, lookback=20):
    """حساب نسبة حجم الشمعة الأخيرة مقارنة بالمتوسط."""
    volumes = [int(b["v"]) for b in bars]
    if len(volumes) < lookback + 1:
        return None
    avg_vol = sum(volumes[-lookback-1:-1]) / lookback
    if avg_vol == 0:
        return None
    return volumes[-1] / avg_vol

# ─── Entry Signal Check ──────────────────────────────────────────────────────

def _std_check_entry():
    """
    فحص شروط الدخول لـ Strategy D (Mosquito Trend).
    شروط CALL: MACD Zero Cross (3M) سالب→موجب + السعر فوق VWAP + RSI 45-70 + OBV صاعد
    شروط PUT: MACD Zero Cross (3M) موجب→سالب + السعر تحت VWAP + RSI 30-55 + OBV هابط
    """
    bars_3m = get_tsla_bars("3Min", 50)
    if not bars_3m or len(bars_3m) < 38:
        return None, {"reason": "بيانات 3M غير كافية"}

    # 1. MACD Zero Cross
    direction, macd_curr, macd_prev = _std_calc_macd_zero_cross(bars_3m)
    if not direction:
        reason = f"لا Zero Cross (MACD: {macd_curr:.4f})" if macd_curr is not None else "MACD غير متاح"
        return None, {"reason": reason}

    # 2. السعر vs VWAP
    snap = get_tsla_snapshot()
    if not snap:
        return None, {"reason": "Snapshot غير متاح"}
    price = snap["price"]
    vwap = snap["vwap"]

    if direction == "CALL" and price <= vwap:
        return None, {"reason": f"CALL لكن السعر ${price:.2f} تحت VWAP ${vwap:.2f}"}
    if direction == "PUT" and price >= vwap:
        return None, {"reason": f"PUT لكن السعر ${price:.2f} فوق VWAP ${vwap:.2f}"}

    # 3. RSI
    rsi = _std_calc_rsi(bars_3m)
    if rsi is None:
        return None, {"reason": "RSI غير متاح"}

    if direction == "CALL" and not (STD_RSI_CALL_MIN <= rsi <= STD_RSI_CALL_MAX):
        return None, {"reason": f"RSI={rsi:.1f} خارج نطاق CALL ({STD_RSI_CALL_MIN}-{STD_RSI_CALL_MAX})"}
    if direction == "PUT" and not (STD_RSI_PUT_MIN <= rsi <= STD_RSI_PUT_MAX):
        return None, {"reason": f"RSI={rsi:.1f} خارج نطاق PUT ({STD_RSI_PUT_MIN}-{STD_RSI_PUT_MAX})"}

    # 4. OBV
    obv_slope, obv_vs_ma = _std_calc_obv_slope(bars_3m)
    if obv_slope is None:
        return None, {"reason": "OBV غير متاح"}

    if direction == "CALL" and obv_slope <= 0:
        return None, {"reason": f"OBV هابط ({obv_slope:,.0f}) — لا يدعم CALL"}
    if direction == "PUT" and obv_slope >= 0:
        return None, {"reason": f"OBV صاعد ({obv_slope:,.0f}) — لا يدعم PUT"}

    # ─── كل الشروط تحققت! ───
    momentum = _std_calc_momentum(bars_3m)
    atr = _std_calc_atr(bars_3m)
    vol_ratio = _std_calc_volume_ratio(bars_3m)
    spy_dir, spy_chg = get_spy_direction()
    macd_hist, _ = _rw_macd_hist([float(b["c"]) for b in bars_3m])

    entry_data = {
        "direction": direction,
        "price": price,
        "vwap": vwap,
        "vwap_distance_pct": round((price - vwap) / vwap * 100, 3),
        "rsi": rsi,
        "macd_line": round(macd_curr, 4) if macd_curr else 0,
        "macd_prev": round(macd_prev, 4) if macd_prev else 0,
        "macd_hist": round(macd_hist, 4) if macd_hist else 0,
        "obv_slope": obv_slope,
        "obv_vs_ma": obv_vs_ma,
        "momentum": round(momentum, 4) if momentum else 0,
        "atr": round(atr, 4) if atr else 0,
        "volume_ratio": round(vol_ratio, 2) if vol_ratio else 0,
        "spy_direction": spy_dir,
        "spy_change": spy_chg,
    }
    return entry_data, {"reason": "✅ كل الشروط تحققت"}

# ─── Contract Selection (ATM) ────────────────────────────────────────────────

def _std_find_contract(price, direction, expiry):
    """اختيار أفضل عقد ATM لـ Strategy D (Delta 0.45-0.60)."""
    option_type = "call" if direction == "CALL" else "put"

    if direction == "CALL":
        strike_min = round(price - 5, 0)
        strike_max = round(price + 2, 0)
    else:
        strike_min = round(price - 2, 0)
        strike_max = round(price + 5, 0)

    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    if not contracts:
        # جرب أقرب expiry بعد اليوم
        from datetime import date, timedelta as td
        tomorrow = (date.today() + td(days=1)).strftime("%Y-%m-%d")
        contracts = get_options_chain(tomorrow, option_type, strike_min, strike_max)
        if contracts:
            expiry = tomorrow

    if not contracts:
        return None

    best = None
    best_score = -1
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        itm_amount = abs(price - strike)
        # تقدير Delta لـ ATM
        if direction == "CALL":
            if strike > price:
                approx_delta = max(0.30, 0.50 - (strike - price) * 0.04)
            else:
                approx_delta = min(0.80, 0.50 + (price - strike) * 0.04)
        else:
            if strike < price:
                approx_delta = max(0.30, 0.50 - (price - strike) * 0.04)
            else:
                approx_delta = min(0.80, 0.50 + (strike - price) * 0.04)

        if not (STD_DELTA_MIN <= approx_delta <= STD_DELTA_MAX):
            continue

        oi = int(c.get("open_interest", 0))
        vol = int(c.get("volume", 0))

        sym = c.get("symbol", "")
        quote = get_option_quote(sym)
        if not quote or quote["mid"] < 0.50:
            continue

        bid = quote["bid"]
        ask = quote["ask"]
        mid = quote["mid"]
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 99
        if spread_pct > 8:
            continue

        atm_closeness = max(0, 10 - itm_amount)
        score = oi * 0.3 + vol * 0.3 + atm_closeness * 50 + (10 - spread_pct) * 20

        if score > best_score:
            best_score = score
            best = {
                "symbol": sym,
                "strike": strike,
                "expiry": expiry,
                "approx_delta": round(approx_delta, 2),
                "open_interest": oi,
                "volume": vol,
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread_pct, 1),
            }
    return best

# ─── Execute Entry ───────────────────────────────────────────────────────────

def _std_execute_entry(entry_data):
    """تنفيذ دخول Strategy D."""
    import pytz
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")
    direction = entry_data["direction"]
    price = entry_data["price"]
    expiry = _today_expiry()

    contract = _std_find_contract(price, direction, expiry)
    if not contract:
        logger.warning(f"[StratD] لا يوجد عقد ATM مناسب لـ {direction}")
        return

    order = place_option_order(contract["symbol"], 1, "buy", order_type="market", position_intent="buy_to_open")
    if not order:
        with _std_lock:
            _std_state["last_failed_entry"] = time.time()
            _std_state["failed_entry_count"] = _std_state.get("failed_entry_count", 0) + 1
            fail_count = _std_state["failed_entry_count"]
        # أرسل رسالة فشل مرة واحدة فقط (ليس في كل محاولة)
        if fail_count == 1:
            send_telegram(f"❌ Strategy D: فشل تنفيذ أمر الشراء لـ {contract['symbol']}\n⏳ إيقاف مؤقت 2 دقيقة")
        logger.warning(f"[StratD] Order failed (attempt {fail_count}) — cooldown 120s")
        return

    trade = {
        "strategy": "D",
        "symbol": contract["symbol"],
        "direction": direction,
        "strike": contract["strike"],
        "expiry": contract["expiry"],
        "approx_delta": contract["approx_delta"],
        "open_interest": contract["open_interest"],
        "option_volume": contract["volume"],
        "spread_pct": contract["spread_pct"],
        "entry_price": contract["mid"],
        "entry_stock_price": price,
        "entry_time": now_str,
        "vwap": entry_data["vwap"],
        "qty": 1,
        "reinforced": False,
        "order_id": order.get("id", ""),
        # بيانات المؤشرات عند الدخول
        "entry_rsi": entry_data["rsi"],
        "entry_macd_line": entry_data["macd_line"],
        "entry_macd_prev": entry_data["macd_prev"],
        "entry_macd_hist": entry_data["macd_hist"],
        "entry_obv_slope": entry_data["obv_slope"],
        "entry_obv_vs_ma": entry_data["obv_vs_ma"],
        "entry_momentum": entry_data["momentum"],
        "entry_atr": entry_data["atr"],
        "entry_volume_ratio": entry_data["volume_ratio"],
        "entry_vwap_distance": entry_data["vwap_distance_pct"],
        "entry_spy_direction": entry_data["spy_direction"],
        "entry_spy_change": entry_data["spy_change"],
        # MAE/MFE
        "mae": 0.0,
        "mfe": 0.0,
        "mae_dollars": 0.0,
        "mfe_dollars": 0.0,
    }

    with _std_lock:
        _std_state["active_trade"] = trade
        _std_state["status_msg"] = f"🦟 صفقة: {direction} @ ${contract['mid']:.2f}"

    _std_send_entry_msg(trade)
    logger.info(f"[StratD] ✅ دخول {direction} | {contract['symbol']} @ ${contract['mid']:.2f} | RSI={entry_data['rsi']:.1f}")

# ─── Monitor & Reinforce ─────────────────────────────────────────────────────

def _std_monitor_trade(trade):
    """مراقبة صفقة مفتوحة + تعزيز + TP/SL."""
    try:
        quote = get_option_quote(trade["symbol"])
        if quote and quote["mid"] > 0:
            current_price = quote["mid"]
        else:
            snap = get_tsla_snapshot()
            if not snap:
                return
            stock_price = snap["price"]
            delta = trade["approx_delta"]
            price_change = stock_price - trade["entry_stock_price"]
            if trade["direction"] == "PUT":
                price_change = -price_change
            current_price = max(0.01, trade["entry_price"] + (price_change * delta))

        entry_price = trade["entry_price"]
        pnl_dollars = current_price - entry_price

        # MAE/MFE tracking
        if pnl_dollars < trade.get("mae_dollars", 0):
            trade["mae_dollars"] = pnl_dollars
            trade["mae"] = pnl_dollars / entry_price
        if pnl_dollars > trade.get("mfe_dollars", 0):
            trade["mfe_dollars"] = pnl_dollars
            trade["mfe"] = pnl_dollars / entry_price

        # التعزيز: +$0.45 → شراء عقد ثاني
        if not trade["reinforced"] and pnl_dollars >= STD_REINFORCE_AT:
            order = place_option_order(trade["symbol"], 1, "buy", order_type="market", position_intent="open")
            if order:
                trade["reinforced"] = True
                trade["qty"] = 2
                trade["reinforce_price"] = current_price
                import pytz
                trade["reinforce_time"] = datetime.now(
                    pytz.timezone("America/New_York")
                ).strftime("%I:%M %p")
                with _std_lock:
                    _std_state["status_msg"] = f"🦟💪 تعزيز! 2 عقود | +${pnl_dollars:.2f}"
                send_telegram(
                    f"🦟💪 *STRATEGY D — تعزيز!*\n"
                    f"📈 الربح: +${pnl_dollars:.2f} ({pnl_dollars/entry_price*100:+.1f}%)\n"
                    f"🎯 عقد ثاني @ ${current_price:.2f}\n"
                    f"🛡️ SL → Breakeven (${entry_price:.2f})"
                )
                logger.info(f"[StratD] 💪 تعزيز @ ${current_price:.2f} | P&L: +${pnl_dollars:.2f}")
            return

        # TP: +$0.95
        if pnl_dollars >= STD_TP_DOLLARS:
            _std_close_trade(trade, current_price, f"TP +${STD_TP_DOLLARS:.2f}", pnl_dollars)
            return

        # SL Logic
        if trade["reinforced"]:
            if current_price <= entry_price:
                _std_close_trade(trade, current_price, "SL Breakeven (بعد تعزيز)", current_price - entry_price)
                return
        else:
            if pnl_dollars <= STD_SL_DOLLARS:
                _std_close_trade(trade, current_price, f"SL ${STD_SL_DOLLARS:.2f}", pnl_dollars)
                return

    except Exception as e:
        logger.error(f"[StratD] Monitor error: {e}")

# ─── Close Trade ─────────────────────────────────────────────────────────────

def _std_close_trade(trade, exit_price, exit_type, pnl_dollars):
    """إغلاق صفقة Strategy D + جمع بيانات الإغلاق."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    now_str = datetime.now(et_tz).strftime("%I:%M %p")

    qty = trade.get("qty", 1)
    place_option_order(trade["symbol"], qty, "sell", order_type="market", position_intent="close")

    total_pnl = pnl_dollars
    if trade["reinforced"]:
        reinforce_pnl = exit_price - trade.get("reinforce_price", exit_price)
        total_pnl = pnl_dollars + reinforce_pnl

    pnl_pct = pnl_dollars / trade["entry_price"] * 100

    # جمع بيانات الإغلاق
    bars_3m = get_tsla_bars("3Min", 50)
    exit_rsi = _std_calc_rsi(bars_3m) if bars_3m and len(bars_3m) >= 15 else None
    exit_macd_hist = None
    if bars_3m and len(bars_3m) >= 38:
        exit_macd_hist, _ = _rw_macd_hist([float(b["c"]) for b in bars_3m])
    exit_obv_slope = None
    if bars_3m and len(bars_3m) >= 6:
        exit_obv_slope, _ = _std_calc_obv_slope(bars_3m)
    exit_momentum = _std_calc_momentum(bars_3m) if bars_3m and len(bars_3m) >= 11 else None
    spy_dir, spy_chg = get_spy_direction()

    snap = get_tsla_snapshot()
    exit_stock_price = snap["price"] if snap else trade["entry_stock_price"]

    trade["exit_price"] = exit_price
    trade["exit_stock_price"] = exit_stock_price
    trade["exit_type"] = exit_type
    trade["exit_time"] = now_str
    trade["pnl_dollars"] = round(pnl_dollars, 2)
    trade["pnl_total"] = round(total_pnl, 2)
    trade["pnl_pct"] = round(pnl_pct, 1)
    trade["exit_rsi"] = exit_rsi
    trade["exit_macd_hist"] = round(exit_macd_hist, 4) if exit_macd_hist else None
    trade["exit_obv_slope"] = exit_obv_slope
    trade["exit_momentum"] = round(exit_momentum, 4) if exit_momentum else None
    trade["exit_spy_direction"] = spy_dir
    trade["exit_spy_change"] = spy_chg

    with _std_lock:
        _std_state["active_trade"] = None
        _std_state["trades_today"].append(trade)
        emoji = "✅" if pnl_dollars > 0 else "❌"
        _std_state["status_msg"] = f"{emoji} آخر: {exit_type} ({pnl_pct:+.1f}%)"

    _std_send_close_msg(trade)
    _std_save_to_csv(trade)
    logger.info(f"[StratD] إغلاق: {exit_type} | P&L: ${pnl_dollars:+.2f} ({pnl_pct:+.1f}%) | Total: ${total_pnl:+.2f}")

# ─── CSV Data Collection ─────────────────────────────────────────────────────

def _std_save_to_csv(trade):
    """حفظ بيانات الصفقة في CSV لتحليل لاحق."""
    import csv
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mosquito_trades.csv")

    file_exists = os.path.exists(csv_path)
    try:
        with open(csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "date", "entry_time", "exit_time", "direction", "symbol", "strike",
                    "entry_price", "exit_price", "entry_stock", "exit_stock",
                    "qty", "reinforced", "pnl_dollars", "pnl_total", "pnl_pct", "exit_type",
                    "entry_rsi", "entry_macd_line", "entry_macd_hist", "entry_obv_slope",
                    "entry_momentum", "entry_atr", "entry_volume_ratio", "entry_vwap_distance",
                    "entry_spy", "entry_spy_chg",
                    "exit_rsi", "exit_macd_hist", "exit_obv_slope", "exit_momentum",
                    "exit_spy", "exit_spy_chg",
                    "mae_dollars", "mfe_dollars", "mae_pct", "mfe_pct",
                    "vwap"
                ])
            writer.writerow([
                trade.get("expiry", ""),
                trade.get("entry_time", ""),
                trade.get("exit_time", ""),
                trade.get("direction", ""),
                trade.get("symbol", ""),
                trade.get("strike", ""),
                trade.get("entry_price", ""),
                trade.get("exit_price", ""),
                trade.get("entry_stock_price", ""),
                trade.get("exit_stock_price", ""),
                trade.get("qty", 1),
                trade.get("reinforced", False),
                trade.get("pnl_dollars", 0),
                trade.get("pnl_total", 0),
                trade.get("pnl_pct", 0),
                trade.get("exit_type", ""),
                trade.get("entry_rsi", ""),
                trade.get("entry_macd_line", ""),
                trade.get("entry_macd_hist", ""),
                trade.get("entry_obv_slope", ""),
                trade.get("entry_momentum", ""),
                trade.get("entry_atr", ""),
                trade.get("entry_volume_ratio", ""),
                trade.get("entry_vwap_distance", ""),
                trade.get("entry_spy_direction", ""),
                trade.get("entry_spy_change", ""),
                trade.get("exit_rsi", ""),
                trade.get("exit_macd_hist", ""),
                trade.get("exit_obv_slope", ""),
                trade.get("exit_momentum", ""),
                trade.get("exit_spy_direction", ""),
                trade.get("exit_spy_change", ""),
                trade.get("mae_dollars", 0),
                trade.get("mfe_dollars", 0),
                round(trade.get("mae", 0) * 100, 1),
                round(trade.get("mfe", 0) * 100, 1),
                trade.get("vwap", ""),
            ])
        logger.info(f"[StratD] 📊 Trade saved to CSV")
    except Exception as e:
        logger.error(f"[StratD] CSV save error: {e}")

# ─── Telegram Messages ───────────────────────────────────────────────────────

def _std_send_entry_msg(trade):
    """إرسال رسالة دخول Strategy D على Telegram."""
    msg = (
        f"🦟 <b>STRATEGY D — Mosquito Trend | {trade['direction']}</b>\n"
        f"🕐 دخول: {trade['entry_time']} ET\n"
        f"📍 TSLA: ${trade['entry_stock_price']:.2f} | VWAP: ${trade['vwap']:.2f}\n"
        f"─────────────────────\n"
        f"🎯 العقد: <code>{trade['symbol']}</code>\n"
        f"   Strike: ${trade['strike']:.0f} | Delta≈{trade['approx_delta']}\n"
        f"   سعر الدخول: ${trade['entry_price']:.2f}\n"
        f"   Spread: {trade['spread_pct']}%\n"
        f"─────────────────────\n"
        f"📊 <b>المؤشرات عند الدخول:</b>\n"
        f"   RSI: {trade['entry_rsi']:.1f}\n"
        f"   MACD Line: {trade['entry_macd_line']:.4f}\n"
        f"   MACD Hist: {trade['entry_macd_hist']:.4f}\n"
        f"   OBV Slope: {trade['entry_obv_slope']:,.0f}\n"
        f"   Momentum: {trade['entry_momentum']:.4f}\n"
        f"   ATR: ${trade['entry_atr']:.3f}\n"
        f"   Vol Ratio: {trade['entry_volume_ratio']:.2f}x\n"
        f"   VWAP Dist: {trade['entry_vwap_distance']:+.3f}%\n"
        f"─────────────────────\n"
        f"🌐 SPY: {trade['entry_spy_direction']} ({trade['entry_spy_change']:+.3f}%)\n"
        f"─────────────────────\n"
        f"🎯 TP: +${STD_TP_DOLLARS:.2f} → بيع الكل\n"
        f"💪 تعزيز: +${STD_REINFORCE_AT:.2f} → عقد ثاني\n"
        f"🛑 SL: ${STD_SL_DOLLARS:.2f}\n"
        f"⏰ إغلاق إجباري: 2:00 PM ET"
    )
    send_telegram(msg)

def _std_send_close_msg(trade):
    """إرسال رسالة إغلاق Strategy D."""
    emoji = "✅" if trade["pnl_dollars"] > 0 else "❌"
    msg = (
        f"{emoji} <b>STRATEGY D — إغلاق ({trade['exit_type']})</b>\n"
        f"🕐 {trade['exit_time']} ET\n"
        f"─────────────────────\n"
        f"💰 <b>النتيجة:</b>\n"
        f"   P&L عقد 1: ${trade['pnl_dollars']:+.2f} ({trade['pnl_pct']:+.1f}%)\n"
        f"   P&L إجمالي: ${trade['pnl_total']:+.2f}\n"
        f"   عدد العقود: {trade['qty']} | تعزيز: {'✅' if trade['reinforced'] else '❌'}\n"
        f"─────────────────────\n"
        f"📊 <b>المؤشرات عند الإغلاق:</b>\n"
        f"   RSI: {trade.get('exit_rsi', 'N/A')}\n"
        f"   MACD Hist: {trade.get('exit_macd_hist', 'N/A')}\n"
        f"   OBV Slope: {trade.get('exit_obv_slope', 'N/A')}\n"
        f"   Momentum: {trade.get('exit_momentum', 'N/A')}\n"
        f"─────────────────────\n"
        f"📉 MAE: ${trade.get('mae_dollars',0):.2f} | MFE: +${trade.get('mfe_dollars',0):.2f}\n"
        f"🌐 SPY: {trade.get('exit_spy_direction','N/A')} ({trade.get('exit_spy_change',0):+.3f}%)\n"
        f"─────────────────────\n"
        f"📋 <b>ملخص:</b>\n"
        f"   {trade['direction']} | <code>{trade['symbol']}</code>\n"
        f"   دخول: ${trade['entry_price']:.2f} → خروج: ${trade['exit_price']:.2f}\n"
        f"   TSLA: ${trade['entry_stock_price']:.2f} → ${trade.get('exit_stock_price', 0):.2f}"
    )
    send_telegram(msg)

# ─── Main Loop ───────────────────────────────────────────────────────────────

def _strategy_d_loop():
    """Main loop لـ Strategy D — Mosquito Trend."""
    import pytz
    et_tz = pytz.timezone("America/New_York")
    logger.info("[StratD] 🦟 V13.0 Mosquito Trend thread started")

    while True:
        try:
            with _std_lock:
                if not _std_state["running"]:
                    break

            now = datetime.now(et_tz)
            now_str = now.strftime("%I:%M %p")
            today_str = now.strftime("%Y-%m-%d")

            with _std_lock:
                _std_state["last_check"] = now_str
                if _std_state["daily_date"] != today_str:
                    _std_state["daily_date"] = today_str
                    _std_state["trades_today"] = []

            # إغلاق إجباري عند 2:00 PM
            if _std_is_force_close():
                with _std_lock:
                    trade = _std_state.get("active_trade")
                if trade:
                    quote = get_option_quote(trade["symbol"])
                    exit_price = quote["mid"] if quote and quote["mid"] > 0 else trade["entry_price"]
                    pnl = exit_price - trade["entry_price"]
                    _std_close_trade(trade, exit_price, "إغلاق إجباري 2:00 PM", pnl)
                time.sleep(60)
                continue

            # خارج النافذة
            if not _std_in_window():
                with _std_lock:
                    _std_state["status_msg"] = "⏳ انتظار نافذة 9:35 AM"
                time.sleep(30)
                continue

            with _std_lock:
                active_trade = _std_state.get("active_trade")
                trades_count = len(_std_state["trades_today"])

            if active_trade:
                _std_monitor_trade(active_trade)
            else:
                if trades_count >= STD_MAX_TRADES_DAY:
                    with _std_lock:
                        _std_state["status_msg"] = f"⛔ الحد اليومي ({STD_MAX_TRADES_DAY} صفقات)"
                    time.sleep(60)
                    continue

                # تحقق من Cooldown بعد فشل التنفيذ (120 ثانية)
                with _std_lock:
                    last_failed = _std_state.get("last_failed_entry", 0)
                    fail_count  = _std_state.get("failed_entry_count", 0)

                if last_failed and (time.time() - last_failed) < 120:
                    remaining = int(120 - (time.time() - last_failed))
                    with _std_lock:
                        _std_state["status_msg"] = f"⏳ Cooldown بعد فشل التنفيذ — {remaining}s"
                    time.sleep(STD_LOOP_SLEEP)
                    continue
                elif last_failed and (time.time() - last_failed) >= 120:
                    # انتهى الـ Cooldown — أعد ضبط العداد
                    with _std_lock:
                        _std_state["last_failed_entry"] = 0
                        _std_state["failed_entry_count"] = 0

                entry_data, info = _std_check_entry()
                if entry_data:
                    _std_execute_entry(entry_data)
                else:
                    with _std_lock:
                        _std_state["status_msg"] = f"🔍 {info.get('reason', 'لا إشارة')[:50]} | {now_str}"

        except Exception as e:
            logger.error(f"[StratD] Loop error: {e}")
        time.sleep(STD_LOOP_SLEEP)

# ─── Start / Stop / Status ───────────────────────────────────────────────────

def start_mosquito():
    """تشغيل Strategy D (Mosquito Trend)."""
    with _std_lock:
        if _std_state["running"]:
            logger.warning("[StratD] already running")
            return False
        _std_state["running"] = True
    threading.Thread(target=_strategy_d_loop, daemon=True).start()
    logger.info("[StratD] ✅ V13.0 Mosquito Trend started")
    return True

def stop_mosquito():
    """إيقاف Strategy D."""
    with _std_lock:
        _std_state["running"] = False
        _std_state["status_msg"] = "موقوف"

def get_mosquito_status():
    """حالة Strategy D."""
    with _std_lock:
        trade = _std_state.get("active_trade")
        return {
            "ok":           True,
            "strategy":     "D — Mosquito Trend (ATM + Reinforce)",
            "running":      _std_state["running"],
            "status":       _std_state["status_msg"],
            "last_check":   _std_state["last_check"],
            "active_trade": {
                "symbol":      trade["symbol"],
                "direction":   trade["direction"],
                "entry_price": trade["entry_price"],
                "entry_time":  trade["entry_time"],
                "qty":         trade["qty"],
                "reinforced":  trade["reinforced"],
                "rsi":         trade["entry_rsi"],
                "mae":         round(trade.get("mae_dollars", 0), 2),
                "mfe":         round(trade.get("mfe_dollars", 0), 2),
            } if trade else None,
            "trades_today": len(_std_state["trades_today"]),
            "max_trades":   STD_MAX_TRADES_DAY,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Market Briefing — رسالة التلقرام كل 15 دقيقة
# ══════════════════════════════════════════════════════════════════════════════

def _calc_rsi_brief(closes, period=14):
    # تعديل الـ period تلقائياً إذا البيانات غير كافية
    if period < 2:
        period = 2
    if len(closes) < period + 1:
        if len(closes) >= 5:
            period = len(closes) - 2  # استخدام كل البيانات المتاحة
        else:
            return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    p = min(period, len(gains))
    ag = sum(gains[-p:]) / p
    al = sum(losses[-p:]) / p
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 1)


def _calc_atr_brief(bars, period=14):
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        pc = float(bars[i-1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    p = min(period, len(trs))
    return round(sum(trs[-p:]) / p, 3)


def _calc_macd_brief(closes, fast=12, slow=26, signal=9):
    # Fallback: إذا البيانات غير كافية للـ standard MACD، نستخدم periods أصغر
    if len(closes) < slow + signal:
        if len(closes) >= 20:  # نستخدم MACD(6,13,5) كـ fallback
            fast, slow, signal = 6, 13, 5
        else:
            return 0.0, 0.0, 0.0
    macd_series = []
    for i in range(slow - 1, len(closes)):
        ef = _ema(closes[:i+1], fast)
        es = _ema(closes[:i+1], slow)
        macd_series.append(ef - es)
    if len(macd_series) < signal:
        return round(macd_series[-1] if macd_series else 0, 3), 0.0, 0.0
    sig_line = _ema(macd_series, signal)
    macd_line = macd_series[-1]
    hist = macd_line - sig_line
    return round(macd_line, 3), round(sig_line, 3), round(hist, 3)


def _calc_bb_brief(closes, period=20, mult=2.0):
    # استخدام كل البيانات المتاحة إذا أقل من period
    actual_period = min(period, len(closes))
    if actual_period < 3:
        c = closes[-1]
        return c, c, c
    window = closes[-actual_period:]
    mid = sum(window) / actual_period
    std = (sum((x - mid) ** 2 for x in window) / actual_period) ** 0.5
    if std < 0.01:  # تجنب BB متطابقة
        std = abs(max(window) - min(window)) / 4 if max(window) != min(window) else 0.5
    return round(mid + mult * std, 2), round(mid, 2), round(mid - mult * std, 2)


def _reversal_zones_brief(bars_5m, price, vwap, pdh, pdl, orh, orl):
    closes = [float(b["c"]) for b in bars_5m]
    highs  = [float(b["h"]) for b in bars_5m]
    lows   = [float(b["l"]) for b in bars_5m]

    zones = []
    bb_upper, bb_mid, bb_lower = _calc_bb_brief(closes, 20)
    zones += [
        {"price": bb_upper, "type": "resistance", "name": "BB Upper", "icon": "🔴"},
        {"price": bb_mid,   "type": "pivot",      "name": "BB Mid",   "icon": "⚪"},
        {"price": bb_lower, "type": "support",    "name": "BB Lower", "icon": "🟢"},
    ]
    if vwap > 0:
        zones.append({"price": round(vwap, 2), "type": "pivot", "name": "VWAP", "icon": "🔵"})
    if pdh > 0:
        zones.append({"price": round(pdh, 2), "type": "resistance", "name": "PDH", "icon": "🔴"})
    if pdl > 0:
        zones.append({"price": round(pdl, 2), "type": "support",    "name": "PDL", "icon": "🟢"})
    if orh > 0:
        zones.append({"price": round(orh, 2), "type": "resistance", "name": "OR High", "icon": "🟠"})
    if orl > 0:
        zones.append({"price": round(orl, 2), "type": "support",    "name": "OR Low",  "icon": "🟠"})
    if len(closes) >= 9:
        zones.append({"price": round(_ema(closes, 9), 2),  "type": "dynamic", "name": "EMA9",  "icon": "🟡"})
    if len(closes) >= 21:
        zones.append({"price": round(_ema(closes, 21), 2), "type": "dynamic", "name": "EMA21", "icon": "🟡"})

    # مستويات نفسية
    base = round(price / 2.5) * 2.5
    for off in [-5, -2.5, 2.5, 5]:
        lvl = round(base + off, 2)
        if 0 < abs(lvl - price) <= 7:
            zones.append({"price": lvl, "type": "psych", "name": f"${lvl:.1f}", "icon": "⚫"})

    zones = [z for z in zones if z["price"] > 0]
    resistances = sorted([z for z in zones if z["type"] == "resistance" and z["price"] > price],
                         key=lambda z: z["price"])[:3]
    supports    = sorted([z for z in zones if z["type"] == "support"    and z["price"] < price],
                         key=lambda z: -z["price"])[:3]
    return resistances, supports


def _wave_brief(bars_5m):
    if len(bars_5m) < 6:
        return {"wave": "غير محدد", "momentum": "محايد", "atr_now": 0, "atr_avg": 0,
                "atr_signal": "⚪", "wave_pos": 50, "recent_high": 0, "recent_low": 0}
    closes = [float(b["c"]) for b in bars_5m]
    highs  = [float(b["h"]) for b in bars_5m]
    lows   = [float(b["l"]) for b in bars_5m]

    atr_now = _calc_atr_brief(bars_5m[-5:], 5)
    atr_avg = _calc_atr_brief(bars_5m, 14)
    ratio = atr_now / atr_avg if atr_avg > 0 else 1.0
    atr_signal = "🔥 نشط جداً" if ratio > 1.3 else ("✅ طبيعي" if ratio > 0.8 else "😴 خامل")

    recent_move = closes[-1] - closes[-4] if len(closes) >= 4 else 0
    prev_move   = closes[-4] - closes[-7] if len(closes) >= 7 else 0
    if recent_move > 0.5 and recent_move > prev_move:
        momentum = "📈 صاعد متسارع"
    elif recent_move > 0.2:
        momentum = "📈 صاعد"
    elif recent_move < -0.5 and recent_move < prev_move:
        momentum = "📉 هابط متسارع"
    elif recent_move < -0.2:
        momentum = "📉 هابط"
    else:
        momentum = "↔️ تذبذب"

    rh = max(highs[-6:])
    rl = min(lows[-6:])
    price = closes[-1]
    wp = (price - rl) / (rh - rl) * 100 if (rh - rl) > 0 else 50
    wave = (f"🔝 قرب القمة (${rh:.2f})" if wp > 80 else
            f"⬇️ قرب القاع (${rl:.2f})"  if wp < 20 else
            "🌊 منتصف الموجة")
    return {"wave": wave, "momentum": momentum, "atr_now": round(atr_now, 2),
            "atr_avg": round(atr_avg, 2), "atr_signal": atr_signal,
            "wave_pos": round(wp), "recent_high": round(rh, 2), "recent_low": round(rl, 2)}


def _decision_brief(rsi, macd_h, wave_pos, price, vwap, trend_5m, trend_15m):
    cs, ps, rc, rp = 0, 0, [], []
    if rsi < 35:   cs += 2; rc.append(f"RSI={rsi} تشبع بيع")
    elif rsi < 45: cs += 1; rc.append(f"RSI={rsi} منطقة شراء")
    elif rsi > 65: ps += 2; rp.append(f"RSI={rsi} تشبع شراء")
    elif rsi > 55: ps += 1; rp.append(f"RSI={rsi} منطقة بيع")
    if macd_h > 0.05:  cs += 1; rc.append("MACD+")
    elif macd_h < -0.05: ps += 1; rp.append("MACD-")
    if wave_pos < 25:  cs += 2; rc.append("قاع الموجة")
    elif wave_pos > 75: ps += 2; rp.append("قمة الموجة")
    if vwap > 0:
        if price > vwap * 1.001:   cs += 1; rc.append("فوق VWAP")
        elif price < vwap * 0.999: ps += 1; rp.append("تحت VWAP")
    t5 = str(trend_5m or "")
    t15 = str(trend_15m or "")
    if t5 == "BULL":  cs += 1; rc.append("Trend5m↑")
    elif t5 == "BEAR": ps += 1; rp.append("Trend5m↓")
    if t15 == "BULL":  cs += 1; rc.append("Trend15m↑")
    elif t15 == "BEAR": ps += 1; rp.append("Trend15m↓")

    total = cs + ps
    if total == 0:
        return "⏸ انتظر", 0, []
    cp = round(cs / total * 100)
    pp = round(ps / total * 100)
    if cs >= 4 and cs > ps + 1:
        return f"✅ CALL ({cp}%)", cp, rc
    elif ps >= 4 and ps > cs + 1:
        return f"✅ PUT ({pp}%)", pp, rp
    elif cs > ps:
        return f"⚠️ CALL ضعيف ({cp}%)", cp, rc
    elif ps > cs:
        return f"⚠️ PUT ضعيف ({pp}%)", pp, rp
    return "⏸ متعادل — انتظر", 50, []


def generate_market_briefing():
    """
    رسالة تلقرام شاملة كل 15 دقيقة:
    - السعر والمؤشرات (RSI, MACD, ATR, BB)
    - تحليل الموجة والزخم
    - أسعار الانعكاس (دعم/مقاومة)
    - قرار CALL/PUT مع نسبة الثقة
    - تنبيهات وتحذيرات
    """
    try:
        now_et = _et_now()
        time_str = now_et.strftime("%H:%M ET")

        snap = get_tsla_snapshot()
        price = snap.get("price", 0) if snap else _state.get("current_price", 0)
        if price == 0:
            return None

        bars_5m  = get_tsla_bars("5Min",  limit=80)
        bars_1m  = get_tsla_bars("1Min",  limit=120)
        bars_15m = get_tsla_bars("15Min", limit=40)

        if not bars_5m or len(bars_5m) < 5:
            return None

        closes_5m  = [float(b["c"]) for b in bars_5m]
        closes_1m  = [float(b["c"]) for b in bars_1m]  if bars_1m  else closes_5m
        closes_15m = [float(b["c"]) for b in bars_15m] if bars_15m else closes_5m

        # ── المؤشرات ──
        # RSI: استخدام period أقصر إذا البيانات غير كافية
        rsi_5m  = _calc_rsi_brief(closes_5m, min(14, len(closes_5m) - 2))
        rsi_1m  = _calc_rsi_brief(closes_1m, min(14, len(closes_1m) - 2))
        # MACD: استخدام 1Min (120 بار) لضمان بيانات كافية دائماً
        # إذا 5Min كافية (>35) نستخدمها، وإلا نستخدم 1Min
        if len(closes_5m) >= 35:
            macd_l, macd_s, macd_h = _calc_macd_brief(closes_5m)
        else:
            macd_l, macd_s, macd_h = _calc_macd_brief(closes_1m)
        atr_5m  = _calc_atr_brief(bars_5m, min(14, len(bars_5m) - 1))
        # BB: استخدام period ديناميكي (min 10) لتجنب القيم المتطابقة
        bb_period = min(20, max(10, len(closes_5m) - 1))
        bb_upper, bb_mid, bb_lower = _calc_bb_brief(closes_5m, bb_period)

        # ── State + Fallback مباشر من Alpaca ──
        # VWAP — من snapshot أو حساب مباشر
        snap2 = get_tsla_snapshot()
        vwap = _state.get("vwap", 0)
        if vwap == 0 and snap2 and snap2.get("vwap", 0) > 0:
            vwap = snap2["vwap"]
        if vwap == 0 and bars_5m:
            total_pv = sum((float(b['h'])+float(b['l'])+float(b['c']))/3 * float(b['v']) for b in bars_5m)
            total_v  = sum(float(b['v']) for b in bars_5m)
            vwap = round(total_pv / total_v, 2) if total_v > 0 else 0
        # Day High/Low — من snapshot أو من bars
        day_high = _state.get("day_high", 0)
        day_low  = _state.get("day_low", 0)
        if day_high == 0 and snap2 and snap2.get("high", 0) > 0:
            day_high = snap2["high"]
            day_low  = snap2.get("low", 0)
        if day_high == 0 and bars_5m:
            day_high = max(float(b['h']) for b in bars_5m)
            day_low  = min(float(b['l']) for b in bars_5m)
        # PDH/PDL
        pdh = _state.get("pdh", 0)
        pdl = _state.get("pdl", 0)
        if pdh == 0:
            try:
                prev = get_previous_day_bars()
                if prev:
                    pdh = float(prev.get('h', 0))
                    pdl = float(prev.get('l', 0))
            except:
                pass
        orh = _state.get("opening_range_high", 0)
        orl = _state.get("opening_range_low", 0)
        # Trend — من _state أو EMA9/EMA21 مباشر
        trend_5m  = _state.get("trend_5m") or _state.get("trend")
        trend_15m = _state.get("trend_15m") or trend_5m
        def _ema_q(data, n):
            k = 2/(n+1); e = data[0]
            for x in data[1:]: e = e*(1-k) + x*k
            return e
        if not trend_5m and len(closes_5m) >= 21:
            ema9  = _ema_q(closes_5m[-21:], 9)
            ema21 = _ema_q(closes_5m[-21:], 21)
            trend_5m = "BULL" if ema9 > ema21 else "BEAR"
        if not trend_15m and len(closes_15m) >= 21:
            ema9_15  = _ema_q(closes_15m[-21:], 9)
            ema21_15 = _ema_q(closes_15m[-21:], 21)
            trend_15m = "BULL" if ema9_15 > ema21_15 else "BEAR"
        if not trend_15m:
            trend_15m = trend_5m

        # ── SPY ──
        spy_dir, spy_chg = get_spy_direction()
        spy_emoji = {"BULL": "📈", "BEAR": "📉", "FLAT": "↔️"}.get(spy_dir, "❓")

        # ── تحليل الموجة ──
        wave = _wave_brief(bars_5m)

        # ── مناطق الانعكاس ──
        resistances, supports = _reversal_zones_brief(bars_5m, price, vwap, pdh, pdl, orh, orl)

        # ── القرار ──
        decision, confidence, reasons = _decision_brief(
            rsi_5m, macd_h, wave["wave_pos"], price, vwap, trend_5m, trend_15m
        )

        # ── تغيير السعر خلال 15 دقيقة ──
        price_15m_ago = closes_5m[-4] if len(closes_5m) >= 4 else closes_5m[0]
        price_change  = price - price_15m_ago
        change_emoji  = "🔺" if price_change > 0 else "🔻"

        # ── تنبيهات ──
        alerts = []
        if rsi_5m > 75:
            alerts.append("⚠️ RSI تشبع شراء — خطر انعكاس")
        if rsi_5m < 25:
            alerts.append("⚠️ RSI تشبع بيع — فرصة ارتداد")
        if vwap > 0 and abs(price - vwap) / price < 0.002:
            alerts.append(f"⚠️ قرب VWAP ${vwap:.2f} — منطقة خطر")
        if pdh > 0 and abs(price - pdh) / price < 0.003:
            alerts.append(f"⚠️ قرب PDH ${pdh:.2f} — مقاومة قوية")
        if pdl > 0 and abs(price - pdl) / price < 0.003:
            alerts.append(f"⚠️ قرب PDL ${pdl:.2f} — دعم قوي")
        if price >= bb_upper * 0.998:
            alerts.append(f"🔴 عند BB Upper ${bb_upper:.2f} — انعكاس محتمل")
        if price <= bb_lower * 1.002:
            alerts.append(f"🟢 عند BB Lower ${bb_lower:.2f} — ارتداد محتمل")
        if macd_h > 0 and macd_h < 0.02:
            alerts.append("⚡ MACD يقترب من الانعكاس السلبي")
        if macd_h < 0 and macd_h > -0.02:
            alerts.append("⚡ MACD يقترب من الانعكاس الإيجابي")
        if now_et.hour == 10:
            alerts.append("🚫 ساعة Chop (10 ET) — تجنب الدخول")
        if now_et.hour >= 13 and now_et.minute >= 30:
            alerts.append("⏳ بعد 1:30 PM ET — حجم منخفض")

        # ── بناء الرسالة ──
        trend_e = {"BULL": "📈", "BEAR": "📉"}.get(str(trend_5m), "↔️")
        vwap_side = "فوق ✅" if price > vwap else "تحت ⚠️"

        msg  = f"🦟 <b>ثاقب — Market Briefing</b>\n"
        msg += f"🕐 {time_str}\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        msg += f"<b>💰 TSLA: ${price:.2f}</b>  {change_emoji}{abs(price_change):.2f}$ (15د)\n"
        msg += f"📊 VWAP: ${vwap:.2f} — {vwap_side}\n"
        msg += f"📅 High: ${day_high:.2f} | Low: ${day_low:.2f}\n"
        msg += f"{spy_emoji} SPY: {spy_dir} ({spy_chg:+.2f}%)\n\n"

        msg += f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"<b>📡 المؤشرات</b>\n"
        msg += f"RSI 5m: <b>{rsi_5m}</b>  |  RSI 1m: {rsi_1m}\n"
        msg += f"MACD: {macd_l:+.3f} | Sig: {macd_s:+.3f} | Hist: <b>{macd_h:+.3f}</b>\n"
        msg += f"ATR 5m: ${atr_5m:.2f}  {wave['atr_signal']}\n"
        msg += f"BB: 🔴{bb_upper:.2f} ⚪{bb_mid:.2f} 🟢{bb_lower:.2f}\n\n"

        msg += f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"<b>🌊 الموجة والزخم</b>\n"
        msg += f"{wave['wave']}\n"
        msg += f"{wave['momentum']}\n"
        msg += f"موقع الموجة: {wave['wave_pos']}%\n"
        msg += f"Trend: {trend_e} 5m={trend_5m or '?'} | 15m={trend_15m or '?'}\n\n"

        msg += f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"<b>🎯 أسعار الانعكاس</b>\n"
        if resistances:
            msg += "🔴 <b>مقاومة:</b>\n"
            for r in resistances:
                dist = r['price'] - price
                msg += f"  {r['icon']} ${r['price']:.2f} ({r['name']}) +${dist:.2f}\n"
        if supports:
            msg += "🟢 <b>دعم:</b>\n"
            for s in supports:
                dist = price - s['price']
                msg += f"  {s['icon']} ${s['price']:.2f} ({s['name']}) -${dist:.2f}\n"

        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"<b>🎲 القرار</b>\n"
        msg += f"<b>{decision}</b>\n"
        if reasons:
            msg += "📌 " + " | ".join(reasons[:4]) + "\n"

        if alerts:
            msg += f"\n<b>⚡ تنبيهات</b>\n"
            for a in alerts[:4]:
                msg += f"{a}\n"

        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━"
        return msg

    except Exception as e:
        logger.error(f"[Briefing] Error: {e}")
        return None
