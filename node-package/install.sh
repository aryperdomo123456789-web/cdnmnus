#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly PACKAGE_SCHEMA=1
readonly MENU_SOURCE_RELATIVE=ansible/roles/node_menu/files/node_menu.py
readonly MENU_TARGET=/usr/local/lib/cdnmnus-node-menu.py
ROLE=""
NODE_ID=""
NODE_NAME=""
CONTROL_PLANE=""
SOURCE_REF=""
SOURCE_COMMIT=""
EXPECTED_MANIFEST_DIGEST=""
DRY_RUN=0
BACKUP_DIR=""
INSTALL_COMPLETE=0

die() { printf '[cdnmnus-node][erro] %s\n' "$*" >&2; exit 1; }
log() { printf '[cdnmnus-node] %s\n' "$*"; }

managed_paths=(
  /etc/cdnmnus/node-id
  /etc/cdnmnus/node-role.json
  /etc/cdnmnus/control-plane.conf
  /usr/local/bin/mago-cdn
  /usr/local/bin/cdnmnus-verify-release
  /usr/local/sbin/cdnmnus-ansible-become
  /usr/local/lib/cdnmnus-node-menu.py
  /usr/local/lib/cdnmnus-node
  /var/lib/cdnmnus-node/package.json
)

backup_existing() {
  local path
  local -a existing=()
  BACKUP_DIR="/var/backups/cdnmnus-node/$(date -u +%Y%m%dT%H%M%SZ)-${SOURCE_COMMIT:0:12}"
  install -d -o root -g root -m 0700 "$BACKUP_DIR"
  for path in "${managed_paths[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
      existing+=("${path#/}")
    fi
  done
  printf '%s\n' "${existing[@]}" > "$BACKUP_DIR/files"
  chmod 0600 "$BACKUP_DIR/files"
  if ((${#existing[@]} > 0)); then
    tar -C / -cpf "$BACKUP_DIR/managed-paths.tar" -- "${existing[@]}"
    chmod 0600 "$BACKUP_DIR/managed-paths.tar"
  fi
}

rollback_install() {
  local status=$? path
  if (( INSTALL_COMPLETE == 0 )) && [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
    log "falha detectada; restaurando backup $BACKUP_DIR"
    for path in "${managed_paths[@]}"; do
      rm -rf -- "$path"
    done
    if [[ -f "$BACKUP_DIR/managed-paths.tar" ]]; then
      tar -C / -xpf "$BACKUP_DIR/managed-paths.tar"
    fi
  fi
  exit "$status"
}

trap rollback_install ERR

usage() {
  printf '%s\n' 'Uso: install.sh --role edge|load_balancer --node-id ID --node-name NOME --control-plane IPv4 --source-ref TAG --source-commit SHA40 --manifest-digest SHA256 [--dry-run]'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --node-id) NODE_ID="${2:-}"; shift 2 ;;
    --node-name) NODE_NAME="${2:-}"; shift 2 ;;
    --control-plane) CONTROL_PLANE="${2:-}"; shift 2 ;;
    --source-ref) SOURCE_REF="${2:-}"; shift 2 ;;
    --source-commit) SOURCE_COMMIT="${2:-}"; shift 2 ;;
    --manifest-digest) EXPECTED_MANIFEST_DIGEST="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "opção desconhecida: $1" ;;
  esac
done

[[ $EUID -eq 0 || $DRY_RUN -eq 1 ]] || die 'execute como root'
[[ "$ROLE" =~ ^(edge|load_balancer)$ ]] || die 'role inválida'
[[ "$NODE_ID" =~ ^[1-9][0-9]*$ ]] || die 'node-id deve ser numérico positivo'
[[ -n "$NODE_NAME" && "$NODE_NAME" != *$'\n'* ]] || die 'node-name inválido'
[[ "$CONTROL_PLANE" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die 'control-plane inválido'
[[ "$SOURCE_REF" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || die 'use uma tag imutável; main/branch são recusados'
[[ "$SOURCE_COMMIT" =~ ^[a-f0-9]{40}$ ]] || die 'source-commit inválido'
[[ "$EXPECTED_MANIFEST_DIGEST" =~ ^[a-f0-9]{64}$ ]] || die 'manifest-digest inválido'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
MANIFEST="$SCRIPT_DIR/manifest.json"
[[ -f "$MANIFEST" ]] || die 'manifest.json ausente'

actual_manifest_digest="$(sha256sum "$MANIFEST" | awk '{print $1}')"
[[ "$actual_manifest_digest" == "$EXPECTED_MANIFEST_DIGEST" ]] || die 'digest do manifesto diverge do autorizado'

python3 "$SCRIPT_DIR/verify.py" "$PROJECT_ROOT" "$MANIFEST" "$SOURCE_REF" "$SOURCE_COMMIT"

source /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die 'somente Ubuntu é suportado'
major="${VERSION_ID%%.*}"
[[ "$major" =~ ^[0-9]+$ && "$major" -ge 22 ]] || die 'Ubuntu 22.04+ obrigatório'
cpu="$(nproc)"; memory_mb="$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))"
(( cpu >= 2 )) || die 'mínimo de 2 vCPU'
(( memory_mb >= 3500 )) || die 'mínimo aproximado de 4 GiB de RAM'
available="$(df -P / | awk 'NR==2 {print $4}')"; total="$(df -P / | awk 'NR==2 {print $2}')"
(( available * 100 / total >= 20 )) || die 'mínimo de 20% de disco livre'
[[ "$(timedatectl show --property=NTPSynchronized --value)" == yes ]] || die 'NTP não sincronizado'

if (( DRY_RUN == 1 )); then
  log "dry-run aprovado: schema=$PACKAGE_SCHEMA role=$ROLE node=$NODE_ID ref=$SOURCE_REF"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
backup_existing
apt-get update
apt-get install -y nginx ufw ca-certificates curl python3 socat haproxy libnginx-mod-http-headers-more-filter
systemctl disable --now haproxy >/dev/null 2>&1 || true

install -d -o root -g root -m 0755 /etc/cdnmnus /usr/local/lib/cdnmnus-node
install -d -o root -g root -m 0755 /opt/cdnmnus/releases /opt/cdnmnus/runtime
install -d -o root -g root -m 0750 /var/lib/cdnmnus-node /var/lib/cdnmnus-edge
install -o root -g root -m 0755 "$PROJECT_ROOT/$MENU_SOURCE_RELATIVE" "$MENU_TARGET"
menu_digest="$(python3 - "$MANIFEST" <<'PY'
import json
import sys

manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(manifest["files"]["ansible/roles/node_menu/files/node_menu.py"])
PY
)"
printf '%s  %s\n' "$menu_digest" "$MENU_TARGET" | sha256sum -c - >/dev/null \
  || die 'menu instalado diverge do manifesto autorizado'
install -o root -g root -m 0755 "$PROJECT_ROOT/ansible/roles/node_menu/files/mago-cdn" /usr/local/bin/mago-cdn
install -o root -g root -m 0755 "$PROJECT_ROOT/ansible/files/verify_release.py" /usr/local/bin/cdnmnus-verify-release
install -o root -g root -m 0755 "$PROJECT_ROOT/scripts/cdnmnus-ansible-become" /usr/local/sbin/cdnmnus-ansible-become
install -o root -g root -m 0644 "$PROJECT_ROOT/panel/multi_tenant_broker.py" /usr/local/lib/cdnmnus-node/multi_tenant_broker.py
install -o root -g root -m 0644 "$PROJECT_ROOT/panel/vod_relay.py" /usr/local/lib/cdnmnus-node/vod_relay.py
install -o root -g root -m 0644 "$PROJECT_ROOT/panel/cdnmnus-tenant-broker@.service" /usr/local/lib/cdnmnus-node/cdnmnus-tenant-broker@.service
install -o root -g root -m 0644 "$PROJECT_ROOT/panel/cdnmnus-vod-relay@.service" /usr/local/lib/cdnmnus-node/cdnmnus-vod-relay@.service

printf '%s\n' "$NODE_ID" > /etc/cdnmnus/node-id
python3 - "$ROLE" "$NODE_ID" "$NODE_NAME" "$CONTROL_PLANE" "$SOURCE_REF" "$SOURCE_COMMIT" <<'PY'
import json, os, sys, tempfile
role, node_id, name, control, ref, commit = sys.argv[1:]
state = "bootstrapping" if role == "edge" else "candidate"
data = {
    "schema": 1, "node_id": node_id, "name": name, "role": role, "state": state,
    "control_plane": {"host": control, "port": 22, "scheme": "ssh", "verify": True},
    "release_id": "", "config_digest": "",
    "package": {"ref": ref, "commit": commit},
    "capabilities": {"edge_runtime": True, "load_balancer_candidate": True,
                     "promotion_requires_control_plane": True},
}
path = "/etc/cdnmnus/node-role.json"
fd, temporary = tempfile.mkstemp(prefix=".node-role.", dir="/etc/cdnmnus", text=True)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2); handle.write("\n")
os.chmod(temporary, 0o644); os.replace(temporary, path)
PY
cat > /etc/cdnmnus/control-plane.conf <<EOF
CONTROL_PLANE_HOST=$CONTROL_PLANE
CONTROL_PLANE_PORT=22
CONTROL_PLANE_SCHEME=ssh
CONTROL_PLANE_VERIFY=1
NODE_BOOTSTRAP_MODE=managed
PACKAGE_REF=$SOURCE_REF
PACKAGE_COMMIT=$SOURCE_COMMIT
EOF
chmod 0644 /etc/cdnmnus/node-id /etc/cdnmnus/node-role.json /etc/cdnmnus/control-plane.conf

cat > /var/lib/cdnmnus-node/package.json <<EOF
{"schema":1,"ref":"$SOURCE_REF","commit":"$SOURCE_COMMIT","manifest_digest":"$EXPECTED_MANIFEST_DIGEST","role":"$ROLE"}
EOF
chmod 0600 /var/lib/cdnmnus-node/package.json

INSTALL_COMPLETE=1
log 'pacote universal instalado; HAProxy permanece desabilitado até autorização do control plane'
log 'o runtime de tenant só será ativado por release imutável do control plane'
