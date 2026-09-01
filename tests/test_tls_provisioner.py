from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database
from core.tls_provisioner import TLSProvisionError, TLSProvisioner


def completed(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class TLSProvisionerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="cdnmnus-tls-test-")
        self.root = Path(self.tempdir.name)
        self.database = Database(self.root / "admin.db")
        self.database.initialize()
        self.database.add_tenant("xui1", "XUI 1", "xui1.test", "origin1.test")
        self.database.add_tenant("xui2", "XUI 2", "xui2.test", "origin2.test")
        self.lineage1 = self._make_lineage("xui1.test")
        self.lineage2 = self._make_lineage("xui2.test")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _make_lineage(self, host: str) -> Path:
        lineage = self.root / "live" / host
        lineage.mkdir(parents=True, exist_ok=True)
        (lineage / "fullchain.pem").write_text("certificate")
        (lineage / "privkey.pem").write_text("private-key")
        return lineage

    def _provisioner(self, *, edge_ips: tuple[str, ...] = ("143.14.168.168", "143.14.168.170")) -> TLSProvisioner:
        return TLSProvisioner(
            self.database,
            runner=self._runner,
            live_root=str(self.root / "live"),
            distribution_script="/opt/cdnmnus/scripts/distribute_tls.sh",
            edge_ips=edge_ips,
        )

    def _runner(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "sudo":
            return completed(
                argv,
                json.dumps({
                    "canonical": "xui1.test",
                    "lineage": str(self.lineage1),
                    "tenant_id": "xui1",
                }),
            )
        if argv[0] == "openssl":
            return completed(argv, "X509v3 Subject Alternative Name:\n    DNS:xui1.test")
        if argv[0] == "curl":
            return completed(argv, "200")
        return completed(argv)

    def test_success_flow_keeps_other_tenants_untouched_and_uses_real_lineage(self) -> None:
        result = self._provisioner().provision("xui1")
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tenant_id"], "xui1")
        self.assertTrue(all(item["tls_status"] == "valid" for item in self.database.tenant("xui1")["hosts"]))
        self.assertTrue(all(item["tls_status"] == "pending" for item in self.database.tenant("xui2")["hosts"]))
        self.assertEqual(
            [item[:2] for item in self.calls if item[0] == "/opt/cdnmnus/scripts/distribute_tls.sh"],
            [["/opt/cdnmnus/scripts/distribute_tls.sh", str(self.lineage1)]],
        )
        self.assertEqual(self.calls[0][:2], ["sudo", "/opt/cdnmnus/scripts/cdnmnus-acme-helper"])
        self.assertIn("--tenant-id", self.calls[0])
        self.assertIn("xui1", self.calls[0])
        health_calls = [call for call in self.calls if call[0] == "curl"]
        self.assertEqual(len(health_calls), 2)
        for call in health_calls:
            self.assertIn("--resolve", call)
            self.assertIn("xui1.test:443:", call[call.index("--resolve") + 1])
            self.assertIn("https://xui1.test/edge-health", call)

    def test_acme_failure_is_sanitized_and_localized(self) -> None:
        def runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(argv))
            if argv[0] == "sudo":
                return completed(argv, "", "token=should-not-leak", 1)
            return completed(argv)

        failed = TLSProvisioner(
            self.database,
            runner=runner,
            live_root=str(self.root / "live"),
            distribution_script="/opt/cdnmnus/scripts/distribute_tls.sh",
            edge_ips=(),
        )
        with self.assertRaises(TLSProvisionError) as ctx:
            failed.provision("xui1")
        self.assertNotIn("should-not-leak", str(ctx.exception))
        self.assertTrue(all(item["tls_status"] == "failed" for item in self.database.tenant("xui1")["hosts"]))
        self.assertTrue(all(item["tls_status"] == "pending" for item in self.database.tenant("xui2")["hosts"]))

    def test_san_failure_marks_only_target_tenant_failed(self) -> None:
        def runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(argv))
            if argv[0] == "sudo":
                return completed(
                    argv,
                    json.dumps({
                        "canonical": "xui1.test",
                        "lineage": str(self.lineage1),
                        "tenant_id": "xui1",
                    }),
                )
            if argv[0] == "openssl":
                return completed(argv, "X509v3 Subject Alternative Name:\n    DNS:missing.test")
            return completed(argv)

        failed = TLSProvisioner(
            self.database,
            runner=runner,
            live_root=str(self.root / "live"),
            distribution_script="/opt/cdnmnus/scripts/distribute_tls.sh",
            edge_ips=(),
        )
        with self.assertRaises(TLSProvisionError) as ctx:
            failed.provision("xui1")
        self.assertIn("SAN ausente", str(ctx.exception))
        self.assertTrue(all(item["tls_status"] == "failed" for item in self.database.tenant("xui1")["hosts"]))
        self.assertTrue(all(item["tls_status"] == "pending" for item in self.database.tenant("xui2")["hosts"]))

    def test_health_421_uses_explicit_health_host(self) -> None:
        self.database.add_cname("xui1", "health1.test")
        with self.database.transaction(immediate=True) as db:
            db.execute("UPDATE xui_tenants SET health_host=? WHERE id=?", ("health1.test", "xui1"))

        def runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(argv))
            if argv[0] == "sudo":
                return completed(
                    argv,
                    json.dumps({
                        "canonical": "xui1.test",
                        "lineage": str(self.lineage1),
                        "tenant_id": "xui1",
                    }),
                )
            if argv[0] == "openssl":
                return completed(argv, "X509v3 Subject Alternative Name:\n    DNS:xui1.test\n    DNS:health1.test")
            if argv[0] == "curl":
                return completed(argv, "421")
            return completed(argv)

        failed = TLSProvisioner(
            self.database,
            runner=runner,
            live_root=str(self.root / "live"),
            distribution_script="/opt/cdnmnus/scripts/distribute_tls.sh",
            edge_ips=("143.14.168.168",),
        )
        with self.assertRaises(TLSProvisionError) as ctx:
            failed.provision("xui1")
        self.assertIn("HTTP 421", str(ctx.exception))
        self.assertTrue(all(item["tls_status"] == "failed" for item in self.database.tenant("xui1")["hosts"]))
        self.assertTrue(any(call for call in self.calls if call[0] == "curl" and "https://health1.test/edge-health" in call))

    def test_claim_recovers_abandoned_job_and_records_timeout(self) -> None:
        with self.database.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO tls_jobs(id,tenant_id,state,attempts,started_at,created_at)
                   VALUES(?,?,?,?,datetime('now','-4000 seconds'),datetime('now','-4000 seconds'))""",
                ("tls-stale", "xui1", "running", 0),
            )
        claimed = self.database.claim_tls_job(timeout_seconds=1, max_attempts=3)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], "tls-stale")
        self.assertEqual(claimed["state"], "running")
        self.assertEqual(claimed["attempts"], 1)
        events = self.database.rows("SELECT * FROM tls_events WHERE tenant_id=? ORDER BY created_at", ("xui1",))
        self.assertTrue(any(event["event_type"] == "tls_job_timeout" for event in events))

    def test_expired_lease_cannot_change_tls_status(self) -> None:
        job = self.database.enqueue_tls_job("xui1")
        first = self.database.claim_tls_job(timeout_seconds=1, max_attempts=3)
        self.assertEqual(first["id"], job["id"])
        with self.database.transaction(immediate=True) as db:
            db.execute("UPDATE tls_jobs SET started_at=datetime('now','-4000 seconds') WHERE id=?", (job["id"],))
        second = self.database.claim_tls_job(timeout_seconds=1, max_attempts=3)
        self.assertNotEqual(first["lease_id"], second["lease_id"])
        with self.assertRaisesRegex(ValueError, "não está mais sob posse"):
            self.database.set_tls_status(
                "xui1", "valid", operator="tls-provisioner", reason="old attempt",
                job_id=first["id"], lease_id=first["lease_id"],
            )
        self.database.finish_tls_job(first["id"], "succeeded", lease_id=first["lease_id"])
        self.assertEqual(self.database.rows("SELECT state FROM tls_jobs WHERE id=?", (job["id"],))[0]["state"], "running")
        self.assertTrue(all(item["tls_status"] == "pending" for item in self.database.tenant("xui1")["hosts"]))

    def test_migrate_identity_preserves_hosts_and_rejects_protected_removal(self) -> None:
        database = Database(self.root / "migration.db")
        database.initialize()
        database.add_tenant("old1", "Old", "old.example", "8.8.8.8")
        database.add_cname("old1", "new.example")
        migrated = database.migrate_tenant_identity("old1", "new1", "new.example")
        self.assertEqual(migrated["id"], "new1")
        self.assertEqual(migrated["health_host"], "old.example")
        self.assertEqual({item["hostname"] for item in migrated["hosts"]}, {"old.example", "new.example"})
        database.add_cname("new1", "playlist.example")
        database.set_playlist_host("new1", "playlist.example")
        with self.assertRaisesRegex(ValueError, "playlist_host"):
            database.migrate_tenant_identity("new1", "new2", "old.example", remove_hosts=("playlist.example",))

    def test_tls_audit_reason_is_sanitized_for_direct_callers(self) -> None:
        self.database.set_tls_status(
            "xui1", "failed", operator="operator with spaces",
            reason="credential=/etc/cdnmnus/secrets/cloudflare_acme.ini token=do-not-store",
        )
        event = self.database.rows(
            "SELECT operator,reason FROM tls_events WHERE tenant_id=? ORDER BY created_at DESC LIMIT 1",
            ("xui1",),
        )[0]
        self.assertEqual(event["operator"], "operator_with_spaces")
        self.assertNotIn("do-not-store", event["reason"])

    def test_enqueue_concurrent_same_tenant_is_single_job(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def enqueue() -> None:
            barrier.wait()
            job = self.database.enqueue_tls_job("xui1")
            results.append(job["id"])

        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        jobs = self.database.rows("SELECT * FROM tls_jobs WHERE tenant_id=?", ("xui1",))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "queued")

    def test_two_tenants_can_queue_independently(self) -> None:
        job1 = self.database.enqueue_tls_job("xui1")
        job2 = self.database.enqueue_tls_job("xui2")
        self.assertNotEqual(job1["id"], job2["id"])
        jobs = self.database.rows("SELECT tenant_id, COUNT(*) AS n FROM tls_jobs GROUP BY tenant_id ORDER BY tenant_id")
        self.assertEqual([(row["tenant_id"], row["n"]) for row in jobs], [("xui1", 1), ("xui2", 1)])


if __name__ == "__main__":
    unittest.main()
