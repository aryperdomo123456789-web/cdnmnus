#!/usr/bin/env python3
"""Offline contracts for the opt-in PostgreSQL migration laboratory."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from core.postgres_lab import (
    LAB_ACK,
    FailoverCoordinator,
    FakeFencingProvider,
    FencingError,
    LogicalSnapshot,
    MemoryLeaseStore,
    backup_restore_plan,
    capture_sqlite,
    compare_snapshots,
    validate_lab_dsn,
)


def expect(error: type[BaseException], operation) -> None:
    try:
        operation()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def create_source(path: Path) -> None:
    migrations = Path("database/postgresql/migrations/0001_current_control_plane.sql")
    # SQLite fixture mirrors only the portable source tables, not PostgreSQL SQL.
    schema = """
    CREATE TABLE xui_tenants(id TEXT PRIMARY KEY,name TEXT,canonical_host TEXT,
      config_version INTEGER,enabled INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE tenant_hosts(hostname TEXT PRIMARY KEY,tenant_id TEXT,
      is_canonical INTEGER,tls_status TEXT,
      FOREIGN KEY(tenant_id) REFERENCES xui_tenants(id));
    CREATE TABLE tenant_upstreams(id TEXT PRIMARY KEY,tenant_id TEXT,kind TEXT,
      host TEXT,port INTEGER,FOREIGN KEY(tenant_id) REFERENCES xui_tenants(id));
    CREATE TABLE edges(id TEXT PRIMARY KEY,name TEXT,ipv4 TEXT,ssh_port INTEGER,
      ssh_user TEXT,host_key_sha256 TEXT,state TEXT,deployed_version TEXT,
      last_health_at TEXT,last_health_status INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE dns_records(hostname TEXT,record_type TEXT,target_ip TEXT,edge_id TEXT,
      status TEXT,PRIMARY KEY(hostname,record_type,target_ip));
    CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
    CREATE TABLE deployments(id TEXT PRIMARY KEY,state TEXT,release_id TEXT,
      config_digest TEXT,artifact_path TEXT,error TEXT,created_at TEXT,
      started_at TEXT,finished_at TEXT);
    """
    assert migrations.is_file()
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.execute("INSERT INTO xui_tenants VALUES(?,?,?,?,?,?,?)", (
        "tenant-1", "Tenant", "example.test", 1, 1, "2026-08-29", "2026-08-29"
    ))
    db.execute("INSERT INTO tenant_hosts VALUES(?,?,?,?)", (
        "example.test", "tenant-1", 1, "valid"
    ))
    db.commit()
    db.close()


with tempfile.TemporaryDirectory(prefix="cdnmnus-pg-lab-") as temporary:
    source_path = Path(temporary) / "source.db"
    create_source(source_path)
    source = capture_sqlite(source_path)
    assert source.counts()["xui_tenants"] == 1
    assert compare_snapshots(source, LogicalSnapshot(dict(source.tables))).matching

expect(ValueError, lambda: compare_snapshots(source, LogicalSnapshot({})))
expect(RuntimeError, lambda: validate_lab_dsn("postgresql://db/lab?sslmode=require"))
expect(ValueError, lambda: validate_lab_dsn("postgresql://db/lab?sslmode=disable", LAB_ACK))
validate_lab_dsn("postgresql://db/lab?sslmode=verify-full", LAB_ACK)

clock = [100.0]
leases = MemoryLeaseStore(lambda: clock[0])
first = leases.acquire("public-lb", "lb-1", 10)
assert first is not None and first.fencing_token == 1
assert leases.acquire("public-lb", "lb-2", 10) is None
clock[0] = 111.0
second = leases.acquire("public-lb", "lb-2", 10)
assert second is not None and second.fencing_token == 2
assert leases.renew(first, 10) is None

fencing = FakeFencingProvider()
coordinator = FailoverCoordinator(leases, fencing)
clock[0] = 122.0
activated = []
promoted = coordinator.promote("public-lb", "lb-1", "lb-2", activated.append, 10)
assert activated == [promoted]
assert fencing.events[-1]["fencing_token"] == promoted.fencing_token
expect(FencingError, lambda: fencing.fence("public-lb", "lb-2", promoted.fencing_token))

clock[0] = 133.0
fencing.available = False
expect(FencingError, lambda: coordinator.promote(
    "public-lb", "lb-2", "lb-1", lambda lease: None, 10
))
fencing.available = True
recovered = leases.acquire("public-lb", "lb-1", 10)
assert recovered is not None, "failed promotion must release its lease"

plan = backup_restore_plan("cdnmnus_lab", "cdnmnus_restore", "/tmp/cdnmnus.dump")
assert "--clean" not in plan.backup_command
assert "--single-transaction" in plan.restore_command
expect(ValueError, lambda: backup_restore_plan("bad service", "restore", "/tmp/x.dump"))
expect(ValueError, lambda: backup_restore_plan("lab", "restore", "/tmp/x.sql"))

print("postgres lab snapshot/TLS/lease/fencing/backup contracts: OK")
