"""PostgreSQL laboratory contracts for the future authoritative control plane.

Nothing in this module is imported by the production SQLite path.  PostgreSQL
access is opt-in, requires an explicit laboratory acknowledgement and never
prints a DSN.  This makes it possible to validate migrations and recovery
before a separately approved production cutover.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, urlparse


MIGRATION_ROOT = Path(__file__).parents[1] / "database/postgresql/migrations"
LAB_ACK = "I_UNDERSTAND_THIS_IS_LAB"


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    boolean_columns: tuple[str, ...] = ()


# Dependency order is also the safe import order.
CURRENT_TABLES: Mapping[str, TableSpec] = {
    "xui_tenants": TableSpec(
        ("id", "name", "canonical_host", "config_version", "enabled", "created_at", "updated_at"),
        ("id",),
        ("enabled",),
    ),
    "tenant_hosts": TableSpec(
        ("hostname", "tenant_id", "is_canonical", "tls_status"),
        ("hostname",),
        ("is_canonical",),
    ),
    "tenant_upstreams": TableSpec(
        ("id", "tenant_id", "kind", "host", "port"), ("id",)
    ),
    "edges": TableSpec(
        (
            "id", "name", "ipv4", "ssh_port", "ssh_user", "host_key_sha256",
            "state", "deployed_version", "last_health_at", "last_health_status",
            "created_at", "updated_at",
        ),
        ("id",),
    ),
    "dns_records": TableSpec(
        ("hostname", "record_type", "target_ip", "edge_id", "status"),
        ("hostname", "record_type", "target_ip"),
    ),
    "settings": TableSpec(("key", "value", "updated_at"), ("key",)),
    "deployments": TableSpec(
        (
            "id", "state", "release_id", "config_digest", "artifact_path", "error",
            "created_at", "started_at", "finished_at",
        ),
        ("id",),
    ),
}


@dataclass(frozen=True)
class LogicalSnapshot:
    tables: Mapping[str, tuple[tuple[Any, ...], ...]]

    def counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.tables.items()}


@dataclass(frozen=True)
class Comparison:
    counts: Mapping[str, tuple[int, int]]
    digests: Mapping[str, tuple[str, str]]
    matching: bool


def _canonical_cell(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _row_digest(rows: Sequence[Sequence[Any]]) -> str:
    canonical = [[_canonical_cell(cell) for cell in row] for row in rows]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sqlite_uri(path: str | Path) -> str:
    # SQLite's URI parser accepts an absolute POSIX path.  Resolving first also
    # prevents a caller from changing the target through a relative cwd.
    return f"file:{Path(path).resolve()}?mode=ro"


def capture_sqlite(path: str | Path) -> LogicalSnapshot:
    """Read one transactionally consistent, read-only SQLite snapshot."""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    db = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=10)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"SQLite foreign_key_check failed ({len(violations)} violations)")
        result: dict[str, tuple[tuple[Any, ...], ...]] = {}
        for name, spec in CURRENT_TABLES.items():
            columns = ",".join(f'"{column}"' for column in spec.columns)
            order = ",".join(f'"{column}"' for column in spec.primary_key)
            rows = db.execute(f'SELECT {columns} FROM "{name}" ORDER BY {order}').fetchall()
            result[name] = tuple(tuple(row) for row in rows)
        db.rollback()
        return LogicalSnapshot(result)
    finally:
        db.close()


def _pg_rows(connection: Any, table: str, spec: TableSpec) -> tuple[tuple[Any, ...], ...]:
    columns = ",".join(f'"{column}"' for column in spec.columns)
    order = ",".join(f'"{column}"' for column in spec.primary_key)
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {columns} FROM "{table}" ORDER BY {order}')
        return tuple(tuple(row) for row in cursor.fetchall())


def capture_postgres(connection: Any) -> LogicalSnapshot:
    return LogicalSnapshot({
        table: _pg_rows(connection, table, spec)
        for table, spec in CURRENT_TABLES.items()
    })


def compare_snapshots(source: LogicalSnapshot, target: LogicalSnapshot) -> Comparison:
    if set(source.tables) != set(target.tables):
        raise ValueError("snapshot table sets differ")
    counts: dict[str, tuple[int, int]] = {}
    digests: dict[str, tuple[str, str]] = {}
    matching = True
    for table in CURRENT_TABLES:
        left, right = source.tables[table], target.tables[table]
        counts[table] = (len(left), len(right))
        digests[table] = (_row_digest(left), _row_digest(right))
        matching = matching and counts[table][0] == counts[table][1]
        matching = matching and digests[table][0] == digests[table][1]
    return Comparison(counts, digests, matching)


def load_snapshot(connection: Any, snapshot: LogicalSnapshot) -> None:
    """Load into an empty laboratory schema; never truncates existing data."""
    if set(snapshot.tables) != set(CURRENT_TABLES):
        raise ValueError("snapshot does not contain the closed current table set")
    with connection.cursor() as cursor:
        for table in reversed(CURRENT_TABLES):
            cursor.execute(f'SELECT count(*) FROM "{table}"')
            if int(cursor.fetchone()[0]) != 0:
                raise ValueError(f"target table {table} is not empty; refusing destructive import")
        for table, spec in CURRENT_TABLES.items():
            rows = snapshot.tables[table]
            if not rows:
                continue
            bool_indexes = {spec.columns.index(name) for name in spec.boolean_columns}
            converted = [
                tuple(bool(value) if index in bool_indexes else value for index, value in enumerate(row))
                for row in rows
            ]
            columns = ",".join(f'"{column}"' for column in spec.columns)
            placeholders = ",".join(["%s"] * len(spec.columns))
            cursor.executemany(
                f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', converted
            )


def validate_lab_dsn(dsn: str, acknowledgement: str | None = None) -> None:
    if acknowledgement != LAB_ACK:
        raise RuntimeError("PostgreSQL laboratory acknowledgement is missing")
    lowered = dsn.lower()
    if lowered.startswith(("postgres://", "postgresql://")):
        query = parse_qs(urlparse(dsn).query)
        sslmode = query.get("sslmode", [""])[-1].lower()
    else:
        match = re.search(r"(?:^|\s)sslmode\s*=\s*([^\s]+)", lowered)
        sslmode = match.group(1).strip("'\"") if match else ""
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("PostgreSQL DSN must explicitly enforce TLS with sslmode")


def connect_postgres_from_env(dsn_env: str = "CDNMNUS_POSTGRES_LAB_DSN") -> Any:
    dsn = os.environ.get(dsn_env, "")
    if not dsn:
        raise RuntimeError(f"{dsn_env} is not set")
    validate_lab_dsn(dsn, os.environ.get("CDNMNUS_POSTGRES_LAB_ACK"))
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("psycopg 3 is required in the isolated laboratory environment") from exc
    return psycopg.connect(dsn, connect_timeout=10, application_name="cdnmnus-postgres-lab")


def apply_migrations(connection: Any, migration_root: str | Path = MIGRATION_ROOT) -> list[str]:
    """Apply immutable SQL migrations under a PostgreSQL advisory lock."""
    root = Path(migration_root)
    files = sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ValueError("no PostgreSQL migrations found")
    applied: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cdnmnus-schema-migrations'))")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("SELECT version, sha256 FROM schema_migrations")
        known = dict(cursor.fetchall())
        for path in files:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if path.name in known:
                if known[path.name] != digest:
                    raise RuntimeError(f"applied migration changed: {path.name}")
                continue
            cursor.execute(sql)
            cursor.execute(
                "INSERT INTO schema_migrations(version,sha256) VALUES(%s,%s)",
                (path.name, digest),
            )
            applied.append(path.name)
    return applied


@dataclass(frozen=True)
class Lease:
    service_id: str
    holder_node_id: str
    lease_id: str
    fencing_token: int
    expires_at: float | datetime


class LeaseStore(Protocol):
    def acquire(self, service_id: str, holder_node_id: str, ttl_seconds: int) -> Lease | None: ...
    def renew(self, lease: Lease, ttl_seconds: int) -> Lease | None: ...
    def release(self, lease: Lease) -> bool: ...


class PostgresLeaseStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def acquire(self, service_id: str, holder_node_id: str, ttl_seconds: int) -> Lease | None:
        if ttl_seconds < 2:
            raise ValueError("lease TTL must be at least two seconds")
        lease_id = str(uuid.uuid4())
        with self.connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO promotion_locks
                    (service_id,holder_node_id,lease_id,expires_at,fencing_token)
                VALUES (%s,%s,%s,CURRENT_TIMESTAMP + (%s * interval '1 second'),1)
                ON CONFLICT (service_id) DO UPDATE SET
                    holder_node_id=EXCLUDED.holder_node_id,
                    lease_id=EXCLUDED.lease_id,
                    expires_at=EXCLUDED.expires_at,
                    fencing_token=promotion_locks.fencing_token+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE promotion_locks.expires_at <= CURRENT_TIMESTAMP
                RETURNING lease_id::text,fencing_token,expires_at
            """, (service_id, holder_node_id, lease_id, ttl_seconds))
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "UPDATE nodes SET lease_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                (lease_id, holder_node_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("lease holder node does not exist")
        return Lease(service_id, holder_node_id, row[0], int(row[1]), row[2])

    def renew(self, lease: Lease, ttl_seconds: int) -> Lease | None:
        with self.connection.cursor() as cursor:
            cursor.execute("""
                UPDATE promotion_locks
                   SET expires_at=CURRENT_TIMESTAMP + (%s * interval '1 second'),
                       updated_at=CURRENT_TIMESTAMP
                 WHERE service_id=%s AND holder_node_id=%s AND lease_id=%s
                   AND fencing_token=%s AND expires_at > CURRENT_TIMESTAMP
                RETURNING expires_at
            """, (ttl_seconds, lease.service_id, lease.holder_node_id,
                    lease.lease_id, lease.fencing_token))
            row = cursor.fetchone()
        return None if row is None else Lease(
            lease.service_id, lease.holder_node_id, lease.lease_id, lease.fencing_token, row[0]
        )

    def release(self, lease: Lease) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("""
                DELETE FROM promotion_locks
                 WHERE service_id=%s AND holder_node_id=%s AND lease_id=%s AND fencing_token=%s
            """, (lease.service_id, lease.holder_node_id, lease.lease_id, lease.fencing_token))
            removed = cursor.rowcount == 1
            if removed:
                cursor.execute(
                    "UPDATE nodes SET lease_id=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=%s AND lease_id=%s",
                    (lease.holder_node_id, lease.lease_id),
                )
        return removed


class MemoryLeaseStore:
    """Thread-safe deterministic contract double; never suitable for production."""
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, Lease] = {}
        self._tokens: dict[str, int] = {}

    def acquire(self, service_id: str, holder_node_id: str, ttl_seconds: int) -> Lease | None:
        if ttl_seconds < 2:
            raise ValueError("lease TTL must be at least two seconds")
        with self._lock:
            now = self._clock()
            current = self._leases.get(service_id)
            if current is not None and float(current.expires_at) > now:
                return None
            token = self._tokens.get(service_id, 0) + 1
            self._tokens[service_id] = token
            lease = Lease(service_id, holder_node_id, str(uuid.uuid4()), token, now + ttl_seconds)
            self._leases[service_id] = lease
            return lease

    def renew(self, lease: Lease, ttl_seconds: int) -> Lease | None:
        with self._lock:
            current = self._leases.get(lease.service_id)
            if current != lease or float(current.expires_at) <= self._clock():
                return None
            renewed = Lease(
                lease.service_id, lease.holder_node_id, lease.lease_id,
                lease.fencing_token, self._clock() + ttl_seconds,
            )
            self._leases[lease.service_id] = renewed
            return renewed

    def release(self, lease: Lease) -> bool:
        with self._lock:
            if self._leases.get(lease.service_id) != lease:
                return False
            del self._leases[lease.service_id]
            return True


class FencingError(RuntimeError):
    pass


class FencingProvider(Protocol):
    def fence(self, resource: str, node_id: str, fencing_token: int) -> None: ...


class FakeFencingProvider:
    """Fail-closed fake provider for partition/failover tests."""
    def __init__(self) -> None:
        self.available = True
        self.fail_next = False
        self.events: list[dict[str, Any]] = []
        self._highest_token: dict[str, int] = {}

    def fence(self, resource: str, node_id: str, fencing_token: int) -> None:
        if not self.available or self.fail_next:
            self.fail_next = False
            raise FencingError("fencing provider unavailable")
        if fencing_token <= self._highest_token.get(resource, 0):
            raise FencingError("stale fencing token rejected")
        self._highest_token[resource] = fencing_token
        self.events.append({"resource": resource, "node_id": node_id, "fencing_token": fencing_token})


class FailoverCoordinator:
    def __init__(self, leases: LeaseStore, fencing: FencingProvider) -> None:
        self.leases = leases
        self.fencing = fencing

    def promote(
        self,
        service_id: str,
        candidate_node_id: str,
        former_active_node_id: str,
        activate: Callable[[Lease], None],
        ttl_seconds: int = 30,
    ) -> Lease:
        lease = self.leases.acquire(service_id, candidate_node_id, ttl_seconds)
        if lease is None:
            raise RuntimeError("promotion lock is held")
        try:
            if former_active_node_id and former_active_node_id != candidate_node_id:
                self.fencing.fence(service_id, former_active_node_id, lease.fencing_token)
            activate(lease)
            return lease
        except Exception:
            self.leases.release(lease)
            raise


def claim_postgres_deployment(connection: Any, worker_id: str, ttl_seconds: int = 120) -> dict[str, Any] | None:
    """Claim one queued deployment without duplicate work across workers."""
    with connection.cursor() as cursor:
        cursor.execute("""
            WITH next_job AS (
                SELECT id FROM deployments
                 WHERE state='queued'
                 ORDER BY created_at,id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE deployments AS job
               SET state='running',claimed_by=%s,
                   claim_expires_at=CURRENT_TIMESTAMP + (%s * interval '1 second'),
                   attempt_count=attempt_count+1,started_at=COALESCE(started_at,CURRENT_TIMESTAMP)
              FROM next_job
             WHERE job.id=next_job.id
            RETURNING job.id,job.state,job.release_id,job.config_digest,job.artifact_path,
                      job.claimed_by,job.claim_expires_at,job.attempt_count
        """, (worker_id, ttl_seconds))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = (
            "id", "state", "release_id", "config_digest", "artifact_path",
            "claimed_by", "claim_expires_at", "attempt_count",
        )
        return dict(zip(columns, row))


SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


@dataclass(frozen=True)
class BackupRestorePlan:
    backup_command: tuple[str, ...]
    restore_command: tuple[str, ...]
    verify_command: tuple[str, ...]


def backup_restore_plan(service: str, restore_service: str, output: str | Path) -> BackupRestorePlan:
    """Return credential-safe commands using pg_service.conf, never a DSN."""
    if not SERVICE_RE.fullmatch(service) or not SERVICE_RE.fullmatch(restore_service):
        raise ValueError("invalid PostgreSQL service name")
    destination = Path(output).resolve()
    if destination.suffix != ".dump":
        raise ValueError("custom PostgreSQL backup must use a .dump path")
    return BackupRestorePlan(
        ("pg_dump", f"--dbname=service={service}", "--format=custom", "--no-owner", "--no-acl", f"--file={destination}"),
        ("pg_restore", f"--dbname=service={restore_service}", "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction", str(destination)),
        ("psql", f"service={restore_service}", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--command=SELECT count(*) FROM schema_migrations;"),
    )
