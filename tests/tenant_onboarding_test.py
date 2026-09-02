from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.tenant_onboarding import TenantOnboardingError, TenantOnboardingService


class TenantOnboardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "admin.db")
        self.db.initialize()
        self.service = TenantOnboardingService(self.db)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_success_commits_only_after_all_effects(self) -> None:
        self.service.register("newxui", "New XUI", "newxui.test", "origin.example.com")
        calls: list[str] = []
        result = self.service.execute(
            "newxui",
            stage_tls=lambda: calls.append("tls"),
                deploy=lambda: calls.append("deploy") or {"deployment_id": "dep-1", "release_id": "rel-1"},
            verify=lambda: calls.append("verify"),
            publish_dns=lambda: calls.append("dns"),
        )
        self.assertEqual(calls, ["tls", "deploy", "verify", "dns"])
        self.assertEqual(result["state"], "committed")
        self.assertEqual(self.db.tenant("newxui")["enabled"], 1)

    def test_failure_disables_tenant_and_compensates(self) -> None:
        self.service.register("newxui", "New XUI", "newxui.test", "origin.example.com")
        calls: list[str] = []
        with self.assertRaises(TenantOnboardingError):
            self.service.execute(
                "newxui",
                stage_tls=lambda: calls.append("tls"),
                deploy=lambda: calls.append("deploy") or {},
                verify=lambda: (_ for _ in ()).throw(RuntimeError("health failed")),
                publish_dns=lambda: calls.append("dns"),
            )
        self.assertEqual(self.db.tenant("newxui")["enabled"], 0)
        self.assertEqual((self.db.tenant_onboarding("newxui") or {})["state"], "rolled_back")
        self.assertEqual(calls, ["tls", "deploy", "dns"])


if __name__ == "__main__":
    unittest.main()
