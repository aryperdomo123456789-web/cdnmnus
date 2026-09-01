#!/usr/bin/env bash
set -euo pipefail
umask 077

LAB_DIR="${LAB_DIR:-/opt/cdnmnus/lab-player}"
DATE="$(date +%Y%m%d_%H%M%S)"

CDN_URL="${CDN_URL:-}"
DIRECT_URL="${DIRECT_URL:-}"
PLAYER_USERNAME="${PLAYER_USERNAME:-}"
PLAYER_PASSWORD="${PLAYER_PASSWORD:-}"
PLAYER_BASE_CDN="${PLAYER_BASE_CDN:-}"
PLAYER_BASE_DIRECT="${PLAYER_BASE_DIRECT:-}"
PLAYER_BASE_CNAME="${PLAYER_BASE_CNAME:-}"
PLAYER_BASE_CNAME_ALIASES="${PLAYER_BASE_CNAME_ALIASES:-}"
USER_AGENT="${USER_AGENT:-XCIPTV / Android 12 / OkHttp/4.9.3}"

if [[ -z "${CDN_URL}" && -n "${PLAYER_BASE_CDN}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  CDN_URL="${PLAYER_BASE_CDN%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi
if [[ -z "${DIRECT_URL}" && -n "${PLAYER_BASE_DIRECT}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  DIRECT_URL="${PLAYER_BASE_DIRECT%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi
if [[ -n "${PLAYER_BASE_CNAME}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  CNAME_URL="${PLAYER_BASE_CNAME%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi

if [[ -z "${CNAME_URL:-}" && ( -z "${CDN_URL}" || -z "${DIRECT_URL}" ) ]]; then
  echo "Defina CDN_URL/DIRECT_URL, CNAME_URL ou as bases correspondentes com PLAYER_USERNAME/PLAYER_PASSWORD." >&2
  exit 1
fi

mkdir -p "${LAB_DIR}/playlists" "${LAB_DIR}/reports"

redact_url() {
  printf '%s' "$1" | sed -E \
    -e 's/([?&](username|user|password|pass|token|auth|api_key|apikey)=)[^&]*/\1[REDACTED]/g' \
    -e 's#/(movie|series)/[^/]+/[^/]+/#/\1/[REDACTED]/[REDACTED]/#g' \
    -e 's#(https?://[^/]+/)[^/]+/[^/]+/([^/]+\.m3u8)#\1[REDACTED]/[REDACTED]/\2#'
}

if [[ -n "${CDN_URL}" ]]; then
  echo "[*] Baixando playlist via CDN..."
  curl -fsSL -A "${USER_AGENT}" "${CDN_URL}" -o "${LAB_DIR}/playlists/cdn_${DATE}.m3u8"
  ln -sfn "${LAB_DIR}/playlists/cdn_${DATE}.m3u8" "${LAB_DIR}/playlists/cdn_latest.m3u8"
fi

if [[ -n "${DIRECT_URL}" ]]; then
  echo "[*] Baixando playlist via IP direto..."
  curl -fsSL -A "${USER_AGENT}" "${DIRECT_URL}" -o "${LAB_DIR}/playlists/direct_${DATE}.m3u8"
  ln -sfn "${LAB_DIR}/playlists/direct_${DATE}.m3u8" "${LAB_DIR}/playlists/direct_latest.m3u8"
fi

if [[ -n "${CNAME_URL:-}" ]]; then
  echo "[*] Baixando playlist via CNAME DNS-only..."
  curl -fsSL -A "${USER_AGENT}" "${CNAME_URL}" -o "${LAB_DIR}/playlists/cname_${DATE}.m3u8"
  ln -sfn "${LAB_DIR}/playlists/cname_${DATE}.m3u8" "${LAB_DIR}/playlists/cname_latest.m3u8"
fi

alias_index=0
IFS=',' read -ra cname_aliases <<< "${PLAYER_BASE_CNAME_ALIASES}"
for alias in "${cname_aliases[@]}"; do
  alias="${alias%/}"
  [[ -z "${alias}" ]] && continue
  alias_index=$((alias_index + 1))
  alias_url="${alias}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
  echo "[*] Baixando playlist via CNAME adicional ${alias}..."
  curl -fsSL -A "${USER_AGENT}" "${alias_url}" -o "${LAB_DIR}/playlists/cname_alias${alias_index}_${DATE}.m3u8"
done

cat > "${LAB_DIR}/reports/sync_${DATE}.txt" <<EOF
cdn=$(redact_url "${CDN_URL}")
direct=$(redact_url "${DIRECT_URL}")
cname=$(redact_url "${CNAME_URL:-disabled}")
aliases=$(redact_url "${PLAYER_BASE_CNAME_ALIASES:-disabled}")
user_agent=${USER_AGENT}
timestamp=${DATE}
EOF

echo "[+] Playlists sincronizadas em ${LAB_DIR}/playlists/"
