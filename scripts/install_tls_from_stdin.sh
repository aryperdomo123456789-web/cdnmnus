#!/usr/bin/env bash
set -Eeuo pipefail

cert_name=${1:?cert name required}
expected_fingerprint=${2:?fingerprint required}
[[ "$cert_name" =~ ^[A-Za-z0-9.-]+$ ]] || exit 2

stage=$(mktemp -d /var/tmp/cdnmnus-tls.XXXXXX)
trap 'find "$stage" -type f -exec shred -u {} + 2>/dev/null || true; rmdir "$stage" 2>/dev/null || true' EXIT
tar -xf - -C "$stage"
chmod 0600 "$stage/privkey.pem"
chmod 0644 "$stage/fullchain.pem"
actual_fingerprint=$(openssl x509 -in "$stage/fullchain.pem" -outform DER | sha256sum | cut -d' ' -f1)
test "$actual_fingerprint" = "$expected_fingerprint"
openssl x509 -in "$stage/fullchain.pem" -noout -checkend 86400 >/dev/null
key_hash=$(openssl pkey -in "$stage/privkey.pem" -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
cert_hash=$(openssl x509 -in "$stage/fullchain.pem" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
test "$key_hash" = "$cert_hash"

install -d -o root -g root -m 0700 "/etc/letsencrypt/live/$cert_name"
install -o root -g root -m 0644 "$stage/fullchain.pem" "/etc/letsencrypt/live/$cert_name/fullchain.pem.new"
install -o root -g root -m 0600 "$stage/privkey.pem" "/etc/letsencrypt/live/$cert_name/privkey.pem.new"
mv -f "/etc/letsencrypt/live/$cert_name/fullchain.pem.new" "/etc/letsencrypt/live/$cert_name/fullchain.pem"
mv -f "/etc/letsencrypt/live/$cert_name/privkey.pem.new" "/etc/letsencrypt/live/$cert_name/privkey.pem"
nginx -t >/dev/null
systemctl reload nginx
