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
from core.tls_provisioner import TLSProvisioner


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Ansible desacoplado cdnmnus")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/etc/cdnmnus/admin.db"))
    parser.add_argument("--playbook", default="ansible/playbooks/deploy-and-activate-edge.yml")
    parser.add_argument("--tls-job-timeout-seconds", type=int,
                        default=int(os.environ.get("CDNMNUS_TLS_JOB_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--tls-job-max-attempts", type=int,
                        default=int(os.environ.get("CDNMNUS_TLS_JOB_MAX_ATTEMPTS", "3")))
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
        tls_job = db.claim_tls_job(
            timeout_seconds=args.tls_job_timeout_seconds,
            max_attempts=args.tls_job_max_attempts,
        )
        if tls_job is not None:
            try:
                TLSProvisioner(db).provision(
                    tls_job["tenant_id"], job_id=tls_job["id"], lease_id=tls_job["lease_id"]
                )
                db.finish_tls_job(tls_job["id"], "succeeded", lease_id=tls_job["lease_id"])
            except Exception as exc:
                db.finish_tls_job(tls_job["id"], "failed", str(exc), lease_id=tls_job["lease_id"])
                print(f"tls job {tls_job['id']} falhou: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
