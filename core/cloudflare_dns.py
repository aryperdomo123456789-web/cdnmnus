"""Cliente mínimo e fail-closed para Cloudflare DNS API."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class CloudflareError(RuntimeError):
    pass


class CloudflareDNS:
    def __init__(self, token: str | None = None, zone: str | None = None, token_file: str | Path | None = None) -> None:
        configured = zone or os.environ.get("CDNMNUS_CLOUDFLARE_ZONES", os.environ.get("CDNMNUS_CLOUDFLARE_ZONE", ""))
        if not configured:
            zones_file = Path(os.environ.get("CDNMNUS_CLOUDFLARE_ZONES_FILE", "/etc/cdnmnus/cloudflare/zones"))
            if zones_file.is_file():
                configured = zones_file.read_text(encoding="utf-8").strip()
        self.zones = [item.strip().lower().rstrip(".") for item in configured.split(",") if item.strip()]
        path = Path(token_file or os.environ.get("CDNMNUS_CLOUDFLARE_TOKEN_FILE", "/etc/cdnmnus/cloudflare/api-token"))
        self.token = token or (path.read_text(encoding="utf-8").strip() if path.is_file() else "")
        if not self.token:
            raise CloudflareError("token Cloudflare ausente")
        if not self.zones:
            raise CloudflareError("zona Cloudflare ausente")
        try:
            if path.is_file() and path.stat().st_mode & 0o077:
                raise CloudflareError("arquivo do token Cloudflare deve ter modo 0600")
        except OSError:
            pass

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + path,
            data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CloudflareError("Cloudflare indisponível") from exc
        if not body.get("success"):
            raise CloudflareError("Cloudflare recusou a operação")
        return body.get("result")

    def verify(self) -> None:
        """Confirma que o token é válido antes de qualquer alteração DNS."""
        result = self._request("GET", "/user/tokens/verify")
        if not isinstance(result, dict) or result.get("status") != "active":
            raise CloudflareError("token Cloudflare não está ativo")

    def zone_for_name(self, name: str) -> str:
        hostname = name.rstrip(".").lower()
        matches = [zone for zone in self.zones if hostname == zone or hostname.endswith("." + zone)]
        if not matches:
            raise CloudflareError(f"hostname fora das zonas autorizadas: {hostname}")
        return max(matches, key=len)

    def zone_id(self, zone: str | None = None) -> str:
        selected = zone or self.zones[0]
        result = self._request("GET", "/zones?name=" + urllib.parse.quote(selected) + "&status=active")
        if not isinstance(result, list) or len(result) != 1:
            raise CloudflareError("zona Cloudflare não encontrada de forma única")
        return str(result[0]["id"])

    def records(self, name: str | None = None) -> list[dict[str, Any]]:
        query = "?per_page=1000"
        if name:
            query += "&name=" + urllib.parse.quote(name.rstrip("."))
        result = self._request("GET", f"/zones/{self.zone_id(self.zone_for_name(name) if name else None)}/dns_records{query}")
        return result if isinstance(result, list) else []

    def upsert(self, name: str, record_type: str, content: str, *, proxied: bool = False, ttl: int = 300) -> dict[str, Any]:
        if proxied or record_type not in {"A", "AAAA", "CNAME"}:
            raise CloudflareError("somente DNS-only A/AAAA/CNAME é permitido")
        name = name.rstrip(".").lower(); content = content.rstrip(".")
        existing = [x for x in self.records(name) if x.get("type") == record_type]
        payload = {"type": record_type, "name": name, "content": content, "ttl": int(ttl), "proxied": False}
        zone_id = self.zone_id(self.zone_for_name(name))
        # A/AAAA pools are intentionally multi-valued. CNAME remains unique.
        if record_type in {"A", "AAAA"}:
            same = [x for x in existing if x.get("content") == content and x.get("proxied") is False]
            if same:
                return same[0]
            return self._request("POST", f"/zones/{zone_id}/dns_records", payload)
        if existing:
            if len(existing) != 1:
                raise CloudflareError(f"registro {name} possui duplicidade")
            if existing[0].get("content") == content and existing[0].get("proxied") is False:
                return existing[0]
            return self._request("PUT", f"/zones/{zone_id}/dns_records/{existing[0]['id']}", payload)
        return self._request("POST", f"/zones/{zone_id}/dns_records", payload)

    def delete_records(self, name: str, *, record_types: set[str] | None = None) -> int:
        removed = 0
        target_name = name.rstrip(".").lower()
        zone_id = self.zone_id(self.zone_for_name(name))
        for record in self.records(name):
            # The API query is exact today; keep this guard so a provider or
            # proxy returning a broader result can never delete another host.
            if str(record.get("name", "")).rstrip(".").lower() != target_name:
                continue
            if record_types and record.get("type") not in record_types:
                continue
            self._request("DELETE", f"/zones/{zone_id}/dns_records/{record['id']}")
            removed += 1
        return removed
