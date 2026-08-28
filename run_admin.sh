#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

usage() {
  printf '%s\n' \
    'Uso:' \
    '  CDNMNUS_ADMIN_PASSWORD=senha ./run_admin.sh web [--bind 0.0.0.0] [--port 8080]' \
    '  ./run_admin.sh cli edge list' \
    '  ./run_admin.sh cli tenant add' \
    '  ./run_admin.sh cli config web-port 8443'
}

mode="${1:-web}"
case "$mode" in
  web)
    shift || true
    exec python3 "$ROOT_DIR/web/app.py" "$@"
    ;;
  cli)
    shift
    exec python3 "$ROOT_DIR/cli/admin_cli.py" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    printf 'Modo inválido: %s\n' "$mode" >&2
    usage >&2
    exit 2
    ;;
esac

