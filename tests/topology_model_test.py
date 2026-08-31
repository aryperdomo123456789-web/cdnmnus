#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.db import Database
from core.topology import LockUnavailable, TopologyConflict, TopologyStore


def expect(error: type[BaseException], callback) -> BaseException:
    try:
        callback()
    except error as caught:
        return caught
    raise AssertionError(f"{error.__name__} não foi gerado")


with tempfile.TemporaryDirectory(prefix="cdnmnus-topology-") as root:
    path = Path(root) / "admin.db"
    database = Database(path)
    database.initialize()
    # Fixture legada: o upgrade deve copiar, não alterar, este registro.
    with database.connect() as db, db:
        db.execute(
            """INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,host_key_sha256,state,deployed_version)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("lb011", "Edge 168", "143.14.168.168", 22, "cdn-deploy", "SHA256:test", "bootstrapping", "legacy-r1"),
        )

    topology = TopologyStore(database)
    topology.initialize()
    topology.initialize()  # upgrade idempotente
    imported = topology.node("lb011")
    assert imported["role"] == "edge" and imported["state"] == "bootstrapping"
    assert imported["release_id"] == "legacy-r1"
    assert database.edge("lb011")["state"] == "bootstrapping"

    # Novas edges entram nos modelos legado e topológico na mesma transação.
    database.add_edge(
        "fresh", "Edge futura", "8.8.4.4", 22, "cdn-deploy",
        "SHA256:futura", "bootstrapping",
    )
    assert topology.node("fresh")["state"] == "bootstrapping"
    future_digest = "a" * 64
    database.set_edge_state(
        "fresh", "ready", "release-futura", config_digest=future_digest,
        operator="onboarding-test", reason="release e health auditados",
    )
    assert database.edge("fresh")["state"] == "ready"
    assert topology.node("fresh")["state"] == "ready"
    assert topology.node("fresh")["release_id"] == "release-futura"
    assert topology.node("fresh")["node_config_digest"] == future_digest
    assert any(
        event["event_type"] == "state_changed" for event in topology.events("fresh")
    )

    # Transição real bootstrapping -> ready e auditoria no mesmo commit.
    ready = topology.transition_node("lb011", "ready", "operator@example", "preflight aprovado")
    assert ready["state"] == "ready"
    assert topology.events("lb011")[-1]["payload"]["from"] == "bootstrapping"
    expect(TopologyConflict, lambda: topology.transition_node(
        "lb011", "pending", "operator@example", "salto inválido"
    ))

    topology.add_node("edge170", "Edge 170", "143.14.168.170", "edge", "ready",
                      "operator@example", "fixture de laboratório", {"connections": 1000})
    topology.add_node("lb66", "LB 66", "143.14.168.66", "load_balancer", "candidate",
                      "operator@example", "fixture de laboratório")
    topology.add_node("lb111", "LB 111", "143.14.168.111", "load_balancer", "candidate",
                      "operator@example", "fixture de laboratório")
    topology.add_node("control1", "Control", "10.0.0.10", "control_plane", "ready",
                      "operator@example", "fixture de laboratório")
    expect(TopologyConflict, lambda: topology.add_node(
        "lbactive", "LB sem eleição", "198.51.100.4", "load_balancer", "active",
        "operator@example", "não permitido"
    ))

    # ID, nome e IPv4 são independemente únicos.
    expect(sqlite3.IntegrityError, lambda: topology.add_node(
        "edge170", "Outro", "198.51.100.2", "edge", "pending", "op", "duplicidade"
    ))
    expect(sqlite3.IntegrityError, lambda: topology.add_node(
        "edge2", "Edge 170", "198.51.100.3", "edge", "pending", "op", "duplicidade"
    ))
    expect(sqlite3.IntegrityError, lambda: topology.add_node(
        "edge3", "Outro 3", "143.14.168.170", "edge", "pending", "op", "duplicidade"
    ))

    topology.add_load_balancer("front66", "lb66", "cdn.example.test",
                               "operator@example", "criar candidato")
    topology.add_load_balancer("front111", "lb111", None,
                               "operator@example", "criar standby")
    topology.transition_node("control1", "disabled", "operator@example", "teste de estado")
    expect(TopologyConflict, lambda: topology.add_load_balancer(
        "invalid-control", "control1", None, "operator@example", "papel inválido"
    ))
    topology.add_backend("front66", "lb011", "operator@example", "backend canário")
    topology.add_backend("front66", "edge170", "operator@example", "backend secundário", 80)

    # Trigger mantém backend restrito a EDGE até contra SQL direto.
    expect(sqlite3.IntegrityError, lambda: topology.add_backend(
        "front66", "control1", "operator@example", "não permitido"
    ))
    expect(TopologyConflict, lambda: topology.change_role(
        "edge170", "load_balancer", "candidate", "operator@example", "ainda é backend"
    ))

    # Promoção sem lease, com token falso e com lease expirada falha fechado.
    expect(TopologyConflict, lambda: topology.promote_load_balancer(
        "front66", "public", "fake", 1, "operator@example", "sem lease"
    ))
    epoch = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    lease66 = topology.acquire_promotion_lock(
        "public", "lb66", "operator@example", "eleição laboratório", 5, now=epoch
    )
    expect(TopologyConflict, lambda: topology.promote_load_balancer(
        "front66", "public", lease66["lease_id"], 999, "operator@example", "token falso",
        now=epoch + timedelta(seconds=1)
    ))
    expect(TopologyConflict, lambda: topology.promote_load_balancer(
        "front66", "public", lease66["lease_id"], lease66["fencing_token"],
        "operator@example", "lease expirada", now=epoch + timedelta(seconds=6)
    ))
    promoted = topology.promote_load_balancer(
        "front66", "public", lease66["lease_id"], lease66["fencing_token"],
        "operator@example", "fencing externo simulado", now=epoch + timedelta(seconds=1)
    )
    assert promoted["state"] == "active" and topology.node("lb66")["state"] == "active"

    # Segundo ACTIVE é recusado mesmo com novo lock/fencing válido.
    lease111 = topology.acquire_promotion_lock(
        "public", "lb111", "operator@example", "takeover laboratório", 5,
        now=epoch + timedelta(seconds=6)
    )
    assert lease111["fencing_token"] == lease66["fencing_token"] + 1
    expect(TopologyConflict, lambda: topology.promote_load_balancer(
        "front111", "public", lease111["lease_id"], lease111["fencing_token"],
        "operator@example", "split brain recusado", now=epoch + timedelta(seconds=7)
    ))
    topology.demote_load_balancer("front66", "standby", "operator@example", "fencing confirmado")
    assert topology.node("lb66")["lease_id"] is None
    assert topology.events("lb66")[-1]["event_type"] == "load_balancer_demoted"
    expect(TopologyConflict, lambda: topology.demote_load_balancer(
        "front66", "standby", "operator@example", "rollback duplicado"
    ))
    assert topology.promote_load_balancer(
        "front111", "public", lease111["lease_id"], lease111["fencing_token"],
        "operator@example", "takeover após demote", now=epoch + timedelta(seconds=7)
    )["state"] == "active"

    # Token não pode diminuir, inclusive por SQL direto.
    with database.connect() as db:
        expect(sqlite3.IntegrityError, lambda: db.execute(
            "UPDATE promotion_locks SET fencing_token=fencing_token-1 WHERE service_id='public'"
        ))
        expect(sqlite3.IntegrityError, lambda: db.execute(
            "UPDATE promotion_locks SET service_id='renamed' WHERE service_id='public'"
        ))
        expect(sqlite3.IntegrityError, lambda: db.execute(
            "DELETE FROM promotion_locks WHERE service_id='public'"
        ))

    # Duas aquisições concorrentes para o mesmo serviço: exatamente uma vence.
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def contender(holder: str) -> None:
        barrier.wait()
        try:
            topology.acquire_promotion_lock("concurrent", holder, "race-test", "concorrência", 30)
            result = "won"
        except LockUnavailable:
            result = "lost"
        with outcomes_lock:
            outcomes.append(result)

    threads = [threading.Thread(target=contender, args=(holder,)) for holder in ("lb66", "lb111")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["lost", "won"], outcomes

    # Auditoria rejeita campos secretos e faz rollback da criação inteira.
    expect(ValueError, lambda: topology.add_node(
        "badnode", "Bad", "198.51.100.99", "edge", "pending",
        "operator@example", "teste", {"password": "não registrar"}
    ))
    assert database.rows("SELECT * FROM nodes WHERE id='badnode'") == []

    # Mudança de papel e rollback são auditados; nenhuma configuração órfã sobra.
    topology.change_role("lb66", "edge", "pending", "operator@example",
                         "reaproveitamento controlado", remove_lb_configuration=True)
    assert topology.node("lb66")["role"] == "edge"
    topology.change_role("lb66", "load_balancer", "candidate", "operator@example",
                         "rollback do papel")
    role_events = [event for event in topology.events("lb66") if event["event_type"] == "role_changed"]
    assert [(event["payload"]["from_role"], event["payload"]["to_role"])
            for event in role_events[-2:]] == [("load_balancer", "edge"), ("edge", "load_balancer")]

    # Downgrade remove apenas o modelo novo; edges e demais tabelas sobrevivem.
    topology.downgrade()
    assert database.edge("lb011")["state"] == "bootstrapping"
    tables = {row["name"] for row in database.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "nodes" not in tables and "edges" in tables and "xui_tenants" in tables
    topology.initialize()
    assert topology.node("lb011")["role"] == "edge"
    automatic = topology.add_node_auto("Edge automática", "198.51.100.44", "edge", "pending",
                                       "operator@example", "cadastro sequencial")
    assert automatic["id"].isdigit()

print("topology nodes/roles/lb/lease/fencing checks: OK")
