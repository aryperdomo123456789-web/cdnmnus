"""Compilação de releases e chamada controlada do playbook serial."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from core.db import Database
from core.render_tenants import broker_snapshot, render_all


def build_release(db: Database, release_root: str | Path = "/var/lib/cdnmnus-admin/releases") -> dict[str, Any]:
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        raise ValueError("nenhum tenant habilitado")
    generation = max(int(item["config_version"]) for item in tenants)
    rendered = render_all(tenants)
    snapshot = broker_snapshot(tenants, generation)
    release_id = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    root = Path(release_root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    final = root / release_id
    with tempfile.TemporaryDirectory(prefix=".release-", dir=root) as temp_name:
        temp = Path(temp_name)
        (temp / "nginx/tenants").mkdir(parents=True)
        (temp / "broker").mkdir()
        hashes: dict[str, str] = {}
        for relative, item in rendered.items():
            destination = temp / "nginx" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(item.content, encoding="utf-8")
            os.chmod(destination, 0o640)
            hashes[str(destination.relative_to(temp))] = item.sha256
        (temp / "broker/tenants.json").write_text(snapshot, encoding="utf-8")
        os.chmod(temp / "broker/tenants.json", 0o640)
        hashes["broker/tenants.json"] = hashlib.sha256(snapshot.encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest = {"schema_version": 1, "release_id": release_id, "generation": generation,
                    "tenant_count": len(tenants), "config_digest": digest, "files": hashes}
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp / "manifest.json", 0o640)
        os.rename(temp, final)
    return {**manifest, "artifact_path": str(final)}


def _inventory(db: Database, key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    hosts: dict[str, Any] = {}
    known_hosts = Path(key_dir) / "known_hosts"
    for edge in db.edges():
        if edge["state"] not in ("pending", "bootstrapping", "ready", "draining"):
            continue
        key_path = Path(key_dir) / f"{edge['id']}.ed25519"
        hosts[edge["id"]] = {
            "ansible_host": edge["ipv4"], "ansible_port": edge["ssh_port"],
            "ansible_user": "cdn-deploy", "ansible_become": True,
            "ansible_ssh_private_key_file": str(key_path),
            "ansible_ssh_common_args": f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}",
        }
    if not hosts:
        raise ValueError("nenhuma edge ready/draining para deploy")
    return {"all": {"children": {"cdn_edges": {"hosts": hosts}}}}


def queue_deployment(db: Database,
                     release_root: str | Path = "/var/lib/cdnmnus-admin/releases") -> dict[str, Any]:
    release = build_release(db, release_root)
    deployment_id = "dep-" + uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute("INSERT INTO deployments(id,state,release_id,config_digest,artifact_path) VALUES(?,?,?,?,?)",
                     (deployment_id, "queued", release["release_id"], release["config_digest"], release["artifact_path"]))
    return {"deployment_id": deployment_id, "state": "queued", **release}


def claim_deployment(db: Database) -> dict[str, Any] | None:
    with db.transaction(immediate=True) as conn:
        row = conn.execute("SELECT * FROM deployments WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        changed = conn.execute("UPDATE deployments SET state='running',started_at=CURRENT_TIMESTAMP WHERE id=? AND state='queued'", (row["id"],))
        if changed.rowcount != 1:
            return None
        return dict(row)


def run_deployment(db: Database, deployment: dict[str, Any], inventory: str | Path | None = None,
                   playbook: str | Path = "ansible/playbooks/deploy-and-activate-edge.yml",
                   key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    if shutil.which("ansible-playbook") is None:
        error = "ansible-playbook não está instalado; instale ansible-core no control node"
        with db.connect() as conn:
            conn.execute("UPDATE deployments SET state='failed',error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (error, deployment["id"]))
        raise RuntimeError(error)
    generated_inventory: Path | None = None
    if inventory is None:
        fd, generated_name = tempfile.mkstemp(prefix="cdnmnus-inventory-", suffix=".json")
        generated_inventory = Path(generated_name)
        try:
            os.write(fd, json.dumps(_inventory(db, key_dir)).encode())
        finally:
            os.close(fd)
        os.chmod(generated_inventory, 0o600)
        inventory = generated_inventory
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        raise ValueError("nenhum tenant habilitado para ativação da edge")
    command = ["ansible-playbook", "-i", str(inventory), str(playbook),
               "--extra-vars", json.dumps({
                   "release_id": deployment["release_id"],
                   "release_source": deployment["artifact_path"],
                   "config_digest": deployment["config_digest"],
                   "canonical_health_host": tenants[0]["canonical_host"],
                   "tenant_ids": [item["id"] for item in tenants],
                   "tenant_health_hosts": [{"host": item["canonical_host"]} for item in tenants],
               })]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    finally:
        if generated_inventory is not None:
            generated_inventory.unlink(missing_ok=True)
    state = "succeeded" if result.returncode == 0 else "failed"
    if result.returncode == 0:
        error = None
    else:
        # O Ansible pode conter caminhos e nomes de hosts, mas não deve levar
        # argv/env com credenciais. Guardamos somente as últimas linhas para
        # diagnóstico operacional no painel/journal.
        tail = "\n".join((result.stderr or result.stdout).splitlines()[-8:])
        error = "ansible-playbook falhou; resumo sanitizado:\n" + tail[:2000]
    with db.connect() as conn:
        conn.execute("UPDATE deployments SET state=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                     (state, error, deployment["id"]))
    if result.returncode != 0:
        raise RuntimeError(error or "deploy falhou")
    for edge in db.edges():
        if edge["state"] in ("ready", "draining"):
            db.set_edge_state(edge["id"], edge["state"], deployment["release_id"])
    return {"deployment_id": deployment["id"], "state": state,
            "release_id": deployment["release_id"], "config_digest": deployment["config_digest"],
            "artifact_path": deployment["artifact_path"]}


def deploy_serial(db: Database, inventory: str | Path | None = None,
                  playbook: str | Path = "ansible/playbooks/deploy-edge.yml",
                  release_root: str | Path = "/var/lib/cdnmnus-admin/releases",
                  key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    queued = queue_deployment(db, release_root)
    claimed = claim_deployment(db)
    if claimed is None or claimed["id"] != queued["deployment_id"]:
        raise RuntimeError("deployment não pôde ser reservado")
    return run_deployment(db, claimed, inventory, playbook, key_dir)
