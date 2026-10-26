"""Adapter for State Machine MVP -> existing Pressure Radar Render handler.

This module deliberately does not modify app.py or pressure_radar_alerts.py.
It maps only the three new pressure events to the existing generic state path.
"""
from __future__ import annotations

from typing import Any, Callable


_SUPPORTED_SOURCE_EVENTS = {"PRESSURE_WAIT", "PRESSURE_BULL", "PRESSURE_BEAR"}
_STATE_BY_SOURCE_EVENT = {
    "PRESSURE_WAIT": "WAIT",
    "PRESSURE_BULL": "BULL_PRESSURE",
    "PRESSURE_BEAR": "BEAR_PRESSURE",
}


def adapt_state_machine_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return a payload accepted by the existing pressure_radar_alerts handler.

    The original event identity is preserved under source_* fields. The generic
    handler's alert_id is intentionally blank: assigning SUPPORT or BREAKOUT
    would be semantically incorrect and would change strategy meaning.
    """
    source_alert_id = str(data.get("alert_id", "")).upper()
    if source_alert_id not in _SUPPORTED_SOURCE_EVENTS:
        raise ValueError(f"unsupported_state_machine_event:{source_alert_id}")

    state = _STATE_BY_SOURCE_EVENT[source_alert_id]
    adapted = dict(data)
    adapted.update(
        {
            "state": state,
            "radar_state": state,
            "alert_id": "",
            "source_alert_id": source_alert_id,
            "source_event_type": data.get("event_type", "PRESSURE_STATE"),
            "source_event_key": data.get("event_key", ""),
            "source_pressure_context": data.get("pressure_context", ""),
        }
    )

    # Existing formatter understands these names. Do not invent a level.
    if adapted.get("support_level") in (None, "") and adapted.get("active_support") not in (None, ""):
        adapted["support_level"] = adapted["active_support"]
    if adapted.get("watch_level") in (None, "") and adapted.get("support_level") not in (None, ""):
        adapted["watch_level"] = adapted["support_level"]

    source_reason = {
        "PRESSURE_WAIT": "حدث ضغط: انتظار — " + str(data.get("wait_reason") or "سبب غير متاح"),
        "PRESSURE_BULL": "حدث ضغط: تحول/تأكيد صاعد",
        "PRESSURE_BEAR": "حدث ضغط: تحول/تأكيد هابط",
    }[source_alert_id]
    adapted["reasons"] = data.get("reasons") or source_reason
    adapted["invalidation"] = data.get("invalidation") or "تتغير القراءة عند تغير الحالة أو السياق"
    return adapted


def process_state_machine_webhook(
    data: dict[str, Any],
    process_render_payload: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Adapt one State Machine event and pass it to the existing handler."""
    adapted = adapt_state_machine_payload(data)
    result = process_render_payload(adapted)
    if isinstance(result, dict):
        result = dict(result)
        result["source_alert_id"] = adapted["source_alert_id"]
        result["adapted_state"] = adapted["state"]
    return result
