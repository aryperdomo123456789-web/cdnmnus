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
[[ -f install.sh && -f panel/panel.py && -f panel/cdnmnus-panel.service ]] || die 'arquivos essenciais ausentes'

stamp="$(date +%Y%m%d%H%M%S)"
backup="$BACKUP_ROOT/$stamp"
install -d -m 700 "$backup"
for path in /etc/nginx/nginx.conf /etc/nginx/conf.d/99-cdnmnus-upstream.conf /etc/cdnmnus/panel.env /etc/cdnmnus/panel.db; do
  [[ -e "$path" ]] && cp -a "$path" "$backup/"
done
log "backup criado em $backup"

python3 -m py_compile panel/panel.py
bash -n install.sh scripts/*.sh tests/*.sh
install -d -m 755 "$PANEL_DIR"
install -m 0755 panel/panel.py "$PANEL_DIR/panel.py"
install -m 0644 panel/cdnmnus-panel.service /etc/systemd/system/cdnmnus-panel.service
systemctl daemon-reload
systemctl restart cdnmnus-panel.service
nginx -t
systemctl reload nginx
systemctl is-active --quiet nginx || die 'Nginx nao ficou ativo'
systemctl is-active --quiet cdnmnus-panel.service || die 'painel nao ficou ativo'
log 'atualizacao concluida; Nginx e painel ativos'
