#!/usr/bin/env python3
"""Offline contract tests for the LB role; never opens a non-loopback socket."""

import http.client
import pathlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/load_balancer"


class FakeEdge(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/edge-health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, _format, *_args):
        pass


class LoadBalancerRoleTest(unittest.TestCase):
    def test_all_yaml_is_parseable(self):
        paths = list(ROLE.rglob("*.yml")) + [ROOT / "ansible/playbooks/load-balancer.yml"]
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNotNone(yaml.safe_load(path.read_text()))

    def test_action_files_exist(self):
        for action in ("preflight", "deploy", "promote", "drain", "demote", "rollback"):
            self.assertTrue((ROLE / "tasks" / f"{action}.yml").is_file())

    def test_deploy_cannot_publish_or_reload_current(self):
        deploy = (ROLE / "tasks/deploy.yml").read_text()
        self.assertIn("load_balancer_candidate_path", deploy)
        self.assertNotIn("load_balancer_config_path", deploy)
        self.assertNotIn("ansible.builtin.systemd", deploy)
        self.assertNotIn("notify:", deploy)

    def test_fail_closed_and_privacy_contract(self):
        preflight = (ROLE / "tasks/preflight.yml").read_text()
        template = (ROLE / "templates/haproxy.cfg.j2").read_text()
        self.assertIn("['127.0.0.1', '::1', 'localhost']", preflight)
        self.assertIn("load_balancer_operation_confirm", preflight)
        self.assertIn("load_balancer_action != 'promote' or load_balancer_mode == 'active'", preflight)
        self.assertIn("load_balancer_promotion_lease_id", preflight)
        self.assertIn("load_balancer_promotion_fencing_token", preflight)
        deploy = (ROLE / "tasks/deploy.yml").read_text()
        self.assertIn("edge-before.tar", deploy)
        for required in ("strict-sni", "/edge-health", "slowstart", "maxconn", "verify required"):
            self.assertIn(required, template)
        log_line = next(line for line in template.splitlines() if line.strip().startswith("log-format"))
        for forbidden in ("%r", "%HU", "query", "cookie", "Authorization"):
            self.assertNotIn(forbidden, log_line)

    def test_firewall_keeps_both_public_frontends_reachable(self):
        firewall = (ROLE / "tasks/firewall.yml").read_text()
        self.assertIn("ufw allow {{ load_balancer_http_port }}/tcp", firewall)
        self.assertIn("ufw allow 443/tcp", firewall)
        self.assertNotIn("ufw delete allow 80/tcp", firewall)

    def test_fake_edge_health_is_loopback_only(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEdge)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/edge-health", headers={"Host": "lab.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
