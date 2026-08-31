from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from core.edge_manager import HostIdentity
from core.node_onboarding import onboard_node
from core.topology import TopologyStore


RELEASE = {"ref": "v9.9.9-test", "commit": "a" * 40, "manifest_digest": "b" * 64}
IDENTITY = HostIdentity("8.8.8.8", 22, "ssh-ed25519", "AAAA", "SHA256:test")


class NodeOnboardingTest(unittest.TestCase):
    def run_onboarding(self, role: str) -> tuple[Database, dict, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = Database(root / "admin.db")
        with patch("core.node_onboarding.scan_host_identity", return_value=IDENTITY), patch(
            "core.node_onboarding.bootstrap_edge",
            return_value={"fingerprint": IDENTITY.sha256,
                          "private_key": str(root / "1.ed25519"), "ssh_user": "cdn-deploy"},
        ) as bootstrap, patch(
            "core.node_onboarding.install_managed_node_package",
            return_value=RELEASE,
        ) as install, patch("core.node_onboarding.converge_ssh_mesh", return_value="ok"):
            result = onboard_node(
                database, name=f"Nó {role}", ipv4="8.8.8.8", ssh_port=22,
                initial_user="root", password="one-time-password", role=role,
                operator="unit-test", control_plane="143.14.168.111",
                approved_release=RELEASE, key_dir=root,
            )
        self.assertEqual(bootstrap.call_args.args[4], IDENTITY.sha256)
        self.assertEqual(install.call_args.args[4], role)
        return database, result, root

    def test_edge_is_bootstrapping_and_password_is_not_persisted(self) -> None:
        database, result, _ = self.run_onboarding("edge")
        self.assertEqual(result["state"], "bootstrapping")
        self.assertIsNone(result["deployment_id"])
        self.assertEqual(database.edge(result["node_id"])["state"], "bootstrapping")
        dump = "\n".join(database.connect().iterdump())
        self.assertNotIn("one-time-password", dump)

    def test_direct_load_balancer_is_candidate_never_active(self) -> None:
        database, result, _ = self.run_onboarding("load_balancer")
        topology = TopologyStore(database); topology.initialize()
        self.assertEqual(result["state"], "candidate")
        node = topology.node(result["node_id"])
        self.assertEqual((node["role"], node["state"]), ("load_balancer", "candidate"))
        self.assertEqual(topology.load_balancer("lb-" + result["node_id"])["state"], "candidate")
        with sqlite3.connect(database.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM load_balancers WHERE state='active'"
            ).fetchone()[0], 0)

    def test_secret_transport_and_control_plane_privilege_are_closed(self) -> None:
        root = Path(__file__).parents[1]
        menu = (root / "ansible/roles/node_menu/files/node_menu.py").read_text()
        receiver = (root / "scripts/submit_node_onboarding.py").read_text()
        mesh = (root / "scripts/converge_ssh_mesh.py").read_text()
        local_identity = mesh.split("def ensure_local_identity", 1)[1].split(
            "def ensure_remote_identity", 1
        )[0]
        self.assertIn("json.dumps(payload)", menu)
        self.assertIn("input=input_text", menu)
        self.assertIn('payload.pop("password")', receiver)
        self.assertNotIn('"--password",', menu)
        self.assertNotIn("NOPASSWD: ALL", local_identity)


if __name__ == "__main__":
    unittest.main()
