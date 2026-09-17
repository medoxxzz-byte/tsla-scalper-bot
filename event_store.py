"""Immutable research-event storage for TM Sniper.

The module records every V17/V18 webhook receipt while keeping the first valid
payload as the sole legal event record.  It deliberately changes no alert logic:
storage remains fail-open, so a database problem never prevents the Telegram
workflow from running.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - handled at runtime when deployment is incomplete
    psycopg = None
    Jsonb = None

LOGGER = logging.getLogger(__name__)
EVENT_TABLE = "tm_research_events"
RECEIPT_TABLE = "webhook_receipts"
EASTERN = ZoneInfo("America/New_York")
OFFICIAL_SCHEMA_VERSION = "event-v2"
PROCESSOR_VERSION = "identity-receipt-v2"

NUMERIC_FIELDS = (
    "price", "vwap", "support", "resistance", "range_low", "range_high",
    "cmf", "macd", "macd_signal", "macd_histogram", "rsi", "obv", "adx",
    "mom", "relative_volume", "atr", "volume",
)

EXPECTED_TIMEFRAMES = {"v17": "5m", "v18": "1m"}
EXPECTED_BAR_SECONDS = {"1m": 60, "5m": 300}
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")

SIGNAL_FAMILIES = {
    "v17": {
        "MAP_PREOPEN": "reversal_map",
        "MAP_OPEN_15": "reversal_map",
        "MAP_45": "reversal_map",
        "MAP_90": "reversal_map",
        "ZONE_CALL_CONFIRM": "zone_reversal",
        "ZONE_PUT_CONFIRM": "zone_reversal",
        "STORAGE_ACCEPTANCE_TEST": "storage_acceptance_test",
    },
    "v18": {
        "MINUTE_MAP": "minute_map",
        "MINUTE_TREND_BULL": "minute_trend",
        "MINUTE_TREND_BEAR": "minute_trend",
        "MINUTE_CALL_CONFIRM": "minute_confirmation",
        "MINUTE_PUT_CONFIRM": "minute_confirmation",
        "CLOSE_BREAKOUT_CALL": "closing_breakout",
        "CLOSE_BREAKDOWN_PUT": "closing_breakout",
    },
}


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or str(value).strip().lower() in {"na", "n/a", "none", "null"}:
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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def calculate_payload_hash(payload: Dict[str, Any]) -> str:
    """Return the deterministic SHA-256 fingerprint of the complete raw payload."""
    canonical = _canonical_json(_clean_value(payload))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_direction(action: str) -> Optional[str]:
    action = (action or "").upper()
    if "CALL" in action or action.endswith("_BULL"):
        return "CALL"
    if "PUT" in action or action.endswith("_BEAR") or "BREAKDOWN" in action:
        return "PUT"
    return None


def _parse_epoch_milliseconds(value: Any, field_name: str, errors: List[str]) -> Optional[Tuple[int, datetime]]:
    """Parse the exact 13-digit epoch-millisecond values sent by Pine ``time`` fields."""
    if isinstance(value, bool) or value is None:
        errors.append(f"missing_or_invalid_{field_name}")
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{13}", text):
        errors.append(f"missing_or_invalid_{field_name}")
        return None
    milliseconds = int(text)
    try:
        parsed = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        errors.append(f"missing_or_invalid_{field_name}")
        return None
    return milliseconds, parsed


def _canonical_bound(value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{value:.4f}"


def canonical_zone_bounds(payload: Dict[str, Any], action: str) -> Dict[str, Optional[str]]:
    """Return stable zone bounds without using the mutable current price.

    A closing breakout is identified by its 15:15–15:30 range.  A confirmed
    CALL/PUT is identified by its applicable decision zone.  Maps and trend
    context retain their declared support/resistance pair.  ``null`` is valid
    for a missing map side; bar identity still prevents a false merge.
    """
    action = (action or "").upper()
    support = _as_float(payload.get("support"))
    resistance = _as_float(payload.get("resistance"))
    range_low = _as_float(payload.get("range_low"))
    range_high = _as_float(payload.get("range_high"))
    half_width = _as_float(payload.get("zone_half_width"))

    if action.startswith("CLOSE_"):
        low, high = range_low, range_high
    elif action in {"ZONE_CALL_CONFIRM", "MINUTE_CALL_CONFIRM"} and support is not None and half_width is not None:
        low, high = support - half_width, support + half_width
    elif action in {"ZONE_PUT_CONFIRM", "MINUTE_PUT_CONFIRM"} and resistance is not None and half_width is not None:
        low, high = resistance - half_width, resistance + half_width
    else:
        declared = sorted(value for value in (support, resistance) if value is not None)
        low = declared[0] if declared else None
        high = declared[-1] if declared else None

    return {"zone_low": _canonical_bound(low), "zone_high": _canonical_bound(high)}


def validate_official_identity(source: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate Pine identity fields and prepare canonical event attributes."""
    errors: List[str] = []
    source = str(source or "").lower()
    if source not in EXPECTED_TIMEFRAMES:
        errors.append("unsupported_source")

    schema_version = str(payload.get("schema_version", "")).strip()
    if schema_version != OFFICIAL_SCHEMA_VERSION:
        errors.append("unsupported_schema_version")

    action = str(payload.get("action", "")).strip().upper()
    signal_family = SIGNAL_FAMILIES.get(source, {}).get(action)
    if not action:
        errors.append("missing_or_invalid_action")
    elif signal_family is None:
        errors.append("unsupported_action_for_source")
    elif action == "STORAGE_ACCEPTANCE_TEST" and payload.get("_research_acceptance_test") is not True:
        errors.append("test_action_not_authorized")

    symbol = str(payload.get("symbol", "")).strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        errors.append("missing_or_invalid_symbol")

    timeframe = str(payload.get("timeframe", "")).strip().lower()
    expected_timeframe = EXPECTED_TIMEFRAMES.get(source)
    if not timeframe:
        errors.append("missing_or_invalid_timeframe")
    elif timeframe != expected_timeframe:
        errors.append("source_timeframe_mismatch")

    open_time = _parse_epoch_milliseconds(payload.get("bar_open_time"), "bar_open_time", errors)
    close_time = _parse_epoch_milliseconds(payload.get("bar_close_time"), "bar_close_time", errors)
    if open_time and close_time:
        open_ms, open_at = open_time
        close_ms, close_at = close_time
        if close_ms <= open_ms:
            errors.append("bar_close_not_after_open")
        elif timeframe in EXPECTED_BAR_SECONDS and (close_ms - open_ms) != EXPECTED_BAR_SECONDS[timeframe] * 1000:
            errors.append("bar_duration_does_not_match_timeframe")
    else:
        open_ms = close_ms = None
        open_at = close_at = None

    if errors:
        return None, sorted(set(errors))

    direction = classify_direction(action)
    return {
        "schema_version": schema_version,
        "source": source,
        "signal_family": signal_family,
        "symbol": symbol,
        "timeframe": timeframe,
        "bar_open_time_ms": open_ms,
        "bar_close_time_ms": close_ms,
        "bar_open_time": open_at,
        "bar_close_time": close_at,
        "action": action,
        "direction": direction,
        "zone_bounds": canonical_zone_bounds(payload, action),
    }, []


def _event_key_from_identity(identity: Dict[str, Any]) -> str:
    key_material = {
        "schema_version": identity["schema_version"],
        "source": identity["source"],
        "signal_family": identity["signal_family"],
        "symbol": identity["symbol"],
        "timeframe": identity["timeframe"],
        "bar_close_time_ms": identity["bar_close_time_ms"],
        "action": identity["action"],
        "direction": identity["direction"],
        **identity["zone_bounds"],
    }
    return hashlib.sha256(_canonical_json(key_material).encode("utf-8")).hexdigest()


def build_event_key(source: str, payload: Dict[str, Any], received_at: Optional[datetime] = None) -> str:
    """Build an official identity key from Pine's immutable bar fields.

    ``received_at`` remains an ignored compatibility argument so older callers do
    not silently switch to a network-time identity.  Invalid payloads raise a
    clear ``ValueError`` rather than using the old price/date fallback.
    """
    del received_at
    identity, errors = validate_official_identity(source, payload)
    if errors or identity is None:
        raise ValueError("invalid official event identity: " + ", ".join(errors))
    return _event_key_from_identity(identity)


def _changed_fields(original: Any, received: Any, prefix: str = "") -> List[str]:
    """List dotted payload paths that differ without exposing values in receipts."""
    if isinstance(original, dict) and isinstance(received, dict):
        paths: List[str] = []
        for key in sorted(set(original) | set(received)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in original or key not in received:
                paths.append(path)
            else:
                paths.extend(_changed_fields(original[key], received[key], path))
        return paths
    if isinstance(original, list) and isinstance(received, list):
        paths = []
        max_len = max(len(original), len(received))
        for index in range(max_len):
            path = f"{prefix}[{index}]"
            if index >= len(original) or index >= len(received):
                paths.append(path)
            else:
                paths.extend(_changed_fields(original[index], received[index], path))
        return paths
    return [] if original == received else [prefix or "$"]


class ResearchEventStore:
    """Fail-open, receipt-first persistence adapter for official research telemetry."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.database_url and psycopg is not None and Jsonb is not None)

    def _record_invalid(
        self,
        source: str,
        raw_payload: Dict[str, Any],
        payload_hash: str,
        validation_errors: Iterable[str],
        received_at: datetime,
    ) -> Dict[str, Any]:
        sql = f"""
            INSERT INTO {RECEIPT_TABLE} (
                event_id, claimed_event_key, source, received_at, payload_hash,
                raw_payload, receipt_status, conflict_fields, validation_errors,
                processor_version
            ) VALUES (
                NULL, NULL, %(source)s, %(received_at)s, %(payload_hash)s,
                %(raw_payload)s, 'invalid', NULL, %(validation_errors)s,
                %(processor_version)s
            )
            RETURNING receipt_id
        """
        params = {
            "source": source,
            "received_at": received_at,
            "payload_hash": payload_hash,
            "raw_payload": Jsonb(raw_payload),
            "validation_errors": Jsonb(sorted(set(validation_errors))),
            "processor_version": PROCESSOR_VERSION,
        }
        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                receipt_id = int(cursor.fetchone()[0])
            connection.commit()
        return {
            "status": "invalid",
            "receipt_id": receipt_id,
            "validation_errors": sorted(set(validation_errors)),
        }

    def record_event(
        self,
        source: str,
        payload: Dict[str, Any],
        data_quality_tier: str = "official",
    ) -> Dict[str, Any]:
        """Record one webhook without ever modifying an existing legal event.

        A valid delivery creates either a legal event plus a ``new`` receipt, or
        a ``duplicate``/``conflict`` receipt linked to the first event.  Invalid
        identity is also retained as a receipt, with no legal event created.
        """
        if not self.enabled:
            reason = "DATABASE_URL is not configured" if not self.database_url else "psycopg is unavailable"
            LOGGER.warning("Research event storage skipped: %s", reason)
            return {"status": "skipped", "reason": reason}

        source = str(source or "").lower()
        raw_payload = _clean_value(payload if isinstance(payload, dict) else {"_invalid_body": str(payload)})
        payload_hash = calculate_payload_hash(raw_payload)
        received_at = datetime.now(timezone.utc)
        identity, validation_errors = validate_official_identity(source, raw_payload)
        if data_quality_tier not in {"official", "test"}:
            validation_errors = sorted(set(validation_errors + ["invalid_data_quality_tier"]))
            identity = None

        try:
            if identity is None:
                return self._record_invalid(source, raw_payload, payload_hash, validation_errors, received_at)

            event_key = _event_key_from_identity(identity)
            values = {key: _as_float(raw_payload.get(key)) for key in NUMERIC_FIELDS}
            values["macd_histogram"] = _first_float(raw_payload, "macd_histogram", "hist")
            values["mom"] = _first_float(raw_payload, "mom", "momentum")
            event_date = identity["bar_close_time"].astimezone(EASTERN).date()

            insert_event_sql = f"""
                INSERT INTO {EVENT_TABLE} (
                    event_key, schema_version, signal_family, source, action, direction,
                    symbol, timeframe, bar_open_time, bar_close_time, data_quality_tier,
                    event_date_et, source_event_time, received_at, last_received_at,
                    price, vwap, support, resistance, range_low, range_high,
                    cmf, macd, macd_signal, macd_histogram, rsi, obv, adx, mom,
                    relative_volume, atr, volume, five_minute_bias, payload, payload_hash
                ) VALUES (
                    %(event_key)s, %(schema_version)s, %(signal_family)s, %(source)s, %(action)s, %(direction)s,
                    %(symbol)s, %(timeframe)s, %(bar_open_time)s, %(bar_close_time)s, %(data_quality_tier)s,
                    %(event_date)s, %(source_event_time)s, %(received_at)s, %(received_at)s,
                    %(price)s, %(vwap)s, %(support)s, %(resistance)s, %(range_low)s, %(range_high)s,
                    %(cmf)s, %(macd)s, %(macd_signal)s, %(macd_histogram)s, %(rsi)s, %(obv)s, %(adx)s, %(mom)s,
                    %(relative_volume)s, %(atr)s, %(volume)s, %(five_minute_bias)s, %(payload)s, %(payload_hash)s
                )
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id, payload_hash, payload
            """
            event_params: Dict[str, Any] = {
                "event_key": event_key,
                "schema_version": identity["schema_version"],
                "signal_family": identity["signal_family"],
                "source": source,
                "action": identity["action"],
                "direction": identity["direction"],
                "symbol": identity["symbol"],
                "timeframe": identity["timeframe"],
                "bar_open_time": identity["bar_open_time"],
                "bar_close_time": identity["bar_close_time"],
                "data_quality_tier": data_quality_tier,
                "event_date": event_date,
                "source_event_time": str(identity["bar_close_time_ms"]),
                "received_at": received_at,
                "five_minute_bias": str(raw_payload.get("five_minute_bias", "")).lower() or None,
                "payload": Jsonb(raw_payload),
                "payload_hash": payload_hash,
                **values,
            }

            receipt_sql = f"""
                INSERT INTO {RECEIPT_TABLE} (
                    event_id, claimed_event_key, source, received_at, payload_hash,
                    raw_payload, receipt_status, conflict_fields, validation_errors,
                    processor_version
                ) VALUES (
                    %(event_id)s, %(event_key)s, %(source)s, %(received_at)s, %(payload_hash)s,
                    %(raw_payload)s, %(receipt_status)s, %(conflict_fields)s, NULL,
                    %(processor_version)s
                )
                RETURNING receipt_id
            """

            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(insert_event_sql, event_params)
                    inserted = cursor.fetchone()
                    if inserted is not None:
                        event_id = int(inserted[0])
                        receipt_status = "new"
                        conflict_fields = None
                    else:
                        # ``ON CONFLICT DO NOTHING`` waits for a concurrent writer
                        # to commit, so this fetch always observes the legal event.
                        cursor.execute(
                            f"SELECT id, payload_hash, payload FROM {EVENT_TABLE} WHERE event_key = %s",
                            (event_key,),
                        )
                        original = cursor.fetchone()
                        if original is None:  # pragma: no cover - defensive database invariant guard
                            raise RuntimeError("event key conflict resolved without a stored event")
                        event_id = int(original[0])
                        if str(original[1]).strip() == payload_hash:
                            receipt_status = "duplicate"
                            conflict_fields = None
                        else:
                            receipt_status = "conflict"
                            conflict_fields = _changed_fields(original[2], raw_payload)

                    receipt_params = {
                        "event_id": event_id,
                        "event_key": event_key,
                        "source": source,
                        "received_at": received_at,
                        "payload_hash": payload_hash,
                        "raw_payload": Jsonb(raw_payload),
                        "receipt_status": receipt_status,
                        "conflict_fields": Jsonb(conflict_fields) if conflict_fields is not None else None,
                        "processor_version": PROCESSOR_VERSION,
                    }
                    cursor.execute(receipt_sql, receipt_params)
                    receipt_id = int(cursor.fetchone()[0])
                connection.commit()

            return {
                "status": receipt_status,
                "event_id": event_id,
                "receipt_id": receipt_id,
                "event_key": event_key,
                "payload_hash": payload_hash,
            }
        except Exception as exc:  # fail open: alert processing continues
            LOGGER.exception("Research event storage failed for %s", source)
            return {"status": "failed", "reason": str(exc)}

    def healthcheck(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "unavailable", "reason": "storage is not configured"}
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT to_regclass('public.tm_research_events'), to_regclass('public.webhook_receipts')"
                    )
                    event_table, receipt_table = cursor.fetchone()
            if event_table and receipt_table:
                return {"status": "ok", "identity_version": OFFICIAL_SCHEMA_VERSION}
            return {"status": "failed", "reason": "identity receipt schema is incomplete"}
        except Exception as exc:
            LOGGER.exception("Research event storage healthcheck failed")
            return {"status": "failed", "reason": str(exc)}
