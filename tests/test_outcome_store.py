import unittest

from outcome_store import (
    _observation_key,
    _observation_metrics,
    _validate_tracking_payload,
)


BASE_PARENT = {
    "parent_schema_version": "event-v2",
    "parent_symbol": "TSLA",
    "parent_timeframe": "1m",
    "parent_bar_open_time": "1789565400000",
    "parent_bar_close_time": "1789565460000",
    "parent_action": "MINUTE_CALL_CONFIRM",
    "parent_support": 359.39,
    "parent_resistance": 361.90,
    "parent_zone_half_width": 0.10,
}


def tracking_payload(**overrides):
    payload = {
        "tracking_version": "tracking-v1",
        **BASE_PARENT,
        "observation_type": "post_3m",
        "observed_bar_open_time": "1789565580000",
        "observed_bar_close_time": "1789565640000",
        "price_open": 360.12,
        "price_high": 360.52,
        "price_low": 360.05,
        "price_close": 360.48,
        "cumulative_high": 360.52,
        "cumulative_low": 360.05,
        "volume": 100000,
        "vwap": 360.20,
        "cmf": 0.12,
        "relative_volume": 1.10,
    }
    payload.update(overrides)
    return payload


class OutcomeStoreValidationTests(unittest.TestCase):
    def test_valid_v18_post_3m_has_exact_parent_time_offset(self):
        observation, errors = _validate_tracking_payload("v18", tracking_payload())
        self.assertEqual(errors, [])
        self.assertEqual(observation["observation_type"], "post_3m")
        self.assertEqual(observation["observed_close_ms"] - observation["parent"]["bar_close_time_ms"], 180000)

    def test_wrong_observation_offset_is_invalid(self):
        observation, errors = _validate_tracking_payload(
            "v18",
            tracking_payload(observed_bar_open_time="1789565640000", observed_bar_close_time="1789565700000"),
        )
        self.assertIsNone(observation)
        self.assertIn("observation_offset_does_not_match_type", errors)

    def test_missing_parent_identity_never_uses_nearest_event(self):
        payload = tracking_payload()
        del payload["parent_bar_close_time"]
        observation, errors = _validate_tracking_payload("v18", payload)
        self.assertIsNone(observation)
        self.assertIn("parent_missing_or_invalid_bar_close_time", errors)

    def test_same_point_has_same_key_despite_measurement_changes(self):
        first, errors = _validate_tracking_payload("v18", tracking_payload())
        self.assertEqual(errors, [])
        changed, errors = _validate_tracking_payload("v18", tracking_payload(price_high=360.70, cumulative_high=360.70))
        self.assertEqual(errors, [])
        self.assertEqual(first["observation_key"], changed["observation_key"])

    def test_directional_metrics_measure_mfe_and_mae_from_entry(self):
        observation, errors = _validate_tracking_payload("v18", tracking_payload())
        self.assertEqual(errors, [])
        parent = {
            "action": "MINUTE_CALL_CONFIRM",
            "direction": "CALL",
            "price": 360.12,
            "payload": {"zone_half_width": 0.10},
            "support": 359.39,
            "resistance": 361.90,
            "range_low": None,
            "range_high": None,
        }
        metrics = _observation_metrics(parent, observation)
        self.assertAlmostEqual(metrics["mfe_dollars"], 0.40)
        self.assertAlmostEqual(metrics["mae_dollars"], 0.07)
        self.assertTrue(metrics["target_030_reached"])
        self.assertFalse(metrics["target_060_reached"])

    def test_non_directional_maps_keep_both_extensions_without_a_false_direction(self):
        observation, errors = _validate_tracking_payload("v18", tracking_payload())
        self.assertEqual(errors, [])
        parent = {
            "action": "MINUTE_MAP",
            "direction": None,
            "price": 360.12,
            "payload": {},
            "support": 359.39,
            "resistance": 361.90,
            "range_low": None,
            "range_high": None,
        }
        metrics = _observation_metrics(parent, observation)
        self.assertIsNone(metrics["mfe_dollars"])
        self.assertIsNone(metrics["target_030_reached"])
        self.assertAlmostEqual(metrics["upside_extension"], 0.40)
        self.assertAlmostEqual(metrics["downside_extension"], 0.07)

    def test_observation_key_binds_parent_type_and_bar(self):
        key = _observation_key("a" * 64, "post_3m", 1789565640000)
        self.assertNotEqual(key, _observation_key("a" * 64, "post_6m", 1789565640000))
        self.assertNotEqual(key, _observation_key("b" * 64, "post_3m", 1789565640000))
        self.assertNotEqual(key, _observation_key("a" * 64, "post_3m", 1789565700000))

    def test_closing_signal_uses_session_close_censoring_not_after_hours_prices(self):
        observation, errors = _validate_tracking_payload(
            "v18",
            tracking_payload(
                parent_bar_open_time="1790106840000",
                parent_bar_close_time="1790106900000",
                parent_action="CLOSE_BREAKOUT_CALL",
                parent_range_low=359.39,
                parent_range_high=361.90,
                observation_type="close_1600",
                observed_bar_open_time="1790107140000",
                observed_bar_close_time="1790107200000",
            ),
        )
        self.assertEqual(errors, [])
        self.assertEqual(observation["observation_type"], "close_1600")

    def test_session_close_censoring_rejects_nonclosing_actions(self):
        observation, errors = _validate_tracking_payload(
            "v18",
            tracking_payload(
                observation_type="close_1600",
                observed_bar_open_time="1790107140000",
                observed_bar_close_time="1790107200000",
            ),
        )
        self.assertIsNone(observation)
        self.assertIn("close_1600_requires_closing_action", errors)

    def test_late_v17_zone_can_be_censored_at_regular_session_close(self):
        observation, errors = _validate_tracking_payload(
            "v17",
            tracking_payload(
                parent_timeframe="5m",
                parent_bar_open_time="1790106600000",
                parent_bar_close_time="1790106900000",
                parent_action="ZONE_CALL_CONFIRM",
                observation_type="close_1600",
                observed_bar_open_time="1790106900000",
                observed_bar_close_time="1790107200000",
            ),
        )
        self.assertEqual(errors, [])
        self.assertEqual(observation["observation_type"], "close_1600")


if __name__ == "__main__":
    unittest.main()
