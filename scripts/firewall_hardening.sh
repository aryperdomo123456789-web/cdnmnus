#!/usr/bin/env bash
# cdnmnus - hardening mínimo e explícito para UFW
set -Eeuo pipefail
IFS=$'\n\t'

BACKEND_PORT=3000
SSH_PORT=22
DRY_RUN=0

log() { printf '[cdnmnus][ufw] %s\n' "$*"; }
die() { printf '[cdnmnus][ufw][erro] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Uso: firewall_hardening.sh [--backend-port PORTA] [--ssh-port PORTA] [--dry-run]

As regras existentes não são apagadas. O módulo aplica política default deny
para entrada, libera saída e garante SSH, HTTP e HTTPS.
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
      --ssh-port)
        [[ $# -ge 2 ]] || die "--ssh-port exige um valor."
        SSH_PORT="$2"
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

validate_port() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name deve ser numérico."
  (( value >= 1 && value <= 65535 )) || die "$name deve estar entre 1 e 65535."
}

show_plan() {
  cat <<EOF
# cdnmnus UFW profile
ufw default deny incoming
ufw default allow outgoing
ufw allow $SSH_PORT/tcp  # SSH administrativo
ufw allow 80/tcp         # HTTP
ufw allow 443/tcp        # HTTPS
ufw deny $BACKEND_PORT/tcp  # backend não deve ser público
ufw --force enable
EOF
}

apply_rules() {
  (( DRY_RUN == 1 )) && return
  (( EUID == 0 )) || die "Execute como root ou com sudo."
  command -v ufw >/dev/null 2>&1 || die "ufw não está instalado."

  # Não usamos 'ufw reset': preservamos regras de operadores e só reforçamos o baseline.
  ufw default deny incoming
  ufw default allow outgoing

  if ! ufw status | grep -Eq "${SSH_PORT}/tcp.*ALLOW"; then
    ufw allow "$SSH_PORT/tcp" comment 'cdnmnus SSH'
  fi
  if ! ufw status | grep -Eq '80/tcp.*ALLOW'; then
    ufw allow 80/tcp comment 'cdnmnus HTTP'
  fi
  if ! ufw status | grep -Eq '443/tcp.*ALLOW'; then
    ufw allow 443/tcp comment 'cdnmnus HTTPS'
  fi
  if ! ufw status | grep -Eq "${BACKEND_PORT}/tcp.*DENY"; then
    ufw insert 1 deny "$BACKEND_PORT/tcp" comment 'cdnmnus backend privado'
  fi
  ufw --force enable
  log "Hardening aplicado sem resetar regras previamente existentes."
}

main() {
  parse_args "$@"
  validate_port backend-port "$BACKEND_PORT"
  validate_port ssh-port "$SSH_PORT"
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
