#!/usr/bin/env bash
# cdnmnus - tuning adaptativo de kernel e limites de arquivos
set -Eeuo pipefail
IFS=$'\n\t'

CORES="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
MEMORY_MB=""
NOFILE=""
DRY_RUN=0
SYSCTL_FILE="/etc/sysctl.d/99-cdnmnus.conf"
LIMITS_FILE="/etc/security/limits.d/99-cdnmnus.conf"

log() { printf '[cdnmnus][sysctl] %s\n' "$*"; }
die() { printf '[cdnmnus][sysctl][erro] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Uso: sysctl_tuning.sh [--cores N] [--memory-mb MB] [--nofile N] [--dry-run]

O módulo escreve um arquivo próprio em /etc/sysctl.d e não reescreve os
arquivos gerenciados pelo sistema operacional.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cores)
        [[ $# -ge 2 ]] || die "--cores exige um valor."
        CORES="$2"
        shift 2
        ;;
      --memory-mb)
        [[ $# -ge 2 ]] || die "--memory-mb exige um valor."
        MEMORY_MB="$2"
        shift 2
        ;;
      --nofile)
        [[ $# -ge 2 ]] || die "--nofile exige um valor."
        NOFILE="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Opção desconhecida: $1"
        ;;
    esac
  done
}

validate_numbers() {
  [[ "$CORES" =~ ^[0-9]+$ ]] || die "cores deve ser numérico."
  [[ "$MEMORY_MB" =~ ^[0-9]+$ ]] || die "memory-mb deve ser numérico."
  [[ "$NOFILE" =~ ^[0-9]+$ ]] || die "nofile deve ser numérico."
  (( CORES >= 1 )) || die "cores deve ser pelo menos 1."
  (( MEMORY_MB >= 128 )) || die "memory-mb deve ser pelo menos 128."
  (( NOFILE >= 4096 )) || die "nofile deve ser pelo menos 4096."
}

calculate_values() {
  # Mantém os valores úteis em hosts pequenos sem inflar tabelas de kernel.
  SOMAXCONN=$(( CORES * 4096 ))
  (( SOMAXCONN < 4096 )) && SOMAXCONN=4096
  (( SOMAXCONN > 65535 )) && SOMAXCONN=65535

  TCP_MAX_SYN_BACKLOG=$(( SOMAXCONN * 2 ))
  (( TCP_MAX_SYN_BACKLOG > 131072 )) && TCP_MAX_SYN_BACKLOG=131072

  # tcp_tw_reuse é um ajuste de cliente TCP; não ativa opções perigosas de NAT.
  TCP_TW_REUSE=1

  FILE_MAX=$(( MEMORY_MB * 256 ))
  (( FILE_MAX < 131072 )) && FILE_MAX=131072
  (( FILE_MAX > 8388608 )) && FILE_MAX=8388608

  VM_SWAPPINESS=10
  if (( MEMORY_MB < 2048 )); then
    VM_SWAPPINESS=20
  fi
}

show_values() {
  cat <<EOF
# cdnmnus sysctl profile
net.core.somaxconn = $SOMAXCONN
net.ipv4.tcp_max_syn_backlog = $TCP_MAX_SYN_BACKLOG
net.ipv4.tcp_tw_reuse = $TCP_TW_REUSE
fs.file-max = $FILE_MAX
vm.swappiness = $VM_SWAPPINESS
# cdnmnus nofile limits
* soft nofile $NOFILE
* hard nofile $NOFILE
root soft nofile $NOFILE
root hard nofile $NOFILE
EOF
}

apply_values() {
  (( DRY_RUN == 1 )) && return
  (( EUID == 0 )) || die "Execute como root ou com sudo."

  umask 022
  install -d -m 0755 "$(dirname -- "$SYSCTL_FILE")" "$(dirname -- "$LIMITS_FILE")"
  cat > "$SYSCTL_FILE" <<EOF
# Gerado pelo cdnmnus; alterações locais devem ser feitas neste arquivo.
net.core.somaxconn = $SOMAXCONN
net.ipv4.tcp_max_syn_backlog = $TCP_MAX_SYN_BACKLOG
net.ipv4.tcp_tw_reuse = $TCP_TW_REUSE
fs.file-max = $FILE_MAX
vm.swappiness = $VM_SWAPPINESS
EOF

  cat > "$LIMITS_FILE" <<EOF
# Gerado pelo cdnmnus; limites para Nginx e sessões administrativas.
* soft nofile $NOFILE
* hard nofile $NOFILE
root soft nofile $NOFILE
root hard nofile $NOFILE
EOF

  sysctl --system >/dev/null
  log "Perfil aplicado: somaxconn=$SOMAXCONN, syn_backlog=$TCP_MAX_SYN_BACKLOG, file-max=$FILE_MAX, nofile=$NOFILE."
}

main() {
  parse_args "$@"
  if [[ -z "$MEMORY_MB" ]]; then
    MEMORY_MB="$(( $(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || printf '524288') / 1024 ))"
  fi
  if [[ -z "$NOFILE" ]]; then
    local calculated=$(( MEMORY_MB * 16 ))
    (( calculated < 65536 )) && calculated=65536
    (( calculated > 1048576 )) && calculated=1048576
    NOFILE="$calculated"
  fi
  validate_numbers
  calculate_values
  if (( DRY_RUN == 1 )); then
    log "dry-run: nenhum arquivo foi alterado."
    show_values
  else
    apply_values
  fi
}

main "$@"
