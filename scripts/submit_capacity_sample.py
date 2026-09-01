#!/usr/bin/env python3
"""RPC SSH restrito para gravar amostras de consumo da VPS solicitante."""
from __future__ import annotations

import gc
import json
import os
import sys
from datetime import datetime, timezone
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
    required = {
        "sampled_at", "tx_mbps", "p95_ms", "http5xx", "active_sessions",
        "cpu_pct", "mem_pct", "nic_errors", "vod_206_ok",
    }
    if not required.issubset(payload):
        raise ValueError("pedido de amostra incompleto")
    database = Database(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    database.initialize()
    topology = TopologyStore(database)
    topology.initialize()
    requester_ip = source_ip()
    node = database.rows("SELECT id FROM nodes WHERE ipv4=?", (requester_ip,))[:1]
    if not node:
        raise PermissionError("nó solicitante não está autorizado no plano de controle")
    requested_node_id = str(payload.get("node_id", node[0]["id"]))
    if requested_node_id != node[0]["id"]:
        raise PermissionError("a amostra só pode ser enviada para a VPS que originou a sessão")
    result = topology.record_capacity_sample(
        requested_node_id,
        str(payload["sampled_at"]),
        float(payload["tx_mbps"]),
        float(payload["p95_ms"]),
        float(payload["http5xx"]),
        int(payload["active_sessions"]),
        float(payload["cpu_pct"]),
        float(payload["mem_pct"]),
        int(payload["nic_errors"]),
        bool(payload["vod_206_ok"]),
        interface_name=(None if payload.get("interface_name") in (None, "") else str(payload["interface_name"])),
        sample_window_sec=int(payload.get("sample_window_sec", 10)),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        gc.collect()
