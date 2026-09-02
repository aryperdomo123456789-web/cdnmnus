"""Estado desejado DNS para edges, XUIs e aliases sem expor origens."""
from __future__ import annotations

from typing import Any

from core.cloudflare_dns import CloudflareDNS
from core.db import Database


class DNSReconciler:
    def __init__(self, provider: CloudflareDNS, *, protected_target: str = "cdn.phpd77.com",
                 db: Database | None = None, operator: str = "control-plane") -> None:
        self.provider = provider
        self.protected_target = protected_target.rstrip(".").lower()
        self.db = db
        self.operator = operator

    def _event(self, action: str, name: str, record_type: str, desired: object,
               result: str, *, observed: object = None, error: str | None = None) -> None:
        if self.db:
            self.db.record_dns_event(action, name, record_type, desired,
                                     result=result, observed=observed, error=error,
                                     operator=self.operator)

    def desired_for_tenant(self, tenant: dict[str, Any]) -> list[dict[str, str]]:
        return [{"name": host["hostname"], "type": "CNAME", "content": self.protected_target}
                for host in tenant.get("hosts", []) if host["hostname"].rstrip(".").lower() != self.protected_target]

    def apply_tenant(self, tenant: dict[str, Any]) -> list[dict[str, str]]:
        applied = []
        for record in self.desired_for_tenant(tenant):
            try:
                self.provider.zone_for_name(record["name"])
            except Exception as exc:
                # External DNS providers may point an alias at our managed
                # hostname. Keep the alias in Nginx, but never write its zone.
                if "fora das zonas autorizadas" not in str(exc):
                    raise
                self._event("external-alias", record["name"], "CNAME", record, "succeeded",
                            observed={"managed_by": "external-dns"})
                applied.append(record)
                continue
            self._event("tenant-alias", record["name"], "CNAME", record, "started")
            try:
                self.provider.delete_records(record["name"], record_types={"A", "AAAA"})
                observed = self.provider.upsert(record["name"], record["type"], record["content"], proxied=False)
            except Exception as exc:
                self._event("tenant-alias", record["name"], "CNAME", record, "failed", error=str(exc))
                raise
            self._event("tenant-alias", record["name"], "CNAME", record, "succeeded", observed=observed)
            applied.append(record)
        return applied

    def repair_canonical_pool(self, canonical: str, edge_ips: list[str], *, forbidden_ips: set[str] | None = None) -> list[dict[str, str]]:
        forbidden = {ip.strip() for ip in (forbidden_ips or set())}
        desired = [ip for ip in edge_ips if ip not in forbidden]
        if not desired:
            raise ValueError("nenhuma edge válida para publicar")
        self._event("canonical-pool", canonical, "A", desired, "started")
        try:
            self.provider.delete_records(canonical, record_types={"A", "AAAA", "CNAME"})
            for ip in desired:
                self.provider.upsert(canonical, "A", ip, proxied=False)
        except Exception as exc:
            self._event("canonical-pool", canonical, "A", desired, "failed", error=str(exc))
            raise
        self._event("canonical-pool", canonical, "A", desired, "succeeded")
        return [{"name": canonical, "type": "A", "content": ip} for ip in desired]


def _load_balancer_ips(db: Database) -> set[str]:
    """Return LB addresses so they can never enter the direct edge pool."""
    tables = {row["name"] for row in db.rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nodes', 'load_balancers')"
    )}
    if "nodes" not in tables:
        return set()
    rows = db.rows("SELECT ipv4 FROM nodes WHERE role='load_balancer'")
    return {str(row["ipv4"]).strip() for row in rows if row["ipv4"]}


def _capacity_blocked_edge_ips(db: Database) -> set[str]:
    """Exclude only edges explicitly marked unable to accept new sessions."""
    rows = db.rows("""
        SELECT n.ipv4
          FROM nodes n
          JOIN node_capacity_runtime r ON r.node_id=n.id
         WHERE n.role='edge' AND r.state IN ('draining','saturated','down')
    """)
    return {str(row["ipv4"]).strip() for row in rows if row["ipv4"]}


def reconcile_cluster_dns(db: Database, *, operator: str = "control-plane",
                          canonical: str | None = None) -> dict[str, list[dict[str, str]]]:
    """Publish a DNS-only pool that sends media directly to healthy edges.

    LBs are control-plane/failover infrastructure in this mode. They must not
    become an accidental data-plane hop when a node is added to the inventory.
    """
    canonical = canonical or db.setting("managed_canonical_host", "cdn.phpd77.com")
    provider = CloudflareDNS()
    reconciler = DNSReconciler(provider, protected_target=canonical, db=db, operator=operator)
    edges = [item for item in db.edges() if item["state"] == "ready"]
    lb_ips = _load_balancer_ips(db)
    edge_ips = [item["ipv4"] for item in edges]
    accidental_lbs = set(edge_ips) & lb_ips
    if accidental_lbs:
        raise RuntimeError(
            "pool DNS direto contém IP registrado como load balancer: "
            + ", ".join(sorted(accidental_lbs))
        )
    pool = reconciler.repair_canonical_pool(
        canonical, edge_ips,
        forbidden_ips=lb_ips | {"143.14.168.111"} | _capacity_blocked_edge_ips(db),
    )
    aliases: list[dict[str, str]] = []
    for tenant in db.tenants(enabled_only=True):
        aliases.extend(reconciler.apply_tenant(tenant))
    return {"pool": pool, "aliases": aliases}
