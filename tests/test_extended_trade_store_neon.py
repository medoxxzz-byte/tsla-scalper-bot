"""Run only against the temporary Neon migration branch via DATABASE_URL."""

import os
import unittest
from datetime import date, datetime, timezone

from extended_trade_store import ExtendedPaperTradeStore


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for Neon acceptance")
class ExtendedPaperLedgerNeonTests(unittest.TestCase):
    def setUp(self):
        self.store = ExtendedPaperTradeStore(os.environ["DATABASE_URL"])
        self.trade_date = date(2099, 1, 4)
        self.now = datetime(2099, 1, 4, 16, 0, tzinfo=timezone.utc)
        self.payload = {
            "trade_date": self.trade_date,
            "trade_key": "extended-paper-v1:20990104",
            "direction": "CALL",
            "decision_at": self.now,
            "tsla_price": 100.00,
            "vwap": 99.80,
            "invalidation_price": 99.68,
            "target_030_price": 100.30,
            "target_060_price": 100.60,
            "target_120_price": 101.20,
            "decision_context": {"acceptance": True, "source": "temporary_neon_branch"},
        }

    def test_one_trade_per_day_and_append_only_milestones(self):
        self.assertEqual(self.store.healthcheck()["status"], "ok")
        reserved, created = self.store.reserve_decision(self.payload)
        self.assertTrue(created)
        self.assertEqual(reserved["status"], "reserved")

        duplicate, created_again = self.store.reserve_decision(self.payload)
        self.assertFalse(created_again)
        self.assertEqual(duplicate["id"], reserved["id"])

        opened = self.store.mark_entry_opened(int(reserved["id"]), {
            "entered_at": self.now,
            "option_symbol": "TSLA990105C00095000",
            "option_expiry": "2099-01-05",
            "option_strike": 95.0,
            "option_entry_mid": 5.10,
            "alpaca_entry_order_id": "entry-acceptance-20990104",
            "tsla_entry_price": 100.00,
            "event_payload": {"acceptance": True},
        })
        self.assertEqual(opened["status"], "open")

        progressed, milestones = self.store.record_progress(
            int(reserved["id"]), self.now, 100.65, 5.55, 0.65, 0.0
        )
        self.assertEqual(set(milestones), {"target_030_hit", "target_060_hit"})
        self.assertIsNotNone(progressed["hit_030_at"])
        self.assertIsNotNone(progressed["hit_060_at"])
        self.assertIsNone(progressed["hit_120_at"])

        rerun, rerun_milestones = self.store.record_progress(
            int(reserved["id"]), self.now, 100.65, 5.55, 0.65, 0.0
        )
        self.assertEqual(list(rerun_milestones), [])
        self.assertEqual(rerun["id"], reserved["id"])

        closed = self.store.close_trade(int(reserved["id"]), {
            "closed_at": self.now,
            "option_exit_mid": 5.55,
            "alpaca_exit_order_id": "exit-acceptance-20990104",
            "tsla_exit_price": 100.65,
            "exit_reason": "acceptance_close",
            "option_pnl_dollars": 45.0,
        })
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["option_pnl_dollars"], 45.0)


if __name__ == "__main__":
    unittest.main()
