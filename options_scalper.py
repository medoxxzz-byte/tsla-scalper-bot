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
ITM_TP_DOLLARS     = 0.20   # +$0.20 جني أرباح (Market Order)
ITM_SL_DOLLARS     = 0.65   # -$0.65 بيع تلقائي (Market Order)
ITM_ALERT1_DOLLARS = 0.30   # تنبيه 1 عند -$0.30
ITM_ALERT2_DOLLARS = 0.50   # تنبيه 2 عند -$0.50
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
    "position": None,      # الصفقة المفتوحة
    "alert1_sent": False,  # تنبيه -$0.30
    "alert2_sent": False,  # تنبيه -$0.50
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
        "entry_tsla": price
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


def _manual_monitor_loop():
    """
    حلقة مراقبة صفقة الخط 2 اليدوي.
    تتحقق كل 10 ثواني من:
    - TP: +$0.20 → بيع تلقائي
    - SL: -$0.65 → بيع تلقائي
    - تنبيه 1: -$0.30
    - تنبيه 2: -$0.50
    """
    global _manual_state
    
    logger.info("[V9 Manual] Monitor started")
    
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
            pos["pnl"] = round(pnl_dollar * 100, 2)
            _manual_state["last_price"] = current_price
            _manual_state["pnl_dollar"] = pnl_dollar

            # V11: SSE — بث فوري لجميع العملاء المتصلين
            try:
                _sse_broadcast({
                    "type":          "pnl",
                    "current_price": current_price,
                    "entry_price":   pos["entry_price"],
                    "pnl_dollar":    pnl_dollar,
                    "pnl_pct":       round(pnl_dollar / pos["entry_price"] * 100, 2),
                    "tp_price":      pos["tp_price"],
                    "sl_price":      pos["sl_price"],
                    "symbol":        pos["symbol"],
                    "direction":     pos.get("type", "").upper(),
                })
            except Exception:
                pass
            
            # ── TP تلقائي: +$0.20 ──
            if current_price >= pos["tp_price"]:
                logger.info(f"[V9 Manual] TP hit! ${current_price:.2f} >= ${pos['tp_price']:.2f}")
                close_manual_itm(reason="TP")
                _sse_broadcast({"type": "closed", "reason": "TP", "pnl_dollar": pnl_dollar})
                break
            
            # ── SL تلقائي: -$0.65 ──
            if current_price <= pos["sl_price"]:
                logger.info(f"[V9 Manual] SL hit! ${current_price:.2f} <= ${pos['sl_price']:.2f}")
                close_manual_itm(reason="SL")
                _sse_broadcast({"type": "closed", "reason": "SL", "pnl_dollar": pnl_dollar})
                break
            
            # ── تنبيه 1: -$0.30 ──
            if not _manual_state["alert1_sent"] and current_price <= pos["alert1_price"]:
                _manual_state["alert1_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 1: -$0.30 | ${current_price:.2f}")
                direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
                msg = (
                    f"⚠️ <b>V9 تنبيه 1 — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر الحالي: ${current_price:.2f}\n"
                    f"📥 دخول: ${pos['entry_price']:.2f}\n"
                    f"📉 خسارة: <b>-$0.30</b> لكل عقد (-$30)\n"
                    f"🛑 SL عند: ${pos['sl_price']:.2f} (-$0.65)\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            # ── تنبيه 2: -$0.50 ──
            if not _manual_state["alert2_sent"] and current_price <= pos["alert2_price"]:
                _manual_state["alert2_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 2: -$0.50 | ${current_price:.2f}")
                direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
                msg = (
                    f"🚨 <b>V9 تحذير — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر الحالي: ${current_price:.2f}\n"
                    f"📥 دخول: ${pos['entry_price']:.2f}\n"
                    f"📉 خسارة: <b>-$0.50</b> لكل عقد (-$50)\n"
                    f"🛑 SL تلقائي عند: ${pos['sl_price']:.2f}\n"
                    f"⏰ <b>قرار مطلوب الآن!</b>\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            time.sleep(5)  # V10: تقليل من 10 إلى 5 ثوانٍ لتحسين استجابة P&L
            
        except Exception as e:
            logger.error(f"[V9 Manual Monitor] Error: {e}")
            time.sleep(5)
    
    logger.info("[V9 Manual] Monitor stopped")


# ══════════════════════════════════════════════════════════════════════════════
# V11 — SSE (Server-Sent Events) broadcaster
# كل عميل متصل يحصل على queue خاصة به — الخادم يبث التحديثات فور حدوثها
# ══════════════════════════════════════════════════════════════════════════════
import queue as _queue_module

_sse_clients: list = []          # قائمة queues للعملاء المتصلين
_sse_lock = threading.Lock()


def sse_subscribe() -> _queue_module.Queue:
    """تسجيل عميل جديد — يُرجع queue يستقبل منها التحديثات."""
    q: _queue_module.Queue = _queue_module.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)
    return q


def sse_unsubscribe(q: _queue_module.Queue) -> None:
    """إلغاء تسجيل عميل عند قطع الاتصال."""
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def _sse_broadcast(data: dict) -> None:
    """إرسال بيانات لجميع العملاء المتصلين."""
    import json
    msg = "data: " + json.dumps(data) + "\n\n"
    dead = []
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(msg)
        except _queue_module.Full:
            dead.append(q)
    for q in dead:
        sse_unsubscribe(q)


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
    
    # ── إرجاع GEX levels وCheddarFlow دائماً (حتى أثناء الصفقة) ──
    gex = _pe_state.get("gex_levels", {})
    cheddar_pct = _pe_state.get("cheddar_call_pct")

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
        "last_pnl": _manual_state.get("last_pnl", 0),
        "gex_levels": gex,
        "cheddar_call_pct": cheddar_pct
    }


# ═══════════════════════════════════════════════════════════════
# V9.3 — TRADE JOURNAL (سجل الصفقات)
# ═══════════════════════════════════════════════════════════════

import json
import os

# Render يحذف الملفات عند Deploy — نستخدم /tmp للاستمرارية خلال الجلسة
# أو مجلد static/journal_images للصور (تُحفظ في الكود)
_JOURNAL_FILE = os.environ.get("JOURNAL_FILE", "/tmp/trade_journal.json")
_IMAGES_DIR   = os.path.join(os.path.dirname(__file__), "static", "journal_images")

# تأكد من وجود المجلد
os.makedirs(_IMAGES_DIR, exist_ok=True)


def _load_journal() -> list:
    """تحميل سجل الصفقات من الملف."""
    if os.path.exists(_JOURNAL_FILE):
        try:
            with open(_JOURNAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []


def _save_journal(entries: list):
    """حفظ سجل الصفقات في الملف."""
    try:
        with open(_JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[Journal] Save error: {e}")


def add_journal_entry(entry: dict):
    """إضافة صفقة جديدة للسجل."""
    entries = _load_journal()
    # ID تسلسلي
    entry["id"] = (entries[-1]["id"] + 1) if entries else 1
    entry["created_at"] = _et_now().strftime("%Y-%m-%d %H:%M:%S ET")
    entries.insert(0, entry)  # أحدث أولاً
    _save_journal(entries)
    logger.info(f"[Journal] Entry #{entry['id']} saved")
    return entry["id"]


def update_journal_entry(entry_id: int, updates: dict):
    """تحديث صفقة موجودة (مثلاً إضافة نتيجة الخروج)."""
    entries = _load_journal()
    for e in entries:
        if e.get("id") == entry_id:
            e.update(updates)
            break
    _save_journal(entries)


def get_journal_entries(limit: int = 50) -> list:
    """إرجاع آخر N صفقة."""
    return _load_journal()[:limit]


def save_journal_image(entry_id: int, image_data: bytes, ext: str = "jpg") -> str:
    """حفظ صورة مرتبطة بصفقة وإرجاع اسم الملف."""
    filename = f"trade_{entry_id}_{int(time.time())}.{ext}"
    filepath = os.path.join(_IMAGES_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(image_data)
    return filename


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
