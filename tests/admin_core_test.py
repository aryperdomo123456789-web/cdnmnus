#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from core.db import Database
from core.deploy import _inventory, build_release, claim_deployment, queue_deployment
from core.render_tenants import broker_snapshot, render_all, render_tenant


with tempfile.TemporaryDirectory() as root:
    db_path = Path(root) / "admin.db"
    db = Database(db_path)
    db.initialize()
    assert db.connect().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    xui1 = db.add_tenant("xui1", "XUI Um", "xui1.cdn.test", "origin1.test", 80, ["lb1.test"])
    db.add_cname("xui1", "cliente.test")
    xui2 = db.add_tenant("xui2", "XUI Dois", "xui2.cdn.test", "origin2.test", 8080, [])
    assert len(db.tenants()) == 2

    output = render_tenant(db.tenant("xui1"))
    assert output.relative_path == "tenants/xui1.conf"
    assert "keys_zone=cache_xui1:32m" in output.content
    assert "unix:/run/cdnmnus/broker-xui1.sock" in output.content
    assert "server_name xui1.cdn.test cliente.test" in output.content
    assert "location ^~ /__cdnmnus_xui1_origin/" in output.content and "internal;" in output.content
    assert "location ^~ /__cdnmnus_xui1_lb_0/" in output.content
    assert output.content == render_tenant(db.tenant("xui1")).content

    rendered = render_all(db.tenants())
    assert sorted(rendered) == ["tenants/xui1.conf", "tenants/xui2.conf"]
    snapshot = json.loads(broker_snapshot(db.tenants(), 2))
    assert snapshot["tenants"]["xui1"]["origin"]["host"] == "origin1.test"

    with db.connect() as conn:
        conn.execute("UPDATE edges SET state='disabled'")
    # Endereços TEST-NET não são aceitos por add_edge; insere fixture diretamente.
    with db.connect() as conn:
        conn.execute("INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,host_key_sha256,state) VALUES(?,?,?,?,?,?,?)",
                     ("edge-a", "Edge A", "203.0.113.10", 22, "cdn-deploy", "SHA256:test", "ready"))
    matrix = db.sync_dns_matrix()
    assert all(item["targets"] == ["203.0.113.10"] for item in matrix)

    release = build_release(db, Path(root) / "releases")
    assert len(release["config_digest"]) == 64
    assert Path(release["artifact_path"], "manifest.json").is_file()
    assert Path(release["artifact_path"], "nginx/tenants/xui1.conf").is_file()
    inventory = _inventory(db, Path(root) / "keys")
    assert inventory["all"]["children"]["cdn_edges"]["hosts"]["edge-a"]["ansible_user"] == "cdn-deploy"
    queued = queue_deployment(db, Path(root) / "releases")
    claimed = claim_deployment(db)
    assert queued["state"] == "queued" and claimed is not None and claimed["id"] == queued["deployment_id"]

    try:
        db.add_cname("xui2", "cliente.test")
        raise AssertionError("hostname duplicado aceito")
    except sqlite3.IntegrityError:
        pass

print("admin db/render/release checks: OK")
