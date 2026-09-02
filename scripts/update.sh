#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

REPO_DIR="/opt/cdnmnus"
PANEL_DIR="/opt/cdnmnus-panel"
BACKUP_ROOT="/var/backups/cdnmnus"
DRY_RUN=0
ALLOW_DIRTY=0
REF="main"

log() { printf '[cdnmnus-update] %s\n' "$*"; }
die() { printf '[cdnmnus-update][erro] %s\n' "$*" >&2; exit 1; }
usage() { printf '%s\n' 'Uso: sudo scripts/update.sh [--dry-run] [--allow-dirty] [--ref main]'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --ref) [[ $# -ge 2 ]] || die '--ref exige valor'; REF="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "opcao desconhecida: $1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die 'execute como root'
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die 'ref invalida'
command -v git >/dev/null || die 'git nao instalado'
command -v python3 >/dev/null || die 'python3 nao instalado'

if [[ ! -d "$REPO_DIR/.git" ]]; then
  (( DRY_RUN == 1 )) && { log "dry-run: clonaria o repositorio em $REPO_DIR"; exit 0; }
  git clone --branch "$REF" --single-branch https://github.com/aryperdomo123456789-web/cdnmnus.git "$REPO_DIR"
fi
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" && "$ALLOW_DIRTY" -ne 1 ]]; then
  die 'worktree sujo; commit/guarde as alteracoes ou use --allow-dirty conscientemente'
fi

if (( DRY_RUN == 1 )); then
  log "dry-run: fetch fast-forward de origin/$REF"
  log 'dry-run: validaria scripts, painel e configuracao Nginx'
  exit 0
fi

git fetch --prune origin "$REF"
git merge --ff-only "origin/$REF"
[[ -f install.sh && -f panel/panel.py && -f panel/token_broker.py && -f scripts/sanitized_monitor.py && -f scripts/edge_health_controller.py && -f scripts/soak_test.py && -f scripts/media_validation.py && -f panel/cdnmnus-panel.service && -f panel/cdnmnus-token-broker.service && -f panel/cdnmnus-monitor.service && -f panel/cdnmnus-monitor.timer && -f panel/cdnmnus-edge-health.service && -f panel/cdnmnus-edge-health.timer && -f panel/cdnmnus-soak@.service ]] || die 'arquivos essenciais ausentes'

stamp="$(date +%Y%m%d%H%M%S)"
backup="$BACKUP_ROOT/$stamp"
install -d -m 700 "$backup"
for path in /etc/nginx/nginx.conf /etc/nginx/conf.d/99-cdnmnus-upstream.conf /etc/cdnmnus/panel.env /etc/cdnmnus/panel.db /etc/cdnmnus/token-broker.json; do
  [[ -e "$path" ]] && cp -a "$path" "$backup/"
done
log "backup criado em $backup"

python3 -m py_compile panel/panel.py
python3 -m py_compile panel/token_broker.py
bash -n install.sh scripts/*.sh tests/*.sh
install -d -m 755 "$PANEL_DIR"
install -d -o www-data -g www-data -m 0750 /var/cache/nginx/cdnmnus-hls
install -m 0755 panel/panel.py "$PANEL_DIR/panel.py"
install -m 0755 panel/token_broker.py "$PANEL_DIR/token_broker.py"
install -m 0755 scripts/sanitized_monitor.py "$PANEL_DIR/sanitized_monitor.py"
install -m 0755 scripts/edge_health_controller.py "$PANEL_DIR/edge_health_controller.py"
install -m 0755 scripts/soak_test.py "$PANEL_DIR/soak_test.py"
install -m 0755 scripts/media_validation.py "$PANEL_DIR/media_validation.py"
install -m 0644 panel/cdnmnus-panel.service /etc/systemd/system/cdnmnus-panel.service
install -m 0644 panel/cdnmnus-token-broker.service /etc/systemd/system/cdnmnus-token-broker.service
install -m 0644 panel/cdnmnus-monitor.service /etc/systemd/system/cdnmnus-monitor.service
install -m 0644 panel/cdnmnus-monitor.timer /etc/systemd/system/cdnmnus-monitor.timer
install -m 0644 panel/cdnmnus-edge-health.service /etc/systemd/system/cdnmnus-edge-health.service
install -m 0644 panel/cdnmnus-edge-health.timer /etc/systemd/system/cdnmnus-edge-health.timer
install -m 0644 panel/cdnmnus-soak@.service /etc/systemd/system/cdnmnus-soak@.service
systemctl daemon-reload
systemctl restart cdnmnus-panel.service
systemctl enable cdnmnus-token-broker.service >/dev/null 2>&1 || true
systemctl enable --now cdnmnus-monitor.timer
systemctl enable --now cdnmnus-edge-health.timer
[[ -f /etc/cdnmnus/token-broker.json ]] && systemctl restart cdnmnus-token-broker.service
nginx -t
systemctl reload nginx
systemctl is-active --quiet nginx || die 'Nginx nao ficou ativo'
systemctl is-active --quiet cdnmnus-panel.service || die 'painel nao ficou ativo'
[[ ! -f /etc/cdnmnus/token-broker.json ]] || systemctl is-active --quiet cdnmnus-token-broker.service || die 'token broker nao ficou ativo'
log 'atualizacao concluida; Nginx e painel ativos'
