import ast
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def load_quality_helpers():
    """Load only the pure quality helpers; do not import app.py or start threads."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_parse_data_timestamp", "_market_session_state", "_data_quality_metadata",
        "_fetch_with_completion", "_fetch_primary_then_fallback",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "timedelta": timedelta,
        "_NEW_YORK_TZ": ZoneInfo("America/New_York"),
        "_RIYADH_TZ": ZoneInfo("Asia/Riyadh"),
        "_DATA_TIMEFRAME_SECONDS": {
            "1Min": 60, "3Min": 180, "5Min": 300, "15Min": 900, "1Hour": 3600
        },
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP_PATH), "exec"), namespace)
    return (
        namespace["_data_quality_metadata"],
        namespace["_market_session_state"],
        namespace["_fetch_with_completion"],
        namespace["_fetch_primary_then_fallback"],
    )


class DataReliabilityHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.quality, cls.session_state, cls.fetch_with_completion,
            cls.fetch_primary_then_fallback,
        ) = load_quality_helpers()
        cls.quality = staticmethod(cls.quality)
        cls.session_state = staticmethod(cls.session_state)
        cls.fetch_with_completion = staticmethod(cls.fetch_with_completion)
        cls.fetch_primary_then_fallback = staticmethod(cls.fetch_primary_then_fallback)

    def test_separates_fetch_age_from_bar_age(self):
        fetched = datetime.now(timezone.utc) - timedelta(seconds=7)
        bar = datetime.now(timezone.utc) - timedelta(seconds=75)
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=fetched,
            bar_time=bar.isoformat(), timeframe="1Min"
        )
        self.assertGreaterEqual(result["fetch_age_seconds"], 7)
        self.assertGreaterEqual(result["bar_age_seconds"], 75)
        self.assertNotEqual(result["fetch_age_seconds"], result["bar_age_seconds"])

    def test_does_not_assume_open_bar_is_closed(self):
        bar = datetime.now(timezone.utc) - timedelta(seconds=10)
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=datetime.now(timezone.utc),
            bar_time=bar.isoformat(), timeframe="1Min"
        )
        self.assertIs(result["bar_closed"], False)
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_unknown_timestamp_is_explicit(self):
        result = self.quality(
            source="Alpaca Snapshot", fetched_at_utc=datetime.now(timezone.utc),
            timeframe=None, warning="timestamp unavailable"
        )
        self.assertEqual(result["bar_closed"], "unknown")
        self.assertIsNone(result["bar_age_seconds"])
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["warning"], "timestamp unavailable")

    def test_invalid_timestamp_is_not_invented(self):
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=datetime.now(timezone.utc),
            bar_time="not-a-timestamp", timeframe="5Min"
        )
        self.assertEqual(result["status"], "UNKNOWN_TIMESTAMP")
        self.assertIsNone(result["bar_time"])
        self.assertIsNone(result["bar_age_seconds"])
        self.assertEqual(result["bar_closed"], "unknown")

    def test_closed_session_is_distinguished_from_freshness(self):
        old_bar = datetime.now(timezone.utc) - timedelta(hours=2)
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=datetime.now(timezone.utc),
            bar_time=old_bar.isoformat(), timeframe="5Min"
        )
        self.assertIn(result["status"], {"SESSION_CLOSED", "AGED"})
        self.assertIn(result["market_session"], {"REGULAR_OPEN", "REGULAR_CLOSED", "WEEKEND"})

    def test_winter_open_maps_to_riyadh_530pm(self):
        winter_open_utc = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(self.session_state(winter_open_utc), "REGULAR_OPEN")
        self.assertEqual(winter_open_utc.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M"), "09:30")
        self.assertEqual(winter_open_utc.astimezone(ZoneInfo("Asia/Riyadh")).strftime("%H:%M"), "17:30")

    def test_summer_open_maps_to_riyadh_430pm(self):
        summer_open_utc = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
        self.assertEqual(self.session_state(summer_open_utc), "REGULAR_OPEN")
        self.assertEqual(summer_open_utc.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M"), "09:30")
        self.assertEqual(summer_open_utc.astimezone(ZoneInfo("Asia/Riyadh")).strftime("%H:%M"), "16:30")

    def test_weekend_is_not_regular_session(self):
        saturday = datetime(2026, 1, 17, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(self.session_state(saturday), "WEEKEND")

    def test_source_contract_marks_missing_values_unavailable(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('"level": None, "label": "غير متاحة", "available": False', source)
        self.assertIn('"available": False,\n                "call_potential": 50', source)
        self.assertIn('status = "ERROR"', source)
        dashboard = (APP_PATH.parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("data.available === false", dashboard)
        self.assertIn("البيانات غير كافية؛ لا تُفسر القيم الحيادية كقراءة سوق.", dashboard)
        self.assertIn("setGaugeNeedlesVisible(false)", dashboard)
        self.assertIn("setGaugeNeedlesVisible(true)", dashboard)

    def test_delayed_fetch_records_completion_time_after_fetch(self):
        timeline = []
        def delayed_fetch():
            timeline.append(("started", datetime.now(timezone.utc)))
            time.sleep(0.02)
            timeline.append(("finished", datetime.now(timezone.utc)))
            return [{"t": "2026-01-15T14:30:00Z"}]

        bars, fetched_at, error = self.fetch_with_completion(delayed_fetch)
        self.assertIsNone(error)
        self.assertEqual(len(bars), 1)
        self.assertEqual([name for name, _ in timeline], ["started", "finished"])
        self.assertGreater(fetched_at, timeline[-1][1])
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=fetched_at,
            bar_time=(fetched_at - timedelta(seconds=360)).isoformat(),
            timeframe="5Min", now_utc=fetched_at + timedelta(seconds=1)
        )
        self.assertEqual(result["fetch_age_seconds"], 1)
        self.assertGreaterEqual(result["bar_age_seconds"], 360)

    def test_friday_bar_is_not_misclassified_as_weekend_freshness_error(self):
        friday_bar = datetime(2026, 1, 16, 21, 0, tzinfo=timezone.utc)
        sunday_now = datetime(2026, 1, 18, 21, 0, tzinfo=timezone.utc)
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=friday_bar,
            bar_time=friday_bar.isoformat(), timeframe="5Min",
            now_utc=sunday_now
        )
        self.assertEqual(result["market_session"], "WEEKEND")
        self.assertEqual(result["status"], "SESSION_CLOSED")
        self.assertGreater(result["bar_age_seconds"], 24 * 3600)

    def test_source_failure_is_error_not_incomplete(self):
        result = self.quality(
            source="Alpaca IEX", fetched_at_utc=datetime.now(timezone.utc),
            timeframe="5Min", warning="Timeout", error=True
        )
        self.assertEqual(result["status"], "ERROR")

    def test_http_fallback_succeeds_when_primary_source_is_missing(self):
        bars, completed_at, error, source = self.fetch_primary_then_fallback(
            None,
            lambda: [{"t": "2026-01-15T14:30:00Z", "v": 100}],
            "Alpaca primary", "Alpaca IEX HTTP",
        )
        self.assertEqual(len(bars), 1)
        self.assertIsNotNone(completed_at)
        self.assertIsNone(error)
        self.assertEqual(source, "Alpaca IEX HTTP")

    def test_both_sources_fail_return_error(self):
        def fail_primary():
            raise TimeoutError("primary timeout")

        def fail_fallback():
            raise TimeoutError("http timeout")

        bars, completed_at, error, source = self.fetch_primary_then_fallback(
            fail_primary, fail_fallback, "Alpaca primary", "Alpaca IEX HTTP"
        )
        self.assertEqual(bars, [])
        self.assertIsNotNone(completed_at)
        self.assertIn("primary timeout", error)
        self.assertIn("http timeout", error)
        self.assertIsNone(source)


if __name__ == "__main__":
    unittest.main()
