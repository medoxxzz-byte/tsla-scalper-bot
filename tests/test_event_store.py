import unittest
from datetime import datetime, timezone

from event_store import (
    build_event_key,
    calculate_payload_hash,
    canonical_zone_bounds,
    classify_direction,
    validate_official_identity,
)


class EventStoreIdentityTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "schema_version": "event-v2",
            "symbol": "TSLA",
            "timeframe": "1m",
            "bar_open_time": "1789565460000",
            "bar_close_time": "1789565520000",
            "action": "CLOSE_BREAKDOWN_PUT",
            "price": 355.45,
            "range_low": 355.60,
            "range_high": 356.69,
            "cmf": -0.08,
            "momentum": -0.96,
            "five_minute_bias": "bear",
        }

    def test_same_official_payload_has_same_identity(self):
        self.assertEqual(build_event_key("v18", self.payload), build_event_key("v18", dict(self.payload)))

    def test_current_price_and_indicators_do_not_change_identity(self):
        changed = dict(self.payload, price=355.20, cmf=-0.30, momentum=-1.21)
        self.assertEqual(build_event_key("v18", self.payload), build_event_key("v18", changed))

    def test_zone_bounds_change_identity(self):
        changed = dict(self.payload, range_low=355.50)
        self.assertNotEqual(build_event_key("v18", self.payload), build_event_key("v18", changed))

    def test_different_bar_close_changes_identity(self):
        changed = dict(
            self.payload,
            bar_open_time="1789565520000",
            bar_close_time="1789565580000",
        )
        self.assertNotEqual(build_event_key("v18", self.payload), build_event_key("v18", changed))

    def test_missing_identity_is_invalid_not_fallback_key(self):
        missing = dict(self.payload)
        del missing["bar_close_time"]
        identity, errors = validate_official_identity("v18", missing)
        self.assertIsNone(identity)
        self.assertIn("missing_or_invalid_bar_close_time", errors)
        with self.assertRaises(ValueError):
            build_event_key("v18", missing)

    def test_source_timeframe_mismatch_is_invalid(self):
        mismatched = dict(self.payload, timeframe="5m")
        identity, errors = validate_official_identity("v18", mismatched)
        self.assertIsNone(identity)
        self.assertIn("source_timeframe_mismatch", errors)

    def test_acceptance_action_requires_explicit_test_marker(self):
        test_payload = dict(
            self.payload,
            action="STORAGE_ACCEPTANCE_TEST",
            timeframe="5m",
            bar_open_time="1789565400000",
            bar_close_time="1789565700000",
        )
        identity, errors = validate_official_identity("v17", test_payload)
        self.assertIsNone(identity)
        self.assertIn("test_action_not_authorized", errors)

        allowed_payload = dict(test_payload, _research_acceptance_test=True)
        identity, errors = validate_official_identity("v17", allowed_payload)
        self.assertIsNotNone(identity)
        self.assertEqual(errors, [])

    def test_payload_hash_is_order_independent_but_value_sensitive(self):
        reordered = {key: self.payload[key] for key in reversed(self.payload)}
        changed = dict(self.payload, cmf=-0.09)
        self.assertEqual(calculate_payload_hash(self.payload), calculate_payload_hash(reordered))
        self.assertNotEqual(calculate_payload_hash(self.payload), calculate_payload_hash(changed))

    def test_canonical_call_zone_uses_support_and_width(self):
        call_payload = {
            "support": 356.54,
            "resistance": 360.23,
            "zone_half_width": 0.12,
        }
        self.assertEqual(
            canonical_zone_bounds(call_payload, "MINUTE_CALL_CONFIRM"),
            {"zone_low": "356.4200", "zone_high": "356.6600"},
        )

    def test_direction_classification(self):
        self.assertEqual(classify_direction("MINUTE_TREND_BULL"), "CALL")
        self.assertEqual(classify_direction("ZONE_PUT_CONFIRM"), "PUT")
        self.assertIsNone(classify_direction("MAP_45"))


if __name__ == "__main__":
    unittest.main()
