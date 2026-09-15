import unittest
from datetime import datetime, timezone

from event_store import build_event_key, classify_direction


class EventStoreIdentityTests(unittest.TestCase):
    def setUp(self):
        self.received_at = datetime(2026, 9, 16, 15, 31, tzinfo=timezone.utc)
        self.payload = {
            "action": "CLOSE_BREAKDOWN_PUT",
            "price": 355.45,
            "range_low": 355.60,
            "range_high": 356.69,
            "five_minute_bias": "bear",
        }

    def test_same_payload_has_same_identity(self):
        first = build_event_key("v18", self.payload, self.received_at)
        second = build_event_key("v18", dict(self.payload), self.received_at)
        self.assertEqual(first, second)

    def test_changed_structural_level_is_a_new_event(self):
        changed = dict(self.payload)
        changed["range_low"] = 355.50
        self.assertNotEqual(
            build_event_key("v18", self.payload, self.received_at),
            build_event_key("v18", changed, self.received_at),
        )

    def test_explicit_bar_time_is_stable_even_if_price_field_changes(self):
        first = dict(self.payload, bar_time="2026-09-16T15:31:00-04:00")
        changed = dict(first, price=355.20)
        self.assertEqual(
            build_event_key("v18", first, self.received_at),
            build_event_key("v18", changed, self.received_at),
        )

    def test_direction_classification(self):
        self.assertEqual(classify_direction("MINUTE_TREND_BULL"), "CALL")
        self.assertEqual(classify_direction("ZONE_PUT_CONFIRM"), "PUT")
        self.assertEqual(classify_direction("MAP_45"), None)


if __name__ == "__main__":
    unittest.main()
