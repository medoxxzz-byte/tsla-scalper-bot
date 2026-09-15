"""Persistent research-event storage for TM Sniper.

This module deliberately records raw webhook events without changing alert logic.
A deterministic key plus a PostgreSQL UNIQUE constraint makes repeated webhook
requests observable but prevents duplicate research rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - handled at runtime when deployment is incomplete
    psycopg = None
    Jsonb = None

LOGGER = logging.getLogger(__name__)
EVENT_TABLE = "tm_research_events"
EASTERN = ZoneInfo("America/New_York")

NUMERIC_FIELDS = (
    "price", "vwap", "support", "resistance", "range_low", "range_high",
    "cmf", "macd", "macd_signal", "macd_histogram", "rsi", "obv", "adx",
    "mom", "relative_volume", "atr", "volume",
)


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or str(value).strip().lower() in {"na", "n/a", "none"}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(payload: Dict[str, Any], *keys: str) -> Optional[float]:
    """Read the first populated Pine field among compatible versions."""
    for key in keys:
        value = _as_float(payload.get(key))
        if value is not None:
            return value
    return None


def _clean_value(value: Any) -> Any:
    """Produce a stable JSON-safe value without mutating the original payload."""
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _source_event_time(payload: Dict[str, Any]) -> str:
    for key in ("event_id", "bar_time", "bar_timestamp", "timestamp", "time", "event_time"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _event_date_eastern(payload: Dict[str, Any], received_at: datetime) -> str:
    for key in ("event_date", "date", "trading_date"):
        value = payload.get(key)
        if value:
            return str(value)[:10]
    return received_at.astimezone(EASTERN).date().isoformat()


def build_event_key(source: str, payload: Dict[str, Any], received_at: Optional[datetime] = None) -> str:
    """Build a deterministic identity for the same TradingView event.

    Pine should eventually send an explicit ``event_id`` or ``bar_time``. Until
    then, date/action/levels/price identify the bounded V17/V18 events and make
    a replay of an unchanged webhook idempotent.
    """
    received_at = received_at or datetime.now(timezone.utc)
    action = str(payload.get("action", "")).strip().upper()
    event_time = _source_event_time(payload)
    identity: Dict[str, Any] = {
        "source": source,
        "action": action,
        "event_date_et": _event_date_eastern(payload, received_at),
        "source_event_time": event_time,
    }
    # Include price and structural levels only when no bar/event identifier is
    # available. This prevents distinct future events from being collapsed.
    if not event_time:
        identity["price"] = _as_float(payload.get("price"))
        for key in ("support", "resistance", "range_low", "range_high"):
            identity[key] = _as_float(payload.get(key))
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_direction(action: str) -> Optional[str]:
    action = (action or "").upper()
    if "CALL" in action or action.endswith("_BULL"):
        return "CALL"
    if "PUT" in action or action.endswith("_BEAR") or "BREAKDOWN" in action:
        return "PUT"
    return None


class ResearchEventStore:
    """A small, fail-open persistence adapter for research telemetry."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.database_url and psycopg is not None and Jsonb is not None)

    def record_event(self, source: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store one raw event and return whether it was new or a replay.

        Alert delivery must never depend on storage availability. Exceptions are
        captured and reported to the caller while the original webhook continues.
        """
        if not self.enabled:
            reason = "DATABASE_URL is not configured" if not self.database_url else "psycopg is unavailable"
            LOGGER.warning("Research event storage skipped: %s", reason)
            return {"status": "skipped", "reason": reason}

        received_at = datetime.now(timezone.utc)
        event_key = build_event_key(source, payload, received_at)
        normalized_payload = _clean_value(payload)
        action = str(payload.get("action", "")).strip().upper()
        values = {key: _as_float(payload.get(key)) for key in NUMERIC_FIELDS}
        # V17/V18 currently use ``hist`` and ``momentum``; V19 may use
        # explicit ``macd_histogram`` and ``mom``. Keep both versions usable.
        values["macd_histogram"] = _first_float(payload, "macd_histogram", "hist")
        values["mom"] = _first_float(payload, "mom", "momentum")
        event_date = _event_date_eastern(payload, received_at)
        direction = classify_direction(action)

        sql = f"""
            INSERT INTO {EVENT_TABLE} (
                event_key, source, action, direction, event_date_et,
                source_event_time, received_at, last_received_at,
                price, vwap, support, resistance, range_low, range_high,
                cmf, macd, macd_signal, macd_histogram, rsi, obv, adx, mom,
                relative_volume, atr, volume, five_minute_bias, payload
            ) VALUES (
                %(event_key)s, %(source)s, %(action)s, %(direction)s, %(event_date)s,
                %(source_event_time)s, %(received_at)s, %(received_at)s,
                %(price)s, %(vwap)s, %(support)s, %(resistance)s, %(range_low)s, %(range_high)s,
                %(cmf)s, %(macd)s, %(macd_signal)s, %(macd_histogram)s, %(rsi)s, %(obv)s, %(adx)s, %(mom)s,
                %(relative_volume)s, %(atr)s, %(volume)s, %(five_minute_bias)s, %(payload)s
            )
            ON CONFLICT (event_key) DO UPDATE
            SET duplicate_count = {EVENT_TABLE}.duplicate_count + 1,
                last_received_at = EXCLUDED.last_received_at
            RETURNING id, duplicate_count
        """
        params: Dict[str, Any] = {
            "event_key": event_key,
            "source": source,
            "action": action,
            "direction": direction,
            "event_date": event_date,
            "source_event_time": _source_event_time(payload) or None,
            "received_at": received_at,
            "five_minute_bias": str(payload.get("five_minute_bias", "")).lower() or None,
            "payload": Jsonb(normalized_payload),
            **values,
        }
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    row = cursor.fetchone()
                connection.commit()
            duplicate_count = int(row[1])
            return {
                "status": "stored" if duplicate_count == 0 else "duplicate",
                "event_id": int(row[0]),
                "event_key": event_key,
                "duplicate_count": duplicate_count,
            }
        except Exception as exc:  # fail open: alert processing continues
            LOGGER.exception("Research event storage failed for %s/%s", source, action)
            return {"status": "failed", "reason": str(exc), "event_key": event_key}

    def healthcheck(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "unavailable", "reason": "storage is not configured"}
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            return {"status": "ok"}
        except Exception as exc:
            LOGGER.exception("Research event storage healthcheck failed")
            return {"status": "failed", "reason": str(exc)}
