#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly REPO_URL="https://github.com/aryperdomo123456789-web/cdnmnus.git"
REPO_DIR="/opt/cdnmnus"
REF="main"

log() { printf '[cdnmnus-bootstrap] %s\n' "$*"; }
die() { printf '[cdnmnus-bootstrap][erro] %s\n' "$*" >&2; exit 1; }

usage() { printf '%s\n' 'Uso: sudo ./install-from-github.sh [--dir /opt/cdnmnus] [--ref main] -- [opcoes do install.sh]'; }

INSTALL_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) [[ $# -ge 2 ]] || die '--dir exige valor'; REPO_DIR="$2"; shift 2 ;;
    --ref) [[ $# -ge 2 ]] || die '--ref exige valor'; REF="$2"; shift 2 ;;
    --) shift; INSTALL_ARGS=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) die "opcao desconhecida: $1; use -- para opcoes do install.sh" ;;
  esac
done

[[ "$EUID" -eq 0 ]] || die 'execute como root'
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die 'ref invalida'
command -v git >/dev/null || die 'git e obrigatorio; instale git e execute novamente'
if [[ -e "$REPO_DIR" && ! -d "$REPO_DIR/.git" ]]; then
  die "$REPO_DIR existe e nao e um clone Git"
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
  log "clonando repositorio oficial em $REPO_DIR"
  git clone --branch "$REF" --single-branch "$REPO_URL" "$REPO_DIR"
else
  cd "$REPO_DIR"
  [[ "$(git remote get-url origin)" == "$REPO_URL" ]] || die 'origin nao corresponde ao repositorio oficial'
  [[ -z "$(git status --porcelain)" ]] || die 'worktree sujo; preserve as alteracoes antes de atualizar'
  git fetch --prune origin "$REF"
  git merge --ff-only "origin/$REF"
fi

cd "$REPO_DIR"
chmod +x install.sh scripts/*.sh tests/*.sh
log "codigo pronto em $REPO_DIR; iniciando instalador principal"
exec ./install.sh "${INSTALL_ARGS[@]}"
