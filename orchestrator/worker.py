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
from core.dns_reconciler import reconcile_cluster_dns
from core.tenant_onboarding import TenantOnboardingService
from core.deploy import queue_deployment


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Ansible desacoplado cdnmnus")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--playbook", default="ansible/playbooks/deploy-and-activate-edge.yml")
    parser.add_argument("--tls-job-timeout-seconds", type=int,
                        default=int(os.environ.get("CDNMNUS_TLS_JOB_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--tls-job-max-attempts", type=int,
                        default=int(os.environ.get("CDNMNUS_TLS_JOB_MAX_ATTEMPTS", "3")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    db = Database(args.db); db.initialize()
    while True:
        onboarding = db.claim_tenant_onboarding()
        if onboarding is not None:
            tenant_id = onboarding["tenant_id"]
            service = TenantOnboardingService(db, operator="onboarding-worker")
            try:
                result = service.execute(
                    tenant_id,
                    stage_tls=lambda: TLSProvisioner(db).stage_shared_certificate(tenant_id),
                    deploy=lambda: _run_onboarding_deploy(db),
                    verify=lambda: TLSProvisioner(db).verify_staged(tenant_id),
                    publish_dns=lambda: reconcile_cluster_dns(db, operator="onboarding-worker"),
                )
                print(f"tenant onboarding {tenant_id} committed: {result.get('release_id', '')}")
            except Exception as exc:
                print(f"tenant onboarding {tenant_id} failed: {exc}", file=sys.stderr)
            continue
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


def _run_onboarding_deploy(db: Database) -> dict[str, object]:
    queued = queue_deployment(db)
    claimed = claim_deployment(db)
    if claimed is None or claimed["id"] != queued["deployment_id"]:
        raise RuntimeError("deployment do onboarding não pôde ser reservado")
    return run_deployment(db, claimed)


if __name__ == "__main__":
    main()
