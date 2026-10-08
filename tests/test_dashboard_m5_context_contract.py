import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


class DashboardM5ContextContractTests(unittest.TestCase):
    def test_api_returns_explainable_context_not_only_a_composite_percent(self):
        self.assertIn("from m5_context import build_m5_context", APP)
        self.assertIn('"m5_context":     m5_context', APP)
        self.assertIn("m5_context = build_m5_context(", APP)

    def test_dashboard_renders_score_reason_and_missing_checks(self):
        self.assertIn('id="m5ContextTitle"', DASHBOARD)
        self.assertIn('id="m5ContextScore"', DASHBOARD)
        self.assertIn('id="m5ContextSummary"', DASHBOARD)
        self.assertIn('id="m5ContextMissing"', DASHBOARD)
        self.assertIn("اتساق الاتجاه M5", DASHBOARD)

    def test_zero_gauge_is_not_rewritten_as_neutral(self):
        self.assertNotIn("data.obv_gauge      || 50", DASHBOARD)
        self.assertIn("const numberOr = (value, fallback) => Number.isFinite(value) ? value : fallback;", DASHBOARD)

    def test_legacy_composite_is_not_presented_as_trade_quality(self):
        self.assertNotIn("عداد لكل صفقة", DASHBOARD)
        self.assertIn("transparent agreement score", DASHBOARD)


if __name__ == "__main__":
    unittest.main()
