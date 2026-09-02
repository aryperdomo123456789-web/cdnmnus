#!/usr/bin/env python3
"""Gera o manifesto fechado do pacote universal para uma tag/commit aprovados."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "node-package/install.sh",
    "node-package/verify.py",
    "ansible/roles/node_menu/files/node_menu.py",
    "ansible/roles/node_menu/files/mago-cdn",
    "ansible/files/verify_release.py",
    "scripts/cdnmnus-ansible-become",
    "panel/multi_tenant_broker.py",
    "panel/vod_relay.py",
    "panel/cdnmnus-tenant-broker@.service",
    "panel/cdnmnus-vod-relay@.service",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "node-package/manifest.json")
    args = parser.parse_args()
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*", args.ref):
        raise SystemExit("--ref precisa ser tag v... imutável")
    hashes = {}
    for relative in FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"arquivo obrigatório inválido: {relative}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "source_ref": args.ref, "files": hashes}
    args.output.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(hashlib.sha256(args.output.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
