import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class AutoExecutionSafetyContractTests(unittest.TestCase):
    def setUp(self):
        self.source = APP_PATH.read_text(encoding="utf-8")

    def test_autonomous_execution_defaults_to_disabled(self):
        self.assertIn(
            'AUTO_ORDER_EXECUTION_ENABLED = os.environ.get("AUTO_ORDER_EXECUTION_ENABLED", "false")',
            self.source,
        )

    def test_startup_is_guarded_by_explicit_flag(self):
        marker = self.source.index('# V13.0: Strategy D')
        guard = self.source.rfind('if AUTO_ORDER_EXECUTION_ENABLED:', 0, marker)
        startup = self.source[guard:self.source.index('# V10.3: Market Briefing')]
        self.assertIn('if AUTO_ORDER_EXECUTION_ENABLED:', startup)
        self.assertIn('start_scalper()', startup)
        self.assertIn('start_pe_engine()', startup)
        self.assertIn('start_strategy_b()', startup)
        self.assertIn('start_strategy_c()', startup)
        self.assertIn('start_mosquito()', startup)

    def test_request_auto_restart_exits_when_disabled(self):
        handler = self.source[self.source.index('def _ensure_strategies_alive():'):self.source.index('# ──────────────────────────────────────────────────────────────────────────────\n# V15.0')]
        self.assertIn('if not AUTO_ORDER_EXECUTION_ENABLED:', handler)
        self.assertIn('return', handler)


if __name__ == '__main__':
    unittest.main()
