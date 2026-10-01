import unittest
from datetime import datetime
from unittest.mock import patch

import extended_paper_engine as engine


def bars(count=50, start=90.0, step=0.25, volume=1000):
    result = []
    for i in range(count):
        close = start + i * step
        result.append({"o": close - 0.05, "h": close + 0.10, "l": close - 0.10, "c": close, "v": volume})
    return result


class ConfiguredStore:
    configured = True


class ExtendedPaperDecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, 10, 15)
        self.store = engine._store
        engine._store = ConfiguredStore()

    def tearDown(self):
        engine._store = self.store

    def _bars(self, timeframe, count):
        return bars(count + 3, start=90.0, step=0.20, volume=1000)

    @patch.object(engine, "_paper_endpoint_ok", return_value=True)
    @patch.object(engine, "get_tsla_snapshot", return_value={"price": 100.0, "vwap": 99.85})
    @patch.object(engine, "get_tsla_bars")
    def test_aligned_trend_near_vwap_creates_call_decision(self, mock_bars, _snapshot, _paper):
        mock_bars.side_effect = self._bars
        decision = engine.evaluate_extended_decision(self.now)
        self.assertEqual(decision["decision"], "CALL")
        self.assertLess(decision["invalidation_price"], decision["tsla_price"])
        self.assertAlmostEqual(decision["target_060_price"], 100.60)
        self.assertIn("trend_15m", decision["context"])

    @patch.object(engine, "_paper_endpoint_ok", return_value=True)
    @patch.object(engine, "get_tsla_snapshot", return_value={"price": 103.0, "vwap": 99.85})
    @patch.object(engine, "get_tsla_bars")
    def test_far_from_vwap_is_no_trade_not_chase(self, mock_bars, _snapshot, _paper):
        mock_bars.side_effect = self._bars
        decision = engine.evaluate_extended_decision(self.now)
        self.assertEqual(decision["decision"], "NO_TRADE")
        self.assertIn("لا مطاردة", decision["reason"])

    def test_outside_window_never_evaluates_a_trade(self):
        decision = engine.evaluate_extended_decision(datetime(2026, 9, 30, 14, 30))
        self.assertEqual(decision["decision"], "NO_TRADE")
        self.assertIn("خارج نافذة", decision["reason"])

    @patch.object(engine, "_paper_endpoint_ok", return_value=False)
    def test_non_paper_endpoint_is_hard_blocked(self, _paper):
        decision = engine.evaluate_extended_decision(self.now)
        self.assertEqual(decision["decision"], "NO_TRADE")
        self.assertIn("Paper", decision["reason"])


class ExtendedPaperExecutionTests(unittest.TestCase):
    @patch.object(engine, "get_option_quote", return_value={"bid": 5.00, "ask": 5.20, "mid": 5.10})
    @patch.object(engine, "get_options_chain")
    def test_extended_contract_is_next_trading_day_itm(self, mock_chain, _quote):
        mock_chain.return_value = [
            {"symbol": "TSLA261001C00095000", "strike_price": "95"},
            {"symbol": "TSLA261001C00090000", "strike_price": "90"},
        ]
        contract = engine.find_extended_itm_contract(100.0, "CALL", datetime(2026, 9, 30, 11, 0))
        self.assertIsNotNone(contract)
        self.assertEqual(contract["expiry"], "2026-10-01")
        self.assertLess(contract["strike"], 100.0)
        self.assertGreaterEqual(contract["approx_delta"], 0.68)

    def test_daily_order_id_is_deterministic(self):
        self.assertEqual(engine._client_order_id(datetime(2026, 9, 30).date()), "tm-extended-20260930")


if __name__ == "__main__":
    unittest.main()
