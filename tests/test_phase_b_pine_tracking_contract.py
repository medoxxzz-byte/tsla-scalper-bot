import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V17 = (ROOT / "tm_reversal_map_v17.pine").read_text(encoding="utf-8")
V18 = (ROOT / "tm_reversal_experiments_v18.pine").read_text(encoding="utf-8")


class PhaseBPineTrackingContractTests(unittest.TestCase):
    def test_v17_has_exact_5_10_15_minute_tracking_points(self):
        for marker in (
            '"tracking_version":"tracking-v1"',
            '"parent_timeframe":"5m"',
            'alert(f_trackingPayload(index, "post_5m")',
            'alert(f_trackingPayload(index, "post_10m")',
            'alert(f_trackingPayload(index, "post_15m")',
            'elapsedMs == 5 * 60 * 1000',
            'elapsedMs == 10 * 60 * 1000',
            'elapsedMs == 15 * 60 * 1000',
        ):
            self.assertIn(marker, V17)

    def test_v18_has_exact_3_6_12_minute_tracking_points(self):
        for marker in (
            '"tracking_version":"tracking-v1"',
            '"parent_timeframe":"1m"',
            'alert(f_trackingPayload(index, "post_3m")',
            'alert(f_trackingPayload(index, "post_6m")',
            'alert(f_trackingPayload(index, "post_12m")',
            'elapsedMs == 3 * 60 * 1000',
            'elapsedMs == 6 * 60 * 1000',
            'elapsedMs == 12 * 60 * 1000',
        ):
            self.assertIn(marker, V18)

    def test_v18_closing_experiment_censors_at_regular_session_close(self):
        for marker in (
            'closingParent = str.startswith(array.get(trackActions, index), "CLOSE_")',
            "atRegularSessionClose = nyHour == 15 and nyMin == 59",
            "finalDueAt = array.get(trackCloseTimes, index) + 12 * 60 * 1000",
            "needsSessionCensor = time_close <= finalDueAt",
            'alert(f_trackingPayload(index, "close_1600")',
            "if closingParent and atRegularSessionClose and needsSessionCensor",
        ):
            self.assertIn(marker, V18)

    def test_v17_late_zone_tracking_censors_at_regular_session_close(self):
        for marker in (
            "atRegularSessionClose = nyHour == 15 and nyMin == 55",
            "finalDueAt = array.get(trackCloseTimes, index) + 15 * 60 * 1000",
            "needsSessionCensor = time_close <= finalDueAt",
            'alert(f_trackingPayload(index, "close_1600")',
            "if atRegularSessionClose and needsSessionCensor",
        ):
            self.assertIn(marker, V17)

    def test_tracking_payloads_include_full_parent_identity_and_ohlcv(self):
        required_fields = (
            '"parent_schema_version":"event-v2"',
            '"parent_symbol":"',
            '"parent_bar_open_time":',
            '"parent_bar_close_time":',
            '"parent_action":"',
            '"observation_type":"',
            '"observed_bar_open_time":',
            '"observed_bar_close_time":',
            '"price_open":',
            '"price_high":',
            '"price_low":',
            '"price_close":',
            '"cumulative_high":',
            '"cumulative_low":',
        )
        for source in (V17, V18):
            for field in required_fields:
                self.assertIn(field, source)

    def test_tracking_never_reuses_telegram_formatter(self):
        for source in (V17, V18):
            tracking_section = source[source.index("// ── Phase B:"):]
            self.assertNotIn("send_telegram", tracking_section)
            self.assertIn("alert(f_trackingPayload", tracking_section)
            self.assertIn("alert.freq_all", tracking_section)


if __name__ == "__main__":
    unittest.main()
