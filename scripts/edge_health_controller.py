#!/usr/bin/env python3
"""Sonda central das Edges e reconcilia o pool DNS somente quando necessário."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CDNMNUS_PROJECT_ROOT", "/opt/cdnmnus")).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.dns_reconciler import reconcile_cluster_dns  # noqa: E402
from core.capacity_policy import evaluate_capacity  # noqa: E402
from core.topology import TopologyStore  # noqa: E402

TIMEOUT = max(2, int(os.environ.get("CDNMNUS_EDGE_HEALTH_TIMEOUT", "8")))
FAIL_THRESHOLD = max(1, int(os.environ.get("CDNMNUS_EDGE_HEALTH_FAIL_THRESHOLD", "3")))
RECOVER_THRESHOLD = max(1, int(os.environ.get("CDNMNUS_EDGE_HEALTH_RECOVER_THRESHOLD", "5")))


def probe(ip: str, host: str) -> tuple[bool, int | None, str]:
    command = [
        "curl", "--silent", "--show-error", "--fail", "--noproxy", "*",
        "--connect-timeout", str(TIMEOUT), "--max-time", str(TIMEOUT),
        "--resolve", f"{host}:443:{ip}", "--output", os.devnull,
        "--write-out", "%{http_code}", f"https://{host}/edge-health",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=TIMEOUT + 2, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, type(exc).__name__
    code = result.stdout.strip()
    status = int(code) if code.isdigit() else None
    return result.returncode == 0 and status == 200, status, "http_probe"


def main() -> int:
    db = Database()
    db.initialize()
    topology = TopologyStore(db)
    topology.initialize()
    changed = False
    results = []
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        return 0
    hosts = [str(item.get("health_host") or item["canonical_host"]) for item in tenants]
    for edge in db.edges():
        if edge["state"] not in {"ready", "failed"}:
            continue
        healthy = True
        status = None
        detail = ""
        for host in hosts:
            ok, status, detail = probe(edge["ipv4"], host)
            if not ok:
                healthy = False
                break
        before = edge["state"]
        after = db.record_edge_health(
            edge["id"], status, healthy=healthy,
            fail_threshold=FAIL_THRESHOLD, recover_threshold=RECOVER_THRESHOLD,
            reason="; ".join(hosts),
        )
        changed |= before != after["state"]
        capacity = topology.capacity_profile(edge["id"])
        sample_rows = db.rows(
            "SELECT * FROM node_capacity_samples WHERE node_id=? ORDER BY sampled_at DESC,id DESC LIMIT 1",
            (edge["id"],),
        )
        capacity_result = None
        if capacity and sample_rows:
            capacity_result = evaluate_capacity(capacity, sample_rows[0])
            previous = topology.capacity_runtime(edge["id"])
            topology.set_capacity_runtime(
                edge["id"], capacity_result["state"], capacity_result["pressure"],
                capacity_result["desired_weight"], capacity_result["desired_weight"],
                "capacity policy from latest sanitized sample",
            )
            changed |= not previous or previous["state"] != capacity_result["state"]
        results.append({"edge_id": edge["id"], "healthy": healthy,
                        "status": status, "state": after["state"], "detail": detail})
        if capacity_result is not None:
            results[-1]["capacity"] = capacity_result
    if changed:
        active = [item["ipv4"] for item in db.edges() if item["state"] == "ready"]
        if active:
            db.sync_dns_matrix()
            reconcile_cluster_dns(db, operator="health-controller")
    print(json.dumps({"changed": changed, "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
