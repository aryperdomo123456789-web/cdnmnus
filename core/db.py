"""Persistência SQLite do plano de controle multi-tenant/multi-edge."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from urllib.parse import urlsplit, urlunsplit
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_DB_PATH = Path(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"(?=^.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
EDGE_STATES = {"pending", "bootstrapping", "ready", "draining", "failed", "disabled"}
EDGE_STATE_TRANSITIONS = {
    "pending": {"bootstrapping", "disabled"},
    "bootstrapping": {"ready", "failed", "disabled"},
    "ready": {"draining", "failed", "disabled"},
    "draining": {"ready", "failed", "disabled"},
    "failed": {"bootstrapping", "disabled"},
    "disabled": {"bootstrapping"},
}
UPSTREAM_KINDS = {"origin", "lb", "vod"}


def _sanitized_event_payload(value: Any) -> Any:
    """Remove segredos e query strings antes de persistir evidência operacional."""
    secret_fragments = ("password", "passwd", "secret", "token", "credential", "private_key")
    if isinstance(value, dict):
        return {
            str(key)[:64]: ("[REDACTED]" if any(part in str(key).lower() for part in secret_fragments)
                            else _sanitized_event_payload(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitized_event_payload(item) for item in value]
    if isinstance(value, str):
        text = value[:512]
        if "://" in text:
            try:
                parsed = urlsplit(text)
                host = parsed.hostname or ""
                if parsed.port:
                    host += f":{parsed.port}"
                return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
            except ValueError:
                return "[REDACTED_URL]"
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:512]


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
                    target_edge_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS edge_events (
                    id TEXT PRIMARY KEY,
                    edge_id TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_sanitized TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS node_id_sequence (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    next_id INTEGER NOT NULL CHECK(next_id > 0)
                );
                INSERT OR IGNORE INTO node_id_sequence(singleton,next_id)
                VALUES(1,1);
                UPDATE node_id_sequence
                   SET next_id=MAX(next_id, COALESCE(
                       (SELECT MAX(CAST(id AS INTEGER))+1 FROM edges
                         WHERE id <> '' AND id NOT GLOB '*[^0-9]*'), 1))
                 WHERE singleton=1;
                CREATE TABLE IF NOT EXISTS promotion_requests (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    requested_mode TEXT NOT NULL CHECK(requested_mode IN ('candidate','standby')),
                    state TEXT NOT NULL CHECK(state IN
                        ('requested','approved','installing','candidate','standby','rejected','failed','cancelled')),
                    package_ref TEXT NOT NULL,
                    package_commit TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    source_ip TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_promotion_request_per_node
                    ON promotion_requests(node_id)
                    WHERE state IN ('requested','approved','installing');
            """)
            deployment_columns = {
                row[1] for row in db.execute("PRAGMA table_info(deployments)").fetchall()
            }
            if "target_edge_id" not in deployment_columns:
                db.execute("ALTER TABLE deployments ADD COLUMN target_edge_id TEXT")
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

    @staticmethod
    def _allocate_node_id(db: sqlite3.Connection) -> str:
        row = db.execute("SELECT next_id FROM node_id_sequence WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("sequência de IDs técnicos não inicializada")
        node_id = str(int(row["next_id"]))
        db.execute("UPDATE node_id_sequence SET next_id=next_id+1 WHERE singleton=1")
        return node_id

    def next_node_id(self) -> str:
        rows = self.rows("SELECT next_id FROM node_id_sequence WHERE singleton=1")
        if not rows:
            raise RuntimeError("sequência de IDs técnicos não inicializada")
        return str(rows[0]["next_id"])

    def reserve_node_id(self) -> str:
        """Reserva atomicamente o próximo ID; falhas posteriores podem deixar lacunas."""
        with self.transaction(immediate=True) as db:
            return self._allocate_node_id(db)

    def add_edge(self, edge_id: str | None, name: str, ipv4: str, ssh_port: int,
                 ssh_user: str, fingerprint: str, state: str = "pending") -> dict[str, Any]:
        import ipaddress
        if edge_id is not None:
            edge_id = normalize_id(edge_id, "edge_id")
        address = ipaddress.ip_address(ipv4.strip())
        if address.version != 4 or not address.is_global:
            raise ValueError("ipv4 da edge deve ser um endereço público global")
        if state not in EDGE_STATES:
            raise ValueError("estado de edge inválido")
        if not name.strip() or not ssh_user.strip():
            raise ValueError("nome e usuário SSH são obrigatórios")
        with self.transaction(immediate=True) as db:
            if edge_id is None:
                edge_id = self._allocate_node_id(db)
            elif edge_id.isdigit():
                db.execute("UPDATE node_id_sequence SET next_id=MAX(next_id,?) WHERE singleton=1",
                           (int(edge_id) + 1,))
            db.execute("""INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,host_key_sha256,state)
                        VALUES(?,?,?,?,?,?,?)""",
                       (edge_id, name.strip(), str(address), normalize_port(ssh_port),
                        ssh_user.strip(), fingerprint, state))
            topology_enabled = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone()
            if topology_enabled:
                db.execute(
                    """INSERT INTO nodes(
                           id,name,ipv4,ssh_port,ssh_user,host_key_sha256,
                           role,state,capacity_json
                       ) VALUES(?,?,?,?,?,?,'edge',?,'{}')""",
                    (edge_id, name.strip(), str(address), normalize_port(ssh_port),
                     ssh_user.strip(), fingerprint, state),
                )
                db.execute(
                    """INSERT INTO node_events(
                           id,node_id,event_type,operator,reason,payload_sanitized)
                       VALUES(?,?,?,?,?,?)""",
                    ("node-evt-" + uuid.uuid4().hex, edge_id, "node_created",
                     "edge-bootstrap", "cadastro transacional da nova edge",
                     json.dumps({"role": "edge", "state": state}, sort_keys=True)),
                )
        return self.edge(edge_id)

    def reassign_edge_id(self, old_id: str, new_id: str, *, operator: str,
                         reason: str) -> dict[str, Any]:
        """Migra a identidade lógica preservando referências e dados operacionais."""
        old_id, new_id = normalize_id(old_id, "old_edge_id"), normalize_id(new_id, "new_edge_id")
        if not new_id.isdigit() or int(new_id) < 1:
            raise ValueError("o novo ID técnico deve ser um número inteiro positivo")
        operator = re.sub(r"[^a-zA-Z0-9_.@-]", "_", operator.strip())[:128]
        reason = str(_sanitized_event_payload(reason.strip())).replace("\n", " ")[:512]
        if not operator or not reason:
            raise ValueError("operador e motivo são obrigatórios")
        with self.transaction(immediate=True) as db:
            db.execute("PRAGMA defer_foreign_keys=ON")
            current = db.execute("SELECT state FROM edges WHERE id=?", (old_id,)).fetchone()
            if current is None:
                raise ValueError("edge de origem não encontrada")
            if db.execute("SELECT 1 FROM edges WHERE id=?", (new_id,)).fetchone():
                raise ValueError("ID técnico de destino já utilizado")
            db.execute("UPDATE edges SET id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (new_id, old_id))
            for table, column in (("dns_records", "edge_id"), ("edge_events", "edge_id")):
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',
                           (new_id, old_id))
            db.execute("""INSERT INTO edge_events
                        (id,edge_id,event_type,from_state,to_state,operator,reason,payload_sanitized)
                        VALUES(?,?,?,?,?,?,?,?)""",
                       ("evt-" + uuid.uuid4().hex, new_id, "edge_id_reassigned",
                        current["state"], current["state"], operator, reason,
                        json.dumps({"old_id": old_id, "new_id": new_id}, sort_keys=True)))
            db.execute("UPDATE node_id_sequence SET next_id=MAX(next_id,?) WHERE singleton=1",
                       (int(new_id) + 1,))
        return self.edge(new_id)

    def set_edge_state(self, edge_id: str, state: str, version: str | None = None, *,
                       operator: str = "legacy-api", reason: str = "state transition",
                       payload: dict[str, Any] | None = None,
                       config_digest: str | None = None) -> None:
        """Transiciona estado e grava a evidência na mesma transação.

        Os três argumentos posicionais históricos continuam aceitos. Chamadores
        operacionais devem informar ``operator`` e ``reason`` por keyword.
        """
        if state not in EDGE_STATES:
            raise ValueError("estado de edge inválido")
        operator = re.sub(r"[^a-zA-Z0-9_.@-]", "_", operator.strip())[:128]
        reason = str(_sanitized_event_payload(reason.strip())).replace("\n", " ")[:512]
        if not operator or not reason:
            raise ValueError("operador e motivo são obrigatórios")
        if config_digest is not None and not re.fullmatch(r"[a-f0-9]{64}", config_digest):
            raise ValueError("config_digest inválido")
        with self.transaction(immediate=True) as db:
            current = db.execute("SELECT state FROM edges WHERE id=?", (edge_id,)).fetchone()
            if current is None:
                raise ValueError("edge não encontrada")
            old_state = current["state"]
            if state != old_state and state not in EDGE_STATE_TRANSITIONS[old_state]:
                raise ValueError(f"transição de edge não permitida: {old_state} -> {state}")
            db.execute("""UPDATE edges SET state=?, deployed_version=COALESCE(?,deployed_version),
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""", (state, version, edge_id))
            event_payload = _sanitized_event_payload(payload or {})
            db.execute("""INSERT INTO edge_events
                        (id,edge_id,event_type,from_state,to_state,operator,reason,payload_sanitized)
                        VALUES(?,?,?,?,?,?,?,?)""",
                       ("evt-" + uuid.uuid4().hex, edge_id, "edge_state_transition",
                        old_state, state, operator, reason,
                        json.dumps(event_payload, ensure_ascii=False, sort_keys=True)))
            topology_enabled = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone()
            if topology_enabled:
                node = db.execute(
                    "SELECT role,state FROM nodes WHERE id=?", (edge_id,)
                ).fetchone()
                if node is None:
                    raise RuntimeError("topologia divergente: edge sem nó correspondente")
                if node["role"] != "edge" or node["state"] != old_state:
                    raise RuntimeError("topologia divergente: papel ou estado da edge não coincide")
                db.execute(
                    """UPDATE nodes SET state=?,release_id=COALESCE(?,release_id),
                           node_config_digest=COALESCE(?,node_config_digest),
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (state, version, config_digest, edge_id),
                )
                node_payload = {
                    "from": old_state, "to": state, "release_id": version,
                    "config_digest": config_digest,
                }
                db.execute(
                    """INSERT INTO node_events(
                           id,node_id,event_type,operator,reason,payload_sanitized)
                       VALUES(?,?,?,?,?,?)""",
                    ("node-evt-" + uuid.uuid4().hex, edge_id, "state_changed",
                     operator, reason,
                     json.dumps(_sanitized_event_payload(node_payload),
                                ensure_ascii=False, sort_keys=True)),
                )

    def edge_events(self, edge_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM edge_events WHERE edge_id=? ORDER BY created_at,id", (edge_id,))

    def rename_edge(self, edge_id: str, name: str, *, operator: str = "local-menu",
                    reason: str = "friendly name changed") -> dict[str, Any]:
        """Change only the friendly label; technical ID and connectivity stay fixed."""
        new_name = name.strip()
        if not new_name or len(new_name) > 80 or any(ord(char) < 32 for char in new_name):
            raise ValueError("nome da edge deve ter entre 1 e 80 caracteres visíveis")
        operator = re.sub(r"[^a-zA-Z0-9_.@-]", "_", operator.strip())[:128]
        reason = str(_sanitized_event_payload(reason.strip())).replace("\n", " ")[:512]
        if not operator or not reason:
            raise ValueError("operador e motivo são obrigatórios")
        with self.transaction(immediate=True) as db:
            current = db.execute("SELECT name,state FROM edges WHERE id=?", (edge_id,)).fetchone()
            if current is None:
                raise ValueError("edge não encontrada")
            if current["name"] == new_name:
                return self.edge(edge_id)
            try:
                db.execute("UPDATE edges SET name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (new_name, edge_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("já existe uma edge com esse nome") from exc
            db.execute("""INSERT INTO edge_events
                        (id,edge_id,event_type,from_state,to_state,operator,reason,payload_sanitized)
                        VALUES(?,?,?,?,?,?,?,?)""",
                       ("evt-" + uuid.uuid4().hex, edge_id, "edge_renamed",
                        current["state"], current["state"], operator, reason,
                        json.dumps({"old_name": current["name"], "new_name": new_name},
                                   ensure_ascii=False, sort_keys=True)))
        return self.edge(edge_id)

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

    def request_load_balancer_promotion(
        self, node_id: str, requested_mode: str, package_ref: str,
        package_commit: str, manifest_digest: str, source_ip: str, reason: str,
    ) -> dict[str, Any]:
        """Registra intenção; nunca muda papel, instala HAProxy ou promove o nó."""
        import ipaddress
        node_id = normalize_id(node_id, "node_id")
        if requested_mode not in {"candidate", "standby"}:
            raise ValueError("solicitação só pode pedir candidate ou standby")
        if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*", package_ref):
            raise ValueError("tag do pacote inválida")
        if not re.fullmatch(r"[a-f0-9]{40}", package_commit):
            raise ValueError("commit do pacote inválido")
        if not re.fullmatch(r"[a-f0-9]{64}", manifest_digest):
            raise ValueError("digest do manifesto inválido")
        source_ip = str(ipaddress.ip_address(source_ip))
        reason = str(_sanitized_event_payload(reason.strip())).replace("\n", " ")[:512]
        if not reason:
            raise ValueError("motivo obrigatório")
        request_id = "prom-" + uuid.uuid4().hex
        with self.transaction(immediate=True) as db:
            node = db.execute(
                "SELECT ipv4,role,state FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            if node is None:
                raise ValueError("nó não encontrado na topologia")
            if node["ipv4"] != source_ip:
                raise PermissionError("IP de origem não corresponde ao nó")
            if node["role"] != "edge" or node["state"] != "ready":
                raise ValueError("somente edge ready pode solicitar preparação para LB")
            db.execute(
                """INSERT INTO promotion_requests(
                       id,node_id,requested_mode,state,package_ref,package_commit,
                       manifest_digest,source_ip,reason)
                   VALUES(?,?,?,'requested',?,?,?,?,?)""",
                (request_id, node_id, requested_mode, package_ref, package_commit,
                 manifest_digest, source_ip, reason),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_events'"
            ).fetchone():
                db.execute(
                    """INSERT INTO node_events(
                           id,node_id,event_type,operator,reason,payload_sanitized)
                       VALUES(?,?,?,?,?,?)""",
                    ("node-evt-" + uuid.uuid4().hex, node_id, "promotion_requested",
                     "node-menu", reason,
                     json.dumps({"request_id": request_id, "requested_mode": requested_mode,
                                 "package_ref": package_ref}, sort_keys=True)),
                )
        return self.rows("SELECT * FROM promotion_requests WHERE id=?", (request_id,))[0]

    def promotion_requests(self, state: str | None = None) -> list[dict[str, Any]]:
        if state is None:
            return self.rows("SELECT * FROM promotion_requests ORDER BY created_at,id")
        return self.rows(
            "SELECT * FROM promotion_requests WHERE state=? ORDER BY created_at,id", (state,)
        )

    def set_promotion_request_state(self, request_id: str, state: str) -> dict[str, Any]:
        allowed = {
            "requested": {"approved", "rejected", "cancelled"},
            "approved": {"installing", "cancelled"},
            "installing": {"candidate", "standby", "failed"},
        }
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT state FROM promotion_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("solicitação não encontrada")
            if state not in allowed.get(row["state"], set()):
                raise ValueError(f"transição de solicitação inválida: {row['state']} -> {state}")
            db.execute(
                "UPDATE promotion_requests SET state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (state, request_id),
            )
        return self.rows("SELECT * FROM promotion_requests WHERE id=?", (request_id,))[0]

    def finalize_load_balancer_candidate(
        self, request_id: str, lb_id: str, backend_node_ids: Sequence[str],
        operator: str, reason: str,
    ) -> dict[str, Any]:
        """Fecha edge->LB candidate/standby em uma única transação auditada."""
        lb_id = normalize_id(lb_id, "load_balancer_id")
        operator = re.sub(r"[^a-zA-Z0-9_.@-]", "_", operator.strip())[:128]
        reason = str(_sanitized_event_payload(reason.strip())).replace("\n", " ")[:512]
        if not operator or not reason:
            raise ValueError("operador e motivo são obrigatórios")
        with self.transaction(immediate=True) as db:
            request = db.execute(
                "SELECT * FROM promotion_requests WHERE id=?", (request_id,)
            ).fetchone()
            if request is None or request["state"] != "installing":
                raise ValueError("solicitação não está em instalação")
            node = db.execute(
                "SELECT role,state FROM nodes WHERE id=?", (request["node_id"],)
            ).fetchone()
            edge = db.execute(
                "SELECT state FROM edges WHERE id=?", (request["node_id"],)
            ).fetchone()
            if node is None or edge is None or node["role"] != "edge":
                raise ValueError("edge/topologia divergente")
            if node["state"] != "draining" or edge["state"] != "draining":
                raise ValueError("edge precisa estar drenada antes da preparação LB")
            if db.execute(
                "SELECT 1 FROM lb_backends WHERE edge_node_id=?", (request["node_id"],)
            ).fetchone():
                raise ValueError("edge ainda pertence a um pool de backends")
            target = request["requested_mode"]
            for backend_id in backend_node_ids:
                backend_id = normalize_id(backend_id, "backend_node_id")
                backend = db.execute(
                    "SELECT role,state FROM nodes WHERE id=?", (backend_id,)
                ).fetchone()
                if backend is None or backend["role"] != "edge" or backend["state"] != "ready":
                    raise ValueError(f"backend não está edge/ready: {backend_id}")
            db.execute(
                "UPDATE edges SET state='disabled',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (request["node_id"],),
            )
            db.execute(
                """UPDATE nodes SET role='load_balancer',state=?,lease_id=NULL,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (target, request["node_id"]),
            )
            db.execute(
                """INSERT INTO load_balancers(id,node_id,mode,state)
                   VALUES(?,?,'active_standby',?)""",
                (lb_id, request["node_id"], target),
            )
            for backend_id in backend_node_ids:
                db.execute(
                    """INSERT INTO lb_backends(load_balancer_id,edge_node_id,weight,state)
                       VALUES(?,?,100,'enabled')""",
                    (lb_id, backend_id),
                )
            db.execute(
                "UPDATE promotion_requests SET state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target, request_id),
            )
            payload = json.dumps(
                {"request_id": request_id, "load_balancer_id": lb_id,
                 "target_state": target, "backends": list(backend_node_ids)},
                sort_keys=True,
            )
            db.execute(
                """INSERT INTO node_events(
                       id,node_id,event_type,operator,reason,payload_sanitized)
                   VALUES(?,?,?,?,?,?)""",
                ("node-evt-" + uuid.uuid4().hex, request["node_id"],
                 "load_balancer_prepared", operator, reason, payload),
            )
        return self.rows("SELECT * FROM load_balancers WHERE id=?", (lb_id,))[0]
