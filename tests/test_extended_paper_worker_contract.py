import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PROCFILE = (ROOT / "Procfile").read_text(encoding="utf-8")


class ExtendedPaperWorkerContractTests(unittest.TestCase):
    def test_gunicorn_does_not_preload_background_workers(self):
        self.assertNotIn("--preload", PROCFILE)
        self.assertIn("--workers 1", PROCFILE)

    def test_request_guard_restarts_extended_worker_before_legacy_return(self):
        start = APP_SOURCE.index("def _ensure_strategies_alive():")
        end = APP_SOURCE.index(
            "# ──────────────────────────────────────────────────────────────────────────────\n# V15.0",
            start,
        )
        handler = APP_SOURCE[start:end]
        restart = handler.index("start_extended_paper_engine()")
        legacy_return = handler.index("if not AUTO_ORDER_EXECUTION_ENABLED:")
        self.assertLess(restart, legacy_return)


if __name__ == "__main__":
    unittest.main()
