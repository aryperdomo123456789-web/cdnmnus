#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly REPO_URL="https://github.com/aryperdomo123456789-web/cdnmnus.git"
REF=""; EXPECTED_COMMIT=""; MANIFEST_DIGEST=""; ROLE=""; NODE_ID=""; NODE_NAME=""; CONTROL_PLANE=""
TMP_DIR=""
die() { printf '[cdnmnus-managed][erro] %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -z "$TMP_DIR" || ! -d "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2;;
    --expected-commit) EXPECTED_COMMIT="${2:-}"; shift 2;;
    --manifest-digest) MANIFEST_DIGEST="${2:-}"; shift 2;;
    --role) ROLE="${2:-}"; shift 2;;
    --node-id) NODE_ID="${2:-}"; shift 2;;
    --node-name) NODE_NAME="${2:-}"; shift 2;;
    --control-plane) CONTROL_PLANE="${2:-}"; shift 2;;
    *) die "opção desconhecida: $1";;
  esac
done
[[ $EUID -eq 0 ]] || die 'execute como root'
[[ "$REF" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || die 'tag imutável obrigatória'
[[ "$EXPECTED_COMMIT" =~ ^[a-f0-9]{40}$ ]] || die 'commit esperado inválido'
[[ "$MANIFEST_DIGEST" =~ ^[a-f0-9]{64}$ ]] || die 'digest do manifesto inválido'
command -v git >/dev/null || die 'git é obrigatório'
TMP_DIR="$(mktemp -d /tmp/cdnmnus-managed.XXXXXX)"
git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$TMP_DIR/source"
actual_commit="$(git -C "$TMP_DIR/source" rev-parse HEAD)"
[[ "$actual_commit" == "$EXPECTED_COMMIT" ]] || die 'commit obtido diverge do autorizado'
"$TMP_DIR/source/node-package/install.sh" \
  --role "$ROLE" --node-id "$NODE_ID" --node-name "$NODE_NAME" \
  --control-plane "$CONTROL_PLANE" --source-ref "$REF" \
  --source-commit "$EXPECTED_COMMIT" --manifest-digest "$MANIFEST_DIGEST"
