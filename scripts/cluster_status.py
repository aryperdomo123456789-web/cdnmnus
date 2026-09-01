#!/usr/bin/env python3
"""Snapshot operacional do cluster para consumo pelo menu SSH."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CDNMNUS_PROJECT_ROOT", "/opt/cdnmnus")).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.topology import TopologyStore  # noqa: E402


def source_ip() -> str | None:
    fields = os.environ.get("SSH_CONNECTION", "").split()
    if len(fields) == 4:
        return fields[0]
    return None


def main() -> int:
    database = Database(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    database.initialize()
    topology = TopologyStore(database)
    topology.initialize()
    requester = source_ip()
    if requester is not None:
        allowed = database.rows(
            "SELECT id,role,state FROM nodes WHERE ipv4=?",
            (requester,),
        )
        if not allowed and os.geteuid() != 0:
            raise PermissionError("sessão SSH não autorizada para consultar o cluster")
    snapshot = topology.capacity_snapshot()
    counts = {
        "nodes": len(snapshot),
        "edges": sum(1 for item in snapshot if item["node"]["role"] == "edge"),
        "load_balancers": sum(1 for item in snapshot if item["node"]["role"] == "load_balancer"),
        "ready": sum(1 for item in snapshot if item["node"]["state"] == "ready"),
        "candidate": sum(1 for item in snapshot if item["node"]["state"] == "candidate"),
        "standby": sum(1 for item in snapshot if item["node"]["state"] == "standby"),
        "active": sum(1 for item in snapshot if item["node"]["state"] == "active"),
    }
    summary = {
        "capacity_mbps": sum(item["capacity_mbps"] or 0 for item in snapshot),
        "usable_mbps": round(sum(item["usable_mbps"] or 0 for item in snapshot), 2),
        "tx_mbps": round(sum(item["tx_mbps"] or 0 for item in snapshot), 2),
        "pressure": round(max((item["pressure"] or 0 for item in snapshot), default=0), 4),
        "pressured_nodes": sum(1 for item in snapshot if (item["pressure"] or 0) >= 0.7),
        "draining_nodes": sum(1 for item in snapshot if item["runtime"] and item["runtime"]["state"] == "draining"),
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "requester_ip": requester,
        "counts": counts,
        "summary": summary,
        "nodes": snapshot,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
