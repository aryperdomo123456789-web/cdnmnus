#!/usr/bin/env bash
set -Eeuo pipefail

# Certbot deploy hook. Configuration (0600), one edge per line:
# name|host|port|user|identity_file|known_hosts_file
config_file=${CDNMNUS_TLS_EDGES_CONFIG:-/etc/cdnmnus/tls-edges.conf}
lineage=${RENEWED_LINEAGE:-${1:-}}

if [[ -z "$lineage" || ! -r "$lineage/fullchain.pem" || ! -r "$lineage/privkey.pem" ]]; then
    echo "lineage TLS ausente ou ilegivel" >&2
    exit 2
fi
if [[ ! -r "$config_file" ]] || [[ $(stat -c '%a' "$config_file") != 600 ]]; then
    echo "configuracao de edges deve existir em modo 0600" >&2
    exit 2
fi

cert_name=$(basename "$lineage")
local_fingerprint=$(openssl x509 -in "$lineage/fullchain.pem" -outform DER | sha256sum | cut -d' ' -f1)
failures=0

while IFS='|' read -r edge_name host port user identity known_hosts; do
    [[ -z "$edge_name" || "$edge_name" == \#* ]] && continue
    if [[ -z "$host" || -z "$port" || -z "$user" || ! -r "$identity" || ! -r "$known_hosts" ]]; then
        echo "edge=$edge_name status=config_error" >&2
        failures=$((failures + 1))
        continue
    fi
    ssh_opts=(-i "$identity" -p "$port" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts")
    if tar -h -C "$lineage" -cf - fullchain.pem privkey.pem | \
        ssh "${ssh_opts[@]}" "$user@$host" sudo -n /usr/local/sbin/cdnmnus-ansible-become \
            /usr/local/sbin/cdnmnus-install-tls "$cert_name" "$local_fingerprint"
    then
        echo "edge=$edge_name status=updated fingerprint=$local_fingerprint"
    else
        echo "edge=$edge_name status=failed" >&2
        failures=$((failures + 1))
    fi
done < "$config_file"

exit "$failures"
