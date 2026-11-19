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
_pressure_contexts: dict[str, dict[str, Any]] = {}
_market_observations: dict[str, dict[str, Any]] = {}
_SUPPORT_PHASES = {
    "SUPPORT_MONITOR": "MONITOR",
    "SUPPORT_SENSITIVITY": "SENSITIVITY",
    "SUPPORT_FAILURE": "FAILURE",
    "SUPPORT_RECOVERY": "RECOVERY",
}
_support_cycles: dict[str, dict[str, Any]] = {}
_support_cycle_counters: dict[str, int] = {}
_support_transition_history: dict[str, list[dict[str, Any]]] = {}


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


def _pressure_context_key(data: dict[str, Any]) -> str | None:
    """Return stable context only; alert_id/event_key are event identity."""
    explicit = data.get("pressure_context")
    support_id = data.get("support_id")
    if explicit not in (None, ""):
        return f"explicit:{explicit}"
    if support_id not in (None, ""):
        return f"support:{support_id}"
    return None


def _observation_time(data: dict[str, Any]) -> float | None:
    """Return a comparable event time; missing or malformed time is unknown."""
    raw = data.get("bar_time", data.get("observed_at", data.get("time")))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _observation_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": _observation_time(data),
        "state": str(data.get("state") or data.get("radar_state", "")).upper(),
        "bull_score": _as_float(data.get("bull_score")),
        "bear_score": _as_float(data.get("bear_score")),
        "momentum": _as_float(data.get("momentum")),
        "cmf": _as_float(data.get("cmf")),
        "relative_volume": _as_float(data.get("relative_volume")),
    }


def _previous_observation(symbol: str, current: dict[str, Any]) -> dict[str, Any] | None:
    previous = _market_observations.get(symbol)
    if not previous:
        return None
    current_time = current.get("time")
    previous_time = previous.get("time")
    if current_time is None or previous_time is None or current_time <= previous_time:
        return None
    return previous


def _market_context(
    data: dict[str, Any], previous: dict[str, Any] | None
) -> tuple[str, str]:
    """Describe only current evidence and conservative, valid prior comparisons."""
    state = str(data.get("state") or data.get("radar_state", "")).upper()
    bull = _as_float(data.get("bull_score"))
    bear = _as_float(data.get("bear_score"))
    momentum = _as_float(data.get("momentum"))

    if previous is None:
        if state == "BULL_PRESSURE":
            return "ضغط صاعد قائم — لا توجد مقارنة زمنية كافية للحكم على قوته.", "انتظار تأكيد جديد؛ لا تُفهم الرسالة كأمر دخول."
        if state == "BEAR_PRESSURE":
            return "ضغط هابط قائم — لا توجد مقارنة زمنية كافية للحكم على قوته.", "انتظار تأكيد جديد؛ لا تُفهم الرسالة كأمر دخول."
        if bear is not None and bear >= 1 and momentum is not None and momentum < 0:
            return "ميل هابط — الضغط غير مؤكد.", "انتظار؛ لا دخول جديد من هذه القراءة وحدها."
        if bull is not None and bull >= 1 and momentum is not None and momentum > 0:
            return "محاولة ارتداد غير مؤكدة.", "انتظار توافق صاعد أوضح."
        return "تذبذب وضغط متضارب.", "انتظار وضوح الاتجاه."

    previous_state = previous.get("state")
    previous_bull = previous.get("bull_score")
    previous_bear = previous.get("bear_score")
    previous_momentum = previous.get("momentum")
    previous_cmf = previous.get("cmf")
    previous_rv = previous.get("relative_volume")
    score_up = bull is not None and previous_bull is not None and bull > previous_bull
    score_down = bull is not None and previous_bull is not None and bull < previous_bull
    bear_up = bear is not None and previous_bear is not None and bear > previous_bear
    meaningful_momentum_change = (
        momentum is not None
        and previous_momentum is not None
        and (momentum * previous_momentum < 0 or abs(momentum - previous_momentum) >= 0.10)
    )
    meaningful_cmf_change = (
        _as_float(data.get("cmf")) is not None
        and previous_cmf is not None
        and (
            _as_float(data.get("cmf")) * previous_cmf < 0
            or abs(_as_float(data.get("cmf")) - previous_cmf) >= 0.08
        )
    )
    meaningful_rv_change = (
        _as_float(data.get("relative_volume")) is not None
        and previous_rv is not None
        and abs(_as_float(data.get("relative_volume")) - previous_rv) >= 0.50
    )

    if state == "BULL_PRESSURE":
        if score_up and (meaningful_momentum_change or meaningful_cmf_change or previous_state != state):
            return "الضغط الصاعد يزداد مقارنة بالقراءة السابقة.", "مراقبة التأكيد السعري؛ لا يعني ذلك دخولًا تلقائيًا."
        if score_down or (
            meaningful_momentum_change
            and momentum is not None
            and previous_momentum is not None
            and momentum < previous_momentum
        ):
            return "الانحياز الصاعد قائم، لكن الدفع يتراجع.", "انتظار دفع جديد أو تراجع صحي؛ لا تطارد السعر."
        if meaningful_rv_change and previous_rv is not None and _as_float(data.get("relative_volume")) < previous_rv:
            return "الضغط الصاعد قائم، لكن النشاط النسبي تراجع.", "انتظار تأكيد سعري؛ الحجم النسبي وحده لا يثبت الاتجاه."
        if meaningful_cmf_change and _as_float(data.get("cmf")) is not None and previous_cmf is not None and _as_float(data.get("cmf")) < previous_cmf:
            return "الضغط الصاعد قائم، لكن التدفق التقريبي أضعف من القراءة السابقة.", "مراقبة فقط؛ CMF لا يثبت شراءً أو بيعًا مؤكدًا."
        return "ضغط صاعد قائم.", "مراقبة الحركة؛ لا تُفهم الرسالة كأمر دخول."

    if state == "BEAR_PRESSURE":
        if bear_up and (meaningful_momentum_change or previous_state != state):
            return "الضغط الهابط يزداد مقارنة بالقراءة السابقة.", "مراقبة التأكيد السعري؛ لا يعني ذلك دخولًا تلقائيًا."
        return "ضغط هابط قائم.", "مراقبة الحركة؛ لا تُفهم الرسالة كأمر دخول."

    if bear is not None and bear >= 1 and momentum is not None and momentum < 0:
        if previous_momentum is not None and momentum > previous_momentum:
            return "الهبوط يتباطأ، لكن لا يوجد ارتداد مؤكد.", "انتظار؛ لا تدخل قبل توافق أوضح."
        if bear_up or meaningful_momentum_change or meaningful_cmf_change:
            return "ميل هابط — الضغط غير مؤكد.", "انتظار؛ لا دخول جديد من هذه القراءة وحدها."
        return "ميل هابط، لكن الضغط غير مكتمل.", "انتظار تأكيد هابط أو ارتداد واضح."
    if bull is not None and bull >= 1 and momentum is not None and momentum > 0:
        return "محاولة ارتداد غير مؤكدة.", "انتظار توافق صاعد أوضح."
    return "تذبذب وضغط متضارب.", "انتظار وضوح الاتجاه."


def format_pressure_message(
    data: dict[str, Any], previous_observation: dict[str, Any] | None = None
) -> str:
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
    if alert_id == "BULLISH_BREAKOUT":
        watch_value = data.get("resistance_level")
    elif alert_id in {
        "SUPPORT_MONITOR",
        "EARLY_BULLISH_CONFIRMATION",
        "SUPPORT_SENSITIVITY",
        "SUPPORT_FAILURE",
        "SUPPORT_RECOVERY",
    }:
        watch_value = data.get("support_level")
    else:
        watch_value = data.get("watch_level")
        if watch_value is None:
            watch_value = data.get("support_level")
        if watch_value is None:
            watch_value = data.get("resistance_level")
    level = _price(watch_value)
    invalidation = data.get("invalidation", "غير متاح")
    reasons = str(data.get("reasons", "غير متاحة"))
    market_context, manual_action = _market_context(data, previous_observation)
    cycle_line = ""
    if data.get("cycle_id") is not None and data.get("attempt_id") is not None:
        cycle_line = f"🔁 الدورة: <code>C{data['cycle_id']}</code> | المحاولة: <code>A{data['attempt_id']}</code>\n"

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
        f"{cycle_line}"
        f"🧭 الفريمات: 5د={m5} | 3د={m3} | 1د={m1}\n"
        f"📊 الدرجات: Bull={bull_score}/3 | Bear={bear_score}/3 | RV3={rv3}\n"
        f"🧪 القراءة: <code>{reasons}</code>\n"
        f"🧠 السياق: {market_context}\n"
        f"📍 مستوى المراقبة: <code>{level}</code>\n"
        f"⚠️ الإلغاء: {invalidation}\n"
        "━━━━━━━━━━━━━━\n"
        f"<b>العمل:</b> {manual_action} {action}\n"
        "🧱 تجربة يدوية فقط: لا تنفيذ آلي ولا تعزيز في الخسارة."
    )


def process_pressure_webhook(
    data: dict[str, Any], send_telegram_func: Callable[[str], bool]
) -> dict[str, Any]:
    """Forward one meaningful state transition and mute duplicates."""
    global _last_state, _last_event_key, _last_alert_keys, _market_observations
    state = str(data.get("state") or data.get("radar_state", "")).upper()
    if state not in _VALID_STATES:
        return {"status": "ignored", "reason": "unknown_state", "state": state}

    alert_id = str(data.get("alert_id", "")).upper()
    if alert_id and alert_id not in _VALID_ALERT_IDS:
        return {"status": "ignored", "reason": "unknown_alert_id", "alert_id": alert_id}

    symbol = str(data.get("symbol", "TSLA"))
    current_observation = _observation_snapshot(data)
    event_key = str(
        data.get("event_key")
        or f"{state}:{data.get('bar_time', data.get('time', ''))}:{data.get('symbol', 'TSLA')}"
    )
    pending_general_state: tuple[str, str, str, str | None] | None = (
        symbol,
        state,
        event_key,
        _pressure_context_key(data),
    )
    with _state_lock:
        previous_observation = _previous_observation(symbol, current_observation)
        stored_observation = _market_observations.get(symbol)
        current_time = current_observation.get("time")
        stored_time = stored_observation.get("time") if stored_observation else None
        if stored_time is not None and current_time is not None and current_time <= stored_time:
            return {
                "status": "ignored",
                "reason": "stale_observation",
                "symbol": symbol,
            }
        previous_state_snapshot = _last_state
        previous_event_key_snapshot = _last_event_key
        previous_alert_keys_snapshot = set(_last_alert_keys)
        previous_pressure_contexts_snapshot = {
            key: dict(value) for key, value in _pressure_contexts.items()
        }
        previous_support_cycles_snapshot = {
            key: dict(value) for key, value in _support_cycles.items()
        }
        previous_support_counters_snapshot = dict(_support_cycle_counters)
        previous_support_history_snapshot = {
            key: list(value) for key, value in _support_transition_history.items()
        }
        previous_market_observations_snapshot = {
            key: dict(value) for key, value in _market_observations.items()
        }
    with _state_lock:
        if alert_id:
            if alert_id in _SUPPORT_PHASES:
                support_id = str(data.get("support_id", ""))
                support_key = f"{symbol}:{support_id}"
                phase = _SUPPORT_PHASES[alert_id]
                current = _support_cycles.get(symbol)
                previous_phase = None
                transition = "NEW_CYCLE"

                if current is None or current["support_key"] != support_key:
                    cycle_id = _support_cycle_counters.get(symbol, 0) + 1
                    _support_cycle_counters[symbol] = cycle_id
                    current = {
                        "support_key": support_key,
                        "support_id": support_id,
                        "cycle_id": cycle_id,
                        "attempt_id": 1,
                        "phase": None,
                        "rearmed": False,
                    }
                    _support_cycles[symbol] = current
                else:
                    cycle_id = current["cycle_id"]
                    previous_phase = current["phase"]
                    if current["rearmed"] and phase != "RECOVERY":
                        current["attempt_id"] += 1
                        current["rearmed"] = False
                        transition = "NEW_ATTEMPT"
                    elif previous_phase == phase:
                        transition = "CONTINUATION"
                    else:
                        transition = "PHASE_TRANSITION"

                attempt_id = current["attempt_id"]
                history = _support_transition_history.setdefault(support_key, [])
                if transition == "CONTINUATION":
                    return {
                        "status": "ignored",
                        "reason": "duplicate_support_phase",
                        "alert_id": alert_id,
                        "cycle_id": cycle_id,
                        "attempt_id": attempt_id,
                    }

                current["phase"] = phase
                if phase == "RECOVERY":
                    current["rearmed"] = True
                record = {
                    "cycle_id": cycle_id,
                    "attempt_id": attempt_id,
                    "from_phase": previous_phase,
                    "to_phase": phase,
                    "alert_id": alert_id,
                    "event_key": event_key,
                    "bar_time": data.get("bar_time", data.get("time")),
                }
                history.append(record)
                payload_for_message = dict(data)
                payload_for_message.update({"cycle_id": cycle_id, "attempt_id": attempt_id})
            else:
                alert_key = str(
                    data.get("event_key")
                    or f"{data.get('symbol', 'TSLA')}:{alert_id}:{data.get('support_id', '')}"
                )
                if alert_key in _last_alert_keys:
                    return {"status": "ignored", "reason": "duplicate_alert", "alert_id": alert_id}
                _last_alert_keys.add(alert_key)
                payload_for_message = data
        else:
            context_key = _pressure_context_key(data)
            previous_context = _pressure_contexts.get(symbol)

            if event_key == _last_event_key:
                return {"status": "ignored", "reason": "duplicate_state", "state": state}

            if state == "WAIT":
                if _last_state == "WAIT":
                    return {"status": "ignored", "reason": "duplicate_state", "state": state}
                payload_for_message = data
                pending_general_state = (symbol, state, event_key, context_key)
            else:
                if (
                    previous_context
                    and previous_context.get("direction") == state
                    and _last_state == state
                    and context_key is None
                ):
                    return {"status": "ignored", "reason": "duplicate_state", "state": state}

                same_confirmed_context = bool(
                    previous_context
                    and previous_context.get("direction") == state
                    and context_key is not None
                    and previous_context.get("context_key") == context_key
                )
                if same_confirmed_context:
                    return {
                        "status": "ignored",
                        "reason": "duplicate_pressure_context",
                        "state": state,
                    }

                same_direction_without_context = bool(
                    previous_context
                    and previous_context.get("direction") == state
                    and (context_key is None or previous_context.get("context_key") is None)
                )
                if same_direction_without_context and _last_state == "WAIT":
                    return {
                        "status": "ignored",
                        "reason": "insufficient_context_for_pressure_reentry",
                        "state": state,
                    }

                payload_for_message = data
                pending_general_state = (symbol, state, event_key, context_key)

    message = format_pressure_message(payload_for_message, previous_observation)
    sent = bool(send_telegram_func(message))
    if not sent:
        with _state_lock:
            _last_state = previous_state_snapshot
            _last_event_key = previous_event_key_snapshot
            _last_alert_keys.clear()
            _last_alert_keys.update(previous_alert_keys_snapshot)
            _pressure_contexts.clear()
            _pressure_contexts.update(previous_pressure_contexts_snapshot)
            _support_cycles.clear()
            _support_cycles.update(previous_support_cycles_snapshot)
            _support_cycle_counters.clear()
            _support_cycle_counters.update(previous_support_counters_snapshot)
            _support_transition_history.clear()
            _support_transition_history.update(previous_support_history_snapshot)
            _market_observations.clear()
            _market_observations.update(previous_market_observations_snapshot)
        result = {"status": "failed", "state": state, "telegram": "failed", "message": message}
    else:
        result = {"status": "processed", "state": state, "telegram": "sent", "message": message}
        if pending_general_state is not None:
            symbol, committed_state, committed_event_key, context_key = pending_general_state
            with _state_lock:
                previous = _pressure_contexts.get(symbol, {})
                _pressure_contexts[symbol] = {
                    "direction": committed_state
                    if committed_state in {"BULL_PRESSURE", "BEAR_PRESSURE"}
                    else previous.get("direction"),
                    "context_key": context_key
                    if context_key is not None
                    else previous.get("context_key"),
                }
                _last_state = committed_state
                _last_event_key = committed_event_key
        with _state_lock:
            if current_observation.get("time") is not None:
                _market_observations[symbol] = current_observation
    if alert_id in _SUPPORT_PHASES:
        result.update({"cycle_id": payload_for_message["cycle_id"], "attempt_id": payload_for_message["attempt_id"]})
    return result


def reset_state_for_test() -> None:
    global _last_state, _last_event_key, _last_alert_keys, _market_observations
    with _state_lock:
        _last_state = None
        _last_event_key = None
        _last_alert_keys = set()
        _pressure_contexts.clear()
        _support_cycles.clear()
        _support_cycle_counters.clear()
        _support_transition_history.clear()
        _market_observations.clear()


def support_history_snapshot() -> dict[str, list[dict[str, Any]]]:
    """Return a copy of support transitions for diagnostics and tests."""
    with _state_lock:
        return {key: list(records) for key, records in _support_transition_history.items()}
