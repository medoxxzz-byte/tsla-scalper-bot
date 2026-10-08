import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


class DashboardM5ContextContractTests(unittest.TestCase):
    def test_api_returns_explainable_context_not_only_a_composite_percent(self):
        self.assertIn("from m5_context import build_m5_context, build_m5_context_v2, normalized_obv_gauge", APP)
        self.assertIn('"m5_context":     m5_context', APP)
        self.assertIn('"m5_context_v2":  m5_context_v2', APP)
        self.assertIn('"obv_gauge_v2":   obv_gauge_v2', APP)
        self.assertIn("m5_context = build_m5_context(", APP)
        self.assertIn("m5_context_v2 = build_m5_context_v2(", APP)

    def test_dashboard_renders_score_reason_and_missing_checks(self):
        self.assertIn('id="m5ContextTitle"', DASHBOARD)
        self.assertIn('id="m5ContextScore"', DASHBOARD)
        self.assertIn('id="m5ContextSummary"', DASHBOARD)
        self.assertIn('id="m5ContextMissing"', DASHBOARD)
        self.assertIn("اتساق الاتجاه M5 V2", DASHBOARD)
        self.assertIn("انعكاس OBV V2", DASHBOARD)
        self.assertIn("data.m5_context_v2 || data.m5_context || {}", DASHBOARD)

    def test_zero_gauge_is_not_rewritten_as_neutral(self):
        self.assertNotIn("data.obv_gauge      || 50", DASHBOARD)
        self.assertIn("const numberOr = (value, fallback) => Number.isFinite(value) ? value : fallback;", DASHBOARD)

    def test_legacy_composite_is_not_presented_as_trade_quality(self):
        self.assertNotIn("عداد لكل صفقة", DASHBOARD)
        self.assertIn("Experimental Context", DASHBOARD)

    def test_context_score_uses_direction_color_not_green_for_put(self):
        self.assertIn("contextDir === 'CALL' ? 'var(--green)' : contextDir === 'PUT' ? 'var(--red)'", DASHBOARD)


if __name__ == "__main__":
    unittest.main()
