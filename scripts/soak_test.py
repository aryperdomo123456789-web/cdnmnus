#!/usr/bin/env python3
"""Soak autorizado; lê URL de arquivo root-only e emite somente contadores."""
from __future__ import annotations
import argparse, json, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urljoin

def fetch(url: str, limit: int = 2_000_000) -> tuple[int, bytes, float]:
    start = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "cdnmnus-soak/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read(limit), time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return exc.code, b"", time.monotonic() - start

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-file", required=True)
    parser.add_argument("--duration", type=int, default=21600)
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--result", default="/var/lib/cdnmnus/soak-result.json")
    args = parser.parse_args()
    source = Path(args.url_file)
    if source.stat().st_mode & 0o077:
        raise SystemExit("url-file deve possuir modo 0600")
    url = source.read_text(encoding="utf-8").strip()
    if not url.lower().startswith("https://"):
        raise SystemExit("soak exige URL HTTPS da CDN")
    deadline = time.monotonic() + args.duration
    stats = {"requests": 0, "success": 0, "errors": 0, "max_latency_seconds": 0.0}
    while time.monotonic() < deadline:
        status, body, latency = fetch(url)
        stats["requests"] += 1
        stats["max_latency_seconds"] = max(stats["max_latency_seconds"], latency)
        if status in (200, 206): stats["success"] += 1
        else: stats["errors"] += 1
        if b"#EXTM3U" in body:
            candidates = [line.strip() for line in body.decode("utf-8", "replace").splitlines() if line and not line.startswith("#")]
            if candidates:
                seg_status, _, seg_latency = fetch(urljoin(url, candidates[-1]))
                stats["requests"] += 1
                stats["max_latency_seconds"] = max(stats["max_latency_seconds"], seg_latency)
                if seg_status in (200, 206): stats["success"] += 1
                else: stats["errors"] += 1
        time.sleep(args.interval)
    stats["duration_seconds"] = args.duration
    stats["success_ratio"] = stats["success"] / max(1, stats["requests"])
    target = Path(args.result); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__": main()
