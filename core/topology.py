"""Modelo transacional de nós, load balancers, leases e auditoria.

Esta migração é deliberadamente opt-in: importar :mod:`core.db` ou executar
``Database.initialize()`` não modifica o banco existente. O chamador deve
instanciar ``TopologyStore`` e executar ``initialize()`` durante uma janela de
migração aprovada.
"""
from __future__ import annotations

import ipaddress
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Tuple, overload

from core.db import Database, normalize_hostname, normalize_id, normalize_port


MIGRATION_ID = "20260829_topology_v1"
NODE_ROLES = {"control_plane", "edge", "load_balancer"}
ROLE_STATES = {
    "control_plane": {"pending", "ready", "failed", "disabled"},
    "edge": {"pending", "bootstrapping", "ready", "draining", "failed", "disabled"},
    "load_balancer": {"pending", "candidate", "standby", "active", "draining", "failed", "disabled"},
}
STATE_TRANSITIONS = {
    "control_plane": {
        "pending": {"ready", "failed", "disabled"},
        "ready": {"failed", "disabled"},
        "failed": {"pending", "disabled"},
        "disabled": {"pending"},
    },
    "edge": {
        "pending": {"bootstrapping", "disabled"},
        "bootstrapping": {"ready", "failed", "disabled"},
        "ready": {"draining", "failed", "disabled"},
        "draining": {"ready", "failed", "disabled"},
        "failed": {"bootstrapping", "disabled"},
        "disabled": {"pending"},
    },
    "load_balancer": {
        "pending": {"candidate", "disabled"},
        "candidate": {"standby", "failed", "disabled"},
        "standby": {"candidate", "draining", "failed", "disabled"},
        "active": {"standby", "draining", "failed", "disabled"},
        "draining": {"standby", "failed", "disabled"},
        "failed": {"candidate", "disabled"},
        "disabled": {"candidate"},
    },
}
LB_STATES = {"candidate", "standby", "active", "draining", "failed", "disabled"}
BACKEND_STATES = {"enabled", "draining", "failed", "disabled"}
_SENSITIVE_EVENT_KEYS = {
    "authorization", "cookie", "credential", "credentials", "password",
    "private_key", "secret", "ssh_key", "access_token", "refresh_token",
}


class TopologyConflict(ValueError):
    """A operação violaria uma invariante ou uma transição de estado."""


class LockUnavailable(TopologyConflict):
    """Outro nó mantém uma lease de promoção ainda válida."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime precisa possuir timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _validate_payload(value: Any, path: str = "payload") -> Any:
    """Aceita apenas JSON pequeno e recusa chaves tipicamente secretas."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_validate_payload(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_EVENT_KEYS:
                raise ValueError(f"{path}.{key} contém campo sensível")
            result[key] = _validate_payload(item, f"{path}.{key}")
        return result
    raise ValueError(f"{path} precisa ser serializável como JSON")


class TopologyStore:
    """API autoritativa para mudanças de papel, promoção e fencing."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        """Aplica topology v1 e importa as edges legadas sem alterá-las."""
        with self.database.transaction(immediate=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    ipv4 TEXT NOT NULL UNIQUE,
                    ssh_port INTEGER CHECK(ssh_port BETWEEN 1 AND 65535),
                    ssh_user TEXT,
                    host_key_sha256 TEXT,
                    role TEXT NOT NULL CHECK(role IN ('control_plane','edge','load_balancer')),
                    state TEXT NOT NULL,
                    release_id TEXT,
                    node_config_digest TEXT,
                    capacity_json TEXT NOT NULL DEFAULT '{}'
                        CHECK(json_valid(capacity_json) AND json_type(capacity_json)='object'),
                    lease_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(
                        (role='control_plane' AND state IN ('pending','ready','failed','disabled')) OR
                        (role='edge' AND state IN ('pending','bootstrapping','ready','draining','failed','disabled')) OR
                        (role='load_balancer' AND state IN ('pending','candidate','standby','active','draining','failed','disabled'))
                    )
                );
                CREATE TABLE IF NOT EXISTS load_balancers (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE RESTRICT,
                    mode TEXT NOT NULL DEFAULT 'active_standby'
                        CHECK(mode IN ('active_standby','active_active')),
                    state TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(state IN ('candidate','standby','active','draining','failed','disabled')),
                    public_endpoint TEXT UNIQUE,
                    config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version > 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_load_balancer
                    ON load_balancers((1)) WHERE state='active';
                CREATE TABLE IF NOT EXISTS lb_backends (
                    load_balancer_id TEXT NOT NULL REFERENCES load_balancers(id) ON DELETE CASCADE,
                    edge_node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
                    weight INTEGER NOT NULL DEFAULT 100 CHECK(weight BETWEEN 1 AND 256),
                    state TEXT NOT NULL DEFAULT 'enabled'
                        CHECK(state IN ('enabled','draining','failed','disabled')),
                    last_health_at TEXT,
                    PRIMARY KEY(load_balancer_id, edge_node_id)
                );
                CREATE TABLE IF NOT EXISTS promotion_locks (
                    service_id TEXT PRIMARY KEY,
                    holder_node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
                    lease_id TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS node_events (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_sanitized TEXT NOT NULL DEFAULT '{}'
                        CHECK(json_valid(payload_sanitized) AND json_type(payload_sanitized)='object'),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS node_events_node_created
                    ON node_events(node_id, created_at, id);
                CREATE TABLE IF NOT EXISTS node_capacity_profiles (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
                    capacity_mbps INTEGER NOT NULL CHECK(capacity_mbps > 0),
                    headroom REAL NOT NULL DEFAULT 0.25 CHECK(headroom BETWEEN 0.2 AND 0.9),
                    max_connections INTEGER NOT NULL DEFAULT 0 CHECK(max_connections >= 0),
                    source TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('manual','contracted','measured','derived')),
                    measured_mbps INTEGER,
                    measured_at TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS node_capacity_profiles_source_idx
                    ON node_capacity_profiles(source, confidence);
                CREATE TABLE IF NOT EXISTS node_capacity_samples (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    sampled_at TEXT NOT NULL,
                    interface_name TEXT,
                    tx_mbps REAL NOT NULL CHECK(tx_mbps >= 0),
                    p95_ms REAL NOT NULL CHECK(p95_ms >= 0),
                    http5xx REAL NOT NULL CHECK(http5xx >= 0),
                    active_sessions INTEGER NOT NULL CHECK(active_sessions >= 0),
                    cpu_pct REAL NOT NULL CHECK(cpu_pct BETWEEN 0 AND 100),
                    mem_pct REAL NOT NULL CHECK(mem_pct BETWEEN 0 AND 100),
                    nic_errors INTEGER NOT NULL CHECK(nic_errors >= 0),
                    vod_206_ok INTEGER NOT NULL DEFAULT 1 CHECK(vod_206_ok IN (0,1)),
                    sample_window_sec INTEGER NOT NULL DEFAULT 10 CHECK(sample_window_sec > 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS node_capacity_samples_node_time
                    ON node_capacity_samples(node_id, sampled_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS node_capacity_runtime (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
                    state TEXT NOT NULL CHECK(state IN ('ready','pressured','draining','saturated','down')),
                    pressure REAL NOT NULL CHECK(pressure >= 0),
                    desired_weight INTEGER NOT NULL CHECK(desired_weight BETWEEN 0 AND 256),
                    applied_weight INTEGER NOT NULL CHECK(applied_weight BETWEEN 0 AND 256),
                    reason TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK(fencing_token >= 0),
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TRIGGER IF NOT EXISTS load_balancer_node_role_insert
                BEFORE INSERT ON load_balancers
                BEGIN
                    SELECT CASE WHEN NOT EXISTS(
                        SELECT 1 FROM nodes WHERE id=NEW.node_id AND role='load_balancer'
                    ) THEN RAISE(ABORT, 'load balancer node must have load_balancer role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS load_balancer_node_role_update
                BEFORE UPDATE OF node_id ON load_balancers
                BEGIN
                    SELECT CASE WHEN NOT EXISTS(
                        SELECT 1 FROM nodes WHERE id=NEW.node_id AND role='load_balancer'
                    ) THEN RAISE(ABORT, 'load balancer node must have load_balancer role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_role_change_with_load_balancer
                BEFORE UPDATE OF role ON nodes
                WHEN OLD.role='load_balancer' AND NEW.role<>'load_balancer'
                BEGIN
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM load_balancers WHERE node_id=OLD.id
                    ) THEN RAISE(ABORT, 'remove load balancer configuration before changing role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS backend_edge_role_insert
                BEFORE INSERT ON lb_backends
                BEGIN
                    SELECT CASE WHEN NOT EXISTS(
                        SELECT 1 FROM nodes WHERE id=NEW.edge_node_id AND role='edge'
                    ) THEN RAISE(ABORT, 'backend node must have edge role') END;
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM load_balancers lb
                        WHERE lb.id=NEW.load_balancer_id AND lb.node_id=NEW.edge_node_id
                    ) THEN RAISE(ABORT, 'load balancer cannot be its own backend') END;
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_backend_role_change
                BEFORE UPDATE OF role ON nodes
                WHEN OLD.role='edge' AND NEW.role<>'edge'
                BEGIN
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM lb_backends WHERE edge_node_id=OLD.id
                    ) THEN RAISE(ABORT, 'remove node from load balancer backends before changing role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS promotion_holder_role_insert
                BEFORE INSERT ON promotion_locks
                BEGIN
                    SELECT CASE WHEN NOT EXISTS(
                        SELECT 1 FROM nodes WHERE id=NEW.holder_node_id AND role='load_balancer'
                    ) THEN RAISE(ABORT, 'promotion lock holder must have load_balancer role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS promotion_holder_role_update
                BEFORE UPDATE OF holder_node_id ON promotion_locks
                BEGIN
                    SELECT CASE WHEN NOT EXISTS(
                        SELECT 1 FROM nodes WHERE id=NEW.holder_node_id AND role='load_balancer'
                    ) THEN RAISE(ABORT, 'promotion lock holder must have load_balancer role') END;
                END;
                CREATE TRIGGER IF NOT EXISTS fencing_token_monotonic
                BEFORE UPDATE OF fencing_token ON promotion_locks
                WHEN NEW.fencing_token <= OLD.fencing_token
                BEGIN
                    SELECT RAISE(ABORT, 'fencing token must increase');
                END;
                CREATE TRIGGER IF NOT EXISTS promotion_service_immutable
                BEFORE UPDATE OF service_id ON promotion_locks
                BEGIN
                    SELECT RAISE(ABORT, 'promotion lock service id is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS promotion_lock_no_delete
                BEFORE DELETE ON promotion_locks
                BEGIN
                    SELECT RAISE(ABORT, 'promotion lock history cannot be deleted');
                END;
            """)
            node_columns = {
                row[1] for row in db.execute("PRAGMA table_info(nodes)").fetchall()
            }
            for name, definition in (
                ("ssh_port", "INTEGER CHECK(ssh_port BETWEEN 1 AND 65535)"),
                ("ssh_user", "TEXT"),
                ("host_key_sha256", "TEXT"),
            ):
                if name not in node_columns:
                    db.execute(f"ALTER TABLE nodes ADD COLUMN {name} {definition}")
            db.execute(
                """UPDATE nodes
                      SET ssh_port=COALESCE(ssh_port,(SELECT e.ssh_port FROM edges e WHERE e.id=nodes.id)),
                          ssh_user=COALESCE(ssh_user,(SELECT e.ssh_user FROM edges e WHERE e.id=nodes.id)),
                          host_key_sha256=COALESCE(host_key_sha256,
                              (SELECT e.host_key_sha256 FROM edges e WHERE e.id=nodes.id))
                    WHERE role='edge'"""
            )
            # Compatibilidade: a tabela edges permanece autoritativa para os
            # deploys atuais. A cópia inicial apenas prepara o modelo novo.
            legacy_edges = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edges'"
            ).fetchone()
            if legacy_edges:
                db.execute("""
                    INSERT INTO nodes(id,name,ipv4,role,state,release_id,capacity_json,created_at,updated_at)
                    SELECT e.id,e.name,e.ipv4,'edge',e.state,e.deployed_version,'{}',e.created_at,e.updated_at
                    FROM edges e WHERE NOT EXISTS(SELECT 1 FROM nodes n WHERE n.id=e.id)
                """)
            db.execute("""UPDATE node_id_sequence
                             SET next_id=MAX(next_id, COALESCE(
                                 (SELECT MAX(CAST(id AS INTEGER))+1 FROM nodes
                                   WHERE id <> '' AND id NOT GLOB '*[^0-9]*'), 1))
                           WHERE singleton=1""")
            db.execute("INSERT OR IGNORE INTO schema_migrations(id) VALUES(?)", (MIGRATION_ID,))

    def downgrade(self) -> None:
        """Remove somente topology v1; nunca altera as tabelas legadas."""
        with self.database.transaction(immediate=True) as db:
            db.executescript("""
                DROP TRIGGER IF EXISTS promotion_lock_no_delete;
                DROP TRIGGER IF EXISTS promotion_service_immutable;
                DROP TRIGGER IF EXISTS fencing_token_monotonic;
                DROP TRIGGER IF EXISTS promotion_holder_role_update;
                DROP TRIGGER IF EXISTS promotion_holder_role_insert;
                DROP TRIGGER IF EXISTS prevent_backend_role_change;
                DROP TRIGGER IF EXISTS backend_edge_role_insert;
                DROP TRIGGER IF EXISTS prevent_role_change_with_load_balancer;
                DROP TRIGGER IF EXISTS load_balancer_node_role_update;
                DROP TRIGGER IF EXISTS load_balancer_node_role_insert;
                DROP TABLE IF EXISTS node_events;
                DROP TABLE IF EXISTS node_capacity_runtime;
                DROP TABLE IF EXISTS node_capacity_samples;
                DROP TABLE IF EXISTS node_capacity_profiles;
                DROP TABLE IF EXISTS promotion_locks;
                DROP TABLE IF EXISTS lb_backends;
                DROP TABLE IF EXISTS load_balancers;
                DROP TABLE IF EXISTS nodes;
            """)
            db.execute("DELETE FROM schema_migrations WHERE id=?", (MIGRATION_ID,))

    def _event(self, db: sqlite3.Connection, node_id: str, event_type: str,
               operator: str, reason: str, payload: Mapping[str, Any] | None = None) -> str:
        if not operator.strip() or not reason.strip():
            raise ValueError("operador e motivo são obrigatórios")
        clean = _validate_payload(payload or {})
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 16_384:
            raise ValueError("payload auditável excede 16 KiB")
        event_id = "evt-" + uuid.uuid4().hex
        db.execute(
            """INSERT INTO node_events(
                   id,node_id,event_type,operator,reason,payload_sanitized,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (event_id, node_id, normalize_id(event_type, "event_type"), operator.strip(),
             reason.strip(), encoded, _timestamp(_utc_now())),
        )
        return event_id

    def add_node(self, node_id: str, name: str, ipv4: str, role: str, state: str,
                 operator: str, reason: str, capacity: Mapping[str, Any] | None = None,
                 ssh_port: int | None = None, ssh_user: str | None = None,
                 host_key_sha256: str | None = None) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        role = role.strip().lower()
        state = state.strip().lower()
        if role not in NODE_ROLES or state not in ROLE_STATES.get(role, set()):
            raise ValueError("papel ou estado de nó inválido")
        if role == "load_balancer" and state == "active":
            raise TopologyConflict("nó load_balancer só pode ficar active por promoção com lock e fencing")
        address = ipaddress.ip_address(ipv4.strip())
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError("ipv4 do nó é inválido")
        if not name.strip():
            raise ValueError("nome do nó é obrigatório")
        supplied_ssh = (ssh_port is not None, bool(ssh_user), bool(host_key_sha256))
        if any(supplied_ssh) and not all(supplied_ssh):
            raise ValueError("metadados SSH devem ser fornecidos em conjunto")
        if all(supplied_ssh):
            ssh_port = normalize_port(ssh_port)
        capacity_json = json.dumps(_validate_payload(capacity or {}), sort_keys=True, separators=(",", ":"))
        with self.database.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO nodes(
                       id,name,ipv4,ssh_port,ssh_user,host_key_sha256,role,state,capacity_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (node_id, name.strip(), str(address), ssh_port, ssh_user,
                 host_key_sha256, role, state, capacity_json),
            )
            self._event(db, node_id, "node_created", operator, reason, {"role": role, "state": state})
        return self.node(node_id)

    def add_node_auto(self, name: str, ipv4: str, role: str, state: str,
                      operator: str, reason: str,
                      capacity: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reserva identidade monotônica e cadastra qualquer novo papel de nó."""
        node_id = self.database.reserve_node_id()
        return self.add_node(node_id, name, ipv4, role, state, operator, reason, capacity)

    def node(self, node_id: str) -> dict[str, Any]:
        rows = self.database.rows("SELECT * FROM nodes WHERE id=?", (normalize_id(node_id, "node_id"),))
        if not rows:
            raise ValueError("nó não encontrado")
        row = rows[0]
        row["capacity"] = json.loads(row.pop("capacity_json"))
        return row

    def events(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.database.rows(
            "SELECT * FROM node_events WHERE node_id=? ORDER BY created_at,id",
            (normalize_id(node_id, "node_id"),),
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_sanitized"))
        return rows

    def transition_node(self, node_id: str, state: str, operator: str, reason: str,
                        release_id: str | None = None,
                        config_digest: str | None = None) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        state = state.strip().lower()
        with self.database.transaction(immediate=True) as db:
            row = db.execute("SELECT role,state FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise ValueError("nó não encontrado")
            if state not in ROLE_STATES[row["role"]]:
                raise TopologyConflict("estado incompatível com o papel do nó")
            if state == row["state"]:
                raise TopologyConflict("nó já está no estado solicitado")
            if state not in STATE_TRANSITIONS[row["role"]].get(row["state"], set()):
                raise TopologyConflict(f"transição {row['state']} -> {state} não permitida")
            if row["role"] == "load_balancer" and state == "active":
                raise TopologyConflict("estado active exige promote_load_balancer com lease e fencing")
            if row["role"] == "load_balancer" and db.execute(
                "SELECT 1 FROM load_balancers WHERE node_id=?", (node_id,)
            ).fetchone():
                raise TopologyConflict("nó configurado como LB exige operação promote/demote específica")
            db.execute(
                """UPDATE nodes SET state=?,release_id=COALESCE(?,release_id),
                   node_config_digest=COALESCE(?,node_config_digest),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (state, release_id, config_digest, node_id),
            )
            self._event(db, node_id, "state_changed", operator, reason,
                        {"from": row["state"], "to": state, "release_id": release_id})
        return self.node(node_id)

    def change_role(self, node_id: str, role: str, state: str, operator: str,
                    reason: str, remove_lb_configuration: bool = False) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        role, state = role.strip().lower(), state.strip().lower()
        if role not in NODE_ROLES or state not in ROLE_STATES.get(role, set()):
            raise ValueError("papel ou estado alvo inválido")
        if role == "load_balancer" and state == "active":
            raise TopologyConflict("mudança de papel nunca concede estado active")
        with self.database.transaction(immediate=True) as db:
            row = db.execute("SELECT role,state FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise ValueError("nó não encontrado")
            if row["role"] == "edge" and db.execute(
                "SELECT 1 FROM lb_backends WHERE edge_node_id=?", (node_id,)
            ).fetchone():
                raise TopologyConflict("drene e remova a edge de todos os backends antes de mudar o papel")
            lb = db.execute("SELECT id,state FROM load_balancers WHERE node_id=?", (node_id,)).fetchone()
            if lb:
                if lb["state"] == "active":
                    raise TopologyConflict("load balancer ACTIVE precisa ser rebaixado antes da mudança de papel")
                if not remove_lb_configuration:
                    raise TopologyConflict("confirme a remoção da configuração do load balancer")
                db.execute("DELETE FROM load_balancers WHERE id=?", (lb["id"],))
            db.execute(
                "UPDATE nodes SET role=?,state=?,lease_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (role, state, node_id),
            )
            self._event(db, node_id, "role_changed", operator, reason,
                        {"from_role": row["role"], "from_state": row["state"],
                         "to_role": role, "to_state": state})
        return self.node(node_id)

    def add_load_balancer(self, lb_id: str, node_id: str, public_endpoint: str | None,
                          operator: str, reason: str, mode: str = "active_standby") -> dict[str, Any]:
        lb_id, node_id = normalize_id(lb_id, "load_balancer_id"), normalize_id(node_id, "node_id")
        if mode not in {"active_standby", "active_active"}:
            raise ValueError("modo de load balancer inválido")
        endpoint = normalize_hostname(public_endpoint) if public_endpoint else None
        with self.database.transaction(immediate=True) as db:
            node = db.execute("SELECT role,state FROM nodes WHERE id=?", (node_id,)).fetchone()
            if node is None:
                raise ValueError("nó não encontrado")
            if node["role"] != "load_balancer" or node["state"] != "candidate":
                raise TopologyConflict("configuração LB nova exige nó load_balancer em estado candidate")
            db.execute(
                "INSERT INTO load_balancers(id,node_id,mode,state,public_endpoint) VALUES(?,?,?,?,?)",
                (lb_id, node_id, mode, "candidate", endpoint),
            )
            self._event(db, node_id, "load_balancer_created", operator, reason,
                        {"load_balancer_id": lb_id, "mode": mode})
        return self.load_balancer(lb_id)

    def load_balancer(self, lb_id: str) -> dict[str, Any]:
        rows = self.database.rows("SELECT * FROM load_balancers WHERE id=?", (normalize_id(lb_id, "load_balancer_id"),))
        if not rows:
            raise ValueError("load balancer não encontrado")
        return rows[0]

    def add_backend(self, lb_id: str, edge_node_id: str, operator: str, reason: str,
                    weight: int = 100) -> dict[str, Any]:
        lb_id = normalize_id(lb_id, "load_balancer_id")
        edge_node_id = normalize_id(edge_node_id, "edge_node_id")
        if not 1 <= int(weight) <= 256:
            raise ValueError("peso precisa estar entre 1 e 256")
        with self.database.transaction(immediate=True) as db:
            lb = db.execute("SELECT node_id FROM load_balancers WHERE id=?", (lb_id,)).fetchone()
            if lb is None:
                raise ValueError("load balancer não encontrado")
            db.execute(
                "INSERT INTO lb_backends(load_balancer_id,edge_node_id,weight) VALUES(?,?,?)",
                (lb_id, edge_node_id, int(weight)),
            )
            self._event(db, lb["node_id"], "backend_added", operator, reason,
                        {"load_balancer_id": lb_id, "edge_node_id": edge_node_id, "weight": int(weight)})
        return self.database.rows(
            "SELECT * FROM lb_backends WHERE load_balancer_id=? AND edge_node_id=?",
            (lb_id, edge_node_id),
        )[0]

    @overload
    def acquire_promotion_lock(self, service_id: str, holder_node_id: str,
                               ttl_seconds: int = 30, now: datetime | None = None
                               ) -> tuple[bool, str | None, int | None]:
        ...

    @overload
    def acquire_promotion_lock(self, service_id: str, holder_node_id: str,
                               operator: str, reason: str, ttl_seconds: int = 30,
                               now: datetime | None = None) -> dict[str, Any]:
        ...

    def acquire_promotion_lock(self, service_id: str, holder_node_id: str, *args: Any,
                               ttl_seconds: int = 30, now: datetime | None = None
                               ) -> tuple[bool, str | None, int | None] | dict[str, Any]:
        service_id = normalize_id(service_id, "service_id")
        holder_node_id = normalize_id(holder_node_id, "holder_node_id")
        operator: str | None = None
        reason: str | None = None
        if len(args) == 0:
            pass
        elif len(args) >= 2:
            operator = str(args[0])
            reason = str(args[1])
            if len(args) >= 3:
                ttl_seconds = int(args[2])
            if len(args) >= 4:
                now = args[3]
        else:
            raise TypeError("acquire_promotion_lock requer ou apenas TTL, ou operator e reason")
        if not 5 <= int(ttl_seconds) <= 300:
            raise ValueError("lease precisa durar entre 5 e 300 segundos")
        current = now or _utc_now()
        lease_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as db:
            holder = db.execute("SELECT role FROM nodes WHERE id=?", (holder_node_id,)).fetchone()
            if holder is None or holder["role"] != "load_balancer":
                raise TopologyConflict("somente nó load_balancer pode adquirir lock de promoção")
            row = db.execute("SELECT * FROM promotion_locks WHERE service_id=?", (service_id,)).fetchone()
            if row is not None and _parse_timestamp(row["expires_at"]) > current:
                raise LockUnavailable("lock de promoção já possui lease válida")
            token = 1 if row is None else int(row["fencing_token"]) + 1
            expires_at = _timestamp(current + timedelta(seconds=int(ttl_seconds)))
            if row is None:
                db.execute(
                    "INSERT INTO promotion_locks(service_id,holder_node_id,lease_id,expires_at,fencing_token) VALUES(?,?,?,?,?)",
                    (service_id, holder_node_id, lease_id, expires_at, token),
                )
            else:
                db.execute(
                    """UPDATE promotion_locks SET holder_node_id=?,lease_id=?,expires_at=?,
                       fencing_token=?,updated_at=CURRENT_TIMESTAMP WHERE service_id=?""",
                    (holder_node_id, lease_id, expires_at, token, service_id),
                )
            db.execute("UPDATE nodes SET lease_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (lease_id, holder_node_id))
            if operator is not None and reason is not None:
                self._event(db, holder_node_id, "promotion_lock_acquired", operator, reason,
                            {"service_id": service_id, "fencing_token": token, "expires_at": expires_at})
        if operator is None or reason is None:
            return True, lease_id, token
        return {"service_id": service_id, "holder_node_id": holder_node_id,
                "lease_id": lease_id, "expires_at": expires_at, "fencing_token": token}

    @overload
    def promote_load_balancer(self, node_id: str, lease_id: str, fencing_token: int,
                              now: datetime | None = None) -> bool:
        ...

    @overload
    def promote_load_balancer(self, lb_id: str, service_id: str, lease_id: str,
                              fencing_token: int, operator: str, reason: str,
                              now: datetime | None = None) -> dict[str, Any]:
        ...

    def promote_load_balancer(self, node_id: str, *args: Any,
                              now: datetime | None = None) -> bool | dict[str, Any]:
        node_id = normalize_id(node_id, "load_balancer_id")
        service_id = "default"
        operator: str | None = None
        reason: str | None = None
        if len(args) == 2:
            lease_id = str(args[0])
            fencing_token = int(args[1])
        elif len(args) == 3:
            lease_id = str(args[0])
            fencing_token = int(args[1])
            now = args[2]
        elif len(args) >= 5:
            service_id = normalize_id(str(args[0]), "service_id")
            lease_id = str(args[1])
            fencing_token = int(args[2])
            operator = str(args[3])
            reason = str(args[4])
            if len(args) >= 6:
                now = args[5]
        else:
            raise TypeError("assinatura inválida para promote_load_balancer")
        current = now or _utc_now()
        with self.database.transaction(immediate=True) as db:
            lb = db.execute("SELECT * FROM load_balancers WHERE id=?", (node_id,)).fetchone()
            if lb is None:
                lb = db.execute("SELECT * FROM load_balancers WHERE node_id=?", (node_id,)).fetchone()
            if lb is None:
                raise ValueError("load balancer não encontrado")
            if lb["state"] not in {"candidate", "standby"}:
                raise TopologyConflict("somente load balancer candidate ou standby pode ser promovido")
            lock = db.execute("SELECT * FROM promotion_locks WHERE service_id=?", (service_id,)).fetchone()
            if (lock is None or lock["holder_node_id"] != lb["node_id"] or
                    lock["lease_id"] != lease_id or int(lock["fencing_token"]) != int(fencing_token) or
                    _parse_timestamp(lock["expires_at"]) <= current):
                raise TopologyConflict("promoção recusada: lease ou fencing token inválido/expirado")
            active = db.execute("SELECT id FROM load_balancers WHERE state='active' AND id<>?", (lb["id"],)).fetchone()
            if active:
                raise TopologyConflict("já existe outro load balancer ACTIVE; fencing externo é obrigatório")
            db.execute("UPDATE load_balancers SET state='active',updated_at=CURRENT_TIMESTAMP WHERE id=?", (lb["id"],))
            db.execute(
                "UPDATE nodes SET state='active',lease_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (lease_id, lb["node_id"]),
            )
            if operator is not None and reason is not None:
                self._event(db, lb["node_id"], "load_balancer_promoted", operator, reason,
                            {"load_balancer_id": lb["id"], "service_id": service_id,
                             "fencing_token": int(fencing_token)})
        return True if operator is None else self.load_balancer(lb["id"])

    @overload
    def demote_load_balancer(self, node_id: str, now: datetime | None = None) -> bool:
        ...

    @overload
    def demote_load_balancer(self, lb_id: str, target_state: str, operator: str,
                             reason: str) -> dict[str, Any]:
        ...

    def demote_load_balancer(self, node_id: str, *args: Any) -> bool | dict[str, Any]:
        node_id = normalize_id(node_id, "load_balancer_id")
        target_state = "standby"
        operator: str | None = None
        reason: str | None = None
        if len(args) == 1 and isinstance(args[0], datetime):
            pass
        elif len(args) == 0:
            pass
        elif len(args) >= 3:
            target_state = str(args[0]).strip().lower()
            operator = str(args[1])
            reason = str(args[2])
        else:
            raise TypeError("assinatura inválida para demote_load_balancer")
        if target_state not in {"standby", "draining", "failed", "disabled", "candidate"}:
            raise ValueError("estado de rebaixamento inválido")
        with self.database.transaction(immediate=True) as db:
            lb = db.execute("SELECT * FROM load_balancers WHERE id=?", (node_id,)).fetchone()
            if lb is None:
                lb = db.execute("SELECT * FROM load_balancers WHERE node_id=?", (node_id,)).fetchone()
            if lb is None:
                raise ValueError("load balancer não encontrado")
            if lb["state"] != "active":
                raise TopologyConflict("somente load balancer ACTIVE pode ser rebaixado")
            db.execute("UPDATE load_balancers SET state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (target_state, lb["id"]))
            db.execute(
                "UPDATE nodes SET state=?,lease_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target_state, lb["node_id"]),
            )
            if operator is not None and reason is not None:
                self._event(db, lb["node_id"], "load_balancer_demoted", operator, reason,
                            {"load_balancer_id": lb["id"], "from": lb["state"], "to": target_state})
        return True if operator is None else self.load_balancer(lb["id"])

    def set_capacity_profile(
        self, node_id: str, capacity_mbps: int, *, source: str,
        confidence: str = "manual", headroom: float = 0.25,
        max_connections: int = 0, measured_mbps: int | None = None,
        measured_at: str | None = None, expires_at: str | None = None,
        operator: str | None = None, reason: str | None = None,
    ) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        capacity_mbps = int(capacity_mbps)
        if capacity_mbps <= 0:
            raise ValueError("capacity_mbps precisa ser positivo")
        source = source.strip()
        if not source:
            raise ValueError("source é obrigatório")
        if confidence not in {"manual", "contracted", "measured", "derived"}:
            raise ValueError("confidence inválido")
        headroom = float(headroom)
        if not 0.2 <= headroom <= 0.9:
            raise ValueError("headroom fora do intervalo permitido")
        max_connections = int(max_connections)
        if max_connections < 0:
            raise ValueError("max_connections precisa ser >= 0")
        if measured_mbps is not None:
            measured_mbps = int(measured_mbps)
            if measured_mbps < 0:
                raise ValueError("measured_mbps precisa ser >= 0")
        with self.database.transaction(immediate=True) as db:
            node = db.execute("SELECT capacity_json FROM nodes WHERE id=?", (node_id,)).fetchone()
            if node is None:
                raise ValueError("nó não encontrado")
            payload = {
                "capacity_mbps": capacity_mbps,
                "headroom": headroom,
                "max_connections": max_connections,
                "source": source,
                "confidence": confidence,
                "measured_mbps": measured_mbps,
                "measured_at": measured_at,
                "expires_at": expires_at,
            }
            db.execute(
                """INSERT INTO node_capacity_profiles(
                       node_id,capacity_mbps,headroom,max_connections,source,confidence,
                       measured_mbps,measured_at,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(node_id) DO UPDATE SET
                       capacity_mbps=excluded.capacity_mbps,
                       headroom=excluded.headroom,
                       max_connections=excluded.max_connections,
                       source=excluded.source,
                       confidence=excluded.confidence,
                       measured_mbps=excluded.measured_mbps,
                       measured_at=excluded.measured_at,
                       expires_at=excluded.expires_at,
                       updated_at=CURRENT_TIMESTAMP""",
                (node_id, capacity_mbps, headroom, max_connections, source, confidence,
                 measured_mbps, measured_at, expires_at),
            )
            current = json.loads(node["capacity_json"] or "{}")
            current["capacity_profile"] = payload
            db.execute(
                "UPDATE nodes SET capacity_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(current, sort_keys=True, separators=(",", ":")), node_id),
            )
            if operator is not None or reason is not None:
                if not operator or not reason:
                    raise ValueError("operator e reason são obrigatórios para auditoria")
                self._event(db, node_id, "capacity_profile_updated", operator, reason,
                            {"capacity_mbps": capacity_mbps, "source": source,
                             "confidence": confidence, "expires_at": expires_at})
        profile = self.capacity_profile(node_id)
        if profile is None:
            raise RuntimeError("perfil de capacidade não persistido")
        return profile

    def register_contracted_capacity(
        self, node_id: str, payload: Mapping[str, Any], *, operator: str, reason: str,
    ) -> dict[str, Any]:
        """Registra, de forma idempotente, a capacidade contratada de um LB.

        Este é o caminho normativo para um contrato de provedor. Diferente de
        :meth:`set_capacity_profile`, não aceita uma fonte ou confiança
        genérica: exige ``provider-contract``/``contracted`` e um vencimento
        futuro. A operação atualiza somente o perfil do nó e deixa evento
        sanitizado; não altera estado, lease, fencing, DNS ou tráfego.

        Rollback operacional: reaplicar o último payload contratual válido
        mantém a linha de capacidade consistente. Remoção ou substituição do
        contrato deve ser uma operação administrativa separada e auditada.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("payload de capacidade precisa ser um objeto")
        required = {"capacity_mbps", "confidence", "source", "expires_at"}
        missing = sorted(required.difference(payload))
        if missing:
            raise ValueError(f"perfil contratual incompleto: {', '.join(missing)}")
        if str(payload["source"]).strip() != "provider-contract":
            raise ValueError("perfil contratual exige source=provider-contract")
        if str(payload["confidence"]).strip() != "contracted":
            raise ValueError("perfil contratual exige confidence=contracted")
        expires_at = payload["expires_at"]
        if not isinstance(expires_at, str) or not expires_at.strip():
            raise ValueError("perfil contratual exige expires_at")
        if _parse_timestamp(expires_at) <= _utc_now():
            raise ValueError("expires_at do contrato precisa estar no futuro")
        node_id = normalize_id(node_id, "node_id")
        node = self.node(node_id)
        if node["role"] != "load_balancer":
            raise TopologyConflict("capacidade contratual deste caminho exige um load_balancer")
        return self.set_capacity_profile(
            node_id, int(payload["capacity_mbps"]), source="provider-contract",
            confidence="contracted", headroom=float(payload.get("headroom", 0.25)),
            max_connections=int(payload.get("max_connections", 0)),
            measured_mbps=(None if payload.get("measured_mbps") in (None, "")
                           else int(payload["measured_mbps"])),
            measured_at=(None if payload.get("measured_at") in (None, "")
                         else str(payload["measured_at"])),
            expires_at=expires_at, operator=operator, reason=reason,
        )

    def capacity_profile(self, node_id: str) -> dict[str, Any] | None:
        rows = self.database.rows(
            "SELECT * FROM node_capacity_profiles WHERE node_id=?",
            (normalize_id(node_id, "node_id"),),
        )
        return rows[0] if rows else None

    def record_capacity_sample(
        self, node_id: str, sampled_at: str, tx_mbps: float, p95_ms: float,
        http5xx: float, active_sessions: int, cpu_pct: float, mem_pct: float,
        nic_errors: int, vod_206_ok: bool, interface_name: str | None = None,
        sample_window_sec: int = 10,
    ) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        sample_id = "cap-" + uuid.uuid4().hex
        if tx_mbps < 0 or p95_ms < 0 or http5xx < 0:
            raise ValueError("valores de amostra precisam ser não negativos")
        if not 0 <= cpu_pct <= 100 or not 0 <= mem_pct <= 100:
            raise ValueError("cpu_pct e mem_pct precisam estar entre 0 e 100")
        if active_sessions < 0 or nic_errors < 0:
            raise ValueError("contadores precisam ser não negativos")
        with self.database.transaction(immediate=True) as db:
            if db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() is None:
                raise ValueError("nó não encontrado")
            db.execute(
                """INSERT INTO node_capacity_samples(
                       id,node_id,sampled_at,interface_name,tx_mbps,p95_ms,http5xx,
                       active_sessions,cpu_pct,mem_pct,nic_errors,vod_206_ok,sample_window_sec
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sample_id, node_id, sampled_at, interface_name, float(tx_mbps), float(p95_ms),
                 float(http5xx), int(active_sessions), float(cpu_pct), float(mem_pct),
                 int(nic_errors), 1 if vod_206_ok else 0, int(sample_window_sec)),
            )
            current = self.node(node_id)["capacity"]
            current["last_sample"] = {
                "sample_id": sample_id,
                "sampled_at": sampled_at,
                "interface_name": interface_name,
                "tx_mbps": float(tx_mbps),
                "p95_ms": float(p95_ms),
                "http5xx": float(http5xx),
                "active_sessions": int(active_sessions),
                "cpu_pct": float(cpu_pct),
                "mem_pct": float(mem_pct),
                "nic_errors": int(nic_errors),
                "vod_206_ok": bool(vod_206_ok),
                "sample_window_sec": int(sample_window_sec),
            }
            db.execute(
                "UPDATE nodes SET capacity_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(current, sort_keys=True, separators=(",", ":")), node_id),
            )
        sample = self.capacity_sample(node_id, sample_id)
        if sample is None:
            raise RuntimeError("amostra de capacidade não persistida")
        return sample

    def capacity_sample(self, node_id: str, sample_id: str) -> dict[str, Any] | None:
        rows = self.database.rows(
            "SELECT * FROM node_capacity_samples WHERE node_id=? AND id=?",
            (normalize_id(node_id, "node_id"), sample_id),
        )
        return rows[0] if rows else None

    def capacity_history(self, node_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        return self.database.rows(
            "SELECT * FROM node_capacity_samples WHERE node_id=? ORDER BY sampled_at DESC,id DESC LIMIT ?",
            (normalize_id(node_id, "node_id"), limit),
        )

    def set_capacity_runtime(
        self, node_id: str, state: str, pressure: float, desired_weight: int,
        applied_weight: int, reason: str, fencing_token: int = 0,
    ) -> dict[str, Any]:
        node_id = normalize_id(node_id, "node_id")
        if state not in {"ready", "pressured", "draining", "saturated", "down"}:
            raise ValueError("state de capacidade inválido")
        reason = reason.strip()
        if not reason:
            raise ValueError("reason é obrigatório")
        pressure = float(pressure)
        desired_weight = int(desired_weight)
        applied_weight = int(applied_weight)
        fencing_token = int(fencing_token)
        if pressure < 0 or desired_weight < 0 or applied_weight < 0 or fencing_token < 0:
            raise ValueError("valores de runtime precisam ser não negativos")
        with self.database.transaction(immediate=True) as db:
            if db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() is None:
                raise ValueError("nó não encontrado")
            db.execute(
                """INSERT INTO node_capacity_runtime(
                       node_id,state,pressure,desired_weight,applied_weight,reason,fencing_token,changed_at
                   ) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(node_id) DO UPDATE SET
                       state=excluded.state,
                       pressure=excluded.pressure,
                       desired_weight=excluded.desired_weight,
                       applied_weight=excluded.applied_weight,
                       reason=excluded.reason,
                       fencing_token=excluded.fencing_token,
                       changed_at=CURRENT_TIMESTAMP""",
                (node_id, state, pressure, desired_weight, applied_weight, reason, fencing_token),
            )
        runtime = self.capacity_runtime(node_id)
        if runtime is None:
            raise RuntimeError("runtime de capacidade não persistido")
        return runtime

    def capacity_runtime(self, node_id: str) -> dict[str, Any] | None:
        rows = self.database.rows(
            "SELECT * FROM node_capacity_runtime WHERE node_id=?",
            (normalize_id(node_id, "node_id"),),
        )
        return rows[0] if rows else None

    def capacity_snapshot(self) -> list[dict[str, Any]]:
        nodes = self.database.rows("SELECT * FROM nodes ORDER BY CAST(id AS INTEGER),id")
        latest_samples = {
            row["node_id"]: row
            for row in self.database.rows("""
                SELECT s.*
                  FROM node_capacity_samples s
                  JOIN (
                      SELECT node_id, MAX(sampled_at) AS sampled_at
                        FROM node_capacity_samples
                       GROUP BY node_id
                  ) latest
                    ON latest.node_id=s.node_id AND latest.sampled_at=s.sampled_at
            """)
        }
        profiles = {row["node_id"]: row for row in self.database.rows("SELECT * FROM node_capacity_profiles")}
        runtimes = {row["node_id"]: row for row in self.database.rows("SELECT * FROM node_capacity_runtime")}
        result: list[dict[str, Any]] = []
        for node in nodes:
            profile = profiles.get(node["id"])
            sample = latest_samples.get(node["id"])
            runtime = runtimes.get(node["id"])
            capacity_mbps = int(profile["capacity_mbps"]) if profile else None
            headroom = float(profile["headroom"]) if profile else 0.25
            usable_mbps = round(capacity_mbps * (1 - headroom), 2) if capacity_mbps else None
            tx_mbps = float(sample["tx_mbps"]) if sample else None
            consumption_pct = round((tx_mbps / usable_mbps) * 100, 2) if tx_mbps is not None and usable_mbps else None
            pressure = float(runtime["pressure"]) if runtime else None
            if pressure is None and tx_mbps is not None and usable_mbps:
                pressure = round(tx_mbps / usable_mbps, 4)
            sample_age_seconds = None
            if sample:
                try:
                    sample_age_seconds = max(0, int((_utc_now() - _parse_timestamp(sample["sampled_at"])).total_seconds()))
                except Exception:
                    sample_age_seconds = None
            result.append({
                "node": node,
                "profile": profile,
                "sample": sample,
                "runtime": runtime,
                "capacity_mbps": capacity_mbps,
                "usable_mbps": usable_mbps,
                "tx_mbps": tx_mbps,
                "consumption_pct": consumption_pct,
                "pressure": pressure,
                "sample_age_seconds": sample_age_seconds,
            })
        return result
