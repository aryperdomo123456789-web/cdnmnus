"""Persistência SQLite do plano de controle multi-tenant/multi-edge."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_DB_PATH = Path(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"(?=^.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
EDGE_STATES = {"pending", "bootstrapping", "ready", "draining", "failed", "disabled"}
UPSTREAM_KINDS = {"origin", "lb", "vod"}


def normalize_id(value: str, label: str = "id") -> str:
    result = value.strip().lower()
    if not ID_RE.fullmatch(result):
        raise ValueError(f"{label} deve corresponder a [a-z0-9][a-z0-9_-] e ter até 32 caracteres")
    return result


def normalize_hostname(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(host):
        raise ValueError("hostname inválido")
    return host


def normalize_port(value: int | str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("porta deve estar entre 1 e 65535")
    return port


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS xui_tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    canonical_host TEXT NOT NULL UNIQUE,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tenant_hosts (
                    hostname TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
                    is_canonical INTEGER NOT NULL DEFAULT 0 CHECK(is_canonical IN (0,1)),
                    tls_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(tls_status IN ('pending','valid','failed','disabled'))
                );
                CREATE TABLE IF NOT EXISTS tenant_upstreams (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('origin','lb','vod')),
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
                    UNIQUE(tenant_id, kind, host, port)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_origin_per_tenant
                    ON tenant_upstreams(tenant_id) WHERE kind='origin';
                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    ipv4 TEXT NOT NULL UNIQUE,
                    ssh_port INTEGER NOT NULL DEFAULT 22 CHECK(ssh_port BETWEEN 1 AND 65535),
                    ssh_user TEXT NOT NULL,
                    host_key_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('pending','bootstrapping','ready','draining','failed','disabled')),
                    deployed_version TEXT,
                    last_health_at TEXT,
                    last_health_status INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS dns_records (
                    hostname TEXT NOT NULL REFERENCES tenant_hosts(hostname) ON DELETE CASCADE,
                    record_type TEXT NOT NULL CHECK(record_type IN ('A','AAAA','CNAME')),
                    target_ip TEXT NOT NULL,
                    edge_id TEXT REFERENCES edges(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','active','draining','failed')),
                    PRIMARY KEY(hostname, record_type, target_ip)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN
                        ('queued','running','succeeded','failed','rolled_back')),
                    release_id TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT
                );
            """)
        try:
            os.chmod(self.path, 0o600)
        except PermissionError:
            return

    def rows(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def add_tenant(self, tenant_id: str, name: str, canonical_host: str,
                   origin_host: str, origin_port: int = 80,
                   load_balancers: Sequence[str] = ()) -> dict[str, Any]:
        tenant_id = normalize_id(tenant_id, "tenant_id")
        canonical_host = normalize_hostname(canonical_host)
        origin_host = normalize_hostname(origin_host)
        origin_port = normalize_port(origin_port)
        if not name.strip():
            raise ValueError("nome do tenant é obrigatório")
        lbs = list(dict.fromkeys(normalize_hostname(item) for item in load_balancers if item.strip()))
        with self.transaction(immediate=True) as db:
            db.execute("INSERT INTO xui_tenants(id,name,canonical_host) VALUES(?,?,?)",
                       (tenant_id, name.strip(), canonical_host))
            db.execute("INSERT INTO tenant_hosts(hostname,tenant_id,is_canonical) VALUES(?,?,1)",
                       (canonical_host, tenant_id))
            db.execute("INSERT INTO tenant_upstreams(id,tenant_id,kind,host,port) VALUES(?,?,?,?,?)",
                       (f"origin-{tenant_id}", tenant_id, "origin", origin_host, origin_port))
            for host in lbs:
                db.execute("INSERT INTO tenant_upstreams(id,tenant_id,kind,host,port) VALUES(?,?,?,?,80)",
                           (f"lb-{tenant_id}-{uuid.uuid4().hex[:10]}", tenant_id, "lb", host))
        return self.tenant(tenant_id)

    def add_cname(self, tenant_id: str, hostname: str) -> dict[str, Any]:
        tenant_id = normalize_id(tenant_id, "tenant_id")
        hostname = normalize_hostname(hostname)
        with self.transaction(immediate=True) as db:
            if db.execute("SELECT 1 FROM xui_tenants WHERE id=?", (tenant_id,)).fetchone() is None:
                raise ValueError("tenant não encontrado")
            db.execute("INSERT INTO tenant_hosts(hostname,tenant_id,is_canonical) VALUES(?,?,0)",
                       (hostname, tenant_id))
            db.execute("UPDATE xui_tenants SET config_version=config_version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (tenant_id,))
        return {"tenant_id": tenant_id, "hostname": hostname, "tls_status": "pending"}

    def add_upstream(self, tenant_id: str, kind: str, host: str, port: int = 80) -> dict[str, Any]:
        tenant_id = normalize_id(tenant_id, "tenant_id")
        if kind not in {"lb", "vod"}:
            raise ValueError("tipo de upstream inválido")
        host = normalize_hostname(host)
        port = normalize_port(port)
        upstream_id = f"{kind}-{tenant_id}-{uuid.uuid4().hex[:10]}"
        with self.transaction(immediate=True) as db:
            if db.execute("SELECT 1 FROM xui_tenants WHERE id=?", (tenant_id,)).fetchone() is None:
                raise ValueError("tenant não encontrado")
            db.execute("INSERT INTO tenant_upstreams(id,tenant_id,kind,host,port) VALUES(?,?,?,?,?)",
                       (upstream_id, tenant_id, kind, host, port))
            db.execute("UPDATE xui_tenants SET config_version=config_version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (tenant_id,))
        return next(item for item in self.tenant(tenant_id)["upstreams"] if item["id"] == upstream_id)

    def update_upstream(self, upstream_id: str, host: str, port: int = 80) -> dict[str, Any]:
        host = normalize_hostname(host); port = normalize_port(port)
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT tenant_id,kind FROM tenant_upstreams WHERE id=?", (upstream_id,)).fetchone()
            if row is None or row["kind"] not in {"lb", "vod"}:
                raise ValueError("upstream editável não encontrado")
            db.execute("UPDATE tenant_upstreams SET host=?,port=? WHERE id=?", (host, port, upstream_id))
            db.execute("UPDATE xui_tenants SET config_version=config_version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["tenant_id"],))
        return next(item for item in self.tenant(row["tenant_id"])["upstreams"] if item["id"] == upstream_id)

    def delete_upstream(self, upstream_id: str) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT tenant_id,kind FROM tenant_upstreams WHERE id=?", (upstream_id,)).fetchone()
            if row is None or row["kind"] not in {"lb", "vod"}:
                raise ValueError("upstream removível não encontrado")
            db.execute("DELETE FROM tenant_upstreams WHERE id=?", (upstream_id,))
            db.execute("UPDATE xui_tenants SET config_version=config_version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["tenant_id"],))

    def add_edge(self, edge_id: str, name: str, ipv4: str, ssh_port: int,
                 ssh_user: str, fingerprint: str, state: str = "pending") -> dict[str, Any]:
        import ipaddress
        edge_id = normalize_id(edge_id, "edge_id")
        address = ipaddress.ip_address(ipv4.strip())
        if address.version != 4 or not address.is_global:
            raise ValueError("ipv4 da edge deve ser um endereço público global")
        if state not in EDGE_STATES:
            raise ValueError("estado de edge inválido")
        if not name.strip() or not ssh_user.strip():
            raise ValueError("nome e usuário SSH são obrigatórios")
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,host_key_sha256,state)
                        VALUES(?,?,?,?,?,?,?)""",
                       (edge_id, name.strip(), str(address), normalize_port(ssh_port),
                        ssh_user.strip(), fingerprint, state))
        return self.edge(edge_id)

    def set_edge_state(self, edge_id: str, state: str, version: str | None = None) -> None:
        if state not in EDGE_STATES:
            raise ValueError("estado de edge inválido")
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE edges SET state=?, deployed_version=COALESCE(?,deployed_version),
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""", (state, version, edge_id))
            if db.total_changes != 1:
                raise ValueError("edge não encontrada")

    def edge(self, edge_id: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM edges WHERE id=?", (edge_id,))
        if not rows:
            raise ValueError("edge não encontrada")
        return rows[0]

    def edges(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM edges ORDER BY name")

    def tenant(self, tenant_id: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM xui_tenants WHERE id=?", (tenant_id,))
        if not rows:
            raise ValueError("tenant não encontrado")
        item = rows[0]
        item["hosts"] = self.rows("SELECT * FROM tenant_hosts WHERE tenant_id=? ORDER BY is_canonical DESC,hostname", (tenant_id,))
        item["upstreams"] = self.rows("SELECT * FROM tenant_upstreams WHERE tenant_id=? ORDER BY kind,id", (tenant_id,))
        return item

    def tenants(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT id FROM xui_tenants" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return [self.tenant(row["id"]) for row in self.rows(sql)]

    def sync_dns_matrix(self) -> list[dict[str, Any]]:
        healthy = [edge for edge in self.edges() if edge["state"] == "ready"]
        hosts = self.rows("""SELECT h.hostname,h.tenant_id,h.tls_status
                           FROM tenant_hosts h JOIN xui_tenants t ON t.id=h.tenant_id
                           WHERE t.enabled=1 ORDER BY h.hostname""")
        with self.transaction(immediate=True) as db:
            db.execute("DELETE FROM dns_records")
            for host in hosts:
                for edge in healthy:
                    db.execute("INSERT INTO dns_records(hostname,record_type,target_ip,edge_id,status) VALUES(?,?,?,?,?)",
                               (host["hostname"], "A", edge["ipv4"], edge["id"], "active"))
        return [{**host, "targets": [edge["ipv4"] for edge in healthy]} for host in hosts]

    def dns_records(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM dns_records ORDER BY hostname,target_ip")

    def setting(self, key: str, default: Any = None) -> Any:
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                       (key, json.dumps(value, ensure_ascii=False)))
