import unittest
from datetime import datetime
from unittest.mock import patch

import options_scalper as scalper


class ManualTradeReadinessTests(unittest.TestCase):
    def test_before_manual_window_is_blocked(self):
        ready = scalper.get_manual_readiness(
            now=datetime(2026, 9, 30, 10, 9),
            account={"options_buying_power": "1000", "trading_blocked": False, "account_blocked": False},
        )
        self.assertFalse(ready["can_buy"])
        self.assertEqual(ready["reason_code"], "outside_manual_window")
        self.assertIn("10:10", ready["message"])

    def test_open_manual_window_and_funded_account_are_ready(self):
        ready = scalper.get_manual_readiness(
            now=datetime(2026, 9, 30, 10, 10),
            account={"options_buying_power": "1000", "trading_blocked": False, "account_blocked": False},
        )
        self.assertTrue(ready["can_buy"])
        self.assertEqual(ready["reason_code"], "ready")
        self.assertEqual(ready["options_buying_power"], 1000.0)

    def test_zero_options_buying_power_is_explicitly_blocked(self):
        ready = scalper.get_manual_readiness(
            now=datetime(2026, 9, 30, 10, 30),
            account={"options_buying_power": "0", "trading_blocked": False, "account_blocked": False},
        )
        self.assertFalse(ready["can_buy"])
        self.assertEqual(ready["reason_code"], "no_options_buying_power")
        self.assertIn("$0", ready["message"])

    def test_manual_buy_does_not_fetch_market_data_when_not_ready(self):
        original_position = scalper._manual_state["position"]
        scalper._manual_state["position"] = None
        try:
            with patch.object(
                scalper,
                "get_manual_readiness",
                return_value={
                    "can_buy": False,
                    "reason_code": "no_options_buying_power",
                    "message": "لا توجد قدرة شراء أوبشن في حساب Alpaca Paper حالياً ($0).",
                    "options_buying_power": 0.0,
                },
            ), patch.object(scalper, "get_tsla_snapshot") as snapshot:
                success, result = scalper.execute_manual_itm("call")
            self.assertFalse(success)
            self.assertEqual(result["reason_code"], "no_options_buying_power")
            snapshot.assert_not_called()
        finally:
            scalper._manual_state["position"] = original_position


if __name__ == "__main__":
    unittest.main()
