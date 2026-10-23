"""Pressure Radar V1 alert formatting and state-change de-duplication.

This module is intentionally isolated from V17/V18. It forwards only meaningful
state transitions from the Pine radar to the existing Telegram sender.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable


_VALID_STATES = {"BULL_PRESSURE", "BEAR_PRESSURE", "WAIT"}
_VALID_ALERT_IDS = {
    "SUPPORT_MONITOR",
    "EARLY_BULLISH_CONFIRMATION",
    "BULLISH_BREAKOUT",
    "SUPPORT_SENSITIVITY",
    "SUPPORT_FAILURE",
    "SUPPORT_RECOVERY",
    "BULLISH_IMPROVEMENT",
}
_state_lock = Lock()
_last_state: str | None = None
_last_event_key: str | None = None
_last_alert_keys: set[str] = set()


def _as_float(value: Any, fallback: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _price(value: Any) -> str:
    number = _as_float(value)
    return f"${number:.2f}" if number is not None else "غير متاح"


def _time_text(data: dict[str, Any]) -> str:
    raw = data.get("observed_at") or data.get("bar_time") or data.get("time")
    if raw in (None, ""):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        millis = float(raw)
        if millis > 10_000_000_000:
            return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return str(raw)


def _state_arabic(state: str) -> str:
    return {
        "BULL_PRESSURE": "ضغط صاعد محتمل",
        "BEAR_PRESSURE": "ضغط هابط محتمل",
        "WAIT": "إلغاء/انتظار",
    }[state]


def _alert_arabic(alert_id: str) -> str:
    return {
        "SUPPORT_MONITOR": "مراقبة الدعم",
        "EARLY_BULLISH_CONFIRMATION": "تأكيد صاعد مبكر",
        "BULLISH_BREAKOUT": "اختراق صاعد",
        "SUPPORT_SENSITIVITY": "حساسية عند الدعم",
        "SUPPORT_FAILURE": "فشل الدعم",
        "SUPPORT_RECOVERY": "استرداد الدعم",
        "BULLISH_IMPROVEMENT": "تحسن صاعد",
    }.get(alert_id, alert_id or "حالة عامة")


def format_pressure_message(data: dict[str, Any]) -> str:
    state = str(data.get("state") or data.get("radar_state", "WAIT")).upper()
    symbol = str(data.get("symbol", "TSLA"))
    alert_id = str(data.get("alert_id", "")).upper()
    price = _price(data.get("price"))
    score = data.get("score", "-")
    bull_score = data.get("bull_score", "-")
    bear_score = data.get("bear_score", "-")
    m5 = data.get("m5", "غير متاح")
    m3 = data.get("m3", "غير متاح")
    m1 = data.get("m1", "غير متاح")
    rv3 = data.get("rv3", "غير متاح")
    level = _price(data.get("watch_level"))
    invalidation = data.get("invalidation", "غير متاح")
    reasons = str(data.get("reasons", "غير متاحة"))

    event_line = f"🧩 الحدث: <code>{_alert_arabic(alert_id)}</code>\n" if alert_id else ""

    if state == "BULL_PRESSURE":
        header = "🟢 <b>Pressure Radar — ضغط صاعد محتمل</b>"
        action = "راقب الدفع يدويًا على فاصل 15 ثانية؛ هذه ليست توصية CALL ولا أمر تداول."
    elif state == "BEAR_PRESSURE":
        header = "🔴 <b>Pressure Radar — ضغط هابط محتمل</b>"
        action = "راقب الدفع الهابط يدويًا على فاصل 15 ثانية؛ هذه ليست توصية PUT ولا أمر تداول."
    else:
        header = "⚪ <b>Pressure Radar — إلغاء/انتظار</b>"
        action = "لا تطارد الحركة؛ انتظر عودة توافق واضح بين الفريمات."

    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━\n"
        f"📌 الرمز: <code>{symbol}</code> | السعر: <code>{price}</code>\n"
        f"🕐 الرصد: <code>{_time_text(data)}</code>\n"
        f"{event_line}"
        f"🧭 الفريمات: 5د={m5} | 3د={m3} | 1د={m1}\n"
        f"📊 الدرجات: Bull={bull_score}/3 | Bear={bear_score}/3 | RV3={rv3}\n"
        f"🧪 القراءة: <code>{reasons}</code>\n"
        f"📍 مستوى المراقبة: <code>{level}</code>\n"
        f"⚠️ الإلغاء: {invalidation}\n"
        "━━━━━━━━━━━━━━\n"
        f"<b>العمل:</b> {action}\n"
        "🧱 تجربة يدوية فقط: لا تنفيذ آلي ولا تعزيز في الخسارة."
    )


def process_pressure_webhook(
    data: dict[str, Any], send_telegram_func: Callable[[str], bool]
) -> dict[str, Any]:
    """Forward one meaningful state transition and mute duplicates."""
    global _last_state, _last_event_key, _last_alert_keys
    state = str(data.get("state") or data.get("radar_state", "")).upper()
    if state not in _VALID_STATES:
        return {"status": "ignored", "reason": "unknown_state", "state": state}

    alert_id = str(data.get("alert_id", "")).upper()
    if alert_id and alert_id not in _VALID_ALERT_IDS:
        return {"status": "ignored", "reason": "unknown_alert_id", "alert_id": alert_id}

    event_key = str(
        data.get("event_key")
        or f"{state}:{data.get('bar_time', data.get('time', ''))}:{data.get('symbol', 'TSLA')}"
    )
    with _state_lock:
        if alert_id:
            alert_key = str(
                data.get("event_key")
                or f"{data.get('symbol', 'TSLA')}:{alert_id}:{data.get('support_id', '')}"
            )
            if alert_key in _last_alert_keys:
                return {"status": "ignored", "reason": "duplicate_alert", "alert_id": alert_id}
            _last_alert_keys.add(alert_key)
        elif state == _last_state or event_key == _last_event_key:
            return {"status": "ignored", "reason": "duplicate_state", "state": state}
        _last_state = state
        _last_event_key = event_key

    message = format_pressure_message(data)
    sent = bool(send_telegram_func(message))
    if not sent:
        return {"status": "failed", "state": state, "telegram": "failed", "message": message}
    return {"status": "processed", "state": state, "telegram": "sent", "message": message}


def reset_state_for_test() -> None:
    global _last_state, _last_event_key, _last_alert_keys
    with _state_lock:
        _last_state = None
        _last_event_key = None
        _last_alert_keys = set()
