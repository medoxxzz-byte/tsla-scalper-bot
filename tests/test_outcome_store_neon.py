"""Phase-B acceptance tests for append-only TM Sniper outcome tracking.

Run only against a prepared temporary Neon branch or the live database after
explicit approval.  The test adds clearly labelled ``test`` events and never
modifies existing observations or research events.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from event_store import ResearchEventStore
from outcome_store import ResearchOutcomeStore


def parent_payload(marker: str, open_time: int) -> dict:
    return {
        "schema_version": "event-v2",
        "symbol": "TSLA",
        "timeframe": "1m",
        "bar_open_time": str(open_time),
        "bar_close_time": str(open_time + 60_000),
        "action": "MINUTE_CALL_CONFIRM",
        "price": 360.12,
        "vwap": 359.90,
        "support": 359.39,
        "resistance": 361.90,
        "zone_half_width": 0.10,
        "cmf": 0.08,
        "momentum": 0.96,
        "rsi": 58.2,
        "five_minute_bias": "bull",
        "acceptance_test_marker": marker,
    }


def tracking_payload(parent: dict, observation_type: str, close_offset_ms: int, **overrides) -> dict:
    parent_close = int(parent["bar_close_time"])
    close_time = parent_close + close_offset_ms
    payload = {
        "tracking_version": "tracking-v1",
        "parent_schema_version": parent["schema_version"],
        "parent_symbol": parent["symbol"],
        "parent_timeframe": parent["timeframe"],
        "parent_bar_open_time": parent["bar_open_time"],
        "parent_bar_close_time": parent["bar_close_time"],
        "parent_action": parent["action"],
        "parent_support": parent["support"],
        "parent_resistance": parent["resistance"],
        "parent_range_low": parent.get("range_low"),
        "parent_range_high": parent.get("range_high"),
        "parent_zone_half_width": parent["zone_half_width"],
        "observation_type": observation_type,
        "observed_bar_open_time": str(close_time - 60_000),
        "observed_bar_close_time": str(close_time),
        "price_open": 360.12,
        "price_high": 360.52,
        "price_low": 360.05,
        "price_close": 360.48,
        "volume": 100000,
        "vwap": 360.20,
        "cmf": 0.12,
        "relative_volume": 1.10,
        "five_minute_close": 360.48,
        "five_minute_vwap": 360.20,
        "cumulative_high": 360.52,
        "cumulative_low": 360.05,
    }
    payload.update(overrides)
    return payload


def scalar(cursor, sql, params):
    cursor.execute(sql, params)
    return cursor.fetchone()[0]


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is missing")
        return 2

    marker = f"phase-b-{uuid.uuid4()}"
    # Test timestamps are intentionally far from market time and uniquely separated.
    base = 1_800_000_000_000 + (uuid.uuid4().int % 50_000) * 60_000
    parent = parent_payload(marker, base)
    event_store = ResearchEventStore()
    outcome_store = ResearchOutcomeStore()

    stored_parent = event_store.record_event("v18", parent, data_quality_tier="test")
    if stored_parent.get("status") != "new":
        print({"status": "failed", "reason": "parent_not_new", "stored_parent": stored_parent})
        return 1

    post_3 = tracking_payload(parent, "post_3m", 180_000)
    first = outcome_store.record_observation("v18", post_3)
    duplicate = outcome_store.record_observation("v18", dict(post_3))
    conflict = outcome_store.record_observation(
        "v18", dict(post_3, price_high=360.70, price_close=360.66, cumulative_high=360.70)
    )
    invalid = outcome_store.record_observation(
        "v18", tracking_payload(parent, "post_6m", 180_000)
    )
    post_6 = outcome_store.record_observation(
        "v18", tracking_payload(
            parent, "post_6m", 360_000,
            price_high=360.68, price_close=360.60, cumulative_high=360.68,
        )
    )
    post_12 = outcome_store.record_observation(
        "v18", tracking_payload(
            parent, "post_12m", 720_000,
            price_high=360.84, price_close=360.78, cumulative_high=360.84,
        )
    )

    orphan_parent = parent_payload(f"{marker}-orphan", base + 3_600_000)
    orphan = outcome_store.record_observation(
        "v18", tracking_payload(orphan_parent, "post_3m", 180_000)
    )
    stored_orphan_parent = event_store.record_event("v18", orphan_parent, data_quality_tier="test")
    reconciled = outcome_store.reconcile_orphans(stored_orphan_parent.get("event_key", ""))

    closing_date = datetime(2028, 1, 3, 15, 54, tzinfo=ZoneInfo("America/New_York")) + timedelta(
        days=uuid.uuid4().int % 10000
    )
    closing_date = closing_date.replace(hour=15, minute=54, second=0, microsecond=0)
    closing_open_ms = int(closing_date.timestamp() * 1000)
    closing_close_ms = closing_open_ms + 60_000
    regular_close_ms = closing_open_ms + 6 * 60_000
    closing_parent = parent_payload(f"{marker}-closing", closing_open_ms)
    closing_parent.update(
        {
            "bar_close_time": str(closing_close_ms),
            "action": "CLOSE_BREAKOUT_CALL",
            "range_low": 359.39,
            "range_high": 361.90,
        }
    )
    stored_closing_parent = event_store.record_event("v18", closing_parent, data_quality_tier="test")
    closing_observation = outcome_store.record_observation(
        "v18",
        tracking_payload(
            closing_parent,
            "close_1600",
            300_000,
            observed_bar_open_time=str(regular_close_ms - 60_000),
            observed_bar_close_time=str(regular_close_ms),
            price_high=360.84,
            price_close=360.78,
            cumulative_high=360.84,
        ),
    )

    statuses_ok = (
        first.get("status") == "new"
        and duplicate.get("status") == "duplicate"
        and conflict.get("status") == "conflict"
        and invalid.get("status") == "invalid"
        and post_6.get("status") == "new"
        and post_12.get("status") == "new"
        and orphan.get("status") == "orphan"
        and stored_orphan_parent.get("status") == "new"
        and reconciled.get("reconciled") == 1
        and stored_closing_parent.get("status") == "new"
        and closing_observation.get("status") == "new"
    )
    if not statuses_ok:
        print(
            {
                "status": "failed",
                "first": first,
                "duplicate": duplicate,
                "conflict": conflict,
                "invalid": invalid,
                "post_6": post_6,
                "post_12": post_12,
                "orphan": orphan,
                "stored_orphan_parent": stored_orphan_parent,
                "reconciled": reconciled,
            }
        )
        return 1

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            receipt_statuses = []
            cursor.execute(
                """
                SELECT receipt_status
                FROM event_observation_receipts
                WHERE event_id = %s
                ORDER BY receipt_id
                """,
                (stored_parent["event_id"],),
            )
            receipt_statuses = [row[0] for row in cursor.fetchall()]
            conflict_fields = scalar(
                cursor,
                "SELECT conflict_fields FROM event_observation_receipts WHERE receipt_id = %s",
                (conflict["receipt_id"],),
            )
            outcome = scalar(
                cursor,
                "SELECT status FROM event_outcomes WHERE event_id = %s AND outcome_version = 'outcome-v1'",
                (stored_parent["event_id"],),
            )
            direct_observations = scalar(
                cursor,
                "SELECT count(*) FROM event_observations WHERE event_id = %s",
                (stored_parent["event_id"],),
            )
            orphan_receipt_status = scalar(
                cursor,
                "SELECT receipt_status FROM event_observation_receipts WHERE receipt_id = %s",
                (orphan["receipt_id"],),
            )
            reconciliation_mode = scalar(
                cursor,
                "SELECT reconciliation_mode FROM event_observations WHERE event_id = %s",
                (stored_orphan_parent["event_id"],),
            )
            reconciliation_count = scalar(
                cursor,
                "SELECT count(*) FROM event_observation_orphan_reconciliations WHERE orphan_receipt_id = %s",
                (orphan["receipt_id"],),
            )
            closing_outcome = scalar(
                cursor,
                "SELECT status FROM event_outcomes WHERE event_id = %s AND outcome_version = 'outcome-v1'",
                (stored_closing_parent["event_id"],),
            )

    checks = {
        "direct_receipt_sequence": receipt_statuses == ["new", "duplicate", "conflict", "new", "new"],
        "conflict_fields_retained": "price_high" in conflict_fields and "cumulative_high" in conflict_fields,
        "three_direct_observations_only": direct_observations == 3,
        "final_outcome_created": outcome == "initial_extension",
        "orphan_preserved": orphan_receipt_status == "orphan",
        "orphan_reconciled_only_after_exact_parent": reconciliation_mode == "reconciled" and reconciliation_count == 1,
        "closing_result_is_censored_not_after_hours": closing_outcome == "censored_at_session_close",
    }
    if not all(checks.values()):
        print({"status": "failed", "checks": checks, "receipt_statuses": receipt_statuses})
        return 1

    print({"status": "accepted", "checks": checks})
    return 0


if __name__ == "__main__":
    sys.exit(main())
