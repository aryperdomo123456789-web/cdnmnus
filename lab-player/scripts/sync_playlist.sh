#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="${LAB_DIR:-/opt/cdnmnus/lab-player}"
DATE="$(date +%Y%m%d_%H%M%S)"

CDN_URL="${CDN_URL:-}"
DIRECT_URL="${DIRECT_URL:-}"
PLAYER_USERNAME="${PLAYER_USERNAME:-}"
PLAYER_PASSWORD="${PLAYER_PASSWORD:-}"
PLAYER_BASE_CDN="${PLAYER_BASE_CDN:-}"
PLAYER_BASE_DIRECT="${PLAYER_BASE_DIRECT:-}"
USER_AGENT="${USER_AGENT:-XCIPTV / Android 12 / OkHttp/4.9.3}"

if [[ -z "${CDN_URL}" && -n "${PLAYER_BASE_CDN}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  CDN_URL="${PLAYER_BASE_CDN%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi
if [[ -z "${DIRECT_URL}" && -n "${PLAYER_BASE_DIRECT}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  DIRECT_URL="${PLAYER_BASE_DIRECT%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi

if [[ -z "${CDN_URL}" || -z "${DIRECT_URL}" ]]; then
  echo "Defina CDN_URL/DIRECT_URL ou PLAYER_BASE_CDN/PLAYER_BASE_DIRECT + PLAYER_USERNAME/PLAYER_PASSWORD." >&2
  exit 1
fi

mkdir -p "${LAB_DIR}/playlists" "${LAB_DIR}/reports"

echo "[*] Baixando playlist via CDN..."
curl -fsSL -A "${USER_AGENT}" "${CDN_URL}" -o "${LAB_DIR}/playlists/cdn_${DATE}.m3u8"
ln -sfn "${LAB_DIR}/playlists/cdn_${DATE}.m3u8" "${LAB_DIR}/playlists/cdn_latest.m3u8"

echo "[*] Baixando playlist via IP direto..."
curl -fsSL -A "${USER_AGENT}" "${DIRECT_URL}" -o "${LAB_DIR}/playlists/direct_${DATE}.m3u8"
ln -sfn "${LAB_DIR}/playlists/direct_${DATE}.m3u8" "${LAB_DIR}/playlists/direct_latest.m3u8"

cat > "${LAB_DIR}/reports/sync_${DATE}.txt" <<EOF
cdn=${CDN_URL}
direct=${DIRECT_URL}
user_agent=${USER_AGENT}
timestamp=${DATE}
EOF

echo "[+] Playlists sincronizadas em ${LAB_DIR}/playlists/"
