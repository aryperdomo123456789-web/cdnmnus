#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/converge_ssh_mesh.py"
SPEC = importlib.util.spec_from_file_location("converge_ssh_mesh", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SshMeshTest(unittest.TestCase):
    def test_managed_block_preserves_operator_keys_and_is_idempotent(self) -> None:
        operator = "ssh-ed25519 AAAAoperator operador"
        old = f"{operator}\n{MODULE.START}\nssh-ed25519 AAAAold old\n{MODULE.END}\n"
        keys = ["ssh-ed25519 AAAA111 node-111", "ssh-ed25519 AAAA168 node-168"]
        first = MODULE.managed_content(old, keys)
        second = MODULE.managed_content(first, keys)
        self.assertEqual(first, second)
        self.assertIn(operator, first)
        self.assertNotIn("AAAAold", first)
        self.assertEqual(first.count(MODULE.START), 1)

    def test_managed_block_deduplicates_cluster_entries(self) -> None:
        key = "ssh-ed25519 AAAA111 node-111"
        result = MODULE.managed_content("", [key, key])
        self.assertEqual(result.count(key), 1)


if __name__ == "__main__":
    unittest.main()
