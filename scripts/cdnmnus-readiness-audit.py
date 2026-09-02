#!/usr/bin/env python3
"""Production readiness gate without changing traffic or node roles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database
from core.topology import TopologyStore


def check(name: str, ok: bool, detail: str, blockers: list[str]) -> dict[str, object]:
    if not ok:
        blockers.append(f"{name}: {detail}")
    return {"name": name, "ok": ok, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--skip-ssh", action="store_true", help="não sondar serviços remotos")
    args = parser.parse_args()

    db = Database(args.db)
    db.initialize()
    # Schema initialization is deliberately explicit here so an old control
    # plane can be audited after the topology migration. No role, lease, DNS,
    # service, or traffic state is changed by this command.
    topology = TopologyStore(db)
    topology.initialize()
    blockers: list[str] = []
    gates: list[dict[str, object]] = []

    nodes = db.rows("SELECT * FROM nodes ORDER BY CAST(id AS INTEGER), id")
    edges = [n for n in nodes if n["role"] == "edge"]
    ready = [n for n in edges if n["state"] == "ready"]
    releases = {n.get("release_id") for n in ready}
    digests = {n.get("node_config_digest") for n in ready}
    gates.append(check("edges-ready", len(ready) >= 2, f"{len(ready)} edges ready", blockers))
    gates.append(check("edge-release-convergence", len(releases) == 1 and None not in releases,
                       f"releases={sorted(releases)}", blockers))
    gates.append(check("edge-digest-convergence", len(digests) == 1 and None not in digests,
                       f"digests={sorted(digests)}", blockers))

    lbs = [n for n in nodes if n["role"] == "load_balancer"]
    active = [n for n in lbs if n["state"] == "active"]
    standby = [n for n in lbs if n["state"] == "standby"]
    gates.append(check("single-active-lb", len(active) == 1, f"active={len(active)}", blockers))
    gates.append(check("standby-lb", len(standby) >= 1, f"standby={len(standby)}", blockers))
    gates.append(check("lb-database-registration", len(db.rows("SELECT id FROM load_balancers")) >= 1,
                       "load_balancers registrados", blockers))
    gates.append(check("lb-backends", len(db.rows("SELECT * FROM lb_backends WHERE state='enabled'")) >= len(ready),
                       "backends enabled coerentes", blockers))
    gates.append(check("promotion-lock", bool(db.rows("SELECT * FROM promotion_locks")),
                       "lease/fencing ainda não registrado", blockers))
    gates.append(check("capacity-profiles", len(db.rows("SELECT * FROM node_capacity_profiles")) >= len(lbs),
                       "perfil contratado de todos os LBs", blockers))

    if not args.skip_ssh:
        inventory = Path("/opt/cdnmnus/ansible/inventories/production/hosts.yml")
        if inventory.is_file():
            try:
                import yaml
                data = yaml.safe_load(inventory.read_text(encoding="utf-8")) or {}
                hosts = data.get("all", {}).get("children", {}).get("load_balancers", {}).get("hosts", {})
                for node in lbs:
                    host = next((item for item in hosts.values() if str(item.get("ansible_host")) == node["ipv4"]), None)
                    gates.append(check(f"inventory-{node['ipv4']}", host is not None,
                                       "presente no inventário oficial" if host else "ausente do inventário",
                                       blockers))
            except (ImportError, OSError, ValueError) as exc:
                gates.append(check("inventory-parse", False, type(exc).__name__, blockers))
        else:
            gates.append(check("inventory-file", False, str(inventory), blockers))

    score = round(10 * sum(bool(item["ok"]) for item in gates) / max(1, len(gates)), 1)
    result = {"score": score, "ready": not blockers, "gates": gates, "blockers": blockers}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
