#!/usr/bin/env python3
"""Consome deployments enfileirados sem expor porta pública."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database
from core.deploy import claim_deployment, run_deployment


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Ansible desacoplado cdnmnus")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/etc/cdnmnus/admin.db"))
    parser.add_argument("--playbook", default="ansible/playbooks/deploy-edge.yml")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    db = Database(args.db); db.initialize()
    while True:
        deployment = claim_deployment(db)
        if deployment is not None:
            try:
                run_deployment(db, deployment, playbook=args.playbook)
            except Exception as exc:
                print(f"deployment {deployment['id']} falhou: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()

