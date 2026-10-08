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

    def test_v17_exposes_a_tradingview_alert_menu_entry(self):
        self.assertIn(
            'alertcondition(false, title="TM V17 Phase B — اختر Any alert() function call"',
            V17,
        )

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

    def test_v18_exposes_a_tradingview_alert_menu_entry(self):
        self.assertIn(
            'alertcondition(false, title="TM V18 Phase B — اختر Any alert() function call"',
            V18,
        )

    def test_v18_closing_experiment_censors_at_regular_session_close(self):
        for marker in (
            'closingParent = str.startswith(array.get(trackActions, index), "CLOSE_")',
            "atRegularSessionClose = nyHour == 15 and nyMin == 59",
            "finalDueAt = parentClose + 12 * 60 * 1000",
            "needsSessionCensor = time_close <= finalDueAt",
            'alert(f_trackingPayload(index, "close_1600")',
            "if closingParent and atRegularSessionClose and needsSessionCensor",
        ):
            self.assertIn(marker, V18)

    def test_v17_late_zone_tracking_censors_at_regular_session_close(self):
        for marker in (
            "atRegularSessionClose = nyHour == 15 and nyMin == 55",
            "finalDueAt = parentClose + 15 * 60 * 1000",
            "needsSessionCensor = time_close <= finalDueAt",
            'alert(f_trackingPayload(index, "close_1600")',
            "if atRegularSessionClose and needsSessionCensor",
        ):
            self.assertIn(marker, V17)

    def test_v17_never_reads_a_tracker_after_session_close_removal(self):
        tracking = V17[V17.index("// ── Phase B:"):V17.index("// ── Visuals")]
        close_remove = tracking.index('f_removeTracking(index)', tracking.index('if atRegularSessionClose'))
        post10 = tracking.index('else if elapsedMs == 10 * 60 * 1000')
        post15 = tracking.index('else if elapsedMs == 15 * 60 * 1000')
        self.assertGreater(post10, close_remove)
        self.assertGreater(post15, post10)
        self.assertNotIn('\n        if elapsedMs == 10 * 60 * 1000', tracking)
        self.assertNotIn('\n        if elapsedMs == 15 * 60 * 1000', tracking)

    def test_post_event_ranges_exclude_each_parent_signal_candle(self):
        for source in (V17, V18):
            tracking = source[source.index("// ── Phase B:"):source.index("// ── Visuals")]
            self.assertIn("parentClose = array.get(trackCloseTimes, index)", tracking)
            self.assertIn("time_close > parentClose", tracking)
            self.assertLess(
                tracking.index("time_close > parentClose"),
                tracking.index("cumulativeHigh = math.max")
            )

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
