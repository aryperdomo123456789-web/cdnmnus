from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.db import Database


class EdgeHealthHysteresisTest(unittest.TestCase):
    def test_removes_after_three_failures_and_recovers_after_five_successes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db = Database(Path(root) / "admin.db")
            db.initialize()
            db.add_edge("edge-a", "edge-a", "8.8.8.8", 22, "root", "sha256:edge-a",
                        state="ready")
            for _ in range(2):
                row = db.record_edge_health("edge-a", 503, healthy=False)
                self.assertEqual(row["state"], "ready")
            row = db.record_edge_health("edge-a", 503, healthy=False)
            self.assertEqual(row["state"], "failed")
            for _ in range(4):
                row = db.record_edge_health("edge-a", 200, healthy=True)
                self.assertEqual(row["state"], "failed")
            row = db.record_edge_health("edge-a", 200, healthy=True)
            self.assertEqual(row["state"], "ready")


if __name__ == "__main__":
    unittest.main()
