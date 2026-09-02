from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import panel.cname_gateway as gateway
from core.cname_discovery import DiscoveryError


class CnameGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.snapshot_path = Path(self.tempdir.name) / "tenants.json"
        self.snapshot_path.write_text(json.dumps({
            "schema_version": 1,
            "generation": 1,
            "automatic_cname_discovery": True,
            "tenants": {
                "xuilab": {
                    "public_hosts": ["xuilab.example"],
                    "origin": {"host": "198.51.100.10", "port": 80},
                    "load_balancers": [], "vod_hosts": [],
                },
                "tvbrasil": {
                    "public_hosts": ["tvbrasil.example"],
                    "origin": {"host": "198.51.100.20", "port": 80},
                    "load_balancers": [], "vod_hosts": [],
                },
            },
        }), encoding="utf-8")
        self.original_path = gateway.SNAPSHOT_PATH
        gateway.SNAPSHOT_PATH = self.snapshot_path

    def tearDown(self) -> None:
        gateway.SNAPSHOT_PATH = self.original_path
        self.tempdir.cleanup()

    def test_aliases_are_isolated_by_terminal_canonical(self) -> None:
        answers = {
            "on.example": {"cname": "xuilab.example", "ttl": 60},
            "cnxt.example": {"cname": "tvbrasil.example", "ttl": 60},
            "xuilab.example": {"addresses": ["8.8.8.8"], "ttl": 60},
            "tvbrasil.example": {"addresses": ["1.1.1.1"], "ttl": 60},
        }
        state = gateway.GatewayState(resolver=answers.__getitem__)
        self.assertEqual(state.decision("on.example").tenant_id, "xuilab")
        self.assertEqual(state.decision("cnxt.example").tenant_id, "tvbrasil")

    def test_unknown_terminal_fails_closed(self) -> None:
        answers = {
            "unknown.example": {"cname": "not-a-tenant.example", "ttl": 60},
            "not-a-tenant.example": {"addresses": ["9.9.9.9"], "ttl": 60},
        }
        state = gateway.GatewayState(resolver=answers.__getitem__)
        with self.assertRaises(DiscoveryError):
            state.decision("unknown.example")

    def test_private_terminal_fails_closed(self) -> None:
        answers = {
            "on.example": {"cname": "xuilab.example", "ttl": 60},
            "xuilab.example": {"addresses": ["10.0.0.10"], "ttl": 60},
        }
        state = gateway.GatewayState(resolver=answers.__getitem__)
        with self.assertRaises(DiscoveryError):
            state.decision("on.example")


if __name__ == "__main__":
    unittest.main()
