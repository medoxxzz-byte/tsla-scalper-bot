"""Append-only post-event outcome tracking for TM Sniper research.

Phase B records Pine-delivered observations after an immutable official event.
It never mutates an event or its webhook receipts, and it never sends Telegram.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import time as clock_time

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    Jsonb = None

from event_store import (
    EASTERN,
    OFFICIAL_SCHEMA_VERSION,
    PROCESSOR_VERSION,
    _as_float,
    _clean_value,
    _event_key_from_identity,
    _parse_epoch_milliseconds,
    _changed_fields,
    calculate_payload_hash,
    validate_official_identity,
)

LOGGER = logging.getLogger(__name__)
OBSERVATION_TABLE = "event_observations"
OBSERVATION_RECEIPT_TABLE = "event_observation_receipts"
ORPHAN_RECONCILIATION_TABLE = "event_observation_orphan_reconciliations"
OUTCOME_TABLE = "event_outcomes"
TRACKING_VERSION = "tracking-v1"
OUTCOME_VERSION = "outcome-v1"
TRACKING_PROCESSOR_VERSION = "outcome-tracking-v1"

OBSERVATION_SPECS = {
    "v18": {
        "post_3m": 3 * 60,
        "post_6m": 6 * 60,
        "post_12m": 12 * 60,
        "close_1600": None,
    },
    "v17": {
        "post_5m": 5 * 60,
        "post_10m": 10 * 60,
        "post_15m": 15 * 60,
        "close_1600": None,
    },
}
FINAL_OBSERVATION_BY_SOURCE = {"v18": "post_12m", "v17": "post_15m"}
EXPECTED_BAR_SECONDS = {"v18": 60, "v17": 300}
DIRECTIONAL_ACTIONS = {
    "ZONE_CALL_CONFIRM",
    "ZONE_PUT_CONFIRM",
    "MINUTE_CALL_CONFIRM",
    "MINUTE_PUT_CONFIRM",
    "MINUTE_B_CALL_CONFIRM",
    "MINUTE_B_PUT_CONFIRM",
    "MINUTE_TREND_BULL",
    "MINUTE_TREND_BEAR",
    "CLOSE_BREAKOUT_CALL",
    "CLOSE_BREAKDOWN_PUT",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _observation_key(parent_event_key: str, observation_type: str, observed_bar_close_ms: int) -> str:
    material = {
        "parent_event_key": parent_event_key,
        "observation_type": observation_type,
        "observed_bar_close_time_ms": observed_bar_close_ms,
        "tracking_version": TRACKING_VERSION,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _as_required_float(raw: Dict[str, Any], field: str, errors: List[str]) -> Optional[float]:
    value = _as_float(raw.get(field))
    if value is None:
        errors.append(f"missing_or_invalid_{field}")
    return value


def _parent_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the complete immutable parent identity sent by Pine."""
    return {
        "schema_version": raw.get("parent_schema_version"),
        "symbol": raw.get("parent_symbol"),
        "timeframe": raw.get("parent_timeframe"),
        "bar_open_time": raw.get("parent_bar_open_time"),
        "bar_close_time": raw.get("parent_bar_close_time"),
        "action": raw.get("parent_action"),
        "support": raw.get("parent_support"),
        "resistance": raw.get("parent_resistance"),
        "range_low": raw.get("parent_range_low"),
        "range_high": raw.get("parent_range_high"),
        "zone_half_width": raw.get("parent_zone_half_width"),
    }


def _validate_tracking_payload(source: str, raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate tracking identity and timing before any database lookup."""
    source = str(source or "").lower()
    errors: List[str] = []
    if raw.get("tracking_version") != TRACKING_VERSION:
        errors.append("unsupported_tracking_version")
    if source not in OBSERVATION_SPECS:
        errors.append("unsupported_tracking_source")

    parent, parent_errors = validate_official_identity(source, _parent_payload(raw))
    errors.extend(f"parent_{error}" for error in parent_errors)
    if parent is None:
        return None, sorted(set(errors))

    observation_type = str(raw.get("observation_type", "")).strip().lower()
    expected_seconds = OBSERVATION_SPECS.get(source, {}).get(observation_type, "unsupported")
    if expected_seconds == "unsupported":
        errors.append("unsupported_observation_type")

    observed_open = _parse_epoch_milliseconds(raw.get("observed_bar_open_time"), "observed_bar_open_time", errors)
    observed_close = _parse_epoch_milliseconds(raw.get("observed_bar_close_time"), "observed_bar_close_time", errors)
    if observed_open and observed_close:
        observed_open_ms, observed_open_at = observed_open
        observed_close_ms, observed_close_at = observed_close
        expected_bar_ms = EXPECTED_BAR_SECONDS.get(source, 0) * 1000
        if observed_close_ms <= observed_open_ms:
            errors.append("observed_bar_close_not_after_open")
        elif observed_close_ms - observed_open_ms != expected_bar_ms:
            errors.append("observed_bar_duration_does_not_match_source")
        if isinstance(expected_seconds, int) and observed_close_ms - parent["bar_close_time_ms"] != expected_seconds * 1000:
            errors.append("observation_offset_does_not_match_type")
        if observation_type == "close_1600":
            observed_et = observed_close_at.astimezone(EASTERN)
            parent_et = parent["bar_close_time"].astimezone(EASTERN)
            if source == "v18" and not str(parent["action"]).startswith("CLOSE_"):
                errors.append("close_1600_requires_closing_action")
            if observed_et.date() != parent_et.date() or observed_et.timetz().replace(tzinfo=None) != clock_time(16, 0):
                errors.append("close_1600_must_end_at_regular_session_close")
    else:
        observed_open_ms = observed_close_ms = None
        observed_open_at = observed_close_at = None

    price_open = _as_required_float(raw, "price_open", errors)
    price_high = _as_required_float(raw, "price_high", errors)
    price_low = _as_required_float(raw, "price_low", errors)
    price_close = _as_required_float(raw, "price_close", errors)
    cumulative_high = _as_required_float(raw, "cumulative_high", errors)
    cumulative_low = _as_required_float(raw, "cumulative_low", errors)

    if price_high is not None and price_low is not None and price_high < price_low:
        errors.append("price_high_below_price_low")
    if price_open is not None and price_low is not None and price_high is not None and not price_low <= price_open <= price_high:
        errors.append("price_open_outside_bar_range")
    if price_close is not None and price_low is not None and price_high is not None and not price_low <= price_close <= price_high:
        errors.append("price_close_outside_bar_range")
    if cumulative_high is not None and cumulative_low is not None and cumulative_high < cumulative_low:
        errors.append("cumulative_high_below_cumulative_low")
    if price_high is not None and cumulative_high is not None and cumulative_high < price_high:
        errors.append("cumulative_high_below_observed_high")
    if price_low is not None and cumulative_low is not None and cumulative_low > price_low:
        errors.append("cumulative_low_above_observed_low")

    if errors:
        return None, sorted(set(errors))

    parent_event_key = _event_key_from_identity(parent)
    observation_key = _observation_key(parent_event_key, observation_type, observed_close_ms)
    return {
        "source": source,
        "parent": parent,
        "parent_event_key": parent_event_key,
        "observation_key": observation_key,
        "observation_type": observation_type,
        "observed_open_ms": observed_open_ms,
        "observed_close_ms": observed_close_ms,
        "observed_open_at": observed_open_at,
        "observed_close_at": observed_close_at,
        "price_open": price_open,
        "price_high": price_high,
        "price_low": price_low,
        "price_close": price_close,
        "cumulative_high": cumulative_high,
        "cumulative_low": cumulative_low,
        "volume": _as_float(raw.get("volume")),
        "vwap": _as_float(raw.get("vwap")),
        "cmf": _as_float(raw.get("cmf")),
        "relative_volume": _as_float(raw.get("relative_volume")),
        "five_minute_close": _as_float(raw.get("five_minute_close")),
        "five_minute_vwap": _as_float(raw.get("five_minute_vwap")),
    }, []


def _parent_from_row(row: Tuple[Any, ...]) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "event_key": str(row[1]).strip(),
        "source": str(row[2]),
        "action": str(row[3]),
        "direction": row[4],
        "price": float(row[5]) if row[5] is not None else None,
        "support": float(row[6]) if row[6] is not None else None,
        "resistance": float(row[7]) if row[7] is not None else None,
        "range_low": float(row[8]) if row[8] is not None else None,
        "range_high": float(row[9]) if row[9] is not None else None,
        "payload": row[10],
    }


def _find_parent(cursor: Any, parent_event_key: str) -> Optional[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, event_key, source, action, direction, price, support, resistance,
               range_low, range_high, payload
        FROM tm_research_events
        WHERE event_key = %s AND data_quality_tier IN ('official', 'test')
        """,
        (parent_event_key,),
    )
    row = cursor.fetchone()
    return _parent_from_row(row) if row else None


def _invalidation_for_observation(parent: Dict[str, Any], observation: Dict[str, Any]) -> bool:
    action = parent["action"]
    close = observation["price_close"]
    payload = parent["payload"] or {}
    width = _as_float(payload.get("zone_half_width")) or 0.0
    support = parent["support"]
    resistance = parent["resistance"]

    if action in {"ZONE_CALL_CONFIRM", "MINUTE_CALL_CONFIRM", "MINUTE_B_CALL_CONFIRM"}:
        return support is not None and close < support - width
    if action in {"ZONE_PUT_CONFIRM", "MINUTE_PUT_CONFIRM", "MINUTE_B_PUT_CONFIRM"}:
        return resistance is not None and close > resistance + width
    if action == "MINUTE_TREND_BULL":
        five_close = observation.get("five_minute_close")
        five_vwap = observation.get("five_minute_vwap")
        return five_close is not None and five_vwap is not None and five_close < five_vwap
    if action == "MINUTE_TREND_BEAR":
        five_close = observation.get("five_minute_close")
        five_vwap = observation.get("five_minute_vwap")
        return five_close is not None and five_vwap is not None and five_close > five_vwap
    if action == "CLOSE_BREAKOUT_CALL":
        return parent["range_high"] is not None and close <= parent["range_high"]
    if action == "CLOSE_BREAKDOWN_PUT":
        return parent["range_low"] is not None and close >= parent["range_low"]
    return False


def _observation_metrics(parent: Dict[str, Any], observation: Dict[str, Any]) -> Dict[str, Any]:
    entry = parent["price"]
    if entry is None:
        raise ValueError("parent event price is unavailable")

    upside_extension = observation["cumulative_high"] - entry
    downside_extension = entry - observation["cumulative_low"]
    directional = parent["action"] in DIRECTIONAL_ACTIONS and parent["direction"] in {"CALL", "PUT"}
    if not directional:
        return {
            "upside_extension": upside_extension,
            "downside_extension": downside_extension,
            "mfe_dollars": None,
            "mae_dollars": None,
            "target_030_reached": None,
            "target_060_reached": None,
            "invalidation_met": False,
        }

    if parent["direction"] == "CALL":
        mfe = upside_extension
        mae = downside_extension
    else:
        mfe = downside_extension
        mae = upside_extension

    return {
        "upside_extension": upside_extension,
        "downside_extension": downside_extension,
        "mfe_dollars": mfe,
        "mae_dollars": mae,
        "target_030_reached": mfe >= 0.30,
        "target_060_reached": mfe >= 0.60,
        "invalidation_met": _invalidation_for_observation(parent, observation),
    }


def _insert_observation(
    cursor: Any,
    parent: Dict[str, Any],
    observation: Dict[str, Any],
    raw_payload: Dict[str, Any],
    payload_hash: str,
    received_at: datetime,
    reconciliation_mode: str,
) -> Tuple[int, str, Optional[List[str]]]:
    """Insert one legal observation or return duplicate/conflict state."""
    metrics = _observation_metrics(parent, observation)
    observed_close = observation["observed_close_at"]
    target_030_at = observed_close if metrics["target_030_reached"] else None
    target_060_at = observed_close if metrics["target_060_reached"] else None
    same_bar_ambiguity = bool(
        metrics["invalidation_met"] and (metrics["target_030_reached"] or metrics["target_060_reached"])
    )

    cursor.execute(
        f"""
        INSERT INTO {OBSERVATION_TABLE} (
            event_id, observation_key, tracking_version, observation_type,
            observed_bar_open_time, observed_bar_close_time, received_at,
            price_open, price_high, price_low, price_close, volume, vwap, cmf, relative_volume,
            cumulative_high, cumulative_low, upside_extension, downside_extension,
            mfe_dollars, mae_dollars, target_030_reached, target_060_reached,
            target_030_at, target_060_at, inside_zone, invalidation_met,
            same_bar_ambiguity, tracking_payload, payload_hash, reconciliation_mode
        ) VALUES (
            %(event_id)s, %(observation_key)s, %(tracking_version)s, %(observation_type)s,
            %(observed_open_at)s, %(observed_close_at)s, %(received_at)s,
            %(price_open)s, %(price_high)s, %(price_low)s, %(price_close)s, %(volume)s, %(vwap)s, %(cmf)s, %(relative_volume)s,
            %(cumulative_high)s, %(cumulative_low)s, %(upside_extension)s, %(downside_extension)s,
            %(mfe_dollars)s, %(mae_dollars)s, %(target_030_reached)s, %(target_060_reached)s,
            %(target_030_at)s, %(target_060_at)s, %(inside_zone)s, %(invalidation_met)s,
            %(same_bar_ambiguity)s, %(tracking_payload)s, %(payload_hash)s, %(reconciliation_mode)s
        )
        ON CONFLICT (observation_key) DO NOTHING
        RETURNING observation_id
        """,
        {
            "event_id": parent["id"],
            "observation_key": observation["observation_key"],
            "tracking_version": TRACKING_VERSION,
            "observation_type": observation["observation_type"],
            "observed_open_at": observation["observed_open_at"],
            "observed_close_at": observation["observed_close_at"],
            "received_at": received_at,
            "price_open": observation["price_open"],
            "price_high": observation["price_high"],
            "price_low": observation["price_low"],
            "price_close": observation["price_close"],
            "volume": observation["volume"],
            "vwap": observation["vwap"],
            "cmf": observation["cmf"],
            "relative_volume": observation["relative_volume"],
            "cumulative_high": observation["cumulative_high"],
            "cumulative_low": observation["cumulative_low"],
            "upside_extension": metrics["upside_extension"],
            "downside_extension": metrics["downside_extension"],
            "mfe_dollars": metrics["mfe_dollars"],
            "mae_dollars": metrics["mae_dollars"],
            "target_030_reached": metrics["target_030_reached"],
            "target_060_reached": metrics["target_060_reached"],
            "target_030_at": target_030_at,
            "target_060_at": target_060_at,
            "inside_zone": None,
            "invalidation_met": metrics["invalidation_met"],
            "same_bar_ambiguity": same_bar_ambiguity,
            "tracking_payload": Jsonb(raw_payload),
            "payload_hash": payload_hash,
            "reconciliation_mode": reconciliation_mode,
        },
    )
    inserted = cursor.fetchone()
    if inserted is not None:
        return int(inserted[0]), "new", None

    cursor.execute(
        f"SELECT observation_id, payload_hash, tracking_payload FROM {OBSERVATION_TABLE} WHERE observation_key = %s",
        (observation["observation_key"],),
    )
    original = cursor.fetchone()
    if original is None:  # pragma: no cover
        raise RuntimeError("observation conflict resolved without an observation")
    if str(original[1]).strip() == payload_hash:
        return int(original[0]), "duplicate", None
    return int(original[0]), "conflict", _changed_fields(original[2], raw_payload)


def _receipt(
    cursor: Any,
    *,
    event_id: Optional[int],
    observation_id: Optional[int],
    parent_event_key: Optional[str],
    observation_key: Optional[str],
    source: str,
    observation_type: Optional[str],
    observed_bar_close_time: Optional[datetime],
    received_at: datetime,
    payload_hash: str,
    raw_payload: Dict[str, Any],
    receipt_status: str,
    conflict_fields: Optional[List[str]] = None,
    validation_errors: Optional[Iterable[str]] = None,
    orphan_reason: Optional[str] = None,
) -> int:
    cursor.execute(
        f"""
        INSERT INTO {OBSERVATION_RECEIPT_TABLE} (
            event_id, observation_id, parent_event_key, observation_key, source,
            observation_type, observed_bar_close_time, received_at, payload_hash,
            raw_payload, receipt_status, conflict_fields, validation_errors,
            orphan_reason, processor_version
        ) VALUES (
            %(event_id)s, %(observation_id)s, %(parent_event_key)s, %(observation_key)s, %(source)s,
            %(observation_type)s, %(observed_bar_close_time)s, %(received_at)s, %(payload_hash)s,
            %(raw_payload)s, %(receipt_status)s, %(conflict_fields)s, %(validation_errors)s,
            %(orphan_reason)s, %(processor_version)s
        ) RETURNING receipt_id
        """,
        {
            "event_id": event_id,
            "observation_id": observation_id,
            "parent_event_key": parent_event_key,
            "observation_key": observation_key,
            "source": source,
            "observation_type": observation_type,
            "observed_bar_close_time": observed_bar_close_time,
            "received_at": received_at,
            "payload_hash": payload_hash,
            "raw_payload": Jsonb(raw_payload),
            "receipt_status": receipt_status,
            "conflict_fields": Jsonb(conflict_fields) if conflict_fields is not None else None,
            "validation_errors": Jsonb(sorted(set(validation_errors))) if validation_errors is not None else None,
            "orphan_reason": orphan_reason,
            "processor_version": TRACKING_PROCESSOR_VERSION,
        },
    )
    return int(cursor.fetchone()[0])


def _compute_outcome_if_final(cursor: Any, parent: Dict[str, Any], observation_id: int, observation: Dict[str, Any]) -> Optional[int]:
    is_close_censored = observation["observation_type"] == "close_1600" and (
        parent["source"] == "v17" or parent["action"].startswith("CLOSE_")
    )
    if not is_close_censored and observation["observation_type"] != FINAL_OBSERVATION_BY_SOURCE.get(parent["source"]):
        return None

    cursor.execute(
        f"SELECT outcome_id FROM {OUTCOME_TABLE} WHERE event_id = %s AND outcome_version = %s",
        (parent["id"], OUTCOME_VERSION),
    )
    existing = cursor.fetchone()
    if existing:
        return int(existing[0])

    cursor.execute(
        f"""
        SELECT observation_id, observed_bar_close_time, mfe_dollars, mae_dollars,
               upside_extension, downside_extension, target_030_reached, target_060_reached,
               invalidation_met, same_bar_ambiguity, price_close
        FROM {OBSERVATION_TABLE}
        WHERE event_id = %s
        ORDER BY observed_bar_close_time
        """,
        (parent["id"],),
    )
    rows = cursor.fetchall()
    if not rows:
        return None

    final = rows[-1]
    directional = parent["action"] in DIRECTIONAL_ACTIONS and parent["direction"] in {"CALL", "PUT"}
    if not directional:
        status = "non_directional_map"
        target_030 = target_060 = None
        target_030_at = target_060_at = invalidated_at = None
        mfe = mae = None
        ambiguity = False
    else:
        target_030_row = next((row for row in rows if row[6]), None)
        target_060_row = next((row for row in rows if row[7]), None)
        invalidation_row = next((row for row in rows if row[8]), None)
        target_030_at = target_030_row[1] if target_030_row else None
        target_060_at = target_060_row[1] if target_060_row else None
        invalidated_at = invalidation_row[1] if invalidation_row else None
        target_030 = bool(target_030_row)
        target_060 = bool(target_060_row)
        mfe = max(float(row[2]) for row in rows if row[2] is not None)
        mae = max(float(row[3]) for row in rows if row[3] is not None)
        ambiguity = bool(
            any(row[9] for row in rows)
            or (target_030_at is not None and invalidated_at is not None and target_030_at == invalidated_at)
        )
        if is_close_censored:
            status = "censored_at_session_close"
        elif ambiguity:
            status = "ambiguous"
        elif target_030 and (invalidated_at is None or target_030_at < invalidated_at):
            status = "initial_extension"
        elif invalidated_at is not None:
            status = "invalidated"
        else:
            status = "neutral"

    cursor.execute(
        f"""
        INSERT INTO {OUTCOME_TABLE} (
            event_id, outcome_version, final_observation_id, status, direction,
            mfe_dollars, mae_dollars, upside_extension, downside_extension,
            target_030_reached, target_060_reached, target_030_at, target_060_at,
            invalidated_at, same_bar_ambiguity, final_observation_at, final_price,
            classification_reason
        ) VALUES (
            %(event_id)s, %(outcome_version)s, %(final_observation_id)s, %(status)s, %(direction)s,
            %(mfe_dollars)s, %(mae_dollars)s, %(upside_extension)s, %(downside_extension)s,
            %(target_030_reached)s, %(target_060_reached)s, %(target_030_at)s, %(target_060_at)s,
            %(invalidated_at)s, %(same_bar_ambiguity)s, %(final_observation_at)s, %(final_price)s,
            %(classification_reason)s
        ) RETURNING outcome_id
        """,
        {
            "event_id": parent["id"],
            "outcome_version": OUTCOME_VERSION,
            "final_observation_id": observation_id,
            "status": status,
            "direction": parent["direction"] if directional else None,
            "mfe_dollars": mfe,
            "mae_dollars": mae,
            "upside_extension": final[4],
            "downside_extension": final[5],
            "target_030_reached": target_030,
            "target_060_reached": target_060,
            "target_030_at": target_030_at,
            "target_060_at": target_060_at,
            "invalidated_at": invalidated_at,
            "same_bar_ambiguity": ambiguity,
            "final_observation_at": final[1],
            "final_price": final[10],
            "classification_reason": Jsonb(
                {
                    "definition": OUTCOME_VERSION,
                    "directional": directional,
                    "final_observation_type": observation["observation_type"],
                    "window_complete": not is_close_censored,
                    "observation_count": len(rows),
                }
            ),
        },
    )
    return int(cursor.fetchone()[0])


class ResearchOutcomeStore:
    """Receipt-first, fail-open persistence for post-event research observations."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.database_url and psycopg is not None and Jsonb is not None)

    def record_observation(self, source: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "skipped", "reason": "outcome storage is not configured"}

        source = str(source or "").lower()
        raw = _clean_value(payload if isinstance(payload, dict) else {"_invalid_body": str(payload)})
        payload_hash = calculate_payload_hash(raw)
        received_at = datetime.now(timezone.utc)
        observation, errors = _validate_tracking_payload(source, raw)

        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    if observation is None:
                        receipt_id = _receipt(
                            cursor,
                            event_id=None,
                            observation_id=None,
                            parent_event_key=None,
                            observation_key=None,
                            source=source,
                            observation_type=str(raw.get("observation_type") or "") or None,
                            observed_bar_close_time=None,
                            received_at=received_at,
                            payload_hash=payload_hash,
                            raw_payload=raw,
                            receipt_status="invalid",
                            validation_errors=errors,
                        )
                        connection.commit()
                        return {"status": "invalid", "receipt_id": receipt_id, "validation_errors": errors}

                    parent = _find_parent(cursor, observation["parent_event_key"])
                    if parent is None:
                        receipt_id = _receipt(
                            cursor,
                            event_id=None,
                            observation_id=None,
                            parent_event_key=observation["parent_event_key"],
                            observation_key=observation["observation_key"],
                            source=source,
                            observation_type=observation["observation_type"],
                            observed_bar_close_time=observation["observed_close_at"],
                            received_at=received_at,
                            payload_hash=payload_hash,
                            raw_payload=raw,
                            receipt_status="orphan",
                            orphan_reason="parent_event_not_found",
                        )
                        connection.commit()
                        return {"status": "orphan", "receipt_id": receipt_id, "parent_event_key": observation["parent_event_key"]}

                    observation_id, receipt_status, conflict_fields = _insert_observation(
                        cursor, parent, observation, raw, payload_hash, received_at, "direct"
                    )
                    receipt_id = _receipt(
                        cursor,
                        event_id=parent["id"],
                        observation_id=observation_id,
                        parent_event_key=observation["parent_event_key"],
                        observation_key=observation["observation_key"],
                        source=source,
                        observation_type=observation["observation_type"],
                        observed_bar_close_time=observation["observed_close_at"],
                        received_at=received_at,
                        payload_hash=payload_hash,
                        raw_payload=raw,
                        receipt_status=receipt_status,
                        conflict_fields=conflict_fields,
                    )
                    outcome_id = None
                    if receipt_status == "new":
                        outcome_id = _compute_outcome_if_final(cursor, parent, observation_id, observation)
                    connection.commit()
                    return {
                        "status": receipt_status,
                        "receipt_id": receipt_id,
                        "event_id": parent["id"],
                        "observation_id": observation_id,
                        "outcome_id": outcome_id,
                    }
        except Exception as exc:  # fail open: primary Telegram behavior remains available
            LOGGER.exception("Outcome tracking storage failed for %s", source)
            return {"status": "failed", "reason": str(exc)}

    def reconcile_orphans(self, parent_event_key: str) -> Dict[str, int]:
        """Link pre-parent orphan receipts only when their exact parent arrives."""
        if not self.enabled or not parent_event_key:
            return {"reconciled": 0, "skipped": 0}

        reconciled = 0
        skipped = 0
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    parent = _find_parent(cursor, parent_event_key)
                    if parent is None:
                        return {"reconciled": 0, "skipped": 0}
                    cursor.execute(
                        f"""
                        SELECT r.receipt_id, r.raw_payload, r.payload_hash, r.received_at
                        FROM {OBSERVATION_RECEIPT_TABLE} r
                        LEFT JOIN {ORPHAN_RECONCILIATION_TABLE} x ON x.orphan_receipt_id = r.receipt_id
                        WHERE r.parent_event_key = %s
                          AND r.receipt_status = 'orphan'
                          AND x.orphan_receipt_id IS NULL
                        ORDER BY r.receipt_id
                        """,
                        (parent_event_key,),
                    )
                    orphans = cursor.fetchall()
                    for orphan_receipt_id, raw_payload, payload_hash, received_at in orphans:
                        observation, errors = _validate_tracking_payload(parent["source"], raw_payload)
                        if observation is None or observation["parent_event_key"] != parent_event_key:
                            skipped += 1
                            continue
                        observation_id, receipt_status, _ = _insert_observation(
                            cursor, parent, observation, raw_payload, payload_hash, received_at, "reconciled"
                        )
                        cursor.execute(
                            f"""
                            INSERT INTO {ORPHAN_RECONCILIATION_TABLE} (
                                orphan_receipt_id, event_id, observation_id, reconciliation_method
                            ) VALUES (%s, %s, %s, 'parent_arrival')
                            """,
                            (orphan_receipt_id, parent["id"], observation_id),
                        )
                        if receipt_status == "new":
                            _compute_outcome_if_final(cursor, parent, observation_id, observation)
                        reconciled += 1
                connection.commit()
        except Exception:
            LOGGER.exception("Outcome orphan reconciliation failed for parent key %s", parent_event_key)
            return {"reconciled": reconciled, "skipped": skipped + 1}
        return {"reconciled": reconciled, "skipped": skipped}

    def healthcheck(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "unavailable", "reason": "outcome tracking is not configured"}
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT to_regclass('public.event_observations'),
                               to_regclass('public.event_observation_receipts'),
                               to_regclass('public.event_outcomes')
                        """
                    )
                    tables = cursor.fetchone()
            if all(tables):
                return {"status": "ok", "tracking_version": TRACKING_VERSION, "outcome_version": OUTCOME_VERSION}
            return {"status": "unavailable", "reason": "outcome tracking schema is incomplete"}
        except Exception as exc:
            LOGGER.exception("Outcome tracking healthcheck failed")
            return {"status": "failed", "reason": str(exc)}
