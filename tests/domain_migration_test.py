#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.db import Database


class DomainMigrationTest(unittest.TestCase):
    def test_switch_adds_new_canonicals_and_preserves_old_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db = Database(Path(root) / "admin.db")
            db.initialize()
            db.add_tenant("xui-tvbrasil", "TV", "tvbrasil.phpd77.com", "origin-tv.test", 80, [])
            db.add_tenant("xuilab", "Lab", "xuilab.phpd77.com", "origin-lab.test", 80, [])
            result = db.switch_managed_domain("dominionovo.com")

            self.assertEqual(result["canonical"], "cdn.dominionovo.com")
            self.assertEqual(db.setting("managed_domain"), "dominionovo.com")
            self.assertEqual(db.setting("managed_canonical_host"), "cdn.dominionovo.com")
            self.assertEqual(db.tenant("xuilab")["canonical_host"], "xuilab.dominionovo.com")
            hosts = {item["hostname"] for item in db.tenant("xuilab")["hosts"]}
            self.assertIn("xuilab.phpd77.com", hosts)
            self.assertIn("xuilab.dominionovo.com", hosts)
            self.assertEqual(len(result["mappings"]), 2)


if __name__ == "__main__":
    unittest.main()
