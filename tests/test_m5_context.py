import unittest

from m5_context import build_m5_context


class M5ContextTests(unittest.TestCase):
    def test_current_put_style_context_is_explained_not_mislabeled_as_call(self):
        context = build_m5_context(
            call_potential=20,
            put_potential=80,
            price=376.27,
            ema9=376.58,
            momentum_atr=-1.457,
            obv_slope=-31973,
            vol_ratio=0.8,
            vol_reversal=26,
            rsi=51.5,
            macd_curr=-0.0586,
            macd_prev=-0.1000,
        )
        self.assertEqual(context["direction"], "PUT")
        self.assertEqual(context["score"], 4)
        self.assertEqual(context["status"], "مراقبة PUT")
        self.assertIn("الحجم", context["missing"])
        self.assertIn("RSI", context["missing"])
        self.assertIn("MACD", context["missing"])

    def test_call_context_requires_direction_and_confirmation_checks(self):
        context = build_m5_context(
            call_potential=72,
            put_potential=28,
            price=380.50,
            ema9=379.90,
            momentum_atr=0.8,
            obv_slope=45000,
            vol_ratio=1.2,
            vol_reversal=62,
            rsi=58.0,
            macd_curr=0.12,
            macd_prev=0.05,
        )
        self.assertEqual(context["direction"], "CALL")
        self.assertEqual(context["score"], 7)
        self.assertEqual(context["gauge"], 100)
        self.assertEqual(context["status"], "سياق CALL متوافق")
        self.assertEqual(context["missing"], [])

    def test_neutral_direction_stays_no_decision(self):
        context = build_m5_context(
            call_potential=50,
            put_potential=50,
            price=380.0,
            ema9=380.0,
            momentum_atr=0.0,
            obv_slope=0,
            vol_ratio=1.0,
            vol_reversal=50,
            rsi=50.0,
            macd_curr=0.0,
            macd_prev=0.0,
        )
        self.assertEqual(context["direction"], "NEUTRAL")
        self.assertEqual(context["score"], 0)
        self.assertEqual(context["status"], "لا قرار")

    def test_context_does_not_count_missing_measurements_as_confirmation(self):
        context = build_m5_context(
            call_potential=70,
            put_potential=30,
            price=None,
            ema9=None,
            momentum_atr=None,
            obv_slope=None,
            vol_ratio=None,
            vol_reversal=None,
            rsi=None,
            macd_curr=None,
            macd_prev=None,
        )
        self.assertEqual(context["score"], 1)
        self.assertIn("EMA9", context["missing"])
        self.assertIn("MACD", context["missing"])


if __name__ == "__main__":
    unittest.main()
