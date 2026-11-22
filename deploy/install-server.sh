#!/usr/bin/env bash
# 安装经校验的 frps 与 Caddy，先保持网关只监听回环地址以便内部验收。
set -euo pipefail
umask 077
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
server_ip="${1:?Usage: install-server.sh SERVER_IP}"
[[ "$server_ip" =~ ^[0-9.]+$ ]] || { echo 'Invalid server IPv4 address' >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo 'Root is required' >&2; exit 1; }
[[ ! -e /etc/frp/frps.toml && ! -e /etc/caddy/Caddyfile ]] || {
    echo 'Existing deployment found; refusing to overwrite configuration' >&2
    exit 1
}

download_dir="$(mktemp -d /tmp/ai-audio-install.XXXXXX)"
if [[ -f "$bundle_dir/frp.tar.gz" ]]; then
    cp "$bundle_dir/frp.tar.gz" "$download_dir/frp.tar.gz"
else
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 --max-time 180 \
        https://github.com/fatedier/frp/releases/download/v0.72.0/frp_0.72.0_linux_amd64.tar.gz \
        -o "$download_dir/frp.tar.gz"
fi
if [[ -f "$bundle_dir/caddy.tar.gz" ]]; then
    cp "$bundle_dir/caddy.tar.gz" "$download_dir/caddy.tar.gz"
else
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 --max-time 180 \
        https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_linux_amd64.tar.gz \
        -o "$download_dir/caddy.tar.gz"
fi
printf '%s  %s\n' daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804 "$download_dir/frp.tar.gz" | sha256sum --check --status
printf '%s  %s\n' 5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb "$download_dir/caddy.tar.gz" | sha256sum --check --status
tar -xzf "$download_dir/frp.tar.gz" -C "$download_dir"
tar -xzf "$download_dir/caddy.tar.gz" -C "$download_dir" caddy
install -m 0755 "$download_dir/frp_0.72.0_linux_amd64/frps" /usr/local/bin/frps
install -m 0755 "$download_dir/caddy" /usr/local/bin/caddy
id frp >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin frp
id caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
install -d -m 0750 -o root -g frp /etc/frp
install -d -m 0750 -o root -g caddy /etc/caddy
install -d -m 0750 -o caddy -g caddy /var/lib/caddy

openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 1825 \
    -subj '/CN=taiger-frp-ca' -keyout /etc/frp/ca.key -out /etc/frp/ca.crt 2>/dev/null
openssl req -new -newkey rsa:3072 -nodes -sha256 \
    -subj "/CN=$server_ip" -keyout /etc/frp/server.key -out /etc/frp/server.csr 2>/dev/null
printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$server_ip" > /etc/frp/server.ext
openssl x509 -req -in /etc/frp/server.csr -CA /etc/frp/ca.crt -CAkey /etc/frp/ca.key \
    -CAcreateserial -days 825 -sha256 -extfile /etc/frp/server.ext -out /etc/frp/server.crt 2>/dev/null
frp_token="$(openssl rand -hex 32)"
sed "s/REPLACE_WITH_RANDOM_TOKEN/$frp_token/" "$bundle_dir/frps.toml.example" > /etc/frp/frps.toml
printf '%s' "$frp_token" > /etc/frp/client-token
chown root:frp /etc/frp/frps.toml /etc/frp/server.key /etc/frp/server.crt /etc/frp/ca.crt
chmod 0640 /etc/frp/frps.toml /etc/frp/server.key /etc/frp/server.crt /etc/frp/ca.crt

# 公开网站仍未启用；此入口仅供 SSH 内部验收使用。
install -m 0640 -o root -g caddy "$bundle_dir/Caddyfile.internal" /etc/caddy/Caddyfile
install -m 0644 "$bundle_dir/frps.service" /etc/systemd/system/frps.service
install -m 0644 "$bundle_dir/caddy.service" /etc/systemd/system/caddy.service
frps verify -c /etc/frp/frps.toml
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable --now frps caddy
frps --version
caddy version
systemctl is-active frps caddy
