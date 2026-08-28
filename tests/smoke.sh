#!/usr/bin/env bash
# Validação local sem instalar pacotes ou alterar o host.
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP_DIR="$(mktemp -d -t cdnmnus-test.XXXXXX)"
trap 'rm -rf -- "$TMP_DIR"' EXIT

fail() { printf '[smoke][erro] %s\n' "$*" >&2; exit 1; }
pass() { printf '[smoke] %s\n' "$*"; }

cd "$ROOT_DIR"
bash -n install.sh scripts/sysctl_tuning.sh scripts/firewall_hardening.sh
pass 'sintaxe Bash aprovada'

dry_output="$(./install.sh --dry-run --yes --main-ip 127.0.0.1 --main-port 3000 --domain exemplo.com)"
grep -q 'Dry-run concluído' <<< "$dry_output" || fail 'dry-run do instalador não concluiu'
grep -q 'worker_connections:' <<< "$dry_output" || fail 'perfil de workers não foi calculado'
pass 'dry-run do instalador aprovado'

small_output="$(./scripts/sysctl_tuning.sh --dry-run --cores 2 --memory-mb 4096 --nofile 65536)"
grep -q 'net.core.somaxconn = 8192' <<< "$small_output" || fail 'somaxconn do perfil pequeno inesperado'
grep -q 'fs.file-max = 1048576' <<< "$small_output" || fail 'file-max do perfil pequeno inesperado'
pass 'perfil sysctl de VPS pequena aprovado'

large_output="$(./scripts/sysctl_tuning.sh --dry-run --cores 16 --memory-mb 65536 --nofile 1048576)"
grep -q 'net.core.somaxconn = 65535' <<< "$large_output" || fail 'somaxconn do perfil grande não respeitou teto'
grep -q 'fs.file-max = 8388608' <<< "$large_output" || fail 'file-max do perfil grande não respeitou teto'
pass 'perfil sysctl de máquina grande aprovado'

ufw_output="$(./scripts/firewall_hardening.sh --dry-run --backend-port 3000 --ssh-port 2222)"
grep -q 'ufw allow 2222/tcp' <<< "$ufw_output" || fail 'porta SSH customizada ausente'
grep -q 'ufw deny 3000/tcp' <<< "$ufw_output" || fail 'negação do backend ausente'
pass 'dry-run UFW aprovado'

if ./install.sh --dry-run --yes --main-ip 127.0.0.1 --main-port 443 --domain exemplo.com >/dev/null 2>&1; then
  fail 'colisão backend/HTTPS deveria falhar'
fi
pass 'validação de colisão de portas aprovada'

rendered="$TMP_DIR/nginx.conf"
sed \
  -e 's|__BACKEND_SERVER__|127.0.0.1:3000|g' \
  -e 's|__DOMAIN__|exemplo.com|g' \
  -e 's|__WORKER_CONNECTIONS__|4096|g' \
  -e 's|__WORKER_RLIMIT_NOFILE__|65536|g' \
  -e 's|__PROXY_BUFFER_SIZE__|8k|g' \
  -e 's|__PROXY_BUFFERS__|4 8k|g' \
  -e 's|__PROXY_BUSY_BUFFERS__|32k|g' \
  -e 's|__CLIENT_MAX_BODY_SIZE__|8m|g' \
  nginx/nginx.conf > "$rendered"
if grep -Eq '__[A-Z_]+__' "$rendered"; then
  fail 'template Nginx ficou com placeholders'
fi
grep -q 'worker_processes auto;' "$rendered" || fail 'worker_processes ausente'
grep -q 'use epoll;' "$rendered" || fail 'epoll ausente'
grep -q 'keepalive 32;' "$rendered" || fail 'upstream keepalive ausente'
grep -q 'X-Real-IP' "$rendered" || fail 'header de IP real ausente'
pass 'renderização determinística do Nginx aprovada'

if command -v nginx >/dev/null 2>&1; then
  nginx -t -c "$rendered" >/dev/null
  pass 'nginx -t aprovado pelo binário disponível'
else
  pass 'nginx -t não executado: binário não disponível no ambiente local'
fi

grep -q 'nginx -t' install.sh || fail 'instalador não contém validação nginx -t'
pass 'smoke test concluído com sucesso'
