"""Acceptance test for TM Sniper research-event storage.

The script intentionally keeps one clearly labeled test event in the empty new
research table. This is non-market data and demonstrates that a replay updates
``duplicate_count`` instead of creating a second record.
"""

import os
import sys

import psycopg

from event_store import ResearchEventStore


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is missing")
        return 2

    payload = {
        "action": "STORAGE_ACCEPTANCE_TEST",
        "event_id": "acceptance-2026-09-15-001",
        "event_date": "2026-09-15",
        "price": 360.12,
        "vwap": 359.81,
        "cmf": -0.08,
        "mom": -0.96,
        "macd": 0.147,
        "macd_signal": 0.279,
        "macd_histogram": -0.263,
        "rsi": 51.93,
        "five_minute_bias": "mixed",
    }
    store = ResearchEventStore()
    first = store.record_event("v18", payload)
    second = store.record_event("v18", payload)
    if first.get("status") != "stored":
        print({"first": first, "second": second})
        return 1
    if second.get("status") != "duplicate" or second.get("duplicate_count") != 1:
        print({"first": first, "second": second})
        return 1

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*), max(duplicate_count) FROM tm_research_events WHERE event_key = %s",
                (first["event_key"],),
            )
            row_count, duplicate_count = cursor.fetchone()
    if row_count != 1 or duplicate_count != 1:
        print({"row_count": row_count, "duplicate_count": duplicate_count})
        return 1

    print({
        "status": "accepted",
        "database_rows_for_test_event": row_count,
        "duplicate_count": duplicate_count,
        "first_write": first["status"],
        "replay": second["status"],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
