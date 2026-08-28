#!/usr/bin/env python3
"""Coleta métricas locais sem registrar URI, usuário, senha ou token."""
from __future__ import annotations
import os, socket, ssl, time, urllib.request
from pathlib import Path

OUTPUT = Path(os.environ.get("CDNMNUS_METRICS_FILE", "/var/lib/cdnmnus/metrics.prom"))
PUBLIC_HOST = os.environ.get("CDNMNUS_PUBLIC_HOST", "cdn.phpd77.com")

def probe(url: str) -> tuple[int, float]:
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            ok = 1 if response.status == 200 else 0
            response.read(16)
    except Exception:
        ok = 0
    return ok, time.monotonic() - start

def certificate_days() -> float:
    context = ssl.create_default_context()
    with socket.create_connection((PUBLIC_HOST, 443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=PUBLIC_HOST) as tls:
            expires = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    return max(0.0, (expires - time.time()) / 86400)

def main() -> None:
    nginx_ok, nginx_seconds = probe("http://127.0.0.1/nginx-health")
    broker_ok, broker_seconds = probe("http://127.0.0.1:9091/health")
    try: cert_days = certificate_days()
    except Exception: cert_days = 0.0
    load1, load5, load15 = os.getloadavg()
    lines = [
        "# Metrics intentionally contain no request labels or media URIs.",
        f"cdnmnus_nginx_up {nginx_ok}", f"cdnmnus_nginx_probe_seconds {nginx_seconds:.6f}",
        f"cdnmnus_broker_up {broker_ok}", f"cdnmnus_broker_probe_seconds {broker_seconds:.6f}",
        f"cdnmnus_tls_certificate_days_remaining {cert_days:.3f}",
        f"cdnmnus_load1 {load1:.3f}", f"cdnmnus_load5 {load5:.3f}", f"cdnmnus_load15 {load15:.3f}",
        f"cdnmnus_monitor_timestamp_seconds {int(time.time())}", "",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, OUTPUT)

if __name__ == "__main__": main()
