"""Safe acceptance test: storage survives a simulated Telegram failure.

The Telegram sender is a local function that always returns ``False``.  No
network call is made to Telegram.  The script requires an explicitly supplied
DATABASE_URL and stores only a clearly labelled ``test`` event.
"""

from __future__ import annotations

import os
import sys
import uuid

import psycopg

import app_v17_update
from event_store import ResearchEventStore


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is missing")
        return 2

    marker = f"telegram-failure-{uuid.uuid4()}"
    payload = {
        "schema_version": "event-v2",
        "symbol": "TSLA",
        "timeframe": "5m",
        "bar_open_time": "1789565400000",
        "bar_close_time": "1789565700000",
        "action": "MAP_45",
        "price": 360.12,
        "vwap": 359.81,
        "support": 359.39,
        "resistance": 361.90,
        "zone_half_width": 0.10,
        "cmf": 0.08,
        "relative_volume": 1.05,
        "hist": 0.05,
        "momentum": 0.96,
        "adx": 24.0,
        "bull_score": 2,
        "bear_score": 0,
        "close_above_vwap": True,
        "acceptance_test_marker": marker,
    }

    # Isolate module-level anti-spam state so no test state reaches production.
    original_state = dict(app_v17_update.v17_state)
    try:
        app_v17_update.v17_state.update(
            {"today": "", "sent_actions": set(), "zone_alert_sent": False, "exit_900_sent": False, "exit_925_sent": False}
        )
        stored = ResearchEventStore().record_event("v17", payload, data_quality_tier="test")
        telegram_result = app_v17_update.process_v17_webhook(payload, lambda _message: False)
    finally:
        app_v17_update.v17_state.clear()
        app_v17_update.v17_state.update(original_state)

    if stored.get("status") != "new" or telegram_result.get("status") != "failed":
        print({"status": "failed", "stored": stored, "telegram": telegram_result})
        return 1

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT e.data_quality_tier, r.receipt_status
                FROM tm_research_events e
                JOIN webhook_receipts r ON r.event_id = e.id
                WHERE e.id = %s
                """,
                (stored["event_id"],),
            )
            row = cursor.fetchone()

    if row != ("test", "new"):
        print({"status": "failed", "storage_row": row, "stored": stored})
        return 1

    print(
        {
            "status": "accepted",
            "storage": "new event and receipt persisted",
            "telegram": "simulated failure returned failed without a Telegram network call",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
