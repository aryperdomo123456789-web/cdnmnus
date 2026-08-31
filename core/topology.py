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

from core.db import Database, normalize_hostname, normalize_id


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
                 operator: str, reason: str, capacity: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
        capacity_json = json.dumps(_validate_payload(capacity or {}), sort_keys=True, separators=(",", ":"))
        with self.database.transaction(immediate=True) as db:
            db.execute(
                "INSERT INTO nodes(id,name,ipv4,role,state,capacity_json) VALUES(?,?,?,?,?,?)",
                (node_id, name.strip(), str(address), role, state, capacity_json),
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
