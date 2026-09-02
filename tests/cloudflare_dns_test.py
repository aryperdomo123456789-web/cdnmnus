#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cloudflare_dns import CloudflareDNS
from core.dns_reconciler import DNSReconciler


class FakeCloudflare(CloudflareDNS):
    def __init__(self) -> None:
        self.zones = ["phpd.com", "phpd77.com"]
        self.token = "test"
        self.items = {
            "cdn.phpd77.com": [
                {"id": "bad", "type": "A", "name": "cdn.phpd77.com", "content": "143.14.168.111"},
            ],
            "outro.phpd77.com": [
                {"id": "other", "type": "A", "name": "outro.phpd77.com", "content": "192.0.2.10"},
            ],
        }

    def records(self, name=None):
        return list(self.items.get(name, []))

    def delete_records(self, name, *, record_types=None):
        old = self.items.get(name, [])
        self.items[name] = [x for x in old if record_types and x["type"] not in record_types]
        return len(old) - len(self.items[name])

    def upsert(self, name, record_type, content, *, proxied=False, ttl=300):
        item = {"id": content, "type": record_type, "name": name, "content": content, "proxied": proxied}
        if record_type in {"A", "AAAA"}:
            if any(x["type"] == record_type and x["content"] == content for x in self.items.get(name, [])):
                return next(x for x in self.items[name] if x["type"] == record_type and x["content"] == content)
            self.items[name] = self.items.get(name, []) + [item]
        else:
            self.items[name] = [x for x in self.items.get(name, []) if x["type"] != record_type] + [item]
        return item


class CloudflareDNSTest(unittest.TestCase):
    def test_zone_selection_is_most_specific(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            token = Path(root) / "token"
            token.write_text("secret\n")
            token.chmod(0o600)
            provider = CloudflareDNS(zone="phpd.com,phpd77.com", token_file=token)
            self.assertEqual(provider.zone_for_name("cdn.phpd77.com"), "phpd77.com")
            self.assertEqual(provider.zone_for_name("gomes.phpd.com"), "phpd.com")

    def test_reconciler_removes_forbidden_canonical_ip_and_creates_alias(self) -> None:
        provider = FakeCloudflare()
        reconciler = DNSReconciler(provider)
        tenant = {"hosts": [{"hostname": "gomes.phpd.com", "is_canonical": 1}]}
        self.assertEqual(reconciler.apply_tenant(tenant)[0]["content"], "cdn.phpd77.com")
        reconciler.repair_canonical_pool("cdn.phpd77.com", ["143.14.168.168", "143.14.168.170"],
                                         forbidden_ips={"143.14.168.111"})
        self.assertNotIn("143.14.168.111", [x["content"] for x in provider.items["cdn.phpd77.com"]])
        self.assertEqual(provider.items["outro.phpd77.com"][0]["content"], "192.0.2.10")

    def test_direct_pool_tracks_load_balancer_ips(self) -> None:
        from core.dns_reconciler import _load_balancer_ips

        class Inventory:
            def rows(self, query):
                if "sqlite_master" in query:
                    return [{"name": "nodes"}]
                return [{"ipv4": "143.14.168.111"}, {"ipv4": "45.140.192.237"}]

        self.assertEqual(_load_balancer_ips(Inventory()),
                         {"143.14.168.111", "45.140.192.237"})


if __name__ == "__main__":
    unittest.main()
