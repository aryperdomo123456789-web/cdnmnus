#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from core.topology import TopologyStore


ROOT = Path(__file__).resolve().parents[1]


class ManualFailoverMenuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="cdnmnus-manual-failover-")
        self.addCleanup(self.tempdir.cleanup)
        self.db = Database(Path(self.tempdir.name) / "admin.db")
        self.db.initialize()
        self.topology = TopologyStore(self.db)
        self.topology.initialize()

        # source 1 e target 4 seguem o contrato documental da receita nova.
        self.topology.add_node("1", "LB 111", "143.14.168.111", "load_balancer", "pending",
                               "test", "fixture")
        self.topology.transition_node("1", "candidate", "test", "fixture")
        self.topology.add_load_balancer("lb-1", "1", None, "test", "fixture")
        with self.db.connect() as conn:
            conn.execute("UPDATE nodes SET state='active' WHERE id='1'")
            conn.execute("UPDATE load_balancers SET state='active' WHERE id='lb-1'")

        self.topology.add_node("4", "LB 237", "45.140.192.237", "load_balancer", "pending",
                               "test", "fixture")
        self.topology.transition_node("4", "candidate", "test", "fixture")
        self.topology.add_load_balancer("lb-4", "4", None, "test", "fixture")
        with self.db.connect() as conn:
            conn.execute("UPDATE nodes SET state='standby' WHERE id='4'")
            conn.execute("UPDATE load_balancers SET state='standby' WHERE id='lb-4'")

        # Edges ready permitem que o reconciliador aceite o pool direto.
        self.topology.add_node("2", "Edge 168", "143.14.168.168", "edge", "ready",
                               "test", "fixture")
        self.topology.add_node("3", "Edge 170", "143.14.168.170", "edge", "ready",
                               "test", "fixture")

    def _ssh_completed(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = argv[-1]
        if "haproxy -c" in command:
            return subprocess.CompletedProcess(argv, 0, "Configuration file is valid\n", "")
        if "nginx -t" in command:
            return subprocess.CompletedProcess(argv, 0, "nginx: configuration file /etc/nginx/nginx.conf test is successful\n", "")
        if "systemctl is-active" in command:
            return subprocess.CompletedProcess(argv, 0, "active\nactive\nactive\n", "")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def test_manual_failover_completes_and_records_event(self) -> None:
        from cli import mago_cdn as menu

        temp_root = Path(self.tempdir.name)
        node_id = temp_root / "node-id"
        node_id.write_text("1\n", encoding="utf-8")

        with patch.object(menu, "LOCAL_NODE_ID", node_id), \
             patch.object(menu, "message"), \
             patch.object(menu, "confirm", side_effect=[True, True]), \
             patch.object(menu, "ask", side_effect=[
                 "Falha confirmada no .111",
                 "id-da-acao-do-provedor",
                 "CONFIRMO_ISOLAMENTO_DO_111",
             ]), \
             patch.object(menu, "_run_manual_failover_lab", return_value={"command": ["lab"], "stdout": "lab ok"}), \
             patch.object(menu, "reconcile_cluster_dns", return_value={"pool": [], "aliases": []}), \
             patch.object(menu.subprocess, "run", side_effect=lambda *args, **kwargs: (
                 self._ssh_completed(args[0]) if args[0] and args[0][0] == "ssh"
                 else subprocess.CompletedProcess(args[0], 0, "lab ok\n", "")
             )):
            menu.manual_controller_failover(self.db)

        target = self.topology.node("4")
        self.assertEqual(target["state"], "active")
        event = self.topology.events("4")[-1]
        self.assertEqual(event["event_type"], "manual_failover")
        self.assertEqual(event["payload"]["operation"], "manual_dns_controller_failover")
        self.assertEqual(event["payload"]["source_node"], "1")
        self.assertEqual(event["payload"]["target_node"], "4")
        self.assertEqual(event["payload"]["isolation_reference"], "id-da-acao-do-provedor")
        self.assertEqual(event["payload"]["lab_result"]["status"], "ok")

    def test_manual_failover_rejects_wrong_confirmation_phrase(self) -> None:
        from cli import mago_cdn as menu

        temp_root = Path(self.tempdir.name)
        node_id = temp_root / "node-id"
        node_id.write_text("1\n", encoding="utf-8")

        with patch.object(menu, "LOCAL_NODE_ID", node_id), \
             patch.object(menu, "message"), \
             patch.object(menu, "confirm", return_value=True), \
             patch.object(menu, "ask", side_effect=[
                 "Falha confirmada no .111",
                 "id-da-acao-do-provedor",
                 "ERRADO",
             ]):
            menu.manual_controller_failover(self.db)

        self.assertEqual(
            [event["event_type"] for event in self.topology.events("4") if event["event_type"] == "manual_failover"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
