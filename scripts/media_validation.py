#!/usr/bin/env python3
"""Valida fluxo autorizado de playlist HLS sem imprimir credenciais ou URLs.

O arquivo de entrada deve ser root-only (0600). O relatório contém apenas
host, categoria, códigos HTTP e contadores; nunca persiste a URL assinada.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit


def request(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str], float]:
    started = time.monotonic()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "cdnmnus-media-validation/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read(4_000_000), dict(response.headers), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, b"", dict(exc.headers), time.monotonic() - started
    except Exception:
        return 0, b"", {}, time.monotonic() - started


def category_entries(body: bytes) -> dict[str, str]:
    lines = body.decode("utf-8", "replace").splitlines()
    found: dict[str, str] = {}
    current = ""
    for line in lines:
        if line.startswith("#EXTINF"):
            current = line
        elif line and not line.startswith("#") and current:
            match = re.search(r'group-title="([^"]+)"', current, re.I)
            if match:
                found.setdefault(match.group(1), line.strip())
            current = ""
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-file", required=True)
    parser.add_argument("--result", default="/var/lib/cdnmnus/media-validation.json")
    parser.add_argument("--category", action="append", dest="categories")
    parser.add_argument("--origin-marker", default="38.46.223.77")
    args = parser.parse_args()
    source = Path(args.url_file)
    if source.stat().st_mode & 0o077:
        raise SystemExit("url-file deve possuir modo 0600")
    playlist_url = source.read_text(encoding="utf-8").strip()
    parsed = urlsplit(playlist_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("a validação exige URL HTTPS autorizada")
    status, body, headers, latency = request(playlist_url)
    result: dict[str, object] = {
        "host": parsed.hostname,
        "playlist_status": status,
        "playlist_latency_seconds": round(latency, 6),
        "playlist_content_type": headers.get("Content-Type", ""),
        "origin_marker_in_body": args.origin_marker.encode() in body,
        "refresh": [],
        "categories": {},
    }
    if status not in (200, 206):
        Path(args.result).parent.mkdir(parents=True, exist_ok=True)
        Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(2)
    entries = category_entries(body)
    wanted = args.categories or ["UFC", "LGBT", "FILMES", "SERIES"]
    selected = {name: uri for name, uri in entries.items() if any(term.lower() in name.lower() for term in wanted)}
    for name, child in sorted(selected.items()):
        child_url = urljoin(playlist_url, child)
        child_status, child_body, child_headers, child_latency = request(child_url)
        range_status, _, _, _ = request(child_url, {"User-Agent": "cdnmnus-media-validation/1.0", "Range": "bytes=0-1"})
        result["categories"][name] = {
            "status": child_status,
            "content_type": child_headers.get("Content-Type", ""),
            "bytes_sampled": len(child_body),
            "latency_seconds": round(child_latency, 6),
            "range_status": range_status,
        }
    for _ in range(3):
        refresh_status, refresh_body, refresh_headers, _ = request(playlist_url)
        result["refresh"].append({
            "status": refresh_status,
            "location_present": bool(refresh_headers.get("Location")),
            "origin_marker_in_body": args.origin_marker.encode() in refresh_body,
        })
    target = Path(args.result)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if any(item["status"] not in (200, 206) for item in result["categories"].values()):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
