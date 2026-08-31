#!/usr/bin/env python3
"""Entrada restrita no control plane para solicitações vindas do menu do nó."""
from __future__ import annotations

import argparse
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
        raise PermissionError("solicitação aceita somente por sessão SSH identificada")
    return fields[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--mode", choices=("candidate", "standby"), required=True)
    parser.add_argument("--package-ref", required=True)
    parser.add_argument("--package-commit", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    database = Database(os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    database.initialize(); TopologyStore(database).initialize()
    result = database.request_load_balancer_promotion(
        args.node_id, args.mode, args.package_ref, args.package_commit,
        args.manifest_digest, source_ip(), args.reason,
    )
    print(json.dumps({"request_id": result["id"], "state": result["state"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
