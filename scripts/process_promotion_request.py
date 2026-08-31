#!/usr/bin/env python3
"""Aprova e prepara LB candidate/standby; ACTIVE não é aceito neste fluxo."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.topology import TopologyStore  # noqa: E402


def load_config(path: Path) -> dict:
    if path.stat().st_mode & 0o077:
        raise PermissionError("configuração de promoção deve possuir modo 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "change_id", "haproxy_version", "public_hosts", "backends",
        "backend_health_host", "tls_fullchain_source", "tls_private_key_source",
    }
    if not required.issubset(data):
        raise ValueError("configuração de promoção incompleta")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", data["change_id"]):
        raise ValueError("change_id inválido")
    if not data["backends"] or not data["public_hosts"]:
        raise ValueError("hosts/backends obrigatórios")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--key-dir", type=Path, default=Path("/etc/cdnmnus/ssh"))
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.confirm:
        raise PermissionError("execução exige root e --confirm")
    ansible_playbook = shutil.which("ansible-playbook")
    project_ansible = ROOT / "venv/bin/ansible-playbook"
    if ansible_playbook is None and project_ansible.is_file():
        ansible_playbook = str(project_ansible)
    if ansible_playbook is None:
        raise RuntimeError("ansible-playbook ausente")
    config = load_config(args.config.resolve())
    database = Database(args.db); database.initialize(); TopologyStore(database).initialize()
    matches = [item for item in database.promotion_requests() if item["id"] == args.request_id]
    if not matches or matches[0]["state"] != "requested":
        raise ValueError("solicitação inexistente ou não está requested")
    request = matches[0]
    edge = database.edge(request["node_id"])
    topology_node = TopologyStore(database).node(request["node_id"])
    active_jobs = database.rows(
        "SELECT id FROM deployments WHERE state IN ('queued','running') LIMIT 1"
    )
    if active_jobs:
        raise RuntimeError("deployment de edge pendente; preparação LB recusada")
    backend_ids = [str(item["node_id"]) for item in config["backends"]]
    database.set_promotion_request_state(args.request_id, "approved")
    database.set_edge_state(
        edge["id"], "draining", operator="promotion-controller",
        reason="drain antes de preparar load balancer",
        payload={"request_id": args.request_id, "change_id": config["change_id"]},
    )
    database.sync_dns_matrix()
    database.set_promotion_request_state(args.request_id, "installing")

    inventory = {
        "all": {"children": {"load_balancers": {"hosts": {
            edge["id"]: {
                "ansible_host": edge["ipv4"], "ansible_port": edge["ssh_port"],
                "ansible_user": "cdn-deploy", "ansible_become": True,
                "ansible_ssh_private_key_file": str(args.key_dir / f"{edge['id']}.ed25519"),
                "ansible_ssh_common_args": (
                    "-o StrictHostKeyChecking=yes -o UserKnownHostsFile="
                    + str(args.key_dir / "known_hosts")
                ),
                "cdnmnus_node_id": edge["id"],
                "cdnmnus_node_name": edge["name"],
                "cdnmnus_release_id": topology_node.get("release_id") or "",
                "cdnmnus_config_digest": topology_node.get("node_config_digest") or "",
                "cdnmnus_control_plane_host": "143.14.168.111",
            }
        }}}}
    }
    extra = {
        "load_balancer_action": "deploy",
        "load_balancer_mode": request["requested_mode"],
        "load_balancer_environment": config.get("environment", "staging"),
        "load_balancer_change_id": config["change_id"],
        "load_balancer_haproxy_version": config["haproxy_version"],
        "load_balancer_public_hosts": config["public_hosts"],
        "load_balancer_backends": [
            {"name": item["name"], "address": item["address"],
             "port": int(item.get("port", 443)), "state": "ready",
             "weight": int(item.get("weight", 100))}
            for item in config["backends"]
        ],
        "load_balancer_backend_health_host": config["backend_health_host"],
        "load_balancer_tls_fullchain_source": config["tls_fullchain_source"],
        "load_balancer_tls_private_key_source": config["tls_private_key_source"],
        "load_balancer_manage_firewall": False,
        "expected_node_package_ref": request["package_ref"],
        "expected_node_package_commit": request["package_commit"],
        "expected_node_manifest_digest": request["manifest_digest"],
    }
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix="cdnmnus-promotion-") as temporary:
            root = Path(temporary)
            inventory_path = root / "inventory.json"; vars_path = root / "vars.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            vars_path.write_text(json.dumps(extra), encoding="utf-8")
            os.chmod(inventory_path, 0o600); os.chmod(vars_path, 0o600)
            result = subprocess.run(
                [ansible_playbook, "-i", str(inventory_path),
                 str(ROOT / "ansible/playbooks/load-balancer.yml"),
                 "--extra-vars", "@" + str(vars_path)],
                cwd=ROOT, capture_output=True, text=True, timeout=1800, check=False,
            )
        if result.returncode != 0:
            detail = "\n".join((result.stderr or result.stdout).splitlines()[-8:])
            raise RuntimeError("role LB falhou:\n" + detail[:2000])
        database.finalize_load_balancer_candidate(
            args.request_id, "lb-" + edge["id"], backend_ids,
            "promotion-controller", "pacote e candidato HAProxy validados",
        )
        database.sync_dns_matrix()
    except Exception:
        current = database.edge(edge["id"])
        if current["state"] == "draining":
            database.set_edge_state(
                edge["id"], "ready", operator="promotion-controller",
                reason="rollback automático da preparação LB",
                payload={"request_id": args.request_id},
            )
            database.sync_dns_matrix()
        state = next(item for item in database.promotion_requests() if item["id"] == args.request_id)["state"]
        if state == "installing":
            database.set_promotion_request_state(args.request_id, "failed")
        raise
    print(json.dumps({"request_id": args.request_id, "state": request["requested_mode"],
                      "node_id": edge["id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
