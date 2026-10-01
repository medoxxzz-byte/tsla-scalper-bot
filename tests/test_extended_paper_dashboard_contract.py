import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
ENGINE = (ROOT / "extended_paper_engine.py").read_text(encoding="utf-8")


class ExtendedPaperDashboardContractTests(unittest.TestCase):
    def test_dashboard_has_a_read_only_extended_status_card(self):
        self.assertIn("الصفقة الممتدة الورقية", DASHBOARD)
        self.assertIn("/extended-paper/status", DASHBOARD)
        self.assertNotIn("extended-paper/start", DASHBOARD)
        self.assertNotIn("extended-paper/execute", DASHBOARD)

    def test_app_exposes_only_status_route_for_dashboard(self):
        self.assertIn("@app.route('/extended-paper/status', methods=['GET'])", APP)
        self.assertNotIn("@app.route('/extended-paper/execute'", APP)

    def test_engine_is_paper_only_and_does_not_import_manual_execution(self):
        self.assertIn("paper-api.alpaca.markets", ENGINE)
        self.assertNotIn("execute_manual_itm", ENGINE)
        self.assertIn("UNIQUE (trade_date)", (ROOT / "database" / "005_create_extended_paper_trade_ledger.sql").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
