#!/usr/bin/env bash
set -euo pipefail
umask 077

LAB_DIR="${LAB_DIR:-/opt/cdnmnus/lab-player}"
CREDENTIALS_FILE="${PLAYER_CREDENTIALS_FILE:-/etc/cdnmnus/lab-player/xuilab.env}"

[[ -f "${CREDENTIALS_FILE}" ]] || { echo "Credenciais do xuilab ausentes." >&2; exit 1; }
[[ "$(stat -c '%u' "${CREDENTIALS_FILE}")" == "0" ]] || { echo "Credenciais devem pertencer a root." >&2; exit 1; }
[[ "$(stat -c '%a' "${CREDENTIALS_FILE}")" == "600" ]] || { echo "Credenciais devem ter modo 0600." >&2; exit 1; }

# This file is local operator configuration, never a repository artifact.
# shellcheck disable=SC1090
source "${CREDENTIALS_FILE}"
: "${PLAYER_USERNAME:?PLAYER_USERNAME ausente}"
: "${PLAYER_PASSWORD:?PLAYER_PASSWORD ausente}"
: "${PLAYER_BASE_DIRECT:?PLAYER_BASE_DIRECT ausente}"
: "${PLAYER_BASE_CNAME:?PLAYER_BASE_CNAME ausente}"
: "${PLAYER_BASE_CDN:?PLAYER_BASE_CDN ausente}"

# Export only the validated values to the Python subprocess.
export PLAYER_USERNAME PLAYER_PASSWORD PLAYER_BASE_DIRECT PLAYER_BASE_CNAME PLAYER_BASE_CDN

export LAB_DIR PLAYER_CREDENTIALS_FILE
exec python3 "${LAB_DIR}/scripts/test_playback_flow.py" --cname --refresh-samples
