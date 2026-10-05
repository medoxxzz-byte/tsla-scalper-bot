"""Dashboard-only extended intraday Paper trade engine.

The engine is deliberately separate from /manual and every historical automatic
strategy. It may submit at most one order sequence per New York trading day and
only when the configured Alpaca endpoint is the Paper endpoint.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple

from extended_trade_store import DECISION_VERSION, ExtendedPaperTradeStore
from options_scalper import (
    ALPACA_BASE_URL,
    _ema,
    _et_now,
    _headers,
    _rw_macd_hist,
    _rw_rsi,
    get_account,
    get_option_quote,
    get_options_chain,
    get_tsla_bars,
    get_tsla_snapshot,
    place_option_order,
    send_telegram,
)

LOGGER = logging.getLogger(__name__)

# The dedicated Paper-only engine is an approved research feature.  A deployment
# can still disable it without a code change, but the default preserves the user
# approved Paper experiment after a normal Render restart.
EXTENDED_PAPER_TRADE_ENABLED = os.environ.get("EXTENDED_PAPER_TRADE_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
CHECK_INTERVAL_SECONDS = 30
ENTRY_START_MINUTES = 9 * 60 + 50
ENTRY_END_MINUTES = 14 * 60 + 30
FORCE_EXIT_MINUTES = 15 * 60 + 50
MIN_VOLUME_RATIO = 0.80
MAX_VWAP_DISTANCE_PCT = 0.003
NEAR_LEVEL_BLOCK_DOLLARS = 0.30
INVALIDATION_BUFFER = 0.12

_engine_lock = threading.Lock()
_engine_thread: Optional[threading.Thread] = None
_engine_state: Dict[str, Any] = {
    "running": False,
    "last_check_at": None,
    "last_decision": "NO_TRADE",
    "last_reason": "لم يبدأ الفحص بعد",
    "last_context": {},
    "last_error": None,
}
_store = ExtendedPaperTradeStore()


def _minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _paper_endpoint_ok() -> bool:
    return "paper-api.alpaca.markets" in str(ALPACA_BASE_URL).lower()


def _client_order_id(trade_date) -> str:
    return f"tm-extended-{trade_date.strftime('%Y%m%d')}"


def _latest_closed_bars(timeframe: str, count: int) -> list[Dict[str, Any]]:
    bars = get_tsla_bars(timeframe, count + 2)
    # Alpaca normally returns completed bars, but removing the newest bar makes
    # the rule independent of whether a partial live candle is present.
    return list(bars[:-1]) if len(bars) > count else list(bars)


def _trend_from_closed_bars(bars: Iterable[Dict[str, Any]]) -> Tuple[Optional[str], int, Dict[str, float]]:
    bars = list(bars)
    if len(bars) < 28:
        return None, 0, {}
    closes = [float(bar["c"]) for bar in bars]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi = _rw_rsi(closes, 14)
    macd_now, macd_before = _rw_macd_hist(closes)
    last5 = closes[-5:]
    rising = sum(1 for index in range(1, len(last5)) if last5[index] > last5[index - 1])
    falling = sum(1 for index in range(1, len(last5)) if last5[index] < last5[index - 1])
    latest = closes[-1]

    bull = 0
    bear = 0
    if ema9 > ema21:
        bull += 30
    elif ema9 < ema21:
        bear += 30
    if latest > ema21:
        bull += 20
    elif latest < ema21:
        bear += 20
    if rising >= 3:
        bull += 20
    if falling >= 3:
        bear += 20
    if rsi is not None and rsi >= 55:
        bull += 15
    if rsi is not None and rsi <= 45:
        bear += 15
    if macd_now is not None and macd_now > 0 and (macd_before is None or macd_now >= macd_before):
        bull += 15
    if macd_now is not None and macd_now < 0 and (macd_before is None or macd_now <= macd_before):
        bear += 15

    meta = {
        "close": round(latest, 4), "ema9": round(ema9, 4), "ema21": round(ema21, 4),
        "rsi": round(rsi, 2) if rsi is not None else None,
        "macd_hist": round(macd_now, 6) if macd_now is not None else None,
        "rising_bars": rising, "falling_bars": falling, "bull_score": bull, "bear_score": bear,
    }
    if bull >= 70 and bull >= bear + 20:
        return "BULL", bull, meta
    if bear >= 70 and bear >= bull + 20:
        return "BEAR", bear, meta
    return None, max(bull, bear), meta


def _near_levels(price: float, bars: Iterable[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    recent = list(bars)[-12:]
    highs = [float(bar["h"]) for bar in recent]
    lows = [float(bar["l"]) for bar in recent]
    resistance_candidates = [value for value in highs if value > price]
    support_candidates = [value for value in lows if value < price]
    resistance = min(resistance_candidates) if resistance_candidates else None
    support = max(support_candidates) if support_candidates else None
    return support, resistance


def evaluate_extended_decision(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Produce a decision only; this function never submits an Alpaca order."""
    now = now or _et_now()
    base = {
        "decision_version": DECISION_VERSION,
        "checked_at": now.isoformat(),
        "decision": "NO_TRADE",
        "reason": "",
        "reasons": [],
    }
    if now.weekday() >= 5:
        return {**base, "reason": "خارج أيام السوق الأمريكي."}
    if not (ENTRY_START_MINUTES <= _minutes(now) < ENTRY_END_MINUTES):
        return {**base, "reason": "خارج نافذة قرار الصفقة الممتدة (09:50–14:30 ET)."}
    if not _paper_endpoint_ok():
        return {**base, "reason": "حماية: التنفيذ الممتد لا يقبل إلا Alpaca Paper."}
    if not _store.configured:
        return {**base, "reason": "دفتر Neon للصفقة الممتدة غير جاهز بعد."}

    snapshot = get_tsla_snapshot()
    if not snapshot or float(snapshot.get("price") or 0) <= 0 or float(snapshot.get("vwap") or 0) <= 0:
        return {**base, "reason": "تعذر جلب سعر TSLA أو VWAP بصورة صالحة."}
    price = float(snapshot["price"])
    vwap = float(snapshot["vwap"])
    bars5 = _latest_closed_bars("5Min", 45)
    bars15 = _latest_closed_bars("15Min", 32)
    direction15, score15, meta15 = _trend_from_closed_bars(bars15)
    direction5, score5, meta5 = _trend_from_closed_bars(bars5)
    if not direction15 or direction15 != direction5:
        return {**base, "reason": "لا اتفاق اتجاه واضح بين 15د و5د.", "context": {"trend_15m": direction15 or "CHOP", "trend_5m": direction5 or "CHOP", "score_15m": score15, "score_5m": score5}}

    volumes = [float(bar.get("v") or 0) for bar in bars5]
    if len(volumes) < 21:
        return {**base, "reason": "بيانات حجم 5د غير كافية."}
    volume_ratio = volumes[-1] / (sum(volumes[-21:-1]) / 20) if sum(volumes[-21:-1]) > 0 else 0
    if volume_ratio < MIN_VOLUME_RATIO:
        return {**base, "reason": f"سيولة 5د ضعيفة ({volume_ratio:.2f}× أقل من {MIN_VOLUME_RATIO:.2f}×).", "context": {"volume_ratio": round(volume_ratio, 3)}}

    vwap_distance = abs(price - vwap) / vwap
    is_bull = direction5 == "BULL"
    on_correct_side = price > vwap if is_bull else price < vwap
    if not on_correct_side:
        return {**base, "reason": "السعر ليس في الجانب الصحيح من VWAP."}
    if vwap_distance > MAX_VWAP_DISTANCE_PCT:
        return {**base, "reason": f"لا مطاردة: السعر بعيد عن VWAP ({vwap_distance * 100:.2f}%).", "context": {"vwap_distance_pct": round(vwap_distance * 100, 3)}}

    support, resistance = _near_levels(price, bars5)
    if is_bull and resistance is not None and resistance - price <= NEAR_LEVEL_BLOCK_DOLLARS:
        return {**base, "reason": f"مقاومة 5د قريبة جداً أمام CALL عند ${resistance:.2f}."}
    if not is_bull and support is not None and price - support <= NEAR_LEVEL_BLOCK_DOLLARS:
        return {**base, "reason": f"دعم 5د قريب جداً أمام PUT عند ${support:.2f}."}

    direction = "CALL" if is_bull else "PUT"
    invalidation = min(vwap, support) - INVALIDATION_BUFFER if is_bull and support else vwap - INVALIDATION_BUFFER
    invalidation = max(vwap, resistance) + INVALIDATION_BUFFER if not is_bull and resistance else (invalidation if is_bull else vwap + INVALIDATION_BUFFER)
    sign = 1 if is_bull else -1
    targets = {"target_030_price": price + sign * 0.30, "target_060_price": price + sign * 0.60, "target_120_price": price + sign * 1.20}
    reasons = [
        f"اتفاق 15د و5د: {'صاعد' if is_bull else 'هابط'}",
        f"السعر {'فوق' if is_bull else 'تحت'} VWAP (${vwap:.2f})",
        f"سيولة 5د {volume_ratio:.2f}×",
        f"عودة منضبطة قرب VWAP ({vwap_distance * 100:.2f}%)",
    ]
    return {
        **base,
        "decision": direction,
        "reason": " | ".join(reasons),
        "reasons": reasons,
        "tsla_price": round(price, 4),
        "vwap": round(vwap, 4),
        "invalidation_price": round(invalidation, 4),
        **{key: round(value, 4) for key, value in targets.items()},
        "context": {
            "trend_15m": direction15, "trend_5m": direction5, "score_15m": score15, "score_5m": score5,
            "trend_15m_meta": meta15, "trend_5m_meta": meta5, "volume_ratio_5m": round(volume_ratio, 4),
            "vwap_distance_pct": round(vwap_distance * 100, 4), "nearest_support_5m": support,
            "nearest_resistance_5m": resistance,
        },
    }


def _next_trading_expiry(now: datetime) -> str:
    candidate = now.date() + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def find_extended_itm_contract(price: float, direction: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Select a one-day-or-later ITM contract; it still exits during this session."""
    now = now or _et_now()
    option_type = "call" if direction == "CALL" else "put"
    if option_type == "call":
        strike_min, strike_max = round(price - 25, 0), round(price - 2, 0)
    else:
        strike_min, strike_max = round(price + 2, 0), round(price + 25, 0)
    expiry = _next_trading_expiry(now)
    contracts = get_options_chain(expiry, option_type, strike_min, strike_max)
    best: Optional[Dict[str, Any]] = None
    best_score = -10.0
    for contract in contracts:
        strike = float(contract.get("strike_price") or 0)
        itm_amount = price - strike if option_type == "call" else strike - price
        if itm_amount <= 0:
            continue
        approx_delta = min(0.97, 0.50 + itm_amount * 0.045)
        if not 0.68 <= approx_delta <= 0.90:
            continue
        quote = get_option_quote(contract.get("symbol", ""))
        if not quote or float(quote.get("mid") or 0) <= 0:
            continue
        mid = float(quote["mid"])
        spread = (float(quote["ask"]) - float(quote["bid"])) / mid if mid else 1
        score = (1 - abs(approx_delta - 0.78) * 5) + max(0, 1 - spread * 3)
        if score > best_score:
            best_score = score
            best = {
                "symbol": contract["symbol"], "strike": strike, "type": option_type, "expiry": expiry,
                "bid": float(quote["bid"]), "ask": float(quote["ask"]), "mid": mid,
                "approx_delta": round(approx_delta, 2), "itm_amount": round(itm_amount, 2),
            }
    return best


def _entry_message(trade: Dict[str, Any]) -> str:
    direction = trade["direction"]
    icon = "🟢" if direction == "CALL" else "🔴"
    return (
        f"🧭 <b>صفقة ممتدة ورقية — {direction} {icon}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"💵 TSLA عند القرار: ${float(trade['tsla_entry_price']):.2f}\n"
        f"📋 العقد: {trade['option_symbol']} ×1 (1DTE أو أقرب)\n"
        f"📥 Mid تقريبي: ${float(trade['option_entry_mid']):.2f}\n"
        f"⚖️ VWAP: ${float(trade['vwap_at_decision']):.2f}\n"
        f"🛑 الإلغاء: إغلاق 5د خلف ${float(trade['invalidation_price']):.2f}\n"
        f"🎯 قياس TSLA: ${float(trade['target_030_price']):.2f} ثم ${float(trade['target_060_price']):.2f} ثم ${float(trade['target_120_price']):.2f}\n"
        "🔬 فُتحت تلقائياً على Alpaca Paper فقط؛ لا يلزم أي إجراء منك."
    )


def _milestone_message(trade: Dict[str, Any], amount: str, tsla_price: float) -> str:
    return (
        f"📍 <b>متابعة الصفقة الممتدة — تحقق +${amount}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{trade['direction']} ورقي | TSLA: ${tsla_price:.2f}\n"
        f"أقصى امتداد مواتٍ: +${float(trade['max_favorable_extension']):.2f}\n"
        "تم القياس والحفظ؛ الصفقة مستمرة حتى الهدف أو الإلغاء."
    )


def _close_message(trade: Dict[str, Any]) -> str:
    pnl = float(trade.get("option_pnl_dollars") or 0)
    icon = "✅" if pnl >= 0 else "🔴"
    return (
        f"{icon} <b>نتيجة الصفقة الممتدة الورقية</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"القرار: {trade['direction']} | السبب: {trade.get('exit_reason')}\n"
        f"TSLA: ${float(trade['tsla_entry_price']):.2f} → ${float(trade['tsla_exit_price']):.2f}\n"
        f"📈 أقصى امتداد مواتٍ: +${float(trade['max_favorable_extension']):.2f}\n"
        f"📉 أقصى عكس: -${float(trade['max_adverse_extension']):.2f}\n"
        f"💰 P&L الورقي للعقد: ${pnl:+.2f}\n"
        "حُفظت النتيجة في سجل الصفقة الممتدة المستقل."
    )


def _attempt_entry(decision: Dict[str, Any], now: datetime) -> None:
    trade_date = now.date()
    payload = {
        "trade_date": trade_date,
        "trade_key": f"{DECISION_VERSION}:{trade_date.strftime('%Y%m%d')}",
        "direction": decision["decision"], "decision_at": now, "tsla_price": decision["tsla_price"],
        "vwap": decision["vwap"], "invalidation_price": decision["invalidation_price"],
        "target_030_price": decision["target_030_price"], "target_060_price": decision["target_060_price"],
        "target_120_price": decision["target_120_price"], "decision_context": {"reasons": decision["reasons"], **decision["context"]},
    }
    trade, reserved = _store.reserve_decision(payload)
    if not trade:
        _engine_state["last_error"] = "تعذر حجز قرار Neon؛ لم يرسل أمر Alpaca."
        return
    if not reserved:
        _engine_state["last_reason"] = "صفقة ممتدة موجودة مسبقاً لهذا اليوم؛ لا تكرار."
        return
    account = get_account()
    if not account or account.get("trading_blocked") or account.get("account_blocked") or float(account.get("options_buying_power") or 0) <= 0:
        _store.mark_entry_failed(int(trade["id"]), now, decision["tsla_price"], {"reason": "paper_account_not_ready"})
        send_telegram("⚠️ <b>الصفقة الممتدة الورقية لم تُفتح</b>\nحساب Alpaca Paper غير جاهز أو لا توجد قدرة شراء أوبشن.")
        return
    contract = find_extended_itm_contract(decision["tsla_price"], decision["decision"], now)
    if not contract:
        _store.mark_entry_failed(int(trade["id"]), now, decision["tsla_price"], {"reason": "no_suitable_1dte_itm_contract"})
        send_telegram("⚠️ <b>الصفقة الممتدة الورقية لم تُفتح</b>\nلا يوجد عقد ITM (1DTE أو أقرب) صالح للتجربة.")
        return
    order = place_option_order(
        symbol=contract["symbol"], qty=1, side="buy", order_type="market", position_intent="buy_to_open",
        client_order_id=_client_order_id(trade_date),
    )
    if not order:
        _store.mark_entry_failed(int(trade["id"]), now, decision["tsla_price"], {"reason": "alpaca_entry_order_failed", "symbol": contract["symbol"]})
        send_telegram("⚠️ <b>الصفقة الممتدة الورقية لم تُفتح</b>\nرفض Alpaca Paper أمر الدخول؛ حُفظ الفشل ولا يوجد تكرار اليوم.")
        return
    opened = _store.mark_entry_opened(int(trade["id"]), {
        "entered_at": now, "option_symbol": contract["symbol"], "option_expiry": contract["expiry"],
        "option_strike": contract["strike"], "option_entry_mid": contract["mid"],
        "alpaca_entry_order_id": order.get("id"), "tsla_entry_price": decision["tsla_price"],
        "event_payload": {"contract": contract, "client_order_id": _client_order_id(trade_date)},
    })
    if opened:
        send_telegram(_entry_message(opened))
    else:
        _engine_state["last_error"] = "تم قبول أمر Paper لكن تعذر تثبيت دخوله في Neon؛ أوقف المحرك للمراجعة."


def _close_open_trade(trade: Dict[str, Any], now: datetime, tsla_price: float, reason: str) -> None:
    symbol = str(trade.get("option_symbol") or "")
    if not symbol:
        _store.record_exit_failure(int(trade["id"]), now, tsla_price, {"reason": "missing_option_symbol"})
        return
    quote = get_option_quote(symbol)
    option_mid = float(quote["mid"]) if quote and float(quote.get("mid") or 0) > 0 else float(trade.get("option_entry_mid") or 0)
    order = place_option_order(symbol=symbol, qty=1, side="sell", order_type="market", position_intent="sell_to_close")
    if not order:
        _store.record_exit_failure(int(trade["id"]), now, tsla_price, {"reason": "alpaca_exit_order_failed", "exit_reason": reason})
        send_telegram("⚠️ <b>تنبيه الصفقة الممتدة الورقية</b>\nتعذر إرسال أمر الخروج؛ سيعيد المحرك المحاولة تلقائياً.")
        return
    entry_mid = float(trade.get("option_entry_mid") or 0)
    closed = _store.close_trade(int(trade["id"]), {
        "closed_at": now, "option_exit_mid": option_mid, "alpaca_exit_order_id": order.get("id"),
        "tsla_exit_price": tsla_price, "exit_reason": reason,
        "option_pnl_dollars": round((option_mid - entry_mid) * 100, 2),
    })
    if closed:
        send_telegram(_close_message(closed))


def _monitor_open_trade(trade: Dict[str, Any], now: datetime) -> None:
    snapshot = get_tsla_snapshot()
    if not snapshot or float(snapshot.get("price") or 0) <= 0:
        return
    price = float(snapshot["price"])
    entry = float(trade.get("tsla_entry_price") or 0)
    is_call = trade.get("direction") == "CALL"
    favorable = (price - entry) if is_call else (entry - price)
    adverse = max(0.0, -favorable)
    symbol = trade.get("option_symbol")
    quote = get_option_quote(symbol) if symbol else None
    option_mid = float(quote["mid"]) if quote and float(quote.get("mid") or 0) > 0 else None
    updated, milestones = _store.record_progress(int(trade["id"]), now, price, option_mid, max(0.0, favorable), adverse)
    if not updated:
        return
    labels = {"target_030_hit": "0.30", "target_060_hit": "0.60"}
    for milestone in milestones:
        if milestone in labels:
            send_telegram(_milestone_message(updated, labels[milestone], price))
    if "target_120_hit" in milestones:
        _close_open_trade(updated, now, price, "target_120_reached")
        return
    if _minutes(now) >= FORCE_EXIT_MINUTES:
        _close_open_trade(updated, now, price, "session_time_exit")
        return
    # Invalidation uses only the last fully completed five-minute close.
    closed_5m = _latest_closed_bars("5Min", 3)
    if closed_5m:
        last_close = float(closed_5m[-1]["c"])
        invalidation = float(updated["invalidation_price"])
        invalidated = last_close < invalidation if is_call else last_close > invalidation
        if invalidated:
            _close_open_trade(updated, now, price, "invalidation_5m_close")


def _engine_loop() -> None:
    LOGGER.info("[ExtendedPaper] Dashboard extended Paper engine started")
    while _engine_state["running"]:
        now = _et_now()
        try:
            _engine_state["last_check_at"] = now.isoformat()
            if not EXTENDED_PAPER_TRADE_ENABLED:
                _engine_state["last_decision"] = "DISABLED"
                _engine_state["last_reason"] = "المحرك معطل بمتغير البيئة."
            elif not _paper_endpoint_ok():
                _engine_state["last_decision"] = "SAFETY_BLOCK"
                _engine_state["last_reason"] = "حماية Paper: endpoint ليس paper-api."
            elif not _store.configured:
                _engine_state["last_decision"] = "NEON_UNAVAILABLE"
                _engine_state["last_reason"] = "دفتر Neon غير متصل؛ لا تنفيذ."
            else:
                open_trade = _store.get_open_trade()
                if open_trade:
                    _engine_state["last_decision"] = open_trade["direction"]
                    _engine_state["last_reason"] = "صفقة ممتدة مفتوحة؛ متابعة تلقائية."
                    _engine_state["last_context"] = {"trade_id": open_trade["id"]}
                    _monitor_open_trade(open_trade, now)
                else:
                    decision = evaluate_extended_decision(now)
                    _engine_state["last_decision"] = decision["decision"]
                    _engine_state["last_reason"] = decision["reason"]
                    _engine_state["last_context"] = decision.get("context", {})
                    if decision["decision"] in {"CALL", "PUT"}:
                        _attempt_entry(decision, now)
            _engine_state["last_error"] = None
        except Exception as exc:  # never allow a failed calculation to produce an order
            LOGGER.exception("[ExtendedPaper] Loop failure: %s", exc)
            _engine_state["last_error"] = "خطأ داخلي؛ لم يُرسل أمر جديد في هذه الدورة."
        time.sleep(CHECK_INTERVAL_SECONDS)
    LOGGER.info("[ExtendedPaper] Dashboard extended Paper engine stopped")


def start_extended_paper_engine() -> Dict[str, Any]:
    global _engine_thread
    if not EXTENDED_PAPER_TRADE_ENABLED:
        return {"ok": False, "status": "disabled"}
    with _engine_lock:
        if _engine_thread and _engine_thread.is_alive():
            return {"ok": True, "status": "already_running"}
        _engine_state["running"] = True
        _engine_thread = threading.Thread(target=_engine_loop, name="ExtendedPaperTrade", daemon=True)
        _engine_thread.start()
    return {"ok": True, "status": "started"}


def stop_extended_paper_engine() -> Dict[str, Any]:
    _engine_state["running"] = False
    return {"ok": True, "status": "stopping"}


def extended_paper_engine_alive() -> bool:
    """Return worker-thread liveness, not merely a copied state flag.

    Gunicorn's preload mode can fork a worker after a parent has set
    ``_engine_state['running']``. Threads do not survive that fork, so the
    state flag alone must never be presented as proof that monitoring runs.
    """
    return bool(_engine_thread and _engine_thread.is_alive())


def get_extended_paper_status() -> Dict[str, Any]:
    latest = _store.latest_trade() if _store.configured else None
    thread_alive = extended_paper_engine_alive()
    status = {
        "ok": True,
        "decision_version": DECISION_VERSION,
        "paper_only": _paper_endpoint_ok(),
        "enabled": EXTENDED_PAPER_TRADE_ENABLED,
        "running": thread_alive,
        "state_running": bool(_engine_state["running"]),
        "thread_alive": thread_alive,
        "last_check_at": _engine_state["last_check_at"],
        "last_decision": _engine_state["last_decision"],
        "last_reason": _engine_state["last_reason"],
        "last_error": _engine_state["last_error"],
        "active_or_latest_trade": latest,
    }
    return status
