#!/usr/bin/env python3
"""RPC SSH restrito para atualizar o perfil contratado de capacidade."""
from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CDNMNUS_PROJECT_ROOT", "/opt/cdnmnus")).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.topology import TopologyStore  # noqa: E402


def source_ip() -> str:
    fields = os.environ.get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        raise PermissionError("operação aceita somente por sessão SSH identificada")
    return fields[0]


def main() -> int:
    raw = sys.stdin.buffer.read(65_537)
    if not raw or len(raw) > 65_536:
        raise ValueError("pedido vazio ou acima do limite")
    payload = json.loads(raw)
    raw = b""
    if not isinstance(payload, dict):
        raise ValueError("pedido deve ser um objeto JSON")
    required = {"node_id", "capacity_mbps", "source"}
    if not required.issubset(payload):
        raise ValueError("perfil de capacidade incompleto")
    database = Database(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    database.initialize()
    topology = TopologyStore(database)
    topology.initialize()
    requester_ip = source_ip()
    requester = database.rows("SELECT id,role,state FROM nodes WHERE ipv4=?", (requester_ip,))[:1]
    if not requester:
        raise PermissionError("nó solicitante não está autorizado no plano de controle")
    result = topology.set_capacity_profile(
        str(payload["node_id"]),
        int(payload["capacity_mbps"]),
        source=str(payload["source"]),
        confidence=str(payload.get("confidence", "manual")),
        headroom=float(payload.get("headroom", 0.25)),
        max_connections=int(payload.get("max_connections", 0)),
        measured_mbps=None if payload.get("measured_mbps") in (None, "") else int(payload["measured_mbps"]),
        measured_at=None if payload.get("measured_at") in (None, "") else str(payload["measured_at"]),
        expires_at=None if payload.get("expires_at") in (None, "") else str(payload["expires_at"]),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        gc.collect()
