#!/usr/bin/env bash
# SilentConnect one-shot node provisioning script.
#
# Bootstraps a bare Ubuntu/Debian VPS into a fully working SilentConnect node:
# 3x-ui (client registry only), Xray-core (DE-style multi-SNI REALITY, no
# dokodemo-door multiplexer), standalone hysteria-server, Caddy+Let's Encrypt,
# subjson-service — with fresh REALITY keys generated and a per-server .env
# written automatically. No manual file copying.
#
# Prerequisite: DNS for --domain must already point at this server's IP
# before running (Caddy/certbot need this to issue a cert).
#
# Usage:
#   ./provision_silentconnect_node.sh --domain fi2.silentconnect.net \
#       --sni-classic sber.ru --sni-fast st.kinopoisk.ru --sni-grpc vk.com \
#       --subjson-repo git@github.com:youruser/subjson-service.git
#
# Re-running is safe for the install steps (idempotent package/binary
# installs) but will NOT regenerate keys/certs that already exist, so it's
# safe to re-run after fixing a failed step partway through.

set -euo pipefail

# ---------- defaults ----------
SNI_CLASSIC="sber.ru"
SNI_FAST="st.kinopoisk.ru"
SNI_GRPC="vk.com"
TCP_PORT=443
GRPC_PORT=29443
XHTTP_PORT=28443
HYSTERIA_PORT=1443
REGISTRY_PORT=23385
CADDY_HTTPS_PORT=4430
SUBJSON_PORT=3088
SUBJSON_REPO=""
DOMAIN=""

# ---------- args ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --sni-classic) SNI_CLASSIC="$2"; shift 2 ;;
    --sni-fast) SNI_FAST="$2"; shift 2 ;;
    --sni-grpc) SNI_GRPC="$2"; shift 2 ;;
    --subjson-repo) SUBJSON_REPO="$2"; shift 2 ;;
    --tcp-port) TCP_PORT="$2"; shift 2 ;;
    --grpc-port) GRPC_PORT="$2"; shift 2 ;;
    --xhttp-port) XHTTP_PORT="$2"; shift 2 ;;
    --hysteria-port) HYSTERIA_PORT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  echo "ERROR: --domain is required (must already resolve to this server's IP)" >&2
  exit 1
fi
if [[ -z "$SUBJSON_REPO" ]]; then
  echo "ERROR: --subjson-repo is required (git URL for your subjson-service code)" >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

STATE_DIR="/root/.silentconnect-provision"
mkdir -p "$STATE_DIR"
log() { echo -e "\n\033[1;32m==>\033[0m $*"; }

# ---------- 1. base packages ----------
log "Installing base packages"
apt-get update -qq
apt-get install -y -qq curl wget git sqlite3 python3 python3-venv jq uuid-runtime >/dev/null

# ---------- 2. Xray-core binary (standalone, for key generation + inbounds) ----------
log "Installing Xray-core"
if [[ ! -x /usr/local/bin/xray ]]; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi
XRAY_BIN="$(command -v xray || echo /usr/local/bin/xray)"

# ---------- 3. Generate REALITY keys (idempotent: reuse if already generated) ----------
KEYS_FILE="$STATE_DIR/reality_keys.env"
if [[ -f "$KEYS_FILE" ]]; then
  log "REALITY keys already generated earlier, reusing $KEYS_FILE"
  source "$KEYS_FILE"
else
  log "Generating fresh REALITY keypairs (TCP shared, gRPC separate)"
  TCP_KEYPAIR="$("$XRAY_BIN" x25519)"
  TCP_PRIVATE_KEY="$(echo "$TCP_KEYPAIR" | grep -i "PrivateKey" | awk '{print $2}')"
  TCP_REALITY_PUBLIC_KEY="$(echo "$TCP_KEYPAIR" | grep -i "Password\|PublicKey" | awk '{print $NF}')"
  GRPC_KEYPAIR="$("$XRAY_BIN" x25519)"
  GRPC_PRIVATE_KEY="$(echo "$GRPC_KEYPAIR" | grep -i "PrivateKey" | awk '{print $2}')"
  GRPC_REALITY_PUBLIC_KEY="$(echo "$GRPC_KEYPAIR" | grep -i "Password\|PublicKey" | awk '{print $NF}')"
  TCP_REALITY_SHORT_ID="$(openssl rand -hex 8)"
  GRPC_REALITY_SHORT_ID="$(openssl rand -hex 2)"
  cat > "$KEYS_FILE" <<EOF
TCP_PRIVATE_KEY=$TCP_PRIVATE_KEY
TCP_REALITY_PUBLIC_KEY=$TCP_REALITY_PUBLIC_KEY
GRPC_PRIVATE_KEY=$GRPC_PRIVATE_KEY
GRPC_REALITY_PUBLIC_KEY=$GRPC_REALITY_PUBLIC_KEY
TCP_REALITY_SHORT_ID=$TCP_REALITY_SHORT_ID
GRPC_REALITY_SHORT_ID=$GRPC_REALITY_SHORT_ID
EOF
  chmod 600 "$KEYS_FILE"
fi

# ---------- 4. Hysteria2 auth + obfuscation secrets ----------
HY_SECRETS_FILE="$STATE_DIR/hysteria_secrets.env"
if [[ -f "$HY_SECRETS_FILE" ]]; then
  source "$HY_SECRETS_FILE"
else
  HYSTERIA_SALAMANDER_PASSWORD="$(openssl rand -hex 12)"
  cat > "$HY_SECRETS_FILE" <<EOF
HYSTERIA_SALAMANDER_PASSWORD=$HYSTERIA_SALAMANDER_PASSWORD
EOF
  chmod 600 "$HY_SECRETS_FILE"
fi

# ---------- 5. 3x-ui (client registry only, not used for the real traffic inbounds) ----------
# NOTE: the official install.sh is skipped deliberately — its own tag-lookup step
# (curl against api.github.com) 404s intermittently in practice (reproduced live
# on a fresh Falkenstein box on 2026-07-06), while the release asset download
# itself works fine once you already have the tag. Installing directly from the
# latest release avoids depending on their installer's own reliability.
log "Installing 3x-ui"
if [[ ! -f /usr/local/x-ui/x-ui ]]; then
  XUI_TAG="$(curl -fsSL https://api.github.com/repos/MHSanaei/3x-ui/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')"
  if [[ -z "$XUI_TAG" ]]; then
    echo "ERROR: could not resolve latest 3x-ui release tag" >&2
    exit 1
  fi
  curl -fsSL -o /tmp/xui.tar.gz "https://github.com/MHSanaei/3x-ui/releases/download/${XUI_TAG}/x-ui-linux-amd64.tar.gz"
  rm -rf /usr/local/x-ui
  tar -xzf /tmp/xui.tar.gz -C /usr/local
  rm -f /tmp/xui.tar.gz
  chmod +x /usr/local/x-ui/x-ui /usr/local/x-ui/x-ui.sh /usr/local/x-ui/bin/xray-linux-amd64
  cp /usr/local/x-ui/x-ui.sh /usr/bin/x-ui
  chmod +x /usr/bin/x-ui
  # The per-distro unit ships inside the release tarball itself — the plain
  # "x-ui.service" the official installer fetches separately from raw.githubusercontent.com
  # doesn't exist at that path (also reproduced live) — use the bundled one instead.
  cp /usr/local/x-ui/x-ui.service.debian /etc/systemd/system/x-ui.service
  systemctl daemon-reload
  systemctl enable --now x-ui
fi

# ---------- 6. Caddy + Let's Encrypt ----------
log "Installing Caddy"
if ! command -v caddy >/dev/null; then
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

log "Obtaining Let's Encrypt certificate for $DOMAIN"
systemctl stop caddy 2>/dev/null || true
if [[ ! -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
  apt-get install -y -qq certbot >/dev/null
  certbot certonly --standalone --non-interactive --agree-tos -m "admin@${DOMAIN}" -d "$DOMAIN" --http-01-port 80
fi

# certbot's default perms only let root read the private key — but Caddy and
# hysteria-server both run as their own dedicated non-root users, so without
# this they fail with "permission denied" on the privkey (reproduced live on
# 2026-07-06). Fine for a server whose ONLY sensitive material is this cert
# (nothing else lives under /etc/letsencrypt); re-run after every renewal via
# certbot's --deploy-hook if this script is ever adapted for long-lived nodes.
chmod 755 /etc/letsencrypt/live /etc/letsencrypt/archive
chmod 644 "/etc/letsencrypt/archive/${DOMAIN}/privkey1.pem"

# ---------- 7. Standalone hysteria-server ----------
log "Installing standalone hysteria-server"
if [[ ! -x /usr/local/bin/hysteria ]]; then
  curl -fsSL https://get.hy2.sh/ -o /tmp/hy2install.sh
  bash /tmp/hy2install.sh
fi

TEST_UUID_FILE="$STATE_DIR/test_uuid"
if [[ -f "$TEST_UUID_FILE" ]]; then
  TEST_UUID="$(cat "$TEST_UUID_FILE")"
else
  TEST_UUID="$(uuidgen)"
  echo "$TEST_UUID" > "$TEST_UUID_FILE"
fi
XHTTP_PATH_FILE="$STATE_DIR/xhttp_path"
if [[ -f "$XHTTP_PATH_FILE" ]]; then
  XHTTP_PATH="$(cat "$XHTTP_PATH_FILE")"
else
  XHTTP_PATH="/xh-mx-$(openssl rand -hex 6)"
  echo "$XHTTP_PATH" > "$XHTTP_PATH_FILE"
fi

cat > /etc/hysteria/config.yaml <<EOF
listen: :${HYSTERIA_PORT}

tls:
  cert: /etc/letsencrypt/live/${DOMAIN}/fullchain.pem
  key: /etc/letsencrypt/live/${DOMAIN}/privkey.pem

auth:
  type: password
  password: ${TEST_UUID}

obfs:
  type: salamander
  salamander:
    password: ${HYSTERIA_SALAMANDER_PASSWORD}

masquerade:
  type: proxy
  proxy:
    url: https://news.ycombinator.com/
    rewriteHost: true
EOF

systemctl enable --now hysteria-server.service

# ---------- 8. Xray-core config: DE-style, no multiplexer, multi-SNI REALITY, REALITY-XHTTP for Незаметный ----------
log "Writing Xray-core config (multiplexer-free, REALITY everywhere including XHTTP)"
mkdir -p /usr/local/etc/xray-maxru

cat > /usr/local/etc/xray-maxru/config.json <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": ${TCP_PORT},
      "protocol": "vless",
      "tag": "vless-tcp-maxru",
      "settings": {
        "clients": [{"id": "${TEST_UUID}", "email": "provision-test", "flow": "xtls-rprx-vision"}],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "target": "${SNI_CLASSIC}:443",
          "serverNames": ["${SNI_CLASSIC}", "${SNI_FAST}"],
          "privateKey": "${TCP_PRIVATE_KEY}",
          "shortIds": ["${TCP_REALITY_SHORT_ID}"],
          "xver": 0
        }
      }
    },
    {
      "listen": "0.0.0.0",
      "port": ${GRPC_PORT},
      "protocol": "vless",
      "tag": "vless-grpc-maxru",
      "settings": {
        "clients": [{"id": "${TEST_UUID}", "email": "provision-test"}],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "grpc",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "target": "${SNI_GRPC}:443",
          "serverNames": ["${SNI_GRPC}"],
          "privateKey": "${GRPC_PRIVATE_KEY}",
          "shortIds": ["${GRPC_REALITY_SHORT_ID}"],
          "xver": 0
        },
        "grpcSettings": { "serviceName": "grpc-maxru", "multiMode": false }
      }
    },
    {
      "listen": "0.0.0.0",
      "port": ${XHTTP_PORT},
      "protocol": "vless",
      "tag": "vless-xhttp-maxru",
      "settings": {
        "clients": [{"id": "${TEST_UUID}", "email": "provision-test"}],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "target": "${SNI_CLASSIC}:443",
          "serverNames": ["${SNI_CLASSIC}", "${SNI_FAST}"],
          "privateKey": "${TCP_PRIVATE_KEY}",
          "shortIds": ["${TCP_REALITY_SHORT_ID}"],
          "xver": 0
        },
        "xhttpSettings": {
          "path": "${XHTTP_PATH}",
          "mode": "stream-up"
        }
      }
    },
    {
      "listen": "127.0.0.1",
      "port": ${REGISTRY_PORT},
      "protocol": "vless",
      "tag": "registry-only",
      "settings": {
        "clients": [],
        "decryption": "none"
      },
      "streamSettings": { "network": "tcp", "security": "none" }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom", "settings": { "domainStrategy": "UseIPv4v6" } },
    { "tag": "block", "protocol": "blackhole" }
  ]
}
EOF

cat > /etc/systemd/system/xray-maxru.service <<'EOF'
[Unit]
Description=SilentConnect Xray (multiplexer-free)
After=network.target

[Service]
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray-maxru/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now xray-maxru.service

# ---------- 9. Caddyfile ----------
log "Writing Caddyfile"
cat > /etc/caddy/Caddyfile <<EOF
{
	https_port ${CADDY_HTTPS_PORT}
}

${DOMAIN} {
	tls /etc/letsencrypt/live/${DOMAIN}/fullchain.pem /etc/letsencrypt/live/${DOMAIN}/privkey.pem

	handle {
		encode zstd gzip
		reverse_proxy 127.0.0.1:${SUBJSON_PORT}
	}
}
EOF
systemctl enable --now caddy

# ---------- 10. subjson-service ----------
log "Deploying subjson-service from $SUBJSON_REPO"
if [[ ! -d /root/subjson-service ]]; then
  git clone "$SUBJSON_REPO" /root/subjson-service
fi

CERT_PIN="$(openssl x509 -in "/etc/letsencrypt/live/${DOMAIN}/cert.pem" -outform der | sha256sum | awk '{print $1}')"

STATIC_CONFIG_DIR="/root/subjson-service/static-configs"
mkdir -p "$STATIC_CONFIG_DIR"

# The server's own public IP geolocates to a real country - reflect that in
# the profile names instead of silently inheriting NL's hardcoded flag
# (found live on 2026-07-07: a Finland-hosted test node showed a Dutch flag).
# Resolve to the actual IP first - geolocation APIs aren't guaranteed to
# accept a hostname directly, and DNS is already required to point here.
SERVER_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
SERVER_COUNTRY_CODE=""
if [[ -n "$SERVER_IP" ]]; then
  SERVER_COUNTRY_CODE="$(curl -fsSL --max-time 5 "https://ipinfo.io/${SERVER_IP}/json" 2>/dev/null | grep -o '"country": *"[A-Z]*"' | grep -o '[A-Z]\{2\}' || true)"
fi
if [[ -n "$SERVER_COUNTRY_CODE" ]]; then
  SERVER_COUNTRY_FLAG="$(python3 -c "
c = '${SERVER_COUNTRY_CODE}'
print(''.join(chr(0x1F1E6 + ord(ch) - ord('A')) for ch in c))
")"
else
  echo "WARNING: could not geolocate $DOMAIN - defaulting SERVER_COUNTRY_FLAG to NL, fix manually in subjson.env if wrong" >&2
  SERVER_COUNTRY_FLAG="🇳🇱"
fi

cat > /root/subjson-service/subjson.env <<EOF
LISTEN_HOST=127.0.0.1
LISTEN_PORT=${SUBJSON_PORT}
SECRET_SEGMENT=my-secret-sub
PUBLIC_HOST=${DOMAIN}
# subjson lives behind Caddy on a non-default port in this multiplexer-free
# design (443 is reserved for REALITY alone) - PUBLIC_HOST alone can't carry
# a port (it's also used verbatim as the VLESS vnext address, which must NOT
# have a port suffix), so the self-referential URLs (Profile-Web-Page-Url
# header, the "Подключить" setup page link) need this separate override or
# they silently come back pointing at bare :443 instead (found live on
# 2026-07-07 - Happ then fetches sber.ru's real REALITY-camouflage page
# instead of the subscription JSON and fails with UnknownContentType).
PUBLIC_SUBSCRIPTION_ORIGIN=https://${DOMAIN}:${CADDY_HTTPS_PORT}
XUI_DB_PATH=/etc/x-ui/x-ui.db
STATIC_JSON_CONFIG_DIR=${STATIC_CONFIG_DIR}
SERVER_COUNTRY_FLAG=${SERVER_COUNTRY_FLAG}
TCP_REALITY_PUBLIC_KEY=${TCP_REALITY_PUBLIC_KEY}
TCP_REALITY_SHORT_ID=${TCP_REALITY_SHORT_ID}
TCP_REALITY_SNI_CLASSIC=${SNI_CLASSIC}
TCP_REALITY_SNI_FAST=${SNI_FAST}
GRPC_REALITY_PUBLIC_KEY=${GRPC_REALITY_PUBLIC_KEY}
GRPC_REALITY_SHORT_ID=${GRPC_REALITY_SHORT_ID}
GRPC_REALITY_SNI=${SNI_GRPC}
HYSTERIA_SALAMANDER_PASSWORD=${HYSTERIA_SALAMANDER_PASSWORD}
STATIC_XHTTP_CERT_PIN=${CERT_PIN}
EOF

log "Writing static JSON template for Незаметный (XHTTP via REALITY — tonight's proven fix, not plain TLS)"
# NOTE (2026-07-07): app.py's build_four_profiles() loads this file by the
# EXACT name "json-nl-maxru-xhttp.json" (hardcoded) - it was previously named
# "json-provisioned-xhttp.json" here, which load_static_json_config() never
# found, so Запасной AND Незаметный were both silently dropped from every
# subscription generated from this template. Also unresolved as of this date:
# this REALITY+stream-up template structurally conflicts with build_four_profiles's
# xhttp_h3_cfg step, which unconditionally does
# outbound["streamSettings"]["tlsSettings"]["alpn"] = ["h3"] - that assumes
# the NL-style plain-TLS+QUIC design and throws KeyError against a
# REALITY-based streamSettings (no "tlsSettings" key at all), silently
# dropping Незаметный again. Confirmed live: swapping this file for a
# TLS+QUIC template (matching NL's actual static-configs/json-nl-maxru-xhttp.json)
# makes the profile reappear in the subscription, but does not by itself prove
# which transport design (REALITY+TCP vs TLS+QUIC) is actually the right
# choice for a new server's own network path - that's a real per-server
# decision, not a bug fix. Pick one deliberately before relying on this.
cat > "$STATIC_CONFIG_DIR/json-nl-maxru-xhttp.json" <<EOF
{
  "log": {"loglevel": "warning", "access": "none", "error": "", "dnsLog": false},
  "dns": {"queryStrategy": "UseIPv4", "servers": ["https://dns.google/dns-query", "8.8.8.8", "localhost"]},
  "inbounds": [
    {"listen": "127.0.0.1", "port": 10808, "protocol": "socks", "tag": "socks-in",
     "settings": {"auth": "noauth", "udp": true},
     "sniffing": {"enabled": true, "destOverride": ["http", "tls", "quic", "fakedns"]}},
    {"listen": "127.0.0.1", "port": 10809, "protocol": "http", "tag": "http-in",
     "settings": {"auth": "noauth", "udp": true},
     "sniffing": {"enabled": true, "destOverride": ["http", "tls", "quic", "fakedns"]}}
  ],
  "outbounds": [
    {
      "tag": "proxy",
      "protocol": "vless",
      "settings": {
        "vnext": [{"address": "${DOMAIN}", "port": ${XHTTP_PORT}, "users": [{"id": "PLACEHOLDER_UUID", "email": "PLACEHOLDER_EMAIL", "flow": "", "encryption": "none"}]}]
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "reality",
        "realitySettings": {
          "serverName": "${SNI_CLASSIC}",
          "fingerprint": "chrome",
          "publicKey": "${TCP_REALITY_PUBLIC_KEY}",
          "shortId": "${TCP_REALITY_SHORT_ID}",
          "spiderX": ""
        },
        "xhttpSettings": {"path": "${XHTTP_PATH}", "host": "", "headers": {}, "mode": "stream-up"}
      }
    },
    {"tag": "direct", "protocol": "freedom", "settings": {}},
    {"tag": "block", "protocol": "blackhole", "settings": {}}
  ],
  "remarks": "Provisioned XHTTP via REALITY",
  "meta": {"serverDescription": "Незаметный (REALITY-wrapped XHTTP)"},
  "routing": {"domainStrategy": "IPIfNonMatch", "rules": [
    {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
    {"type": "field", "ip": ["10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"], "outboundTag": "direct"}
  ]}
}
EOF

cat > /etc/systemd/system/subjson.service <<'EOF'
[Unit]
Description=Sub JSON Service
After=network.target

[Service]
EnvironmentFile=/root/subjson-service/subjson.env
WorkingDirectory=/root/subjson-service
ExecStart=/usr/bin/python3 /root/subjson-service/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now subjson.service

# ---------- 10b. test subscription registry entry ----------
# subjson-service resolves a subscription link by searching x-ui.db's own
# inbounds table for a client with a matching subId - it never reads
# xray-maxru's config.json directly. The TEST_UUID client above is already
# registered on every REAL inbound (so it actually works), but without an
# x-ui.db row there is no subId to build a link from at all (found live on
# 2026-07-07 - a fresh server could not produce a working test link without
# this, requiring manual sqlite surgery every time). Idempotent: reuses the
# same subId across re-runs instead of minting a new one every time.
log "Ensuring a test subscription link exists"
TEST_SUBID_FILE="$STATE_DIR/test_subid"
if [[ -f "$TEST_SUBID_FILE" ]]; then
  TEST_SUBID="$(cat "$TEST_SUBID_FILE")"
else
  TEST_SUBID="$(openssl rand -hex 8)"
  echo "$TEST_SUBID" > "$TEST_SUBID_FILE"
fi

python3 -c "
import sqlite3, json, time

test_uuid = '${TEST_UUID}'
test_subid = '${TEST_SUBID}'
registry_port = ${REGISTRY_PORT}
now_ms = int(time.time() * 1000)

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cur = conn.cursor()
cur.execute('SELECT id, settings FROM inbounds WHERE tag = ?', ('registry-only',))
row = cur.fetchone()

client = {
    'id': test_uuid,
    'flow': 'xtls-rprx-vision',
    'email': 'provision-test',
    'limitIp': 0,
    'totalGB': 0,
    'expiryTime': 0,
    'enable': True,
    'tgId': 0,
    'subId': test_subid,
    'comment': '',
    'reset': 0,
    'created_at': now_ms,
    'updated_at': now_ms,
}

if row is None:
    settings = json.dumps({'clients': [client], 'decryption': 'none'})
    stream_settings = json.dumps({'network': 'tcp', 'security': 'none'})
    sniffing = json.dumps({'enabled': False})
    cur.execute('''
        INSERT INTO inbounds (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol, settings, stream_settings, tag, sniffing)
        VALUES (1, 0, 0, 0, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)
    ''', ('registry-only', '127.0.0.1', registry_port, 'vless', settings, stream_settings, 'registry-only', sniffing))
else:
    inbound_id, raw_settings = row
    settings = json.loads(raw_settings)
    if not any(c.get('id') == test_uuid for c in settings.get('clients', [])):
        settings.setdefault('clients', []).append(client)
        cur.execute('UPDATE inbounds SET settings = ? WHERE id = ?', (json.dumps(settings), inbound_id))

conn.commit()
conn.close()
"

# ---------- 11. self-test ----------
log "Self-test: checking every port is listening"
sleep 2
for p in "$TCP_PORT" "$GRPC_PORT" "$HYSTERIA_PORT" "$SUBJSON_PORT" "$CADDY_HTTPS_PORT"; do
  if ss -tlnp 2>/dev/null | grep -q ":$p " || ss -ulnp 2>/dev/null | grep -q ":$p "; then
    echo "  port $p: LISTENING"
  else
    echo "  port $p: NOT LISTENING <-- check logs"
  fi
done

log "Done. Summary:"
echo "  Domain: $DOMAIN"
echo "  Keys stored at: $KEYS_FILE (chmod 600)"
echo "  TCP REALITY public key: $TCP_REALITY_PUBLIC_KEY"
echo "  gRPC REALITY public key: $GRPC_REALITY_PUBLIC_KEY"
echo "  subjson.env written at /root/subjson-service/subjson.env"
echo "  Test client UUID (Классический/Запасной/Незаметный/Скоростной): $TEST_UUID"
echo "  Test subscription link: https://${DOMAIN}:${CADDY_HTTPS_PORT}/my-secret-sub/import/${TEST_SUBID}"
echo ""
echo "NOT yet done automatically: adding real paying clients (still goes through your existing bot/DB flow)."
echo "This script only bootstraps the SERVER side — client provisioning is a separate, existing process."
