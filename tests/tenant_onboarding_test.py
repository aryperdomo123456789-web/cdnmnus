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

    def test_resync_replaces_only_media_map_and_reopens_onboarding(self) -> None:
        self.service.register("newxui", "New XUI", "newxui.test", "origin.example.com",
                              load_balancers=("old-lb.test",), vod_seeds=("old-vod.test",))
        self.db.set_tenant_enabled("newxui", True, operator="test", reason="fixture")
        self.db.update_tenant_onboarding("newxui", "committed", reason="fixture publicado")
        result = self.service.resync("newxui", ("new-lb.test",), ("new-vod.test",))
        self.assertEqual(result["state"], "pending")
        tenant = self.db.tenant("newxui")
        self.assertEqual(tenant["enabled"], 0)
        self.assertEqual({(u["kind"], u["host"]) for u in tenant["upstreams"]},
                         {("origin", "origin.example.com"), ("lb", "new-lb.test"),
                          ("vod", "new-vod.test")})


if __name__ == "__main__":
    unittest.main()
