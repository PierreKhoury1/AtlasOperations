#!/usr/bin/env bash
# Atlas Desks on one Oracle Cloud Always-Free VM (Ampere A1, Ubuntu 22.04/24.04 arm64; also fine on Hetzner CAX/x86).
# Installs and wires, on the same box:
#   * the Atlas portal (gunicorn, 127.0.0.1:8094)          systemd: atlas.service
#   * a real Hermes Agent (Nous Research) API server        systemd: hermes.service   127.0.0.1:8642
#   * PostgreSQL (persistent accounts/desks/approvals)      DATABASE_URL in /etc/atlas/atlas.env
#   * Caddy (automatic HTTPS when a domain is given)        /etc/caddy/Caddyfile
#
# Run ONCE on a fresh VM as the default `ubuntu` user:
#   curl -fsSL https://raw.githubusercontent.com/PierreKhoury1/hermes-ops/master/deploy/oracle/setup_portal.sh | bash -s -- \
#        --openrouter-key sk-or-v1-XXXX [--domain atlasdesks.com] [--open] [--vision] [--repo URL] [--branch master]
#
#   --domain   public hostname (DNS A record -> this VM). Without it Caddy serves plain http on :80.
#   --open     DESK_OPEN=1 (no login, every desk public). Default is accounts ON - this box is on the internet.
#   --vision   also install the local CV stack (ultralytics + opencv, ~1.5 GB) for on-box camera detection.
# Re-running is safe: it updates the checkout, keeps existing keys/passwords, restarts the services.
# Later updates: /opt/atlas/src/deploy/oracle/deploy.sh   (or the GitHub Action in .github/workflows/deploy-oracle.yml)
set -euo pipefail

REPO="https://github.com/PierreKhoury1/hermes-ops.git"
BRANCH="master"
DOMAIN=""
OPENROUTER=""
OPEN="0"
VISION="0"
while [ $# -gt 0 ]; do
  case "$1" in
    --openrouter-key) OPENROUTER="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --open) OPEN="1"; shift ;;
    --vision) VISION="1"; shift ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done

ROOT=/opt/atlas
ENVF=/etc/atlas/atlas.env
ME="$(id -un)"
HOME_DIR="$(getent passwd "$ME" | cut -d: -f6)"
export DEBIAN_FRONTEND=noninteractive

rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

echo "==> packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates gnupg python3 python3-venv python3-pip \
     postgresql postgresql-contrib debian-keyring debian-archive-keyring apt-transport-https >/dev/null

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy >/dev/null
fi

echo "==> firewall (Oracle images ship an iptables allow-list: open 80/443 in it, and in the VCN security list too)"
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null || \
  sudo iptables -I INPUT 5 -p tcp --dport "$p" -m state --state NEW -j ACCEPT
done
if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save >/dev/null 2>&1 || true; fi

echo "==> postgres"
sudo systemctl enable --now postgresql >/dev/null
if [ -f "$ENVF" ] && grep -q '^DATABASE_URL=' "$ENVF"; then
  PGPASS="$(sudo grep '^DATABASE_URL=' "$ENVF" | sed -E 's#.*://atlas:([^@]+)@.*#\1#')"
else
  PGPASS="$(rand 24)"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='atlas'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE ROLE atlas LOGIN PASSWORD '$PGPASS'"
sudo -u postgres psql -qc "ALTER ROLE atlas PASSWORD '$PGPASS'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='atlas'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE DATABASE atlas OWNER atlas"

echo "==> atlas checkout + venv"
sudo mkdir -p "$ROOT" /etc/atlas && sudo chown -R "$ME:$ME" "$ROOT"
if [ -d "$ROOT/src/.git" ]; then
  git -C "$ROOT/src" fetch -q origin && git -C "$ROOT/src" checkout -q "$BRANCH" && git -C "$ROOT/src" pull -q --ff-only
else
  git clone -q --branch "$BRANCH" "$REPO" "$ROOT/src"
fi
[ -d "$ROOT/venv" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -q --upgrade pip
"$ROOT/venv/bin/pip" install -q -r "$ROOT/src/requirements.txt"
if [ "$VISION" = "1" ]; then "$ROOT/venv/bin/pip" install -q -r "$ROOT/src/requirements-vision.txt"; fi
mkdir -p "$ROOT/data"

echo "==> hermes agent (Nous Research)"
HERMES_KEY=""
if [ -f "$ENVF" ]; then HERMES_KEY="$(sudo grep '^HERMES_AGENT_KEY=' "$ENVF" | cut -d= -f2- || true)"; fi
[ -n "$HERMES_KEY" ] || HERMES_KEY="atlas-$(rand 18)"
if ! command -v hermes >/dev/null 2>&1 && [ ! -x "$HOME_DIR/.local/bin/hermes" ]; then
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash </dev/null || echo "hermes installer returned non-zero - check output above"
fi
HERMES_BIN="$(command -v hermes || echo "$HOME_DIR/.local/bin/hermes")"
mkdir -p "$HOME_DIR/.hermes"
touch "$HOME_DIR/.hermes/.env"; chmod 600 "$HOME_DIR/.hermes/.env"
setenv() { # setenv FILE KEY VALUE  - replace or append
  if grep -q "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi
}
setenv "$HOME_DIR/.hermes/.env" OPENROUTER_API_KEY "$OPENROUTER"
setenv "$HOME_DIR/.hermes/.env" API_SERVER_ENABLED true
setenv "$HOME_DIR/.hermes/.env" API_SERVER_KEY "$HERMES_KEY"
setenv "$HOME_DIR/.hermes/.env" API_SERVER_HOST 127.0.0.1
setenv "$HOME_DIR/.hermes/.env" API_SERVER_PORT 8642
if [ ! -f "$HOME_DIR/.hermes/config.yaml" ]; then
  cat > "$HOME_DIR/.hermes/config.yaml" <<'YAML'
model:
  default: anthropic/claude-haiku-4.5
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
agent:
  max_turns: 150
  verbose: false
terminal:
  backend: local
  timeout: 180
YAML
fi

echo "==> /etc/atlas/atlas.env"
if [ ! -f "$ENVF" ]; then
  sudo tee "$ENVF" >/dev/null <<EOF
# Atlas portal runtime (read by atlas.service). Keep private: chmod 600.
DESK_MODE=live
DESK_PROVIDER=openrouter
OPENROUTER_API_KEY=$OPENROUTER
VISION_DEMO_KEY=$OPENROUTER
VISION_DEMO_MODEL=minimax/minimax-m3:free
DESK_OPEN=$OPEN
DESK_SECRET=$(rand 32)
DESK_TEMPLATE=sales_desk
DESK_DEFAULT_ENGINE=hermes_agent
HERMES_AGENT_URL=http://127.0.0.1:8642
HERMES_AGENT_KEY=$HERMES_KEY
DATABASE_URL=postgresql://atlas:$PGPASS@127.0.0.1:5432/atlas
ATLAS_DATA_DIR=$ROOT/data
SPEND_CAP_USD=10
PORT=8094
PYTHONIOENCODING=utf-8
PYTHONUNBUFFERED=1
EOF
  sudo chmod 600 "$ENVF"
else
  echo "    keeping existing $ENVF (edit it by hand, then: sudo systemctl restart atlas)"
  [ -z "$OPENROUTER" ] || sudo sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$OPENROUTER|" "$ENVF"
fi

echo "==> systemd"
sudo tee /etc/systemd/system/hermes.service >/dev/null <<EOF
[Unit]
Description=Hermes Agent gateway (API server on 127.0.0.1:8642)
After=network-online.target
Wants=network-online.target

[Service]
User=$ME
Environment=HOME=$HOME_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$HOME_DIR
ExecStart=$HERMES_BIN gateway
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/atlas.service >/dev/null <<EOF
[Unit]
Description=Atlas Desks portal (gunicorn on 127.0.0.1:8094)
After=network-online.target postgresql.service hermes.service
Wants=network-online.target

[Service]
User=$ME
EnvironmentFile=$ENVF
WorkingDirectory=$ROOT/src
ExecStart=$ROOT/venv/bin/gunicorn -w 1 --threads 32 --timeout 120 -b 127.0.0.1:8094 atlas.desk.app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now hermes atlas >/dev/null
sudo systemctl restart hermes atlas

echo "==> caddy"
if [ -n "$DOMAIN" ]; then SITE="$DOMAIN, www.$DOMAIN"; else SITE=":80"; fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$SITE {
	encode gzip
	reverse_proxy 127.0.0.1:8094 {
		flush_interval -1
		transport http {
			read_timeout 10m
		}
	}
}
EOF
sudo systemctl enable --now caddy >/dev/null && sudo systemctl reload caddy

sleep 4
echo
echo "================================================================"
echo " health:  $(curl -s -m 10 http://127.0.0.1:8094/api/health || echo 'portal not answering yet - sudo journalctl -u atlas -n 50')"
echo " hermes:  $(curl -s -m 10 http://127.0.0.1:8642/health || echo 'not up yet - sudo journalctl -u hermes -n 50')"
echo " url:     ${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://$(curl -s -m 5 ifconfig.me || hostname -I | awk '{print $1}')}"
echo " env:     $ENVF   hermes: $HOME_DIR/.hermes/.env"
echo " logs:    sudo journalctl -fu atlas   |   sudo journalctl -fu hermes"
echo " update:  $ROOT/src/deploy/oracle/deploy.sh"
echo " remember: open TCP 80 and 443 in the VCN security list (Networking -> VCN -> subnet -> security list)"
echo "================================================================"
