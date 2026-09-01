#!/usr/bin/env python3
"""Audita release aprovada versus pacote e manifesto ativos de um nó.

Esta ferramenta é somente leitura. Ela reutiliza a validação de release
aprovada de :mod:`core.node_onboarding` e o transporte SSH estrito do
``lb_candidate_preflight``. Nunca altera o registry, instala pacote, troca
symlink ou reinicia serviço. O código 2 significa divergência ou evidência
insuficiente; a decisão de aprovar uma release permanece administrativa.

Schema JSON: ``schema``, ``node``, ``approved``, ``installed``,
``active_path``, ``active_manifest``, ``matches`` e ``errors``.
Rollback: não aplicável; não há mutação. Em caso de divergência, manter o nó
fora do pool e escolher formalmente entre aprovar a release auditada ou
executar o rollback determinístico pelo runbook de releases.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CDNMNUS_PROJECT_ROOT", "/opt/cdnmnus")).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.node_onboarding import load_approved_release  # noqa: E402
from core.topology import TopologyStore  # noqa: E402
from lb_candidate_preflight import check_release  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Executa a reconciliação sem alterar banco, nó remoto ou registry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="IPv4 do nó gerenciado")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args(argv)
    db = Database(args.db)
    rows = db.rows("SELECT * FROM nodes WHERE ipv4=?", (args.node,))
    if not rows:
        print(json.dumps({"schema": "cdnmnus.managed_release_reconciliation.v1", "node": args.node,
                          "matches": False, "errors": ["node_not_registered"]}, ensure_ascii=False))
        return 2
    node = dict(rows[0])
    approved = load_approved_release()
    observed = check_release(node, approved, args.timeout)
    report = {"schema": "cdnmnus.managed_release_reconciliation.v1", "node": args.node,
              "node_id": node["id"], **observed, "errors": []}
    if not observed.get("matches"):
        report["errors"].append("installed release does not match approved release")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["matches"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
