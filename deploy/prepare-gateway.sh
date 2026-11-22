#!/usr/bin/env bash
# 生成带访问密码的待上线 HTTPS 配置，校验后保留为 pending 文件而不启用。
set -euo pipefail
umask 077
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
domain="${1:?Usage: prepare-gateway.sh DOMAIN}"
[[ "$domain" =~ ^[a-z0-9.-]+$ ]] || { echo 'Invalid domain' >&2; exit 1; }
[[ ! -e /etc/caddy/Caddyfile.pending ]] || { echo 'Pending config already exists' >&2; exit 1; }
password="$(openssl rand -base64 24 | tr '/+' '_-')"
password_hash="$(printf '%s\n' "$password" | caddy hash-password)"
sed -e "s/audio.example.com/$domain/" -e "s@REPLACE_WITH_CADDY_PASSWORD_HASH@$password_hash@" \
    "$bundle_dir/Caddyfile.example" > /etc/caddy/Caddyfile.pending
chown root:caddy /etc/caddy/Caddyfile.pending
chmod 0640 /etc/caddy/Caddyfile.pending
caddy fmt --overwrite /etc/caddy/Caddyfile.pending
caddy validate --config /etc/caddy/Caddyfile.pending --adapter caddyfile
printf 'Domain: https://%s\nUsername: demo\nPassword: %s\n' "$domain" "$password" > /root/ai-audio-gateway-access.txt
echo 'HTTPS configuration validated and staged; public gateway is not activated.'
