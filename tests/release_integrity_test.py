#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from core.db import Database
from core.deploy import _inventory, build_release


VERIFY = Path(__file__).parents[1] / "ansible/files/verify_release.py"
TENANT_TASKS = Path(__file__).parents[1] / "ansible/roles/cdn_tenants/tasks/main.yml"


def verify(path: Path, release: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VERIFY), str(path), str(release["release_id"]), str(release["config_digest"])],
        text=True,
        capture_output=True,
        check=False,
    )


with tempfile.TemporaryDirectory() as temp_name:
    root = Path(temp_name)
    db = Database(root / "admin.db")
    db.initialize()
    db.add_tenant("xui1", "XUI", "xui.cdn.test", "origin.test")
    first = build_release(db, root / "releases")
    second = build_release(db, root / "releases")
    assert first["config_digest"] == second["config_digest"]
    release_path = Path(str(first["artifact_path"]))
    assert verify(release_path, first).returncode == 0
    assert (release_path / "runtime/multi_tenant_broker.py").is_file()
    assert "runtime/multi_tenant_broker.py" in first["files"]
    assert (release_path / "runtime/vod_relay.py").is_file()
    assert "runtime/vod_relay.py" in first["files"]
    assert (release_path / "runtime/cdnmnus-vod-relay@.service").is_file()
    for unit_name in (
        "cdnmnus-tenant-broker@.service",
        "cdnmnus-vod-relay@.service",
    ):
        unit = (release_path / "runtime" / unit_name).read_text(encoding="utf-8")
        assert "/opt/cdnmnus/current/runtime/" in unit
        assert "/opt/cdnmnus/runtime/" not in unit

    tenant_file = release_path / "nginx/tenants/xui1.conf"
    original = tenant_file.read_text(encoding="utf-8")
    tenant_file.write_text(original + "# drift\n", encoding="utf-8")
    assert verify(release_path, first).returncode != 0
    tenant_file.write_text(original, encoding="utf-8")

    (release_path / "unexpected.conf").write_text("extra", encoding="utf-8")
    assert verify(release_path, first).returncode != 0
    (release_path / "unexpected.conf").unlink()

    manifest_path = release_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../escape"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify(release_path, first).returncode != 0

    # O inventário de rollout jamais inclui uma máquina ainda em bootstrap.
    with db.connect() as conn, conn:
        for edge_id, state, address in (
            ("pending1", "pending", "192.0.2.10"),
            ("bootstrap1", "bootstrapping", "192.0.2.11"),
            ("ready1", "ready", "192.0.2.12"),
            ("draining1", "draining", "192.0.2.13"),
            ("failed1", "failed", "192.0.2.14"),
        ):
            conn.execute(
                """INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,
                   host_key_sha256,state) VALUES(?,?,?,?,?,?,?)""",
                (edge_id, edge_id, address, 22, "cdn-deploy", "fixture", state),
            )
    hosts = _inventory(db, root / "ssh")["all"]["children"]["cdn_edges"]["hosts"]
    assert set(hosts) == {"ready1", "draining1"}

    rollback_tasks = TENANT_TASKS.read_text(encoding="utf-8")
    assert "Preservar conteúdo das units anteriores para rollback" in rollback_tasks
    assert "Restaurar conteúdo das units que existiam" in rollback_tasks
    assert rollback_tasks.index("Restaurar release anterior atomicamente") < rollback_tasks.index(
        "Iniciar exatamente os brokers do snapshot anterior"
    )

print("release integrity checks: OK")
