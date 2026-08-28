#!/usr/bin/env bash
# cdnmnus - instalador modular de reverse proxy Nginx para Ubuntu 20.04+
set -Eeuo pipefail
IFS=$'\n\t'

readonly INSTALLER_VERSION="1.0.0"
readonly DEFAULT_BACKEND_IP="127.0.0.1"
readonly DEFAULT_BACKEND_PORT="3000"
readonly DEFAULT_DOMAIN="_"
readonly RAW_BASE_DEFAULT="https://raw.githubusercontent.com/aryperdomo123456789-web/cdnmnus/main"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || true)"
TMP_DIR=""
MAIN_IP=""
MAIN_PORT=""
DOMAIN=""
DRY_RUN=0
NO_FIREWALL=0
ASSUME_YES=0
SSH_PORT=22
WITH_PANEL=0

CPU_CORES=1
MEM_MB=512
WORKER_CONNECTIONS=4096
WORKER_RLIMIT_NOFILE=65536
PROXY_BUFFER_SIZE="8k"
PROXY_BUFFERS="4 8k"
PROXY_BUSY_BUFFERS="16k"
CLIENT_MAX_BODY_SIZE="16m"
BACKEND_SERVER=""

log() { printf '[cdnmnus] %s\n' "$*"; }
warn() { printf '[cdnmnus][aviso] %s\n' "$*" >&2; }
die() { printf '[cdnmnus][erro] %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    rm -rf -- "${TMP_DIR}"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Uso:
  sudo ./install.sh [opções]
  curl -sSL https://raw.githubusercontent.com/aryperdomo123456789-web/cdnmnus/main/install.sh | bash -s -- [opções]

Opções:
  --main-ip IP|HOST       Backend da aplicação (padrão: 127.0.0.1)
  --main-port PORTA       Porta do backend (padrão: 3000)
  --domain DOMÍNIO        server_name do Nginx (padrão: _)
  --ssh-port PORTA        Porta SSH a liberar no UFW (padrão: 22)
  --with-panel            Instalar painel autenticado em localhost:9090
  --no-firewall           Não aplicar as regras UFW
  --dry-run               Apenas validar e mostrar o plano, sem alterar o sistema
  --yes                   Aceitar o resumo sem confirmação interativa
  -h, --help              Mostrar esta ajuda

Exemplos:
  sudo ./install.sh --main-ip 127.0.0.1 --main-port 3000 --domain exemplo.com
  sudo ./install.sh --main-ip 10.0.0.12 --main-port 8080 --domain api.exemplo.com --no-firewall
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --main-ip)
        [[ $# -ge 2 ]] || die "--main-ip exige um valor."
        MAIN_IP="$2"
        shift 2
        ;;
      --main-port)
        [[ $# -ge 2 ]] || die "--main-port exige um valor."
        MAIN_PORT="$2"
        shift 2
        ;;
      --domain)
        [[ $# -ge 2 ]] || die "--domain exige um valor."
        DOMAIN="$2"
        shift 2
        ;;
      --ssh-port)
        [[ $# -ge 2 ]] || die "--ssh-port exige um valor."
        SSH_PORT="$2"
        shift 2
        ;;
      --with-panel)
        WITH_PANEL=1
        shift
        ;;
      --no-firewall)
        NO_FIREWALL=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --yes)
        ASSUME_YES=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Opção desconhecida: $1. Use --help para consultar o uso."
        ;;
    esac
  done
}

interactive_menu() {
  if [[ ! -t 0 || ! -t 1 ]]; then
    return
  fi

  printf '\n=== cdnmnus %s | Nginx de alta eficiência ===\n' "$INSTALLER_VERSION"
  printf '1) Proxy local padrão (%s:%s)\n' "$DEFAULT_BACKEND_IP" "$DEFAULT_BACKEND_PORT"
  printf '2) Configurar backend personalizado\n'
  printf 'Escolha [1]: '
  local choice
  read -r choice || true
  choice="${choice:-1}"

  case "$choice" in
    1)
      MAIN_IP="${MAIN_IP:-$DEFAULT_BACKEND_IP}"
      MAIN_PORT="${MAIN_PORT:-$DEFAULT_BACKEND_PORT}"
      ;;
    2)
      if [[ -z "$MAIN_IP" ]]; then
        read -r -p "IP/host do backend [$DEFAULT_BACKEND_IP]: " MAIN_IP || true
        MAIN_IP="${MAIN_IP:-$DEFAULT_BACKEND_IP}"
      fi
      if [[ -z "$MAIN_PORT" ]]; then
        read -r -p "Porta do backend [$DEFAULT_BACKEND_PORT]: " MAIN_PORT || true
        MAIN_PORT="${MAIN_PORT:-$DEFAULT_BACKEND_PORT}"
      fi
      ;;
    *)
      die "Escolha inválida. Pare de meter o louco no menu e use 1 ou 2."
      ;;
  esac

  if [[ -z "$DOMAIN" ]]; then
    read -r -p "Domínio/server_name [$DEFAULT_DOMAIN]: " DOMAIN || true
    DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
  fi
}

set_defaults() {
  MAIN_IP="${MAIN_IP:-$DEFAULT_BACKEND_IP}"
  MAIN_PORT="${MAIN_PORT:-$DEFAULT_BACKEND_PORT}"
  DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
}

validate_inputs() {
  [[ "$MAIN_PORT" =~ ^[0-9]+$ ]] || die "A porta do backend deve ser numérica."
  (( MAIN_PORT >= 1 && MAIN_PORT <= 65535 )) || die "A porta do backend deve estar entre 1 e 65535."
  [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "A porta SSH deve ser numérica."
  (( SSH_PORT >= 1 && SSH_PORT <= 65535 )) || die "A porta SSH deve estar entre 1 e 65535."
  if (( NO_FIREWALL == 0 )) && { (( MAIN_PORT == SSH_PORT )) || (( MAIN_PORT == 80 )) || (( MAIN_PORT == 443 )); }; then
    die "A porta do backend não pode coincidir com SSH, HTTP ou HTTPS quando o UFW está ativo."
  fi

  # Permite IPv4, IPv6 sem colchetes e host DNS, sem aceitar caracteres de configuração.
  [[ "$MAIN_IP" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "--main-ip contém caracteres inválidos."
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.*_-]+$ ]] || die "--domain contém caracteres inválidos."
  [[ "$DOMAIN" != *.*.*.*.*.* ]] || die "--domain parece inválido demais para ser um server_name seguro."

  if [[ "$MAIN_IP" == *:* ]]; then
    BACKEND_SERVER="[$MAIN_IP]:$MAIN_PORT"
  else
    BACKEND_SERVER="$MAIN_IP:$MAIN_PORT"
  fi
}

validate_os() {
  [[ -r /etc/os-release ]] || die "Não foi possível detectar /etc/os-release."
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "Distribuição não suportada: ${ID:-desconhecida}. Use Ubuntu 20.04 ou superior."
  local major="${VERSION_ID%%.*}"
  [[ "$major" =~ ^[0-9]+$ ]] || die "Não foi possível interpretar a versão do Ubuntu."
  (( major >= 20 )) || die "Ubuntu ${VERSION_ID} detectado; o mínimo suportado é 20.04."
}

require_root() {
  (( DRY_RUN == 1 )) && return
  (( EUID == 0 )) || die "Execute como root ou com sudo. O Nginx não aceita telepatia administrativa."
}

read_environment() {
  CPU_CORES="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
  [[ "$CPU_CORES" =~ ^[0-9]+$ ]] || CPU_CORES=1
  (( CPU_CORES >= 1 )) || CPU_CORES=1

  if [[ -r /proc/meminfo ]]; then
    MEM_MB="$(( $(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo) / 1024 ))"
  fi
  [[ "$MEM_MB" =~ ^[0-9]+$ ]] || MEM_MB=512
  (( MEM_MB >= 128 )) || MEM_MB=128

  # Conexões por worker: usa memória disponível e divide pela capacidade de CPU.
  # Limites conservadores em máquinas pequenas evitam consumir RAM em excesso.
  local calculated=$(( MEM_MB * 16 / CPU_CORES ))
  (( calculated < 4096 )) && calculated=4096
  (( calculated > 65536 )) && calculated=65536
  WORKER_CONNECTIONS="$calculated"

  local nofile=$(( WORKER_CONNECTIONS * CPU_CORES * 2 ))
  (( nofile < 65536 )) && nofile=65536
  (( nofile > 1048576 )) && nofile=1048576
  WORKER_RLIMIT_NOFILE="$nofile"

  if (( MEM_MB < 2048 )); then
    PROXY_BUFFER_SIZE="8k"
    PROXY_BUFFERS="4 8k"
    PROXY_BUSY_BUFFERS="16k"
    CLIENT_MAX_BODY_SIZE="8m"
  elif (( MEM_MB < 8192 )); then
    PROXY_BUFFER_SIZE="16k"
    PROXY_BUFFERS="8 16k"
    PROXY_BUSY_BUFFERS="64k"
    CLIENT_MAX_BODY_SIZE="16m"
  else
    PROXY_BUFFER_SIZE="32k"
    PROXY_BUFFERS="16 32k"
    PROXY_BUSY_BUFFERS="128k"
    CLIENT_MAX_BODY_SIZE="32m"
  fi
}

confirm_plan() {
  printf '\nPlano de execução:\n'
  printf '  Backend:              %s\n' "$BACKEND_SERVER"
  printf '  Domínio:              %s\n' "$DOMAIN"
  printf '  CPU/RAM detectados:   %s vCPU / %s MB\n' "$CPU_CORES" "$MEM_MB"
  printf '  worker_connections:   %s\n' "$WORKER_CONNECTIONS"
  printf '  limite de arquivos:   %s\n' "$WORKER_RLIMIT_NOFILE"
  printf '  Firewall UFW:          %s\n' "$([[ $NO_FIREWALL -eq 1 ]] && printf 'não alterar' || printf 'aplicar hardening; SSH %s' "$SSH_PORT")"
  printf '  Painel autenticado:    %s\n' "$([[ $WITH_PANEL -eq 1 ]] && printf 'instalar em 127.0.0.1:9090' || printf 'não instalar')"
  printf '  Modo:                  %s\n' "$([[ $DRY_RUN -eq 1 ]] && printf 'dry-run' || printf 'instalação')"

  if (( DRY_RUN == 1 || ASSUME_YES == 1 )) || [[ ! -t 0 ]]; then
    return
  fi
  printf 'Continuar? [s/N]: '
  local answer
  read -r answer || true
  [[ "$answer" =~ ^([sS][iI][mM]|[sS]|[yY][eE][sS])$ ]] || die "Operação cancelada pelo operador."
}

fetch_asset() {
  local source="$1"
  local destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 10 "$source" -o "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries=3 --timeout=10 "$source" -O "$destination"
  else
    die "É necessário curl ou wget para o modo de instalação remota."
  fi
}

prepare_assets() {
  local local_ok=0
  if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/sysctl_tuning.sh" && -f "$SCRIPT_DIR/scripts/firewall_hardening.sh" && -f "$SCRIPT_DIR/nginx/nginx.conf" ]]; then
    local_ok=1
  fi

  if (( local_ok == 1 )); then
    ASSET_DIR="$SCRIPT_DIR"
    return
  fi

  TMP_DIR="$(mktemp -d -t cdnmnus.XXXXXX)"
  mkdir -p "$TMP_DIR/scripts" "$TMP_DIR/nginx"
  local raw_base="${CDN_INSTALLER_RAW_BASE:-$RAW_BASE_DEFAULT}"
  log "Modo remoto detectado; baixando os módulos oficiais do repositório."
  fetch_asset "$raw_base/scripts/sysctl_tuning.sh" "$TMP_DIR/scripts/sysctl_tuning.sh"
  fetch_asset "$raw_base/scripts/firewall_hardening.sh" "$TMP_DIR/scripts/firewall_hardening.sh"
  fetch_asset "$raw_base/nginx/nginx.conf" "$TMP_DIR/nginx/nginx.conf"
  chmod 0755 "$TMP_DIR/scripts/sysctl_tuning.sh" "$TMP_DIR/scripts/firewall_hardening.sh"
  ASSET_DIR="$TMP_DIR"
}

install_packages() {
  (( DRY_RUN == 1 )) && return
  export DEBIAN_FRONTEND=noninteractive
  log "Instalando apenas os pacotes nativos necessários: nginx e ufw."
  apt-get update -y
  apt-get install -y --no-install-recommends nginx ufw ca-certificates
}

render_nginx_config() {
  local template="$ASSET_DIR/nginx/nginx.conf"
  local rendered="$TMP_DIR/rendered-nginx.conf"
  [[ -f "$template" ]] || die "Template Nginx não encontrado: $template"
  [[ -n "$TMP_DIR" ]] || TMP_DIR="$(mktemp -d -t cdnmnus.XXXXXX)"
  rendered="$TMP_DIR/rendered-nginx.conf"

  sed \
    -e "s|__BACKEND_SERVER__|$BACKEND_SERVER|g" \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__WORKER_CONNECTIONS__|$WORKER_CONNECTIONS|g" \
    -e "s|__WORKER_RLIMIT_NOFILE__|$WORKER_RLIMIT_NOFILE|g" \
    -e "s|__PROXY_BUFFER_SIZE__|$PROXY_BUFFER_SIZE|g" \
    -e "s|__PROXY_BUFFERS__|$PROXY_BUFFERS|g" \
    -e "s|__PROXY_BUSY_BUFFERS__|$PROXY_BUSY_BUFFERS|g" \
    -e "s|__CLIENT_MAX_BODY_SIZE__|$CLIENT_MAX_BODY_SIZE|g" \
    "$template" > "$rendered"

  if grep -Eq '__[A-Z_]+__' "$rendered"; then
    die "O template Nginx ficou com placeholders não renderizados."
  fi
  RENDERED_CONFIG="$rendered"
}

run_tuning() {
  local tuning="$ASSET_DIR/scripts/sysctl_tuning.sh"
  if (( DRY_RUN == 1 )); then
    bash "$tuning" --dry-run --cores "$CPU_CORES" --memory-mb "$MEM_MB" --nofile "$WORKER_RLIMIT_NOFILE"
  else
    bash "$tuning" --cores "$CPU_CORES" --memory-mb "$MEM_MB" --nofile "$WORKER_RLIMIT_NOFILE"
  fi
}

run_firewall() {
  (( NO_FIREWALL == 1 )) && { warn "UFW desativado por --no-firewall."; return; }
  local firewall="$ASSET_DIR/scripts/firewall_hardening.sh"
  if (( DRY_RUN == 1 )); then
    bash "$firewall" --dry-run --backend-port "$MAIN_PORT" --ssh-port "$SSH_PORT"
  else
    bash "$firewall" --backend-port "$MAIN_PORT" --ssh-port "$SSH_PORT"
  fi
}

install_panel() {
  (( WITH_PANEL == 0 )) && return
  if (( DRY_RUN == 1 )); then
    log "dry-run: painel seria instalado em 127.0.0.1:9090."
    return
  fi
  [[ -f "$ASSET_DIR/panel/panel.py" && -f "$ASSET_DIR/panel/cdnmnus-panel.service" ]] || die "Arquivos do painel não encontrados."
  local panel_dir="/opt/cdnmnus-panel"
  local env_file="/etc/cdnmnus/panel.env"
  local password
  install -d -m 0755 "$panel_dir" /etc/cdnmnus
  install -m 0755 "$ASSET_DIR/panel/panel.py" "$panel_dir/panel.py"
  install -m 0644 "$ASSET_DIR/panel/cdnmnus-panel.service" /etc/systemd/system/cdnmnus-panel.service
  if [[ -f "$env_file" ]] && grep -q '^CDNMNUS_PANEL_PASSWORD=' "$env_file"; then
    password="$(sed -n 's/^CDNMNUS_PANEL_PASSWORD=//p' "$env_file")"
  else
    password="$(openssl rand -base64 24 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 24)"
    [[ -n "$password" ]] || die "Não foi possível gerar a credencial do painel."
    umask 077
    printf 'CDNMNUS_PANEL_USER=admin\nCDNMNUS_PANEL_PASSWORD=%s\n' "$password" > "$env_file"
    chmod 0600 "$env_file"
    log "Credencial inicial do painel (guarde-a agora): usuário=admin senha=$password"
  fi
  systemctl daemon-reload
  systemctl enable --now cdnmnus-panel.service
}

deploy_nginx() {
  (( DRY_RUN == 1 )) && return
  [[ -d /etc/nginx ]] || die "/etc/nginx não existe após a instalação do pacote."
  local backup="/etc/nginx/nginx.conf.bak.$(date +%Y%m%d%H%M%S)"
  if [[ -f /etc/nginx/nginx.conf ]]; then
    cp -a /etc/nginx/nginx.conf "$backup"
    log "Configuração anterior preservada em $backup."
  fi

  install -m 0644 "$RENDERED_CONFIG" /etc/nginx/nginx.conf
  if ! nginx -t; then
    if [[ -f "$backup" ]]; then
      install -m 0644 "$backup" /etc/nginx/nginx.conf
    fi
    die "nginx -t falhou; configuração anterior restaurada."
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable nginx >/dev/null 2>&1 || true
    systemctl restart nginx
  else
    service nginx restart
  fi
}

main() {
  parse_args "$@"
  interactive_menu
  set_defaults
  validate_inputs
  validate_os
  require_root
  read_environment
  confirm_plan

  if (( DRY_RUN == 1 )); then
    prepare_assets
    render_nginx_config
    run_tuning
    run_firewall
    log "Dry-run concluído: nenhum arquivo do sistema foi alterado."
    return 0
  fi

  prepare_assets
  install_packages
  render_nginx_config
  run_tuning
  deploy_nginx
  run_firewall
  install_panel

  log "Instalação concluída com sucesso. Nginx está encaminhando para http://$BACKEND_SERVER."
  log "Health check local: curl -i http://127.0.0.1/nginx-health"
}

main "$@"
