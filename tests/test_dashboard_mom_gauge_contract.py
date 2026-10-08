import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


class DashboardMomGaugeContractTests(unittest.TestCase):
    def test_dashboard_replaces_duplicate_volume_gauge_with_momentum(self):
        self.assertIn('id="mom_needle"', DASHBOARD)
        self.assertIn('id="momGaugeVal"', DASHBOARD)
        self.assertIn('زخم MOM(12)', DASHBOARD)
        self.assertIn('setSmallNeedle(\'mom_needle\', mom)', DASHBOARD)
        self.assertNotIn('id="vol2_needle"', DASHBOARD)
        self.assertNotIn('id="vol2GaugeVal"', DASHBOARD)

    def test_api_exposes_normalized_momentum_without_removing_volume(self):
        self.assertIn('"mom_gauge":      mom_gauge', APP)
        self.assertIn('"vol_reversal":   vol_gauge', APP)
        self.assertIn('momentum_atr = momentum / atr14', APP)
        self.assertIn('mom_period = 12', APP)
        self.assertIn('"momentum_atr":   round(momentum_atr, 3)', APP)

    def test_momentum_fallback_is_neutral(self):
        self.assertIn('"mom_gauge": 50', APP)


if __name__ == "__main__":
    unittest.main()
