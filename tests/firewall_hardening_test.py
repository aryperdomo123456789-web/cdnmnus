#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "firewall_hardening.sh"


class FirewallHardeningTest(unittest.TestCase):
    def run_script(self, *args: str) -> str:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout

    def test_edge_profile_keeps_ssh_http_and_https_public(self) -> None:
        output = self.run_script(
            "--profile", "edge",
            "--backend-port", "3000",
            "--ssh-port", "22",
            "--ssh-allow-from", "143.14.168.111",
            "--ssh-allow-from", "143.14.168.170",
        )
        self.assertIn("ufw default deny incoming", output)
        self.assertIn("ufw allow 443/tcp", output)
        self.assertIn("ufw allow 80/tcp", output)
        self.assertIn("ufw allow 22/tcp", output)
        self.assertNotIn("ufw deny 3000/tcp", output)

    def test_load_balancer_profile_keeps_http_https_and_ssh_public(self) -> None:
        output = self.run_script(
            "--profile", "load_balancer",
            "--backend-port", "3000",
            "--ssh-port", "22",
        )
        self.assertIn("ufw allow 443/tcp", output)
        self.assertIn("ufw allow 80/tcp", output)
        self.assertIn("ufw allow 22/tcp", output)
        self.assertNotIn("ufw deny 3000/tcp", output)


if __name__ == "__main__":
    unittest.main()
