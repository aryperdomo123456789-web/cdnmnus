#!/usr/bin/env bash
# cdnmnus - hardening mínimo e explícito para UFW
set -Eeuo pipefail
IFS=$'\n\t'

BACKEND_PORT=3000
SSH_PORT=22
PROFILE="edge"
ALLOW_HTTP=0
ALLOW_SSH_PUBLIC=0
SSH_ALLOW_FROM=()
DRY_RUN=0

log() { printf '[cdnmnus][ufw] %s\n' "$*"; }
die() { printf '[cdnmnus][ufw][erro] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Uso: firewall_hardening.sh [--profile edge|load_balancer] [--backend-port PORTA] [--ssh-port PORTA] [--ssh-allow-from CIDR|IP] [--allow-http] [--allow-ssh-public] [--dry-run]

As regras existentes não são apagadas. O módulo aplica política default deny
para entrada, libera saída e aplica HTTP, HTTPS e SSH conforme o perfil.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backend-port)
        [[ $# -ge 2 ]] || die "--backend-port exige um valor."
        BACKEND_PORT="$2"
        shift 2
        ;;
      --profile)
        [[ $# -ge 2 ]] || die "--profile exige um valor."
        PROFILE="$2"
        shift 2
        ;;
      --ssh-port)
        [[ $# -ge 2 ]] || die "--ssh-port exige um valor."
        SSH_PORT="$2"
        shift 2
        ;;
      --ssh-allow-from)
        [[ $# -ge 2 ]] || die "--ssh-allow-from exige um valor."
        SSH_ALLOW_FROM+=("$2")
        shift 2
        ;;
      --allow-http)
        ALLOW_HTTP=1
        shift
        ;;
      --allow-ssh-public)
        ALLOW_SSH_PUBLIC=1
        shift
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

validate_port() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name deve ser numérico."
  (( value >= 1 && value <= 65535 )) || die "$name deve estar entre 1 e 65535."
}

validate_profile() {
  case "$PROFILE" in
    edge|load_balancer)
      # HTTP, HTTPS e SSH fazem parte do contrato público de todos os nós.
      ALLOW_HTTP=1
      ALLOW_SSH_PUBLIC=1
      ;;
    *)
      die "Perfil de firewall inválido: $PROFILE"
      ;;
  esac
}

show_plan() {
  cat <<EOF
# cdnmnus UFW profile
ufw default deny incoming
ufw default allow outgoing
ufw allow 443/tcp        # HTTPS
EOF
  if (( ALLOW_HTTP == 1 )); then
    printf 'ufw allow 80/tcp         # HTTP\n'
  fi
  if (( ALLOW_SSH_PUBLIC == 1 )) || [[ "$PROFILE" == "load_balancer" ]]; then
    printf 'ufw allow %s/tcp        # SSH público\n' "$SSH_PORT"
  else
    for source in "${SSH_ALLOW_FROM[@]}"; do
      printf "ufw allow from %s to any port %s proto tcp  # SSH restrito\n" "$source" "$SSH_PORT"
    done
  fi
  cat <<'EOF'
ufw --force enable
EOF
}

apply_rules() {
  (( DRY_RUN == 1 )) && return
  (( EUID == 0 )) || die "Execute como root ou com sudo."
  command -v ufw >/dev/null 2>&1 || die "ufw não está instalado."
  validate_profile

  # Não usamos 'ufw reset': preservamos regras de operadores e só reforçamos o baseline.
  ufw default deny incoming
  ufw default allow outgoing

  if [[ "$PROFILE" == "load_balancer" ]] || (( ALLOW_SSH_PUBLIC == 1 )); then
    # Não confundir uma regra restrita por origem com SSH público.
    ufw allow "$SSH_PORT/tcp" comment 'cdnmnus SSH público'
  else
    if ! ((${#SSH_ALLOW_FROM[@]} > 0)); then
      die "Perfil edge exige ao menos uma origem SSH autorizada."
    fi
    if ufw status | grep -Eq "${SSH_PORT}/tcp.*ALLOW"; then
      ufw delete allow "$SSH_PORT/tcp" >/dev/null 2>&1 || true
    fi
    local source
    for source in "${SSH_ALLOW_FROM[@]}"; do
      if ! ufw status | grep -Fq "from ${source} to any port ${SSH_PORT}"; then
        ufw allow from "$source" to any port "$SSH_PORT" proto tcp comment 'cdnmnus SSH restrito'
      fi
    done
  fi
  if (( ALLOW_HTTP == 1 )); then
    if ! ufw status | grep -Eq '80/tcp.*ALLOW'; then
      ufw allow 80/tcp comment 'cdnmnus HTTP'
    fi
  elif ufw status | grep -Eq '80/tcp.*ALLOW'; then
    ufw delete allow 80/tcp >/dev/null 2>&1 || true
  fi
  if ! ufw status | grep -Eq '443/tcp.*ALLOW'; then
    ufw allow 443/tcp comment 'cdnmnus HTTPS'
  fi
  ufw --force enable
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable ufw >/dev/null
  fi
  log "Hardening aplicado sem resetar regras previamente existentes."
}

main() {
  parse_args "$@"
  validate_port backend-port "$BACKEND_PORT"
  validate_port ssh-port "$SSH_PORT"
  validate_profile
  if (( BACKEND_PORT == SSH_PORT || BACKEND_PORT == 80 || BACKEND_PORT == 443 )); then
    die "A porta do backend não pode coincidir com SSH, HTTP ou HTTPS."
  fi
  if (( DRY_RUN == 1 )); then
    log "dry-run: nenhum estado do firewall foi alterado."
    show_plan
  else
    apply_rules
  fi
}

main "$@"
