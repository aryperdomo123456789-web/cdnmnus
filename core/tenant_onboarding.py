"""Onboarding transacional de tenants, com compensação de efeitos externos."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.db import Database


class TenantOnboardingError(RuntimeError):
    """Falha em uma etapa do onboarding; o tenant deve permanecer fechado."""


class TenantOnboardingService:
    """Coordena banco, TLS, release e DNS sem publicar estado intermediário."""

    def __init__(self, db: Database, *, operator: str = "mago-cdn-menu") -> None:
        self.db = db
        self.operator = operator

    def register(self, tenant_id: str, name: str, canonical_host: str,
                 origin_host: str, origin_port: int = 80,
                 load_balancers: tuple[str, ...] = (),
                 vod_seeds: tuple[str, ...] = ()) -> dict[str, Any]:
        tenant = self.db.add_tenant(
            tenant_id, name, canonical_host, origin_host, origin_port, load_balancers,
            vod_seeds=vod_seeds,
            enabled=False,
        )
        return self.db.begin_tenant_onboarding(
            tenant_id, operator=self.operator,
            reason="tenant criado sem publicação pública",
        )

    def execute(self, tenant_id: str, *, stage_tls: Callable[[], Any],
                deploy: Callable[[], Any], verify: Callable[[], Any],
                publish_dns: Callable[[], Any], rollback_tls: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Executa as etapas e compensa publicação/configuração em qualquer falha."""
        tls_rollback = rollback_tls
        try:
            self.db.update_tenant_onboarding(tenant_id, "staging", reason="início do staging TLS")
            staged = stage_tls()
            if callable(staged):
                tls_rollback = staged
            self.db.set_tenant_enabled(tenant_id, True, operator=self.operator,
                                       reason="configuração staged; ainda sem publicação DNS")
            deployment = deploy()
            deployment_id = deployment.get("deployment_id") if isinstance(deployment, dict) else None
            release_id = deployment.get("release_id") if isinstance(deployment, dict) else None
            self.db.update_tenant_onboarding(
                tenant_id, "verifying", reason="release distribuída; iniciando gates",
                deployment_id=deployment_id, release_id=release_id,
            )
            verify()
            publish_dns()
            self.db.update_tenant_onboarding(tenant_id, "committed", reason="TLS, release, health e DNS aprovados")
            return self.db.tenant_onboarding(tenant_id) or {}
        except Exception as exc:
            reason = str(exc)[:512]
            try:
                self.db.set_tenant_enabled(tenant_id, False, operator=self.operator,
                                           reason="rollback automático do onboarding")
                publish_dns()
            except Exception:
                pass
            if tls_rollback is not None:
                try:
                    tls_rollback()
                except Exception:
                    pass
            try:
                self.db.update_tenant_onboarding(
                    tenant_id, "rolled_back", reason="onboarding compensado", error=reason,
                )
            except Exception:
                pass
            raise TenantOnboardingError(reason) from exc
