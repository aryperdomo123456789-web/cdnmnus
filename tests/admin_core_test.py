#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from unittest.mock import patch
from pathlib import Path

from core.db import Database
from core.deploy import (_inventory, build_release, claim_deployment, queue_deployment,
                         run_deployment, tenant_deployment_contexts)
from core.render_tenants import broker_snapshot, render_all, render_tenant
from core.topology import TopologyStore


with tempfile.TemporaryDirectory() as root:
    db_path = Path(root) / "admin.db"
    db = Database(db_path)
    db.initialize()
    assert db.connect().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    xui1 = db.add_tenant("xui1", "XUI Um", "xui1.cdn.test", "origin1.test", 80, ["lb1.test"])
    db.add_cname("xui1", "cliente.test")
    xui2 = db.add_tenant("xui2", "XUI Dois", "xui2.cdn.test", "origin2.test", 8080, [])
    xui3 = db.add_tenant("xui3", "XUI IP", "xui3.cdn.test", "173.208.244.139", 80, [])
    assert xui3["upstreams"][0]["host"] == "173.208.244.139"
    assert len(db.tenants()) == 3
    contexts = tenant_deployment_contexts(db.tenants(enabled_only=True))
    assert [item["tenant_id"] for item in contexts] == ["xui1", "xui2", "xui3"]
    assert contexts[0]["origin"]["host"] == "origin1.test"
    assert contexts[1]["origin"]["host"] == "origin2.test"
    assert contexts[2]["origin"]["host"] == "173.208.244.139"
    assert len({item["certificate_dir"] for item in contexts}) == 3

    output = render_tenant(db.tenant("xui1"))
    assert output.relative_path == "tenants/xui1.conf"
    assert "keys_zone=cache_xui1:32m" in output.content
    assert "unix:/run/cdnmnus/broker-xui1.sock" in output.content
    assert "server_name xui1.cdn.test cliente.test" in output.content
    assert "location ^~ /__cdnmnus_xui1_origin/" in output.content and "internal;" in output.content
    assert "location ^~ /__cdnmnus_xui1_lb_0/" in output.content
    assert "location = /get.php" in output.content
    assert "location = /player_api.php" in output.content
    assert "location ~* ^/(?:admin|administrator|phpmyadmin|pma|mysql|database|internal)" in output.content
    assert "more_clear_headers" not in output.content
    assert "sub_filter 'origin1.test' 'xui1.cdn.test';" in output.content
    assert "sub_filter 'http://origin1.test:80' 'http://xui1.cdn.test';" in output.content
    assert output.content == render_tenant(db.tenant("xui1")).content

    no_vod_output = render_tenant(db.tenant("xui2")).content
    assert "upstream vod_relay_xui2" not in no_vod_output
    assert "location ~ ^/(?:movie|series)/" in no_vod_output and "return 503;" in no_vod_output

    rendered = render_all(db.tenants())
    assert sorted(rendered) == ["tenants/xui1.conf", "tenants/xui2.conf", "tenants/xui3.conf"]
    snapshot = json.loads(broker_snapshot(db.tenants(), 2))
    assert snapshot["tenants"]["xui1"]["origin"]["host"] == "origin1.test"

    with db.connect() as conn:
        conn.execute("UPDATE edges SET state='disabled'")
    # Endereços TEST-NET não são aceitos por add_edge; insere fixture diretamente.
    with db.connect() as conn:
        conn.execute("INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,host_key_sha256,state) VALUES(?,?,?,?,?,?,?)",
                     ("edge-a", "Edge A", "203.0.113.10", 22, "cdn-deploy", "SHA256:test", "bootstrapping"))
    db.set_edge_state("edge-a", "ready", operator="test-operator", reason="preflight aprovado",
                      payload={"health": 200, "probe_url": "https://cdn.test/edge-health?token=secret",
                               "api_token": "must-not-be-stored"})
    event = db.edge_events("edge-a")[-1]
    assert event["from_state"] == "bootstrapping" and event["to_state"] == "ready"
    assert "?" not in event["payload_sanitized"] and "must-not-be-stored" not in event["payload_sanitized"]
    try:
        db.set_edge_state("edge-a", "bootstrapping", operator="test", reason="regressão inválida")
        raise AssertionError("transição ready -> bootstrapping aceita")
    except ValueError:
        pass
    assert len(db.edge_events("edge-a")) == 1
    renamed = db.rename_edge("edge-a", "Edge São Paulo", operator="test-operator")
    assert renamed["id"] == "edge-a" and renamed["name"] == "Edge São Paulo"
    rename_event = next(event for event in db.edge_events("edge-a")
                        if event["event_type"] == "edge_renamed")
    assert rename_event["event_type"] == "edge_renamed"
    assert '"old_name": "Edge A"' in rename_event["payload_sanitized"]
    assert '"new_name": "Edge São Paulo"' in rename_event["payload_sanitized"]
    db.rename_edge("edge-a", "Edge São Paulo")  # idempotente: não duplica evento
    assert len(db.edge_events("edge-a")) == 2
    reassigned = db.reassign_edge_id("edge-a", "2", operator="migration-test",
                                     reason="padronização numérica")
    assert reassigned["id"] == "2" and db.next_node_id() == "3"
    assert any(event["event_type"] == "edge_id_reassigned" for event in db.edge_events("2"))
    automatic_id = db.reserve_node_id()
    assert automatic_id == "3" and db.next_node_id() == "4"
    matrix = db.sync_dns_matrix()
    assert all(item["targets"] == ["203.0.113.10"] for item in matrix)

    release = build_release(db, Path(root) / "releases")
    assert len(release["config_digest"]) == 64
    assert Path(release["artifact_path"], "manifest.json").is_file()
    assert Path(release["artifact_path"], "nginx/tenants/xui1.conf").is_file()
    inventory = _inventory(db, Path(root) / "keys")
    assert inventory["all"]["vars"]["cdnmnus_firewall_admin_sources"] == ["143.14.168.111"]
    assert inventory["all"]["children"]["cdn_edges"]["hosts"]["2"]["ansible_user"] == "cdn-deploy"
    queued = queue_deployment(db, Path(root) / "releases")
    claimed = claim_deployment(db)
    assert queued["state"] == "queued" and claimed is not None and claimed["id"] == queued["deployment_id"]
    # O fallback externo é uma escolha operacional explícita quando há vários
    # tenants; nunca deve ser inferido pelo índice da lista.
    db.set_setting("external_alias_tenant_id", "xui1")

    # Onboarding gerenciado é o único deployment que admite bootstrapping.
    topology = TopologyStore(db)
    topology.initialize()
    db.add_edge(
        "future-ok", "Edge futura OK", "1.1.1.1", 22, "cdn-deploy",
        "SHA256:future-ok", "bootstrapping",
    )
    normal_hosts = _inventory(db, Path(root) / "keys")["all"]["children"]["cdn_edges"]["hosts"]
    assert "future-ok" not in normal_hosts
    onboarding_hosts = _inventory(
        db, Path(root) / "keys", include_bootstrapping=True, edge_ids={"future-ok"},
    )["all"]["children"]["cdn_edges"]["hosts"]
    assert set(onboarding_hosts) == {"future-ok"}
    onboarding = queue_deployment(db, Path(root) / "releases", target_edge_id="future-ok")
    onboarding_claim = claim_deployment(db)
    assert onboarding_claim is not None and onboarding_claim["id"] == onboarding["deployment_id"]
    with patch("core.deploy.shutil.which", return_value="/usr/bin/ansible-playbook"), patch(
        "core.deploy.subprocess.run",
        return_value=subprocess.CompletedProcess(["ansible-playbook"], 0, "ok", ""),
    ):
        run_deployment(db, onboarding_claim, key_dir=Path(root) / "ssh")
    assert db.edge("future-ok")["state"] == "ready"
    assert topology.node("future-ok")["state"] == "ready"
    assert topology.node("future-ok")["release_id"] == onboarding["release_id"]
    assert topology.node("future-ok")["node_config_digest"] == onboarding["config_digest"]

    db.add_edge(
        "future-fail", "Edge futura falha", "8.8.8.8", 22, "cdn-deploy",
        "SHA256:future-fail", "bootstrapping",
    )
    rejected = queue_deployment(db, Path(root) / "releases", target_edge_id="future-fail")
    rejected_claim = claim_deployment(db)
    assert rejected_claim is not None and rejected_claim["id"] == rejected["deployment_id"]
    try:
        with patch("core.deploy.shutil.which", return_value="/usr/bin/ansible-playbook"), patch(
            "core.deploy.subprocess.run",
            return_value=subprocess.CompletedProcess(["ansible-playbook"], 2, "", "gate recusado"),
        ):
            run_deployment(db, rejected_claim, key_dir=Path(root) / "ssh")
        raise AssertionError("onboarding falho foi aceito")
    except RuntimeError:
        pass
    assert db.edge("future-fail")["state"] == "failed"
    assert topology.node("future-fail")["state"] == "failed"

    try:
        db.add_cname("xui2", "cliente.test")
        raise AssertionError("hostname duplicado aceito")
    except ValueError:
        pass

print("admin db/render/release checks: OK")
