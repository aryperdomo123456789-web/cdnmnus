"""Compilação de releases e chamada controlada do playbook serial."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import closing
import time
import uuid
from pathlib import Path
from typing import Any

from core.db import Database
from core.render_tenants import broker_snapshot, render_all


def _release_digest(files: dict[str, str]) -> str:
    """Digest canônico da lista fechada de artefatos da release."""
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
        (temp / "runtime").mkdir()
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
        project_root = Path(__file__).resolve().parents[1]
        runtime_sources = {
            "runtime/token_broker.py": project_root / "panel/token_broker.py",
            "runtime/multi_tenant_broker.py": project_root / "panel/multi_tenant_broker.py",
            "runtime/vod_relay.py": project_root / "panel/vod_relay.py",
            "runtime/cdnmnus-tenant-broker@.service": project_root / "panel/cdnmnus-tenant-broker@.service",
            "runtime/cdnmnus-vod-relay@.service": project_root / "panel/cdnmnus-vod-relay@.service",
        }
        for relative, source in runtime_sources.items():
            if not source.is_file():
                raise FileNotFoundError(f"artefato obrigatório do runtime ausente: {source}")
            content = source.read_bytes()
            destination = temp / relative
            destination.write_bytes(content)
            os.chmod(destination, 0o640)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        digest = _release_digest(hashes)
        manifest = {"schema_version": 1, "release_id": release_id, "generation": generation,
                    "tenant_count": len(tenants), "config_digest": digest, "files": hashes}
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp / "manifest.json", 0o640)
        # Mesmo filesystem: a release só se torna visível depois de completa.
        os.rename(temp, final)
    return {**manifest, "artifact_path": str(final)}


def _inventory(db: Database, key_dir: str | Path = "/etc/cdnmnus/ssh", *,
               include_bootstrapping: bool = False,
               edge_ids: set[str] | None = None) -> dict[str, Any]:
    hosts: dict[str, Any] = {}
    known_hosts = Path(key_dir) / "known_hosts"
    admitted_states = {"ready", "draining"}
    if include_bootstrapping:
        admitted_states.add("bootstrapping")
    for edge in db.edges():
        # ``bootstrapping`` só é admitido pelo pipeline completo de onboarding,
        # que executa preflight, ativação e auditoria antes de promover a ready.
        if edge["state"] not in admitted_states:
            continue
        if edge_ids is not None and edge["id"] not in edge_ids:
            continue
        key_path = Path(key_dir) / f"{edge['id']}.ed25519"
        hosts[edge["id"]] = {
            "ansible_host": edge["ipv4"], "ansible_port": edge["ssh_port"],
            "ansible_user": "cdn-deploy", "ansible_become": True,
            "ansible_ssh_private_key_file": str(key_path),
            "ansible_ssh_common_args": f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}",
            "cdnmnus_node_id": edge["id"],
            "cdnmnus_node_name": edge["name"],
            "cdnmnus_node_role": "edge",
            "cdnmnus_node_state": "ready",
        }
    if not hosts:
        raise ValueError("nenhuma edge elegível para este deployment")
    return {"all": {"children": {"cdn_edges": {"hosts": hosts}}}}


def queue_deployment(db: Database,
                     release_root: str | Path = "/var/lib/cdnmnus-admin/releases",
                     target_edge_id: str | None = None) -> dict[str, Any]:
    if target_edge_id is not None:
        target_edge = db.edge(target_edge_id)
        if target_edge["state"] != "bootstrapping":
            raise ValueError("o alvo explícito de onboarding deve estar em bootstrapping")
    release = build_release(db, release_root)
    deployment_id = "dep-" + uuid.uuid4().hex
    with closing(db.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO deployments(id,state,release_id,config_digest,artifact_path,target_edge_id) "
            "VALUES(?,?,?,?,?,?)",
            (deployment_id, "queued", release["release_id"], release["config_digest"],
             release["artifact_path"], target_edge_id),
        )
    return {"deployment_id": deployment_id, "state": "queued",
            "target_edge_id": target_edge_id, **release}


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
        with closing(db.connect()) as conn, conn:
            conn.execute("UPDATE deployments SET state='failed',error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (error, deployment["id"]))
        raise RuntimeError(error)
    generated_inventory: Path | None = None
    managed_edge_ids: set[str] | None = None
    if inventory is None:
        fd, generated_name = tempfile.mkstemp(prefix="cdnmnus-inventory-", suffix=".json")
        generated_inventory = Path(generated_name)
        target_edge_id = deployment.get("target_edge_id")
        target_edge_ids = {str(target_edge_id)} if target_edge_id else None
        inventory_data = _inventory(
            db,
            key_dir,
            include_bootstrapping=bool(target_edge_id),
            edge_ids=target_edge_ids,
        )
        managed_edge_ids = set(
            inventory_data["all"]["children"]["cdn_edges"]["hosts"]
        )
        try:
            os.write(fd, json.dumps(inventory_data).encode())
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
                   "vod_tenant_ids": [item["id"] for item in tenants
                                      if any(upstream["kind"] == "vod" for upstream in item.get("upstreams", []))],
                   "tenant_health_hosts": [{"host": item["canonical_host"]} for item in tenants],
               })]
    onboarding_edges = [
        edge for edge in db.edges()
        if edge["state"] == "bootstrapping"
        and managed_edge_ids is not None
        and edge["id"] in managed_edge_ids
    ]
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
    with closing(db.connect()) as conn, conn:
        conn.execute("UPDATE deployments SET state=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                     (state, error, deployment["id"]))
    if result.returncode != 0:
        for edge in onboarding_edges:
            db.set_edge_state(
                edge["id"], "failed", operator="deployment-worker",
                reason="onboarding recusado por preflight, ativação ou auditoria",
                payload={"deployment_id": deployment["id"], "release_id": deployment["release_id"]},
            )
        raise RuntimeError(error or "deploy falhou")
    for edge in db.edges():
        selected = managed_edge_ids is None or edge["id"] in managed_edge_ids
        if edge["state"] == "bootstrapping" and managed_edge_ids is None:
            continue
        if selected and edge["state"] in ("bootstrapping", "ready", "draining"):
            target_state = "ready" if edge["state"] == "bootstrapping" else edge["state"]
            db.set_edge_state(
                edge["id"], target_state, deployment["release_id"],
                config_digest=deployment["config_digest"], operator="deployment-worker",
                reason="release verificada, ativada e auditada",
                payload={"deployment_id": deployment["id"], "release_id": deployment["release_id"]},
            )
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
