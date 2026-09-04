from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.db import Database
from core.topology import TopologyStore


def load_audit_module():
    path = Path(__file__).parents[1] / "scripts/cdnmnus-readiness-audit.py"
    spec = importlib.util.spec_from_file_location("cdnmnus_readiness_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with tempfile.TemporaryDirectory(prefix="cdnmnus-readiness-") as root:
    database = Database(Path(root) / "admin.db")
    database.initialize()
    topology = TopologyStore(database)
    topology.initialize()
    topology.add_node("lb1", "LB 1", "198.51.100.10", "load_balancer", "standby", "test", "fixture")
    now = datetime.now(timezone.utc)
    with database.connect() as connection, connection:
        connection.execute(
            "INSERT INTO promotion_locks(service_id,holder_node_id,lease_id,expires_at,fencing_token) "
            "VALUES(?,?,?,?,?)",
            ("public", "lb1", str(uuid.uuid4()), (now + timedelta(minutes=5)).isoformat(), 1),
        )

    audit = load_audit_module()
    assert len(audit.valid_promotion_locks(database)) == 1
    database.set_setting("external_fencing_provider", {"enabled": True, "verified": True})
    assert audit.external_fencing_is_verified(database)

    with database.connect() as connection, connection:
        connection.execute(
            "UPDATE promotion_locks SET expires_at=?",
            ((now - timedelta(minutes=1)).isoformat(),),
        )
    assert audit.valid_promotion_locks(database) == []
