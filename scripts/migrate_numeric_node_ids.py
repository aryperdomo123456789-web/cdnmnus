#!/usr/bin/env python3
"""Migração única e auditável para IDs técnicos numéricos."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.db import Database
from core.topology import TopologyStore

EXPECTED = {
    "143.14.168.168": ("lb011", "2", "Edge 168"),
    "143.14.168.170": ("lb02", "3", "Edge 170"),
}


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_db = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_db = sqlite3.connect(destination)
    try:
        source_db.backup(target_db)
        if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup SQLite não passou no integrity_check")
    finally:
        target_db.close(); source_db.close()
    destination.chmod(0o600)


def migrate(database_path: Path, apply: bool) -> dict:
    database = Database(database_path)
    database.initialize()
    before = {item["ipv4"]: item for item in database.edges()}
    for ipv4, (old_id, new_id, _) in EXPECTED.items():
        item = before.get(ipv4)
        if item is None:
            raise RuntimeError(f"edge esperada ausente: {ipv4}")
        if item["id"] not in {old_id, new_id}:
            raise RuntimeError(f"ID inesperado para {ipv4}: {item['id']}")
    plan = {ipv4: {"from": before[ipv4]["id"], "to": values[1]} for ipv4, values in EXPECTED.items()}
    if not apply:
        return {"mode": "dry-run", "plan": plan, "next_id": "4"}

    for ipv4, (_, new_id, new_name) in EXPECTED.items():
        current = database.edge(next(item["id"] for item in database.edges() if item["ipv4"] == ipv4))
        if current["id"] != new_id:
            database.reassign_edge_id(current["id"], new_id, operator="numeric-id-migration",
                                      reason="padronização de identidade técnica aprovada")
        database.rename_edge(new_id, new_name, operator="numeric-id-migration",
                             reason="nome amigável alinhado ao papel EDGE")

    topology = TopologyStore(database)
    topology.initialize()
    try:
        node1 = topology.node("1")
        if node1["ipv4"] != "143.14.168.111" or node1["role"] != "load_balancer":
            raise RuntimeError("nó 1 existente diverge da identidade aprovada")
    except ValueError:
        topology.add_node("1", "Load Balancer Principal", "143.14.168.111",
                          "load_balancer", "candidate", "numeric-id-migration",
                          "registro do primeiro load balancer sem ativação")
    try:
        topology.load_balancer("public-lb")
    except ValueError:
        topology.add_load_balancer("public-lb", "1", "cdn.phpd77.com",
                                   "numeric-id-migration",
                                   "candidata registrada; publicação exige promoção e fencing")

    edges = database.edges()
    nodes = [topology.node(str(node_id)) for node_id in (1, 2, 3)]
    if [(item["id"], item["ipv4"]) for item in edges] != [
        ("2", "143.14.168.168"), ("3", "143.14.168.170")
    ]:
        raise RuntimeError("validação final das edges divergiu")
    if database.next_node_id() != "4":
        raise RuntimeError("próximo ID técnico não é 4")
    return {"mode": "applied", "edges": edges, "nodes": nodes, "next_id": "4"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="/var/lib/cdnmnus-admin/admin.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="/var/backups/cdnmnus")
    args = parser.parse_args()
    path = Path(args.database)
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_database(path, Path(args.backup_dir) / f"admin-before-numeric-ids-{stamp}.db")
    print(json.dumps(migrate(path, args.apply), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
