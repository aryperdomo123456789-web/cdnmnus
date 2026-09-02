"""Descoberta CNAME por tenant com cache e rejeição em modo fail-closed."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

MAX_HOST_LENGTH = 253
MAX_CNAME_HOPS = 4
MIN_TTL_SECONDS = 15
MAX_TTL_SECONDS = 300
INTERNAL_PUBLIC_PREFIX = "__cdnmnus_"


class DiscoveryError(ValueError):
    """Falha de descoberta que deve resultar em 421 no gateway."""


@dataclass(frozen=True)
class TenantDiscoveryTarget:
    tenant_id: str
    canonical_host: str
    enabled: bool = True
    config_version: int | None = None


@dataclass(frozen=True)
class DiscoveryHop:
    host: str
    ttl: int
    cname: str | None = None
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    alias_host: str
    canonical_host: str | None
    tenant_id: str | None
    observed_chain: tuple[DiscoveryHop, ...]
    expires_at: float
    decision_id: str
    state: str
    last_error: str | None = None

    @property
    def ttl_seconds(self) -> int:
        return max(0, int(round(self.expires_at - time.time())))


Resolver = Callable[[str], Mapping[str, Any] | Sequence[Any] | Any]


def _normalize_label(label: str) -> str:
    if not label:
        raise DiscoveryError("hostname inválido")
    try:
        ascii_label = label.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DiscoveryError("hostname inválido") from exc
    if len(ascii_label.encode("ascii")) > 63:
        raise DiscoveryError("hostname inválido")
    return ascii_label.lower()


def normalize_discovery_host(value: str) -> str:
    raw = value.strip().rstrip(".")
    if not raw or any(marker in raw for marker in ("://", "/", "@", "?", "#", "\\")):
        raise DiscoveryError("hostname inválido")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise DiscoveryError("hostname não pode ser IP")
    labels = raw.split(".")
    if any(not label for label in labels):
        raise DiscoveryError("hostname inválido")
    normalized = ".".join(_normalize_label(label) for label in labels)
    if len(normalized.encode("ascii")) > MAX_HOST_LENGTH:
        raise DiscoveryError("hostname muito longo")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", normalized):
        raise DiscoveryError("hostname inválido")
    return normalized


def _normalize_ip(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if not address.is_global:
        raise DiscoveryError("endereço público não permitido")
    return str(address)


def _coerce_observation(answer: Mapping[str, Any] | Sequence[Any] | Any) -> dict[str, Any]:
    if isinstance(answer, Mapping):
        return dict(answer)
    if isinstance(answer, tuple) or isinstance(answer, list):
        if len(answer) == 3:
            return {"cname": answer[0], "addresses": answer[1], "ttl": answer[2]}
        if len(answer) == 2:
            return {"addresses": answer[0], "ttl": answer[1]}
    result: dict[str, Any] = {}
    for key in ("cname", "canonical", "canonical_host", "addresses", "ips", "ttl", "enabled", "tenant_id"):
        if hasattr(answer, key):
            result[key] = getattr(answer, key)
    if result:
        return result
    raise DiscoveryError("resposta DNS inválida")


def _coerce_ttl(value: Any) -> int:
    ttl = int(value)
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise DiscoveryError("TTL fora do intervalo permitido")
    return ttl


def _coerce_addresses(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value)
    normalized: list[str] = []
    for item in values:
        normalized.append(_normalize_ip(str(item)))
    return tuple(sorted(dict.fromkeys(normalized)))


def _tenant_enabled(item: Any) -> bool:
    if isinstance(item, TenantDiscoveryTarget):
        return item.enabled
    if isinstance(item, Mapping):
        return bool(item.get("enabled", True))
    return bool(getattr(item, "enabled", True))


def _tenant_id(item: Any) -> str:
    if isinstance(item, TenantDiscoveryTarget):
        return item.tenant_id
    if isinstance(item, Mapping):
        value = item.get("tenant_id", item.get("id"))
    else:
        value = getattr(item, "tenant_id", getattr(item, "id", None))
    if value is None:
        raise DiscoveryError("tenant sem identidade")
    return str(value)


def _canonical_host(item: Any) -> str:
    if isinstance(item, TenantDiscoveryTarget):
        return item.canonical_host
    if isinstance(item, Mapping):
        value = item.get("canonical_host")
    else:
        value = getattr(item, "canonical_host", None)
    if value is None:
        raise DiscoveryError("tenant sem canonical_host")
    return normalize_discovery_host(str(value))


def build_tenant_index(tenants: Iterable[Mapping[str, Any]]) -> dict[str, TenantDiscoveryTarget]:
    index: dict[str, TenantDiscoveryTarget] = {}
    for tenant in tenants:
        canonical = normalize_discovery_host(str(tenant["canonical_host"]))
        if canonical in index:
            raise DiscoveryError(f"canonical duplicado: {canonical}")
        index[canonical] = TenantDiscoveryTarget(
            tenant_id=str(tenant["id"]),
            canonical_host=canonical,
            enabled=bool(tenant.get("enabled", 1)),
            config_version=int(tenant.get("config_version", 0)) if str(tenant.get("config_version", "")).strip() else None,
        )
    return index


def _lookup_tenant(tenant_index: Mapping[str, Any], canonical_host: str) -> TenantDiscoveryTarget | None:
    item = tenant_index.get(canonical_host)
    if item is None:
        return None
    return TenantDiscoveryTarget(
        tenant_id=_tenant_id(item),
        canonical_host=_canonical_host(item),
        enabled=_tenant_enabled(item),
        config_version=(int(item.get("config_version")) if isinstance(item, Mapping) and item.get("config_version") is not None
                        else getattr(item, "config_version", None)),
    )


def _decision_id(alias_host: str, canonical_host: str, tenant_id: str,
                 chain: tuple[DiscoveryHop, ...], expires_at: float) -> str:
    payload = {
        "alias_host": alias_host,
        "canonical_host": canonical_host,
        "tenant_id": tenant_id,
        "chain": [asdict(hop) for hop in chain],
        "expires_at": round(expires_at, 3),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def discover_alias(host: str, tenant_index: Mapping[str, Any], resolver: Resolver,
                   now: float | None = None) -> DiscoveryResult:
    alias_host = normalize_discovery_host(host)
    current_host = alias_host
    visited = {alias_host}
    chain: list[DiscoveryHop] = []
    ttl_floor: int | None = None
    matched_tenant: TenantDiscoveryTarget | None = None
    now = time.time() if now is None else float(now)

    for _ in range(MAX_CNAME_HOPS + 1):
        # A canonical tenant may itself CNAME to the shared CDN endpoint.
        # Keep the first recognized canonical while still validating the
        # complete chain and its public terminal address.
        candidate = _lookup_tenant(tenant_index, current_host)
        if candidate is not None:
            if not candidate.enabled:
                raise DiscoveryError("canonical desabilitado")
            if matched_tenant is not None and matched_tenant.tenant_id != candidate.tenant_id:
                raise DiscoveryError("cadeia CNAME muda de tenant")
            matched_tenant = candidate
        answer = _coerce_observation(resolver(current_host))
        ttl = _coerce_ttl(answer.get("ttl", MAX_TTL_SECONDS))
        ttl_floor = ttl if ttl_floor is None else min(ttl_floor, ttl)
        cname_value = answer.get("cname") or answer.get("canonical") or answer.get("canonical_host")
        if cname_value:
            next_host = normalize_discovery_host(str(cname_value))
            if next_host.startswith(INTERNAL_PUBLIC_PREFIX):
                raise DiscoveryError("destino CNAME reservado")
            if next_host in visited:
                raise DiscoveryError("cadeia CNAME circular")
            chain.append(DiscoveryHop(host=current_host, ttl=ttl, cname=next_host))
            visited.add(next_host)
            current_host = next_host
            continue

        addresses = _coerce_addresses(answer.get("addresses", answer.get("ips", ())))
        if not addresses:
            raise DiscoveryError("terminal sem endereço público")
        if matched_tenant is None:
            raise DiscoveryError("canonical inexistente")
        chain.append(DiscoveryHop(host=current_host, ttl=ttl, addresses=addresses))
        effective_ttl = ttl_floor if ttl_floor is not None else ttl
        if effective_ttl < MIN_TTL_SECONDS or effective_ttl > MAX_TTL_SECONDS:
            raise DiscoveryError("TTL efetivo fora do intervalo permitido")
        expires_at = now + effective_ttl
        decision_id = _decision_id(alias_host, matched_tenant.canonical_host, matched_tenant.tenant_id, tuple(chain), expires_at)
        return DiscoveryResult(
            alias_host=alias_host,
            canonical_host=matched_tenant.canonical_host,
            tenant_id=matched_tenant.tenant_id,
            observed_chain=tuple(chain),
            expires_at=expires_at,
            decision_id=decision_id,
            state="valid",
        )

    raise DiscoveryError("cadeia CNAME excede quatro saltos")


class DigResolver:
    """Resolvedor de laboratório usando ``dig`` do sistema."""

    def __init__(self, *, timeout: int = 5) -> None:
        self.timeout = timeout

    def _answers(self, host: str, *record_types: str) -> list[tuple[int, str]]:
        cmd = ["dig", "+noall", "+answer", normalize_discovery_host(host), *record_types]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=self.timeout, check=False)
        if result.returncode != 0:
            raise DiscoveryError(result.stderr.strip() or "dig falhou")
        answers: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                ttl = int(parts[1])
            except ValueError:
                continue
            rtype = parts[3].upper()
            if rtype == "CNAME":
                answers.append((ttl, parts[4].rstrip(".")))
            elif rtype in {"A", "AAAA"}:
                answers.append((ttl, parts[4]))
        return answers

    def __call__(self, host: str) -> Mapping[str, Any]:
        cname_answers = self._answers(host, "CNAME")
        if cname_answers:
            ttl, target = cname_answers[0]
            return {"cname": target, "addresses": (), "ttl": ttl}
        # ``dig`` accepts one record type per invocation here; combining A
        # and AAAA would be parsed as an invalid query and hide valid A data.
        address_answers = self._answers(host, "A") + self._answers(host, "AAAA")
        addresses = [address for _, address in address_answers]
        ttl = min((ttl for ttl, _ in address_answers), default=MAX_TTL_SECONDS)
        return {"addresses": addresses, "ttl": ttl}


def system_resolver() -> DigResolver:
    return DigResolver()
