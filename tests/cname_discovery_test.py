#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cname_discovery import (
    DiscoveryError,
    build_tenant_index,
    discover_alias,
    normalize_discovery_host,
)
from core.db import Database


class CnameDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cdnmnus-cname-test-")
        self.db = Database(Path(self.tmp.name) / "admin.db")
        self.db.initialize()
        self.db.add_tenant("xuilab", "Lab", "xuilab.phpd77.com", "origin-lab.test")
        self.db.add_tenant("tvbrasil", "TV", "tvbrasil.phpd77.com", "origin-tv.test")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_discovery_follows_cname_chain_and_uses_terminal_tenant(self) -> None:
        tenants = build_tenant_index(reversed(self.db.tenants(enabled_only=True)))
        responses = {
            "on.acxxl.com": {"cname": "edge-hop.example", "ttl": 60},
            "edge-hop.example": {"cname": "xuilab.phpd77.com", "ttl": 45},
            "xuilab.phpd77.com": {"addresses": ["1.1.1.1"], "ttl": 30},
        }

        result = discover_alias("ON.acxxl.com.", tenants, lambda host: responses[normalize_discovery_host(host)], now=1_700_000_000.0)
        self.assertEqual(result.alias_host, "on.acxxl.com")
        self.assertEqual(result.canonical_host, "xuilab.phpd77.com")
        self.assertEqual(result.tenant_id, "xuilab")
        self.assertEqual(len(result.observed_chain), 3)
        self.assertEqual(result.observed_chain[-1].addresses, ("1.1.1.1",))
        self.assertEqual(result.expires_at, 1_700_000_030.0)
        self.assertEqual(len(result.decision_id), 64)

    def test_loop_private_ip_and_disabled_tenant_are_rejected(self) -> None:
        tenants = build_tenant_index(self.db.tenants(enabled_only=True))
        loop = {
            "loop.test": {"cname": "next.loop.test", "ttl": 30},
            "next.loop.test": {"cname": "loop.test", "ttl": 30},
        }
        with self.assertRaises(DiscoveryError):
            discover_alias("loop.test", tenants, lambda host: loop[normalize_discovery_host(host)])

        private = {"alias.test": {"addresses": ["10.0.0.1"], "ttl": 30}}
        with self.assertRaises(DiscoveryError):
            discover_alias("alias.test", tenants, lambda host: private[normalize_discovery_host(host)])

        disabled = build_tenant_index([
            {"id": "xui1", "canonical_host": "disabled.example", "enabled": 0},
        ])
        terminal = {
            "alias.example": {"cname": "disabled.example", "ttl": 30},
            "disabled.example": {"addresses": ["1.1.1.1"], "ttl": 30},
        }
        with self.assertRaises(DiscoveryError):
            discover_alias("alias.example", disabled, lambda host: terminal[normalize_discovery_host(host)])

    def test_chain_length_ttl_and_reserved_prefix_are_fail_closed(self) -> None:
        tenants = build_tenant_index(self.db.tenants(enabled_only=True))
        long_chain = {
            "alias.test": {"cname": "hop1.test", "ttl": 30},
            "hop1.test": {"cname": "hop2.test", "ttl": 30},
            "hop2.test": {"cname": "hop3.test", "ttl": 30},
            "hop3.test": {"cname": "hop4.test", "ttl": 30},
            "hop4.test": {"cname": "xuilab.phpd77.com", "ttl": 30},
            "xuilab.phpd77.com": {"addresses": ["1.1.1.1"], "ttl": 30},
        }
        with self.assertRaises(DiscoveryError):
            discover_alias("alias.test", tenants, lambda host: long_chain[normalize_discovery_host(host)])

        too_small_ttl = {
            "alias.test": {"cname": "xuilab.phpd77.com", "ttl": 10},
            "xuilab.phpd77.com": {"addresses": ["1.1.1.1"], "ttl": 10},
        }
        with self.assertRaises(DiscoveryError):
            discover_alias("alias.test", tenants, lambda host: too_small_ttl[normalize_discovery_host(host)])

        reserved = {
            "alias.test": {"cname": "__cdnmnus_alias.example", "ttl": 30},
        }
        with self.assertRaises(DiscoveryError):
            discover_alias("alias.test", tenants, lambda host: reserved[normalize_discovery_host(host)])

    def test_canonical_may_point_to_shared_cdn_endpoint(self) -> None:
        tenants = build_tenant_index(self.db.tenants(enabled_only=True))
        responses = {
            "on.acxxl.com": {"cname": "xuilab.phpd77.com", "ttl": 60},
            "xuilab.phpd77.com": {"cname": "cdn.example", "ttl": 60},
            "cdn.example": {"addresses": ["1.1.1.1"], "ttl": 60},
        }
        result = discover_alias("on.acxxl.com", tenants, lambda host: responses[normalize_discovery_host(host)])
        self.assertEqual(result.tenant_id, "xuilab")
        self.assertEqual(result.canonical_host, "xuilab.phpd77.com")
        self.assertEqual(result.observed_chain[-1].addresses, ("1.1.1.1",))

    def test_duplicate_canonical_detection(self) -> None:
        with self.assertRaises(DiscoveryError):
            build_tenant_index([
                {"id": "xui1", "canonical_host": "dup.example"},
                {"id": "xui2", "canonical_host": "dup.example"},
            ])


if __name__ == "__main__":
    unittest.main()
