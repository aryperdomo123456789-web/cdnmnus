from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.topology import TopologyStore


ROOT = Path(__file__).parents[1]


class NodePackageTest(unittest.TestCase):
    def test_manifest_is_closed_and_verifiable(self) -> None:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            result = subprocess.run(
                [str(ROOT / "scripts/build_node_package_manifest.py"),
                 "--ref", "v9.9.9-test",
                 "--output", str(manifest)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            verified = subprocess.run(
                ["python3", str(ROOT / "node-package/verify.py"), str(ROOT),
                 str(manifest), "v9.9.9-test", commit],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            data = json.loads(manifest.read_text())
            self.assertNotIn("source_commit", data)
            self.assertIn("panel/vod_relay.py", data["files"])
            self.assertIn("panel/multi_tenant_broker.py", data["files"])
            self.assertNotIn("node-package/manifest.json", data["files"])

    def test_installer_is_pinned_and_rollbackable(self) -> None:
        installer = (ROOT / "node-package/install.sh").read_text()
        bootstrap = (ROOT / "install-managed-node-from-github.sh").read_text()
        self.assertIn("use uma tag imutável; main/branch são recusados", installer)
        self.assertIn("manifest-digest", installer)
        self.assertIn("rollback_install", installer)
        self.assertIn("systemctl disable --now haproxy", installer)
        self.assertIn("git clone --quiet --depth 1 --branch", bootstrap)
        self.assertIn("actual_commit", bootstrap)

    def test_menu_only_requests_and_control_plane_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "admin.db")
            database.initialize(); topology = TopologyStore(database); topology.initialize()
            database.add_edge(
                "9", "Edge laboratório", "1.1.1.1", 22, "cdn-deploy",
                "SHA256:test", "ready",
            )
            request = database.request_load_balancer_promotion(
                "9", "standby", "v1.2.3", "a" * 40, "b" * 64,
                "1.1.1.1", "homologação de promoção",
            )
            self.assertEqual(request["state"], "requested")
            self.assertEqual(topology.node("9")["role"], "edge")
            self.assertEqual(topology.node("9")["state"], "ready")
            with self.assertRaises(Exception):
                database.request_load_balancer_promotion(
                    "9", "standby", "v1.2.3", "a" * 40, "b" * 64,
                    "1.1.1.1", "duplicada",
                )
            database.add_edge(
                "10", "Backend laboratório", "8.8.8.8", 22, "cdn-deploy",
                "SHA256:backend", "ready",
            )
            database.set_promotion_request_state(request["id"], "approved")
            database.set_edge_state(
                "9", "draining", operator="test", reason="drain de teste"
            )
            database.set_promotion_request_state(request["id"], "installing")
            lb = database.finalize_load_balancer_candidate(
                request["id"], "lb-9", ["10"], "test", "candidato validado"
            )
            self.assertEqual(lb["state"], "standby")
            self.assertEqual(database.edge("9")["state"], "disabled")
            self.assertEqual(topology.node("9")["role"], "load_balancer")
            self.assertEqual(topology.node("9")["state"], "standby")
        menu = (ROOT / "ansible/roles/node_menu/files/node_menu.py").read_text()
        self.assertIn("Promover esta Edge para Load Balancer", menu)
        self.assertIn("Cadastrar nova máquina (Edge ou Load Balancer)", menu)
        self.assertIn("cdnmnus-submit-node-onboarding", menu)
        self.assertIn("cdnmnus-submit-promotion-request", menu)
        self.assertNotIn("systemctl enable haproxy", menu)
        processor = (ROOT / "scripts/process_promotion_request.py").read_text()
        self.assertNotIn('"load_balancer_action": "promote"', processor)
        self.assertIn('"load_balancer_action": "deploy"', processor)


if __name__ == "__main__":
    unittest.main()
