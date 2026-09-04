#!/usr/bin/env bash
set -euo pipefail
umask 077

LAB_DIR="${LAB_DIR:-/opt/cdnmnus/lab-player}"
DATE="$(date +%Y%m%d_%H%M%S)"

# Credential loading is opt-in and restricted to a root-only file.
PLAYER_CREDENTIALS_FILE="${PLAYER_CREDENTIALS_FILE:-}"
_player_username_set="${PLAYER_USERNAME+x}"
_player_username_value="${PLAYER_USERNAME-}"
_player_password_set="${PLAYER_PASSWORD+x}"
_player_password_value="${PLAYER_PASSWORD-}"
_player_base_cdn_set="${PLAYER_BASE_CDN+x}"
_player_base_cdn_value="${PLAYER_BASE_CDN-}"
_player_base_direct_set="${PLAYER_BASE_DIRECT+x}"
_player_base_direct_value="${PLAYER_BASE_DIRECT-}"
_player_base_cname_set="${PLAYER_BASE_CNAME+x}"
_player_base_cname_value="${PLAYER_BASE_CNAME-}"
_player_skip_cname_set="${PLAYER_SKIP_CNAME+x}"
_player_skip_cname_value="${PLAYER_SKIP_CNAME-}"
if [[ -n "${PLAYER_CREDENTIALS_FILE}" ]]; then
  [[ -f "${PLAYER_CREDENTIALS_FILE}" ]] || { echo "Arquivo de credenciais ausente." >&2; exit 1; }
  [[ "$(stat -c '%u' "${PLAYER_CREDENTIALS_FILE}")" == "0" &&
     "$(stat -c '%a' "${PLAYER_CREDENTIALS_FILE}")" == "600" ]] || {
    echo "Arquivo de credenciais deve pertencer a root e ter modo 0600." >&2
    exit 1
  }
  # shellcheck disable=SC1090
  source "${PLAYER_CREDENTIALS_FILE}"
fi
# Explicit process environment always wins over the local credential file.
[[ "${_player_username_set}" == x ]] && PLAYER_USERNAME="${_player_username_value}"
[[ "${_player_password_set}" == x ]] && PLAYER_PASSWORD="${_player_password_value}"
[[ "${_player_base_cdn_set}" == x ]] && PLAYER_BASE_CDN="${_player_base_cdn_value}"
[[ "${_player_base_direct_set}" == x ]] && PLAYER_BASE_DIRECT="${_player_base_direct_value}"
[[ "${_player_base_cname_set}" == x ]] && PLAYER_BASE_CNAME="${_player_base_cname_value}"
[[ "${_player_skip_cname_set}" == x ]] && PLAYER_SKIP_CNAME="${_player_skip_cname_value}"

CDN_URL="${CDN_URL:-}"
DIRECT_URL="${DIRECT_URL:-}"
PLAYER_USERNAME="${PLAYER_USERNAME:-}"
PLAYER_PASSWORD="${PLAYER_PASSWORD:-}"
PLAYER_BASE_CDN="${PLAYER_BASE_CDN:-}"
PLAYER_BASE_DIRECT="${PLAYER_BASE_DIRECT:-}"
PLAYER_BASE_CNAME="${PLAYER_BASE_CNAME:-}"
PLAYER_BASE_CNAME_ALIASES="${PLAYER_BASE_CNAME_ALIASES:-}"
PLAYER_SKIP_CNAME="${PLAYER_SKIP_CNAME:-0}"
USER_AGENT="${USER_AGENT:-XCIPTV / Android 12 / OkHttp/4.9.3}"
PLAYER_CURL_TIMEOUT="${PLAYER_CURL_TIMEOUT:-30}"
PLAYER_CURL_CONNECT_TIMEOUT="${PLAYER_CURL_CONNECT_TIMEOUT:-8}"
# Request compression like the player does, while saving the decompressed M3U
# so the local playback checks exercise the playlist contents, not gzip bytes.
CURL_PLAYLIST_OPTS=(--compressed --connect-timeout "${PLAYER_CURL_CONNECT_TIMEOUT}" --max-time "${PLAYER_CURL_TIMEOUT}")

if [[ -z "${CDN_URL}" && -n "${PLAYER_BASE_CDN}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  CDN_URL="${PLAYER_BASE_CDN%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi
if [[ -z "${DIRECT_URL}" && -n "${PLAYER_BASE_DIRECT}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
  DIRECT_URL="${PLAYER_BASE_DIRECT%/}/get.php?username=${PLAYER_USERNAME}&password=${PLAYER_PASSWORD}&type=m3u_plus&output=hls"
fi
if [[ "${PLAYER_SKIP_CNAME}" != "1" && -n "${PLAYER_BASE_CNAME}" && -n "${PLAYER_USERNAME}" && -n "${PLAYER_PASSWORD}" ]]; then
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

fetch_playlist() {
  local target="$1" output="$2"
  # Keep credentials out of argv and /proc/<pid>/cmdline. curl reads the URL
  # from its private stdin configuration instead.
  printf 'url = "%s"\n' "${target}" | curl --config - -fsSL "${CURL_PLAYLIST_OPTS[@]}" -A "${USER_AGENT}" -o "${output}"
}

if [[ -n "${CDN_URL}" ]]; then
  echo "[*] Baixando playlist via CDN..."
  fetch_playlist "${CDN_URL}" "${LAB_DIR}/playlists/cdn_${DATE}.m3u8"
  ln -sfn "${LAB_DIR}/playlists/cdn_${DATE}.m3u8" "${LAB_DIR}/playlists/cdn_latest.m3u8"
fi

if [[ -n "${DIRECT_URL}" ]]; then
  echo "[*] Baixando playlist via IP direto..."
  fetch_playlist "${DIRECT_URL}" "${LAB_DIR}/playlists/direct_${DATE}.m3u8"
  ln -sfn "${LAB_DIR}/playlists/direct_${DATE}.m3u8" "${LAB_DIR}/playlists/direct_latest.m3u8"
fi

if [[ -n "${CNAME_URL:-}" ]]; then
  echo "[*] Baixando playlist via CNAME DNS-only..."
  fetch_playlist "${CNAME_URL}" "${LAB_DIR}/playlists/cname_${DATE}.m3u8"
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
  fetch_playlist "${alias_url}" "${LAB_DIR}/playlists/cname_alias${alias_index}_${DATE}.m3u8"
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
