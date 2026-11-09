#!/bin/bash
# The groups server as the «تعليقات» app's way to Claude, for users whose home VPN Claude refuses: a small proxy
# (tinyproxy) with a username and a password, that lets through only Claude's address (api.anthropic.com, port
# 443). The connection stays encrypted from the phone to Claude, so this server cannot read the users' keys.
#
# On the groups server, as root:
#   curl -fsSL https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr/comments-app/proxy-setup.sh | bash
#
# It prints the proxy's link, to paste in the app's «الإعدادات» ← «البروكسي». Run again, it keeps the same password
# (the link already given stays good). PROXY_PORT=… chooses another port (8899 by default).
set -eu

PORT="${PROXY_PORT:-8899}"
NAME="tg"
CONF=/etc/tinyproxy/tinyproxy.conf
ONLY=/etc/tinyproxy/claude-only

if [ "$(id -u)" != "0" ]; then
    echo "شغّل الأمر بحساب root (أو أضف sudo قبل bash)." >&2
    exit 1
fi
if ! command -v tinyproxy >/dev/null 2>&1; then
    echo "تثبيت tinyproxy…"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tinyproxy >/dev/null
fi

# The password kept from a run before, or a new one.
PASS="$(sed -n "s/^BasicAuth $NAME \([A-Za-z0-9]\{16,64\}\)$/\1/p" "$CONF" 2>/dev/null | head -n 1 || true)"
if [ -z "$PASS" ]; then
    PASS="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
fi

mkdir -p /etc/tinyproxy
cat > "$CONF" <<EOF
# Written by proxy-setup.sh (the «تعليقات» app): only Claude's address, with a username and a password.
User tinyproxy
Group tinyproxy
Port $PORT
Timeout 120
MaxClients 64
LogFile "/var/log/tinyproxy/tinyproxy.log"
LogLevel Warning
PidFile "/run/tinyproxy/tinyproxy.pid"
DisableViaHeader Yes
BasicAuth $NAME $PASS
ConnectPort 443
FilterType ere
FilterDefaultDeny Yes
Filter "$ONLY"
EOF
printf '%s\n' '^api\.anthropic\.com$' > "$ONLY"
chmod 640 "$CONF"
chgrp tinyproxy "$CONF" 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl enable tinyproxy >/dev/null 2>&1 || true
    systemctl restart tinyproxy
else
    service tinyproxy restart >/dev/null
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null
fi

# A check from the server itself: through the proxy to Claude, with a made-up key (Claude answers 401, free).
sleep 1
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --noproxy '' -x "http://$NAME:$PASS@127.0.0.1:$PORT" \
    https://api.anthropic.com/v1/messages -H 'x-api-key: sk-ant-check' -H 'anthropic-version: 2026-10-21' \
    -H 'content-type: application/json' -d '{}' 2>/dev/null || true)"
if [ "$CODE" = "401" ]; then
    echo "✓ البروكسي يعمل ويصل إلى Claude."
else
    echo "⚠️ البروكسي لم يصل إلى Claude (الرمز: ${CODE:-لا رد}). راجع السجل: /var/log/tinyproxy/tinyproxy.log" >&2
fi

IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
if [ -z "$IP" ]; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
echo
echo "رابط البروكسي، يُلصق في التطبيق: «الإعدادات» ← «البروكسي»:"
echo "http://$NAME:$PASS@$IP:$PORT"
