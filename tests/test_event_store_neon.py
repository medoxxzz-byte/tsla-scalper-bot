"""Phase-A acceptance test for immutable TM Sniper event receipts.

Run only against the prepared Neon temporary branch or the live database after
explicit approval.  It deliberately leaves clearly labelled ``test`` rows rather
than deleting research history.
"""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

from event_store import ResearchEventStore


def payload_for(marker: str, bar_open_time: int) -> dict:
    return {
        "schema_version": "event-v2",
        "symbol": "TSLA",
        "timeframe": "1m",
        "bar_open_time": str(bar_open_time),
        "bar_close_time": str(bar_open_time + 60_000),
        "action": "MINUTE_TREND_BULL",
        "price": 360.12,
        "vwap": 359.81,
        "support": 359.39,
        "resistance": 361.90,
        "zone_half_width": 0.10,
        "cmf": 0.08,
        "momentum": 0.96,
        "rsi": 58.2,
        "five_minute_bias": "bull",
        "acceptance_test_marker": marker,
    }


def scalar(cursor, sql, params):
    cursor.execute(sql, params)
    return cursor.fetchone()[0]


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is missing")
        return 2

    marker = f"phase-a-{uuid.uuid4()}"
    payload = payload_for(marker, 1_789_565_460_000)
    store = ResearchEventStore()

    first = store.record_event("v18", payload, data_quality_tier="test")
    replay = store.record_event("v18", dict(payload), data_quality_tier="test")
    conflict_payload = dict(payload, cmf=0.22, momentum=1.31)
    conflict = store.record_event("v18", conflict_payload, data_quality_tier="test")
    invalid_payload = dict(payload)
    del invalid_payload["bar_close_time"]
    invalid = store.record_event("v18", invalid_payload, data_quality_tier="test")

    concurrent_payload = payload_for(f"{marker}-concurrent", 1_789_565_520_000)
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = list(
            executor.map(
                lambda _: ResearchEventStore().record_event("v18", dict(concurrent_payload), data_quality_tier="test"),
                range(2),
            )
        )

    expected_statuses = {
        "first": first.get("status"),
        "replay": replay.get("status"),
        "conflict": conflict.get("status"),
        "invalid": invalid.get("status"),
        "concurrent": sorted(item.get("status") for item in concurrent_results),
    }
    required_statuses = (
        expected_statuses["first"] == "new"
        and expected_statuses["replay"] == "duplicate"
        and expected_statuses["conflict"] == "conflict"
        and expected_statuses["invalid"] == "invalid"
        and expected_statuses["concurrent"] == ["duplicate", "new"]
    )
    if not required_statuses:
        print({"status": "failed", "statuses": expected_statuses})
        return 1

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            event_count = scalar(
                cursor,
                "SELECT count(*) FROM tm_research_events WHERE payload ->> 'acceptance_test_marker' = %s",
                (marker,),
            )
            original_cmf = scalar(
                cursor,
                "SELECT cmf FROM tm_research_events WHERE id = %s",
                (first["event_id"],),
            )
            receipt_statuses = []
            cursor.execute(
                "SELECT receipt_status FROM webhook_receipts WHERE event_id = %s ORDER BY receipt_id",
                (first["event_id"],),
            )
            receipt_statuses = [row[0] for row in cursor.fetchall()]
            conflict_fields = scalar(
                cursor,
                "SELECT conflict_fields FROM webhook_receipts WHERE receipt_id = %s",
                (conflict["receipt_id"],),
            )
            invalid_count = scalar(
                cursor,
                "SELECT count(*) FROM webhook_receipts WHERE raw_payload ->> 'acceptance_test_marker' = %s AND receipt_status = 'invalid'",
                (marker,),
            )
            concurrent_event_count = scalar(
                cursor,
                "SELECT count(*) FROM tm_research_events WHERE payload ->> 'acceptance_test_marker' = %s",
                (f"{marker}-concurrent",),
            )
            concurrent_receipt_count = scalar(
                cursor,
                "SELECT count(*) FROM webhook_receipts WHERE raw_payload ->> 'acceptance_test_marker' = %s",
                (f"{marker}-concurrent",),
            )

    checks = {
        "one_original_event": event_count == 1,
        "original_unchanged_after_conflict": float(original_cmf) == 0.08,
        "receipt_sequence": receipt_statuses == ["new", "duplicate", "conflict"],
        "conflict_fields_retained": "cmf" in conflict_fields and "momentum" in conflict_fields,
        "invalid_has_receipt_only": invalid_count == 1,
        "concurrent_one_event_two_receipts": concurrent_event_count == 1 and concurrent_receipt_count == 2,
    }
    if not all(checks.values()):
        print({"status": "failed", "checks": checks, "statuses": expected_statuses})
        return 1

    print({"status": "accepted", "checks": checks, "statuses": expected_statuses})
    return 0


if __name__ == "__main__":
    sys.exit(main())
