#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

_env = {key: os.environ.get(key) for key in (
    "CDNMNUS_TENANT_ID", "CDNMNUS_TENANTS_CONFIG", "CDNMNUS_BROKER_SOCKET", "CDNMNUS_PLAYBACK_STORE",
    "CDNMNUS_PLAYLIST_TOKEN_STORE",
)}

try:
    with tempfile.TemporaryDirectory() as root:
        snapshot = Path(root) / "tenants.json"
        snapshot.write_text(json.dumps({
            "schema_version": 1, "generation": 1,
            "tenants": {"xui1": {
                "public_hosts": ["xui1.test", "alias.test"],
                "origin": {"host": "origin.test", "port": 80},
                "load_balancers": [{"host": "lb.test", "port": 80}],
                "vod_hosts": [{"host": "vod.test", "port": 80}], "ttl_seconds": 15,
                "playback_sessions_v1": 1,
            }},
        }))
        os.environ["CDNMNUS_TENANT_ID"] = "xui1"
        os.environ["CDNMNUS_TENANTS_CONFIG"] = str(snapshot)
        os.environ["CDNMNUS_BROKER_SOCKET"] = str(Path(root) / "broker.sock")
        os.environ["CDNMNUS_PLAYBACK_STORE"] = str(Path(root) / "playback.db")
        os.environ["CDNMNUS_PLAYLIST_TOKEN_STORE"] = str(Path(root) / "playlist-tokens.db")
        spec = importlib.util.spec_from_file_location("multi_tenant_broker", "panel/multi_tenant_broker.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        config = module.STATE.load()
        assert config["origin_host"] == "origin.test" and config["load_balancers"] == ["lb.test"]
        assert str(module.STATE.playlist_tokens.path) == str(Path(root) / "playlist-tokens.db")
        module.STATE.validate_request("xui1", "alias.test")
        for tenant, host in (("xui2", "alias.test"), ("xui1", "other.test")):
            try:
                module.STATE.validate_request(tenant, host)
                raise AssertionError("tenant/host divergente aceito")
            except PermissionError:
                pass
        module.legacy.query_origin = lambda uri, cfg: "/__cdnmnus_resolved_lb_0/token.ts"
        assert module.STATE.resolve("/hls/test.ts", False, False) == "/__cdnmnus_xui1_lb_0/token.ts"
        module.legacy.query_origin = lambda uri, cfg: "/__cdnmnus_resolved_origin/segment.ts"
        module.STATE.state.cache.clear()
        assert module.STATE.resolve("/hls/other.ts", False, False) == "/__cdnmnus_xui1_origin/segment.ts"
        playback = module.STATE.create_playback_session({"channel_id": "canal-1", "media_type": "live"})
        assert playback["tenant_id"] == "xui1" and playback["edge_id"] == "lb.test"
finally:
    for key, value in _env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print("multi-tenant broker snapshot/isolation/routes: OK")
