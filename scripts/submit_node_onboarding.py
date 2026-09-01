#!/usr/bin/env python3
"""RPC SSH restrito: cadastra edge/LB sem persistir a senha inicial."""
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
from core.control_plane import resolve_control_plane_host  # noqa: E402
from core.node_onboarding import onboard_node  # noqa: E402
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
    required = {"name", "ipv4", "ssh_port", "initial_user", "password", "role"}
    if set(payload) != required:
        raise ValueError("campos do pedido divergentes do contrato fechado")
    database = Database(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    database.initialize(); TopologyStore(database).initialize()
    requester_ip = source_ip()
    requester = database.rows(
        """SELECT id,role,state FROM nodes
             WHERE ipv4=? AND role IN ('edge','load_balancer')
               AND state NOT IN ('pending','bootstrapping','failed','disabled')""",
        (requester_ip,),
    )
    if not requester:
        raise PermissionError("nó solicitante não está autorizado no plano de controle")
    password = str(payload.pop("password"))
    try:
        result = onboard_node(
            database,
            name=str(payload["name"]), ipv4=str(payload["ipv4"]),
            ssh_port=int(payload["ssh_port"]), initial_user=str(payload["initial_user"]),
            password=password, role=str(payload["role"]),
            operator=f"menu-node-{requester[0]['id']}",
            control_plane=resolve_control_plane_host(require_explicit=True),
        )
    finally:
        password = ""
        payload.clear()
        gc.collect()
    print(json.dumps({
        "node_id": result["node_id"], "role": result["role"],
        "state": result["state"], "deployment_id": result["deployment_id"],
        "package_ref": result["package_ref"], "trust_mode": result["trust_mode"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
