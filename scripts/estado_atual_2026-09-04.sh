#!/usr/bin/env bash
set -Eeuo pipefail

# Registro operacional seguro: nao imprime credenciais, bancos ou certificados.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "cdnmnus estado atual"
echo "capturado_em_utc=$STAMP"
echo "repo=$REPO_ROOT"
echo "branch=$(git branch --show-current)"
echo "commit=$(git rev-parse HEAD)"
echo "remote=$(git remote get-url origin)"
echo
echo "arquivos_versionados=$(git ls-files | wc -l | tr -d ' ')"
echo "arquivos_modificados=$(git diff --name-only | wc -l | tr -d ' ')"
echo "arquivos_novos=$(git ls-files --others --exclude-standard | wc -l | tr -d ' ')"
echo
echo "ultimo_commit:"
git log -1 --format='%h %ad %s' --date=iso-strict
echo
echo "nginx:"
if command -v nginx >/dev/null 2>&1; then
    nginx -t 2>&1 | tail -2
else
    echo "nao instalado neste host"
fi
echo
echo "servicos:"
if command -v systemctl >/dev/null 2>&1; then
    for service in nginx cdnmnus-token-broker.service cdnmnus-orchestrator.service; do
        printf '%s=' "$service"
        systemctl is-active "$service" 2>/dev/null || true
    done
else
    echo "systemd nao disponivel neste ambiente"
fi
echo
echo "observacao=segredos, bancos, certificados, logs e artefatos de runtime sao mantidos fora do Git por seguranca"
