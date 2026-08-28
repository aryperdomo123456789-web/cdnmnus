#!/usr/bin/env python3
"""Terminal administrativo multi-tenant/multi-edge."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database, normalize_id
from core.deploy import deploy_serial
from core.edge_manager import bootstrap_edge, scan_host_identity
from core.render_tenants import render_tenant


def ask(value: str | None, label: str, default: str | None = None) -> str:
    if value:
        return value
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or (default or "")


def print_table(headers: list[str], rows: list[list[object]]) -> None:
    values = [[str(cell if cell is not None else "-") for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        widths = [max(width, len(row[index])) for index, width in enumerate(widths)]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in values:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def edge_add(args: argparse.Namespace, db: Database) -> None:
    name = ask(args.name, "Nome da edge")
    edge_id = normalize_id(args.id or name.lower().replace(" ", "-"), "edge_id")
    ipv4 = ask(args.ipv4, "IPv4 público")
    port = int(ask(str(args.port) if args.port else None, "Porta SSH", "22"))
    user = ask(args.user, "Usuário inicial", "root")
    identity = scan_host_identity(ipv4, port)
    print(f"Fingerprint apresentada pela edge ({identity.key_type}): {identity.sha256}")
    confirmation = input("Confirme digitando o fingerprint completo: ").strip()
    if confirmation != identity.sha256:
        raise PermissionError("fingerprint não confirmado")
    password = getpass.getpass("Senha inicial (não será armazenada): ")
    try:
        result = bootstrap_edge(ipv4, port, user, password, confirmation, edge_id)
    finally:
        password = ""
        del password
    db.add_edge(edge_id, name, ipv4, port, result["ssh_user"], result["fingerprint"], "bootstrapping")
    print(f"Edge {edge_id} em bootstrap; autenticação recorrente por chave Ed25519 validada.")


def edge_list(_: argparse.Namespace, db: Database) -> None:
    print_table(["ID", "Nome", "IPv4", "SSH", "Status", "Versão"],
                [[x["id"], x["name"], x["ipv4"], x["ssh_port"], x["state"], x["deployed_version"]] for x in db.edges()])


def tenant_add(args: argparse.Namespace, db: Database) -> None:
    tenant_id = ask(args.id, "ID do tenant")
    name = ask(args.name, "Nome")
    canonical = ask(args.canonical_host, "Host canônico")
    origin = ask(args.origin_host, "Host/IP da origem")
    port = int(ask(str(args.origin_port) if args.origin_port else None, "Porta da origem", "80"))
    raw_lbs = args.lb or input("Load balancers separados por vírgula (opcional): ").strip()
    lbs = [item.strip() for item in raw_lbs.split(",") if item.strip()]
    tenant = db.add_tenant(tenant_id, name, canonical, origin, port, lbs)
    print(f"Tenant {tenant['id']} criado com versão {tenant['config_version']}.")


def tenant_cname(args: argparse.Namespace, db: Database) -> None:
    result = db.add_cname(ask(args.tenant, "ID do tenant"), ask(args.hostname, "CNAME/alias"))
    print(f"Alias {result['hostname']} associado a {result['tenant_id']}; TLS pendente.")


def tenant_list(_: argparse.Namespace, db: Database) -> None:
    tenants = db.tenants()
    print_table(["ID", "Nome", "Canônico", "Versão", "Ativo"],
                [[x["id"], x["name"], x["canonical_host"], x["config_version"], "sim" if x["enabled"] else "não"] for x in tenants])


def tenant_show(args: argparse.Namespace, db: Database) -> None:
    print(render_tenant(db.tenant(args.tenant)).content)


def dns_sync(args: argparse.Namespace, db: Database) -> None:
    matrix = db.sync_dns_matrix()
    for item in matrix:
        print(f"{item['hostname']} -> [{', '.join(item['targets']) or 'sem edge ready'}]")
    script = args.script or os.environ.get("CDNMNUS_DNS_SYNC_SCRIPT", "")
    if script:
        path = Path(script).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("script DNS configurado não existe ou não é executável")
        subprocess.run([str(path)], input=json.dumps(matrix), text=True, check=True, timeout=60)


def deploy(args: argparse.Namespace, db: Database) -> None:
    result = deploy_serial(db, args.inventory, args.playbook, args.release_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def config_port(args: argparse.Namespace, db: Database) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("porta deve estar entre 1 e 65535")
    db.set_setting("web_port", args.port)
    print(f"Porta salva: {args.port}. Reinicie run_admin.sh para aplicar.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="admin", description="Administração local cdnmnus")
    root.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/etc/cdnmnus/admin.db"))
    commands = root.add_subparsers(dest="area", required=True)
    edge = commands.add_parser("edge").add_subparsers(dest="command", required=True)
    add = edge.add_parser("add")
    add.add_argument("--id"); add.add_argument("--name"); add.add_argument("--ipv4"); add.add_argument("--port", type=int); add.add_argument("--user")
    add.set_defaults(handler=edge_add)
    edge.add_parser("list").set_defaults(handler=edge_list)
    tenant = commands.add_parser("tenant").add_subparsers(dest="command", required=True)
    add_t = tenant.add_parser("add")
    add_t.add_argument("--id"); add_t.add_argument("--name"); add_t.add_argument("--canonical-host"); add_t.add_argument("--origin-host"); add_t.add_argument("--origin-port", type=int); add_t.add_argument("--lb")
    add_t.set_defaults(handler=tenant_add)
    cname = tenant.add_parser("add-cname"); cname.add_argument("tenant", nargs="?"); cname.add_argument("hostname", nargs="?"); cname.set_defaults(handler=tenant_cname)
    tenant.add_parser("list").set_defaults(handler=tenant_list)
    show = tenant.add_parser("show-vhost"); show.add_argument("tenant"); show.set_defaults(handler=tenant_show)
    dns = commands.add_parser("dns").add_subparsers(dest="command", required=True)
    sync = dns.add_parser("sync"); sync.add_argument("--script"); sync.set_defaults(handler=dns_sync)
    dep = commands.add_parser("deploy"); dep.add_argument("--inventory", default=None, help="opcional; por padrão é gerado do SQLite"); dep.add_argument("--playbook", default="ansible/playbooks/deploy-edge.yml"); dep.add_argument("--release-root", default="/var/lib/cdnmnus-admin/releases"); dep.set_defaults(handler=deploy)
    config = commands.add_parser("config").add_subparsers(dest="command", required=True)
    port = config.add_parser("web-port"); port.add_argument("port", type=int); port.set_defaults(handler=config_port)
    return root


def main() -> int:
    args = parser().parse_args()
    db = Database(args.db)
    db.initialize()
    try:
        args.handler(args, db)
        return 0
    except (ValueError, RuntimeError, PermissionError, FileExistsError, subprocess.SubprocessError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
