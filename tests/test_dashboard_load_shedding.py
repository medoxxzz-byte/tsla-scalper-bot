import unittest
from pathlib import Path
from unittest.mock import patch

import extended_paper_engine as engine


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


class DashboardLoadSheddingTests(unittest.TestCase):
    def test_extended_paper_defaults_to_opt_in_not_always_on(self):
        engine_source = (ROOT / "extended_paper_engine.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("EXTENDED_PAPER_TRADE_ENABLED", "false")', engine_source)

    def test_disabled_extended_status_does_not_query_ledger(self):
        class ExplodingStore:
            configured = True

            @staticmethod
            def latest_trade():
                raise AssertionError("paused status must not query the extended ledger")

        original_store = engine._store
        try:
            engine._store = ExplodingStore()
            with patch.object(engine, "EXTENDED_PAPER_TRADE_ENABLED", False):
                status = engine.get_extended_paper_status()
            self.assertTrue(status["ok"])
            self.assertFalse(status["enabled"])
            self.assertFalse(status["running"])
            self.assertEqual(status["last_decision"], "DISABLED")
        finally:
            engine._store = original_store

    def test_dashboard_renders_core_before_optional_legacy_cards(self):
        self.assertIn("const CORE_FETCH_TIMEOUT_MS = 7000;", DASHBOARD)
        self.assertIn("fetchJSON('/extended-paper/status', 2500)", DASHBOARD)
        self.assertIn("The core research view must not wait for optional legacy experiments", DASHBOARD)
        core_section = DASHBOARD.split("// Optional cards update afterwards.")[0]
        self.assertNotIn("fetchJSON('/extended-paper/status'", core_section)


if __name__ == "__main__":
    unittest.main()
