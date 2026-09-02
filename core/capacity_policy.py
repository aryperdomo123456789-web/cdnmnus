"""Política determinística de admissão de edges para o pool DNS-only."""
from __future__ import annotations

from typing import Any, Mapping


def evaluate_capacity(profile: Mapping[str, Any], sample: Mapping[str, Any]) -> dict[str, Any]:
    """Classifica uma amostra sem aplicar estado, DNS ou tráfego."""
    capacity = float(profile["capacity_mbps"])
    headroom = float(profile.get("headroom", 0.25))
    usable = capacity * (1.0 - headroom)
    ratios = {
        "bandwidth": float(sample["tx_mbps"]) / max(usable, 1.0),
        "cpu": float(sample["cpu_pct"]) / 85.0,
        "latency": float(sample["p95_ms"]) / 200.0,
        "http5xx": float(sample["http5xx"]),
    }
    if int(sample.get("nic_errors", 0)) > 0 or not bool(sample.get("vod_206_ok", True)):
        state = "down"
    else:
        pressure = max(ratios.values())
        state = "down" if pressure >= 0.95 else "draining" if pressure >= 0.85 else "pressured" if pressure >= 0.70 else "ready"
    pressure = max(ratios.values())
    return {
        "state": state,
        "pressure": round(pressure, 4),
        "usable_mbps": round(usable, 2),
        "desired_weight": 0 if state in {"draining", "down"} else 100,
        "ratios": {key: round(value, 4) for key, value in ratios.items()},
    }
